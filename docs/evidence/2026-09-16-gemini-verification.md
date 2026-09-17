# Evidence — Gemini API verification

**Date:** 16 Sept 2026, 21:53 IST
**Purpose:** Verify Gemini as the LLM provider after OpenAI credits proved unavailable
**Method:** Direct REST calls to `generativelanguage.googleapis.com/v1beta`, no SDK

---

## Result: PASS

The key authenticates, generates, and — critically — **calls tools correctly**.

## Key

Resolves from repo-root `.env` as `GEMINI_API_KEY`. 53 chars, prefix `AQ.A` — note this is
**not** the classic AI Studio `AIza...` shape, but it authenticates against the
Generative Language API and is accepted on the `x-goog-api-key` header.

## Model availability

`gemini-2.5-flash` returns **404 NOT_FOUND**: *"no longer available to new users. Please
update your code to use models/gemini-3.6-flash."* Any documentation or plan referencing
2.5 Flash is stale.

Generative models reachable by this key include `gemini-3.6-flash`, `gemini-3.7-flash`,
`gemini-3.8-flash`, `gemini-3.5-flash`, `gemini-3.1-pro-preview`, `gemini-flash-latest`,
plus image/TTS/transcribe variants.

## Latency — trivial generation ("Reply with exactly: OK")

| Model | Latency | Result |
|---|---|---|
| `gemini-3.6-flash` | **2265 ms** | `OK` |
| `gemini-3.8-flash` | — | **HTTP 503**, high demand |
| `gemini-flash-latest` | 26053 ms | `OK` — unusable |

## Tool calling — realistic triage turn

Input: *"He collapsed and is not breathing. No pulse."* with `lookup_protocol` (enum-
constrained `category`) and `assess_criticality` declared.

| Model | Latency | Tool calls |
|---|---|---|
| `gemini-3.6-flash` | **2629 ms** | `assess_criticality(level=5, …)` + `lookup_protocol(category="cardiac", …)` |
| `gemini-3.5-flash` | 10885 ms | same two calls |

Both emitted **parallel tool calls** with well-formed arguments, correctly assessed
cardiac arrest as Level 5, and respected the `category` enum. Token usage 152 in / 64 out.

## Decision

**`gemini-3.6-flash` is the LLM provider.** Fastest of the working models and the only one
with acceptable latency under tool load. `gemini-3.7-flash` is the fallback if 3.6 degrades.

## Open issue: latency vs NFR-002

NFR-002 budgets **p95 ≤ 500 ms** end-to-end, final transcript → first TTS byte. The LLM
turn alone measured **2.3–2.6 s** — roughly **5× the entire end-to-end budget**, before STT,
retrieval, the output gate, or TTS.

These were cold, unstreamed, single-shot calls from a residential connection in India to a
US endpoint, so the steady-state streamed number will be materially better — but not
5× better.

This does not block the build. It does mean **NFR-002 must be re-derived from measurement
rather than restated from the design**. Options, in order of preference:

1. Measure streamed first-token latency properly in Spike 3 and rewrite NFR-002 to the
   number the stack actually achieves.
2. Report time-to-first-*token* rather than full completion, which is what a voice pipeline
   perceives.
3. State the budget as a target with the measured gap disclosed — consistent with the
   evidence discipline in `CLAUDE.md`.

What must not happen is NFR-002 staying marked BUILT at 500 ms with a 2.6 s measurement on
record. That is precisely the unproven-claim failure the readiness reviews flagged.

## Reproduce

No SDK required — `urllib` against the REST endpoint, key on the `x-goog-api-key` header,
`POST /v1beta/models/{model}:generateContent`.
