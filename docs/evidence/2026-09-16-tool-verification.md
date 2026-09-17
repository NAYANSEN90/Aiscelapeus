# Evidence — Spike 1: external tool verification

**Date:** 16 Sept 2026, 22:00–22:20 IST
**Method:** direct SDK / REST calls, no project code imported
**Env:** Windows 11, Python 3.13.13, `moss` 1.12.0

---

## Summary

| Tool | Auth | Function | Verdict |
|---|---|---|---|
| LiveKit | key present (rotated) | not yet exercised | pending Spike 2 |
| Deepgram STT `nova-3-medical` | **OK** | **0.999 confidence round-trip** | **PASS** |
| Deepgram TTS `aura-2` | **OK** | 31 KB WAV generated | **PASS** |
| Gemini `gemini-3.6-flash` | **OK** | parallel tool calls, correct args | **PASS** |
| Moss — latency | **OK** | **p100 6.3 ms, p50 4.9 ms** | **PASS** |
| Moss — retrieval quality | **OK** | **2/5 top-1** | **FAIL — see below** |

---

## 1. Deepgram — PASS, both directions

One key covers STT and TTS, so **ElevenLabs is not needed** (see §5).

**TTS `aura-2-thalia-en`:** 31,536-byte WAV, full response 2.8 s (not TTFB).

**STT `nova-3-medical`** on that audio, with `smart_format`, `diarize`, `numerals`, and
keyterms `tourniquet / unresponsive / CPR / anaphylaxis`:

```
input : "He collapsed and is not breathing. I cannot find a pulse. Starting CPR now."
output: "He collapsed and is not breathing. I cannot find a pulse. Starting CPR now."
confidence: 0.999   diarization speakers: [0]   latency: 1496 ms (batch, not streaming)
```

Exact round-trip including "CPR" — a keyterm that is also an escalation marker in
`phrases.py`. Diarization returns a speaker index, which is what `transcript.py:91`
`_speaker_index()` needs.

## 2. Gemini — PASS

Covered in full in `2026-09-16-gemini-verification.md`. Headline: `gemini-2.5-flash` is
**404, retired for new users**; `gemini-3.6-flash` works at 2.6 s under tool load and emits
correct parallel tool calls. `gemini-3.8-flash` returned 503 (demand), so the provider
config needs a fallback chain rather than one hardcoded name.

## 3. Moss — latency PASS, retrieval quality FAIL

### 3.1 SDK surface matches our assumptions

`moss` 1.12.0. `MossClient(project_id, project_key)`; **all methods are async**;
`session(index_name, model_id=None)` returns `SessionIndex`; `DocumentInfo(id, text,
metadata)` — note **`text`, not `content`**; `doc_count` is a **property, not a method**;
`SearchResult` exposes **`.docs`**, plus `.time_taken_ms`, `.query`, `.index_name`.

`moss_context.py` already uses `text=` and `.docs` correctly.

### 3.2 Latency — PASS, and this validates ADR-001

Cloud-built index (`create_index` + `wait_for_job` + `load_index`), queried in-process,
5 protocol docs, 5 queries × 6 runs:

```
n=30   p50 = 6.04 ms   p95 = 7.02 ms   p100 = 7.42 ms      BUDGET 10 ms -> PASS
```

Session-index path measured separately: p50 4.9 ms, p100 6.3 ms. **Both comfortably inside
NFR-001.** The sub-10 ms claim — previously asserted with no execution behind it — is now
measured. ADR-001 (Moss in-process, no network hop on the voice path) stands.

Setup costs, off the voice path: `create_index` 6379 ms, `wait_for_job` 405 ms,
`load_index` 4269 ms, `session()` ≈ 2600 ms, `add_docs`(5) 34–43 ms.

> ## ⚠ SUPERSEDED — §3.3–3.7 below are WRONG
>
> An independent verification pass on 17 Sept **refuted the diagnosis in §3.4** and failed to
> reproduce the headline result in §3.3. Embeddings **are** engaging. See
> `2026-09-17-moss-retrieval-verified.md` for the corrected measurements.
>
> Retained unedited because the reasoning error is itself worth keeping: a 5-document
> fixture made a working retriever look broken, and three separate "signals" were all
> misreadings of normal SDK behaviour.
>
> **Corrected headline:** retrieval is mediocre (**64 % top-1**, not 2/5) for a different
> reason — weak *within-topic* discrimination. §3.2's latency result is unaffected and stands.

### 3.3 Retrieval quality — FAIL, 2/5 top-1  *(SUPERSEDED — see banner)*

Corpus of 5 unambiguous protocols, 5 queries phrased the way a responder would speak:

| Query | Expected | Returned | |
|---|---|---|---|
| "not breathing no pulse" | cpr-adult | **drowning** | MISS |
| "blood everywhere wont stop" | bleeding | bleeding | OK |
| "cant breathe something stuck in throat" | choking | **drowning** | MISS |
| "bee sting face swelling" | anaphylaxis | **bleeding** | MISS |
| "pulled the kid out of the pool" | drowning | drowning | OK |

**"not breathing no pulse" returning the drowning protocol instead of CPR is a clinical
safety failure**, not a relevance nitpick.

### 3.4 Diagnosis: embeddings appear not to be engaging

Evidence:

1. **Scores are quantised**, not continuous. Every result scores exactly `1.0`, `0.8` or
   `0.775` regardless of query — semantic cosine similarity does not behave this way.
2. **`alpha` has no effect.** Per Moss docs, `QueryOptions(alpha=)` blends semantic (1.0)
   and keyword (0.0), default 0.8. Measured: alpha 1.0 / 0.8 / 0.5 all give **identical**
   results; alpha 0.0 returns **nothing at all**.
3. **`session.model_id` reads `None`** even when `session(name, 'moss-minilm')` passes it
   explicitly.
4. Same 2/5 through **both** paths — session index and cloud-built index.

### 3.5 Candidate causes, not yet distinguished

- The corpus is 5 documents. `moss-minilm` may need more mass to separate meaningfully —
  though "not breathing" → drowning over CPR is a large error for any working embedder.
- `model_id` may need to be set at **index creation** and is silently ignored on
  `session()`; our `create_index` did pass `'moss-minilm'` and still scored 2/5.
- The account may be on a tier where embeddings are not provisioned, falling back to lexical.
- `moss-mediumlm` ("higher accuracy") is untested.

### 3.6 Why this matters more than the latency result

The architecture spends its entire retrieval budget on Moss **because** it is fast. A
retrieval layer that is fast and wrong is worse than one that is slow and right: the agent
speaks a confident instruction from the wrong protocol, and the output gate (B4) cannot
catch it, because the instruction *is* correctly cited — to the wrong document.

This is the same class of defect as CONFLICT-5 (a drifted corpus is confidently wrong),
arriving by a different route.

### 3.7 Next actions, in order

1. Re-test with **`moss-mediumlm`**.
2. Test with the **full 13-category corpus** in `protocols.py` rather than 5 docs — the
   real corpus may separate where a toy one does not.
3. Confirm with Moss whether `model_id` on `session()` is honoured, and why `alpha=0.0`
   returns nothing.
4. Compare against the official `livekit-examples/moss-hacker-starter`, which uses
   `MOSS_MODEL_ID=moss-minilm` with `create_index`.
5. **If quality does not improve:** the deterministic `phrases.py` classifier must carry
   protocol selection for the escalation-critical categories, with Moss retrieval as
   supporting detail rather than the primary router. That is a design change and belongs in
   `DESIGN.md`, not a silent code workaround.

---

## 4. Config gaps found

Non-secret values were dropped from `.env` during credential entry and need restoring:
`MOSS_PROTOCOLS_INDEX`, `DEEPGRAM_MODEL`, `LLM_MODEL`, `NEXT_PUBLIC_LIVEKIT_URL`.

## 5. Vendor decisions confirmed by measurement

- **ElevenLabs removed.** Deepgram Aura-2 covers TTS on the key already required for STT.
  Published gap is 40–50 ms TTFB against a budget the LLM currently overruns by ~2 s.
- **OpenAI replaced by Gemini `gemini-3.6-flash`** (no OpenAI credits).
- Stack is now **four vendors**: LiveKit (transport), Deepgram (STT+TTS), Gemini (LLM),
  Moss (retrieval).

## 6. Environment note

Python **3.13.13** installed; `pyproject.toml` declares `>=3.11` and CI tests only
3.11/3.12. `moss` 1.12.0 installs and runs fine on 3.13. `livekit-agents==1.8.1` is a hard
pin and remains **unverified on 3.13** — Spike 0.
