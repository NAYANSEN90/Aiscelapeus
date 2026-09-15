# Aiscelapeus — Multi-Persona Readiness Review

**Date:** 2026-09-15
**Commit:** ddf963c (`feat: end-to-end voice triage slice on Moss + LiveKit`)
**Method:** `/readiness` — 7 independent read-only persona agents, fixed rubric, evidence required
**Baseline:** first run; no delta available

---

## Score matrix

| Persona | DEMO | PROD |
|---|:--:|:--:|
| Project Manager | 3 | 1 |
| Senior QA | 2 | 1 |
| Solution Architect | 4 | 2 |
| Senior UX | 3 | 1 |
| Systems Developer | 3 | 1 |
| Senior Marketing | 3 | 1 |
| Field Engineer | 3 | 1 |
| **Mean** | **3.0** | **1.1** |

**Read:** a credible demo with one hole in its central beat, and a system nobody
should point at a real patient. The spread is narrow — no persona is an outlier,
which means the picture is consistent rather than driven by one harsh reviewer.
The Architect is the only DEMO 4 and the only PROD 2, and specifically credits
the Moss bet as genuinely implemented rather than marketed.

---

## Convergence — found independently by 3+ personas

These are the highest-signal findings in the review. Each was reached separately,
from a different lens, by reading the code.

### C1. Escalation summons nobody — 5 personas (PM, QA, UX, Marketing, Field)

`agent/aiscelapeus/agent.py:253-288`. `_do_escalate` sets a flag, records a Moss
fact, and calls `publish("triage.escalation", ...)` — which is
`room.local_participant.publish_data`, reaching only participants **already
connected to the room**. Grepped across all source for
`notify|sms|twilio|webhook|sip|invite|oncall|paging|dispatch`: no notification
path of any kind exists.

The README's opening claim — "bridges a human clinician onto the call the moment
the case gets past what field first aid should decide" (`README.md:8-10`) — is
not implemented. A clinician joins only if a human already happens to be sitting
on `/doctor` and types the incident ID by hand
(`web/app/doctor/page.tsx:82-94`).

UX raises the sharpest version: the agent is *instructed to speak the promise*.
`prompts.py:114,137-138` tells it to say it is bringing a doctor onto the line,
and `prompts.py:163-165` to reassure with "A doctor is connecting now, a few more
seconds." The system speaks a commitment it has no mechanism to keep, to someone
doing compressions.

### C2. `connect()` is unguarded — a Moss failure kills the session before a word — 4 personas (QA, Architect, Systems, PM-adjacent)

`agent/aiscelapeus/main.py:62` awaits `context.connect()` bare, before
`session.start()` at `main.py:166`. Any failure in `MossClient(...)`,
`load_index`, or `client.session()` propagates out of `entrypoint`: the room
never gets an agent, and the responder hears silence. There is no degraded mode,
despite `lookup_protocol` already having a clean no-hits branch
(`agent.py:104-111`) that a failure path could reuse.

Systems ranks this the most likely thing to break live on demo day.

### C3. No tests, no CI, no reproducible build — 4 personas (PM, QA, Systems, Architect)

`ABSENT:` searched all tracked files via `git ls-files` — no `test_*.py`,
`*.test.ts`, `conftest.py`, `pytest.ini`, `pyproject.toml`, `Dockerfile`,
`Makefile`, or `.github/`. `web/package.json:6-10` defines no `test` script. The
only `test_*.py` on disk live inside `agent/.venv/`.

Compounded by floating dependencies: `agent/requirements.txt:2-11` has six
`livekit-plugins-*` with **no version constraint at all**, plus `moss>=1.11.0`,
`openai>=1.0`. A fresh `pip install` today resolves differently from the working
`.venv`. Nobody — including the author on a new laptop — can rebuild the
environment the demo was verified against.

`triage.py:3-5` states the levels live in code "so the escalation rule is
auditable and **testable** independently of the LLM." The testability was built
and never exercised.

### C4. Failures are silent — swallowed, debug-logged, or visual-only — 4 personas (QA, UX, Systems, Field)

`main.py:78-79`: the `publish()` helper catches bare `Exception` and logs at
`logger.debug` while `basicConfig` is `INFO` (`main.py:29`). Every UI event that
fails to reach the dashboards vanishes with no visible trace — the latency
counter, criticality badge and escalation banner can silently stop updating while
the agent still sounds fine.

Client-side, `web/app/page.tsx:39-42` handles `Disconnected` by resetting
`phase` to `"idle"` and repainting the Start button — indistinguishable from a
normal hang-up. No `Reconnecting`, `Reconnected`, `ConnectionQualityChanged`, or
`MediaDevicesError` handler exists anywhere. A denied mic permission surfaces as
red text in a `<span>` (`page.tsx:75-78`).

UX states the contradiction plainly: every failure state resolves to pixels on a
screen the product's own prompt (`prompts.py:60-62`) says the user cannot look at.

### C5. Nothing is durable until shutdown — an agent crash loses the whole incident — 3 personas (Architect, Field, PM)

`moss_context.py:325-341` + `main.py:157-160`. Every fact written by
`record_fact` lives only in the in-memory `SessionIndex`; `archive()` runs only
in the shutdown callback, and a `push_index` failure is caught and merely logged
(`main.py:159`). A `SIGKILL` mid-incident destroys every vital, intervention and
timestamp — and the SOAP note with them. No local spool, no retry, no replay.

### C6. Late-joining clinician sees an empty dashboard — 3 personas (PM, QA, Architect)

`web/lib/useTriageStream.ts:97-103` subscribes only to `RoomEvent.DataReceived`
going forward; `main.py:66-79` publishes each event once, at the moment it
happens. There is no replay on `participant_connected` and no fetch of
`context.timeline()` — which already exists at `moss_context.py:312-323`.

`web/app/doctor/page.tsx:76` tells the doctor they arrive with the full recorded
timeline. A doctor joining at T+90s sees "Awaiting assessment" until the next
event fires. **This is the demo beat the dual-UI story is built around.**

---

## The safety rule is broken in three independent ways (QA, sole finding)

Only QA read `triage.py` line by line, and it found the most serious cluster in
the review. Recorded separately because it did not converge — not because it is
less severe.

**S1 — the net is downstream of the failure it backstops.**
`hard_escalation_triggered` is invoked *only* inside the `record_finding` tool
(`agent.py:143`, sole call site, grep-confirmed). It fires only after the LLM
chooses to call a tool with the phrase in its `detail` argument. If the model
fails to call `record_finding` — the exact failure `triage.py:51-53` says the net
exists to catch — a report of no breathing reaches the transcript handler
(`main.py:128-139`), is republished, and nothing escalates.

**S2 — naive substring match, with a curly-apostrophe hole.**
`triage.py:73-79` uses `marker in lowered` against `HARD_ESCALATION_MARKERS`.
Executed against the real list:

*False positives (force irreversible Level 5):*
- "He was unresponsive but he's awake and talking now"
- "No cardiac arrest here, just a panic attack"
- "She's not responding to the painkiller"

*Silent misses (no escalation):*
- "I can't find a pulse" · "he has no heartbeat" · "barely breathing" ·
  "agonal" · "he collapsed and isn't moving"

Worst: the curly-apostrophe form of "isn't breathing" (U+2019) **misses**, while
the straight-quote variant hits — and Deepgram runs with `smart_format=True`
(`main.py:96`), which is precisely what emits curly punctuation. The single most
likely real utterance defeats the rule.

**S3 — a false Level 5 is permanent.**
`triage.py:107-116` discards every downgrade; `triage.py:133-142` makes
`mark_escalated` one-way. Combined with S2, one negated or misheard sentence pins
the incident at Critical for the rest of the call with no code path to lower it.
On stage: the demo turns permanently red. In the field: an un-clearable
over-triage that erodes trust in the level.

---

## Contested

**The latency panel on the responder screen.** Marketing and Field call the live
last/best/worst readout the strongest asset in the repo — a claim a judge can
verify in thirty seconds. UX calls it the second-largest element on a crisis
screen, an investor-facing metric competing with the criticality card, and argues
it belongs on `/doctor` where the retrieval log already lives
(`doctor/page.tsx:151-185`).

*Both are right about different audiences.* The resolution is not to delete it —
it is that the responder view and the demo view have different jobs, and the
project currently has one screen doing both.

**Where to spend before demo day.** PM argues explicitly *against* auth and test
work: they are PROD-only, and the commit history (three commits, the third
carrying 3,350 insertions across 25 files) shows a project still spreading rather
than converging — the discipline needed now is to make the existing story land.
Systems argues the opposite priority, that pinning and containerising is the
cheapest close and the one that turns every other blocker from "debuggable" into
"unreproducible."

---

## Marketing-specific liabilities

Not converged, but the only category that can end a demo on the spot.

**M1 — the corpus dispenses drug dosages the prompt forbids.**
`protocols.py:268-269` instructs a 300 milligram aspirin chewed slowly;
`protocols.py:173-176` directs a second adrenaline dose at 5 minutes. Meanwhile
`prompts.py:78-80` states the agent does not authorise drug doses and
`prompts.py:108` says never to invent a protocol, a dose, or a device. The
guardrail is satisfied on a technicality — the dose came from retrieval — so the
model will read a milligram figure to a bystander. A clinically literate judge
finds this first.

**M2 — no medical disclaimer anywhere in the UI.**
`ABSENT:` searched all 263 lines of `page.tsx`, all 206 of `doctor/page.tsx`, and
`layout.tsx`. The honest safety posture lives only at `README.md:172-187` — a
file no audience member opens. On screen: a red **Start emergency call** button
and text saying the assistant is listening, with no caveat.

**M3 — no scripted demo scenario.** `ABSENT:` repo-wide search for
`*demo*|*scenario*|*runbook*|*pitch*` → zero files. A live voice demo with
`allow_interruptions=True` produces a different conversation every run, and the
one path that must work — reaching Level 5 — depends on the responder saying a
phrase that trips a rule which S2 shows is unreliable.

**M4 — naming and story.** "Aiscelapeus" is a mangled "Asclepius" with "AI"
wedged in: unspellable from hearing, and misspellings autocorrect to nothing.
The one-liner (real-time first-aid triage over voice) is what several other
voice-track teams will also pitch. The actual differentiator — a live session
index written and queried mid-conversation (`moss_context.py:254-310`) — is
buried at `README.md:29-37` instead of leading.

---

## Field-specific findings

**F1 — diarization is enabled and then discarded.** `main.py:94` sets
`enable_diarization=True`; `main.py:137` hardcodes `"speaker": "responder"` on
every final transcript. A screaming patient's or bystander's words are attributed
to the responder — and can trip the phrase rule as if the responder had reported
them.

**F2 — no noise handling for a real scene.** `silero.VAD.load()` uses defaults;
`min_interruption_duration=0.3` on a scene with sirens will make the agent cut
itself off on background noise. `new Room({...})` at `page.tsx:16` sets no
`audioCaptureDefaults` — no `noiseSuppression`, `echoCancellation`, or
`autoGainControl` — and the Krisp filter present in the LiveKit dependency tree
is never applied.

**F3 — no local-first fallback exists.** `ABSENT:` grepped source for
`offline|local-first|fallback|serviceWorker|navigator.onLine|IndexedDB|@moss-dev/moss-web`
→ zero hits. The one asset useful with no bars — the verified corpus, already a
static Python literal in `protocols.py` — is never shipped to the device.

**F4 — no battery, thermal, or wake-lock awareness.** `ABSENT:` grepped for
`battery|getBattery|thermal|wakeLock|visibilitychange|document.hidden` → zero
hits. On mobile, the page being hidden can suspend the audio element attached at
`page.tsx:31-34`, silencing the assistant with no notification.

**F5 — no session handoff.** Responder identity is regenerated per connect
(`page.tsx:63`); a relieving responder must retype the incident ID and joins as
an unrelated participant. Both hold `canPublish: true`
(`token/route.ts:70`) and speak into the same STT stream.

---

## Production-only blockers

**P1 — `/api/token` authenticates nobody.** `web/app/api/token/route.ts:26-88`
validates the *shape* of `room`/`identity`, then signs. Any caller who guesses or
observes an incident ID gets full audio access to a live emergency. `role` comes
straight from the request body (`:46`) and both roles receive identical
permissions — the clinician dashboard's `role: "clinician"` is a client-side
claim with nothing behind it. The route's own comment concedes this.

**P2 — no timeouts on any external call.** `moss_context.py:129,187,301,331`;
`soap.py:81`. Not one `asyncio.wait_for` or `timeout=` in tracked Python source.
A stalled Moss or OpenAI call blocks the coroutine indefinitely — presenting as
the agent going dead mid-instruction, the exact failure `prompts.py:117` tells
the model to avoid.

**P3 — `record_fact`'s lock guards the counter, not the write.**
`moss_context.py:269-304`: the `asyncio.Lock` releases after incrementing `_seq`;
the timestamp and `add_docs` happen outside it. With `max_tool_steps=4`
(`main.py:125`), two concurrent `record_finding` calls can take seq 7 and 8 but
land with inverted `recorded_at` — so "tourniquet applied" can misorder against
"bleeding stopped" in the clinical timeline.

**P4 — the agent↔web contract is hand-duplicated, and already drifting.**
`web/lib/types.ts:15-79` vs `triage.py:147-150`. Nothing generates or validates
one from the other; a renamed key fails silently in the browser
(`useTriageStream.ts:91` falls through to `default: return prev`). Proof it is
already live: `distress_score` is required in `types.ts:24` and declared at
`triage.py:93`, but **no code path ever writes it** — permanently `0.0`, while
`TriageConfig.distress_threshold` (`config.py:110`) is read from env and consumed
by nothing.

**P5 — SOAP generates after teardown, so nothing renders it.**
`main.py:141-164`: `_shutdown` publishes `triage.soap` as a shutdown callback —
by which point the responder has hit End call (`page.tsx:81-85`) and
disconnected, and the failure is swallowed at `main.py:78-79`. The SOAP panel at
`page.tsx:252-259` never populates in a normal run.

**P6 — config split across two loaders with no fail-fast gate.**
`config.py:57-58` requires only `MOSS_PROJECT_ID`/`MOSS_PROJECT_KEY`.
`OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `LIVEKIT_*` are never
validated at startup — a missing key surfaces mid-incident. `soap.py:69`
constructs a bare `AsyncOpenAI()` reading env directly, honouring no timeout or
base URL from `Settings`: the 12-factor component has a hole exactly where the
audit artefact is produced.

---

## What is genuinely strong

Recorded because a review that only lists faults is not an accurate picture.

- **The Moss bet is real, not marketing.** Verified against the vendored SDK:
  `load_index` happens once in `connect()` (`moss_context.py:129`) and `query()`
  then runs on the local IndexManager. This is in-process retrieval, not a
  wrapped network call. *(Architect, independently confirmed by 5 others.)*
- **The latency claim is evidenced in three places** — per-query wall-clock
  alongside Moss's own `time_taken_ms` with a budget flag
  (`moss_context.py:216-250`), a standalone pass/fail harness
  (`seed_moss.py:38-63`), and a live last/best/worst readout
  (`page.tsx:188-206`). A judge can verify it in thirty seconds. *(All 7.)*
- **Clinical logic is properly decoupled from transport.** `triage.py`,
  `moss_context.py` and `soap.py` import zero LiveKit symbols (grep-verified) —
  the ratchet and the phrase rule are testable without a room, a mic, or a
  process. Which makes C3 all the more frustrating: the seam exists and is unused.
- **The live session index is the actual differentiator and it is fully built.**
  `moss_context.py:290` stamps elapsed time inline into indexed text
  (`[T+42s] intervention: ...`), so asking when the tourniquet went on is
  answerable semantically, wired to `recall_state` at `agent.py:159-192`.
- **The spoken output contract is well designed for panic** — one instruction per
  turn, two sentences target and four ceiling, numbered lists banned, numbers
  spelled for speech (`prompts.py:101-108`), with six exemplars pinning the hard
  cases including refuse-and-escalate.
- **PII discipline is implemented, not just described.** `allow_pii` gated on
  development (`main.py:54`); prompts hashed rather than logged
  (`telemetry.py:102-107`).
- **The token route is the tightest code in the repo** — anchored regexes,
  15-minute TTL, `canPublishData: false` so only the agent writes state,
  `cache-control: no-store` — and it names its own missing auth rather than
  overclaiming.
- **Interruption handling matches how responders actually talk**
  (`main.py:121-123`), and the setup flow is genuinely low-precision: one large
  button, no form fields, operable gloved (`page.tsx:120-126`).

---

## Prioritized actions

Ranked across all personas. Tags: `[DEMO]` matters for the sprint, `[PROD]` for
real deployment, `[BOTH]` for either.

| # | Action | Tag | Backed by | Evidence |
|---|---|---|---|---|
| 1 | Fix the phrase rule: move the check into the transcript handler, normalise U+2019, use word boundaries, add a negation guard | BOTH | QA | `triage.py:73-79`, `agent.py:143`, `main.py:96` |
| 2 | Put a medical disclaimer in `layout.tsx`; delete the milligram doses from the corpus | BOTH | Marketing | `protocols.py:268-269,173-176` |
| 3 | Replay state to late joiners on `participant_connected` using the existing `context.timeline()` | DEMO | PM, QA, Architect | `useTriageStream.ts:97-103`, `moss_context.py:312-323` |
| 4 | Guard `connect()` with a degraded no-retrieval mode that escalates rather than dying | BOTH | QA, Architect, Systems | `main.py:62` |
| 5 | Make failure audible: spoken cues for mic-denied, connection-lost, doctor-joined; `logger.debug`→`warning` | BOTH | UX, Field, Systems | `page.tsx:39-42,75-78`, `main.py:79` |
| 6 | Write `agent/tests/test_triage.py` — table-driven, no network, ~60 lines | BOTH | QA, PM | `ABSENT:` repo-wide |
| 7 | Pin `requirements.txt` to `==`, add `.python-version`, Dockerfile, minimal CI | BOTH | Systems, PM | `requirements.txt:2-11` |
| 8 | Append-only local spool per fact; make `archive()` idempotent and retryable | PROD | Architect, Field | `moss_context.py:292-309`, `main.py:157-160` |
| 9 | Move SOAP generation off the shutdown callback to a responder-triggered step | DEMO | PM | `main.py:141-164` |
| 10 | Authenticate `/api/token`; enforce `role` server-side | PROD | Architect, QA, PM | `token/route.ts:44-75` |
| 11 | Add timeouts to every external call | PROD | Systems | `moss_context.py:129,187,301,331` |
| 12 | Script a demo scenario; record a clean run as contingency | DEMO | Marketing, PM | `ABSENT:` repo-wide |
| 13 | Consume the diarization label instead of hardcoding `"responder"` | PROD | Field | `main.py:94,137` |
| 14 | Add `audioCaptureDefaults` + Krisp; tune VAD for a noisy scene | PROD | Field | `page.tsx:16`, `main.py:90-126` |
| 15 | Generate `types.ts` from the Python payloads, or validate at the boundary; remove or wire `distress_score` | PROD | Architect | `types.ts:24`, `triage.py:93` |

---

## The single thing most likely to sink this

**The escalation beat.** It is the emotional peak of the demo, the headline claim
of the README, the name of the tool, and the core of the safety story — and five
of seven personas independently found that it notifies nobody, while a sixth
found that the clinician who *does* manually join sees an empty dashboard.

If a judge asks who actually gets paged, the honest answer today is nobody — a
doctor has to already be watching and to have typed the incident ID by hand.
Actions 1, 3, and 5 together convert that from a claim into a demonstrable
sequence, and they are among the cheapest items on the list.

---

*Generated by `/readiness`. Personas are read-only and cite `file:line` or
`ABSENT:` for every finding. Re-run to produce a dated delta.*
