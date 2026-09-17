# Evidence — Spike 0: dependency resolution on Python 3.13

**Date:** 17 Sept 2026
**Question:** Can the declared dependency set install at all? Is Python 3.13 viable, or do we
pin to 3.12?
**Status:** **PASS** — 3.13 is viable. No pin required.
**Closes:** `docs/BUILD-PLAN.md` §3 Spike 0 (was "ready", never run) and the second half of
the §2 install-time risk.

---

## 1. What was run

```bash
cd agent && python -m pip install --dry-run "livekit-agents==1.8.1"
```

A `--dry-run` resolve rather than a real install: the question is whether the pin *can* be
satisfied on this interpreter, and a dry run answers it without mutating the environment
that the hermetic test suite runs in.

| Fact | Value |
|---|---|
| Interpreter | **Python 3.13.13** |
| `pyproject.toml` declares | `requires-python = ">=3.11"` |
| CI matrix tests | 3.11, 3.12 — **not 3.13** |
| Exit code | **0** |

## 2. Result

Resolution succeeded. The pinned `livekit-agents==1.8.1` and its full transitive closure
have `cp313` wheels — no source builds, no missing platform tags:

```
grpcio-1.84.0-cp313-cp313-win_amd64.whl
av-18.1.0
livekit-1.1.18
livekit-agents-1.8.1
```

Would install, in full: `aiofiles-25.1.0 av-18.1.0 distro-1.9.0 docstring_parser-0.18.0
eval_type_backport-0.4.0 googleapis-common-protos-1.75.3 grpcio-1.84.0 jiter-0.17.0
json_repair-0.60.1 livekit-1.1.18 livekit-agents-1.8.1 livekit-api-1.2.1
livekit-blingfire-1.1.0 livekit-local-inference-0.2.7 livekit-protocol-1.1.27
nest-asyncio-1.6.0 openai-2.54.0 opentelemetry-exporter-otlp-1.44.0
opentelemetry-exporter-otlp-proto-common-1.44.0 opentelemetry-exporter-otlp-proto-grpc-1.44.0
opentelemetry-exporter-otlp-proto-http-1.44.0 prometheus_client-0.26.0 sniffio-1.3.1
sounddevice-0.5.6 tqdm-4.70.1 types-protobuf-7.35.1.20260906 watchfiles-1.2.0`

**The BUILD-PLAN §2 concern that `livekit-agents==1.8.1` "may have no wheel for Python 3.13"
is resolved: it does.**

## 3. Two findings the resolve exposed beyond pass/fail

### 3.1 `pydantic` arrives free — B4 has no new dependency cost

`livekit-agents` requires `pydantic<3,>=2.5`, already satisfied in this environment.
B4's output-schema gate (`ClinicalTurn`) therefore adds **no** dependency. One less reason
to defer it.

### 3.2 `openai` installs unavoidably, which makes `main.py`'s stale wiring worse

`openai-2.54.0` is a **hard transitive dependency** of `livekit-agents`. It installs whether
or not this project calls it.

`docs/WORKLOG.md` (Session 2) records the decision **OpenAI → Gemini `gemini-3.6-flash`**,
and `.env` reflects it (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_FALLBACK_MODEL`, `GEMINI_API_KEY`
all set). But `agent/aiscelapeus/main.py` still constructs:

- `openai.LLM(...)` — the decision was recorded in prose and never reached the code
- `elevenlabs.TTS(...)` — despite ElevenLabs being dropped in the same session

Because `openai` will now be importable, **this fails at runtime on a missing key rather
than at import on a missing module.** That is the worse failure shape: the wiring looks
correct and breaks only when a responder is on the call. Tracked as a B2-adjacent defect.

## 4. What this does *not* prove

Stated explicitly, per the evidence rule — a resolve is not an execution:

- **Not proven:** that `livekit-agents` *runs* on 3.13, only that it resolves. No import, no
  room connect, no agent session. Spike 2 (LiveKit token → room → data channel) remains
  un-run.
- **Not proven:** that the `livekit-plugins-*` packages in `requirements.txt`
  (`deepgram`, `elevenlabs`, `openai`, `silero`, `turn-detector`) resolve. Only the
  `livekit-agents` pin was tested; the plugins were not.
- **Not changed:** nothing was installed. `import livekit` still fails in this environment,
  so `main.py` remains unimportable and B2's wiring step is still blocked on a real install.

## 5. Consequent action

| # | Action | Rationale |
|---|---|---|
| 1 | Add **3.13** to the CI matrix | The dev machine runs 3.13 and CI does not test it. A green CI on 3.11/3.12 currently says nothing about the interpreter the code is actually written on. |
| 2 | Keep `requires-python = ">=3.11"` | Justified: the floor holds and the ceiling is open. No pin to 3.12 needed. |
| 3 | Fix `main.py`'s provider wiring | Gemini + Deepgram TTS per Session 2. Do it in B2, which already edits `main.py` — not as a drive-by. |
| 4 | Run the plugin resolve before B2's wire step | §4 above: untested. |
