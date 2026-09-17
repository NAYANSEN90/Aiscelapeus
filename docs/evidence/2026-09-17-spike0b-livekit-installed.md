# Evidence — Spike 0b: livekit installed, and what it unmasked

**Date:** 17 Sept 2026
**Question:** Does `livekit-agents==1.8.1` actually *run* on Python 3.13 — and what does
the type checker see once the vendor is present?
**Status:** installs and imports. **Three pre-existing defects unmasked, one of them a real
runtime break in the code B2 is about to rewrite.**
**Follows:** `2026-09-17-spike0-environment.md`, which proved only that the pin *resolved*.

---

## 1. Installed

```bash
python -m pip install "livekit-agents==1.8.1"
python -m pip install livekit-plugins-deepgram livekit-plugins-silero livekit-plugins-google
```

Both exit 0 on **Python 3.13.13**. `import livekit.agents` succeeds. The Spike 0 resolve is
now backed by an execution.

Plugins chosen to match the Session 2 provider decision (`docs/WORKLOG.md`): Deepgram for
STT **and** TTS, Google for Gemini, Silero for VAD. `livekit-plugins-elevenlabs` and
`livekit-plugins-openai` were deliberately **not** installed — they are the two dropped
vendors, and `main.py` still imports both, which is the P2 wiring subsystem's job to remove.

## 2. The "mypy clean" claim was measured against a weaker check than CI runs

Before this install: `Success: no issues found in 17 source files`.
After, with **no source change**:

```
aiscelapeus\agent.py:70: error: Incompatible return value type
    (got "str | Instructions", expected "str")            [return-value]
aiscelapeus\main.py:110: error: Need type annotation for "session"  [var-annotated]
aiscelapeus\main.py:155: error: "JobContext" has no attribute "create_task"  [attr-defined]
Found 3 errors in 2 files (checked 17 source files)
```

An independent reviewer predicted exactly this and its exact count, having reasoned that
`livekit-agents` is a **base** dependency in `pyproject.toml` (not under
`[project.optional-dependencies].dev`), so CI's `pip install -e ".[dev]"` always installs it
— while the local venv did not have it. The green local signal was therefore an artifact of
a missing dependency, and CI would have failed on first run.

Recorded plainly because it is this project's documented failure mode: **a check that passes
because it is not looking.** Same shape as the five BUILT rows whose evidence named a
mechanism that did not exist.

## 3. `main.py:155` is a real runtime break, not a typing nit

`JobContext` has **no task-spawning method at all**:

```python
>>> sorted(m for m in dir(JobContext) if not m.startswith('_'))
['add_participant_entrypoint', 'add_shutdown_callback', 'add_sip_participant', 'agent',
 'api', 'connect', 'delete_room', 'inference_executor', 'inference_headers',
 'init_recording', 'is_fake_job', 'job', 'local_participant_identity',
 'log_context_fields', 'make_session_report', 'primary_session', 'proc', 'room',
 'session_directory', 'shutdown', 'simulation_context', 'tagger', 'token_claims',
 'transfer_sip_participant', 'wait_for_participant', 'worker_id']

>>> [m for m in dir(JobContext) if 'task' in m.lower() or 'spawn' in m.lower()]
[]
```

`main.py:155` calls `ctx.create_task(...)` inside `_on_transcript`, the handler for
`user_input_transcribed`. So **the first final transcript of every session raises
`AttributeError`** and no transcript is ever published to the UI.

This is the same defect class as `scripts/seed_moss.py` earlier today: a vendor or internal
API moved, the module was outside the test import graph, no type checker ran, and nothing
noticed. It sat behind `ModuleNotFoundError: livekit` — the module could not even be
imported, so the broken line was unreachable by any check.

**It is in the exact handler B2 replaces**, so the fix lands there rather than as a separate
patch.

## 4. Consequent actions

| # | Action | Owner |
|---|---|---|
| 1 | Replace `ctx.create_task(...)` with a supported pattern (`asyncio.create_task`, or make the handler's publish awaited off the event loop properly) | B2/P2 wiring |
| 2 | Remove the `elevenlabs`/`openai` plugin imports from `main.py` | P2 wiring |
| 3 | Annotate `session` at `main.py:110` | P2 wiring |
| 4 | `agent.py:70` — `Agent.instructions` is `str | Instructions`; `system_prompt` narrows to `str` without proof | separate, small |
| 5 | Keep livekit installed locally from now on, so the local mypy signal matches CI's | standing |

## 5. What this does *not* prove

- **Not proven:** that an agent session actually connects to a LiveKit room, or that the data
  channel round-trips. That is **Spike 2**, still un-run. This spike covers import and static
  analysis only.
- **Not proven:** that Deepgram TTS (Aura-2) works through `livekit-plugins-deepgram`'s TTS
  class. Session 2 verified the Deepgram *API* directly, not the plugin wrapper.
- **Not proven:** that `livekit-plugins-google` is the right Gemini adapter for this
  `livekit-agents` version. The P2 wiring subsystem must verify the constructor it calls.
