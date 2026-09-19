# Aiscelapeus — live-demo completion plan

**Date:** 19 September 2026

**Target:** a real, repeatable responder-to-clinician LiveKit demonstration tonight.
**Rule:** a green check must correspond to an executed assertion. Anything else is
named as a limitation, not presented as complete.

## Definition of done

The build is ready for tonight's live demonstration only when all of these are true:

1. The committed work is on `origin/main`, and a pre-push audit finds no secret file or
   configured credential value in reachable Git history.
2. The web unit suite, Python suite, headless scenarios, lint, typecheck, and production
   web build all pass.
3. A responder can join a real LiveKit room, publish microphone audio, hear the agent,
   and see transcript, criticality, retrieval, and escalation events.
4. A clinician can join the same incident after it has begun and immediately receive a
   current state plus the recorded timeline.
5. The clinician join and disconnect move the escalation lifecycle through
   `REQUESTED -> CLINICIAN_JOINED -> CLINICIAN_LOST`; the UI never says "bridged" merely
   because a request exists.
6. A real smoke scenario proves the deterministic transcript-edge escalation survives
   model inaction and the output gate refuses an unsupported clinical number.
7. The demo runbook records the exact commands, URLs, expected signals, recovery steps,
   and remaining non-production limitations.

## Counterfactual failure matrix

The order below is driven by the failure that would occur if each assumption were false.

| Assumption | Counterfactual | Observable test | Required response |
|---|---|---|---|
| "Clinician requested" means a clinician is present | Nobody has joined, but the responder stops acting because the UI says "bridged" | Request escalation without a clinician participant | UI says **requested / waiting**, never joined |
| A late clinician sees the incident | LiveKit data messages sent before join are gone | Join after findings and escalation already exist | Targeted snapshot contains state and the full current timeline |
| A clinician remains on the call | Their network drops while the responder believes help is present | Disconnect the clinician participant | State becomes `CLINICIAN_LOST`; responder is told the agent remains in charge |
| The token route is safe because room IDs are random | An invalid role or malformed request still receives a usable token | Route tests over malformed JSON, identifiers, role, missing config, and grants | Reject closed with no token |
| A build that worked once will work tonight | `next/font/google` needs network during the build | Run `npm run build` with no font-network access | Build uses no remote font fetch |
| The deterministic net protects every emergency | STT emits the opposite meaning or almost nothing | Preserve the two measured corpus failures as strict expected failures | Demo calls them out; do not claim downstream recovery |
| Moss is resident and fast | Connect fails or the protocol index is absent | Start with a failing context fake and run the gate | Clinical instruction is refused; the call remains alive |
| The model calls the right tool | It narrates instead of recording | Raw transcript contains a hard marker with the model path inert | L1 still raises Critical and requests escalation exactly once |
| Two output surfaces agree | Audio is gated but the dashboard shows unsafe text | Drive independent TTS and transcription streams | Both resolve to identical approved or safe replacement text |
| Local secrets are harmless because `.env` is ignored | A value is copied into a source or generated file | Compare configured sensitive values with all reachable Git blobs before push | Zero matches; otherwise stop, rotate, and purge before push |

## Execution order

### T0 — preserve and audit — DONE

- Commit `54aa53d` contains the voice corpus and safety calibration.
- `origin/main` contains the commit.
- No `.env*` secret file and no configured LiveKit, Moss, Deepgram, or Gemini credential
  value occurs in any reachable commit.

### T1 — make the web boundary testable and honest

- Extract the data-channel reducer as a pure function and test every topic, malformed
  envelope handling, bounded buffers, snapshot replacement, and latency aggregation.
- Add token-route tests for every reject path and the exact room-scoped grant.
- Reject unknown roles instead of silently treating them as responders.
- Remove the remote Google-font build dependency.
- Enable the web test job in CI once it has real tests.

### T2 — clinician lifecycle and late-join catch-up

- Parse participant metadata defensively at one boundary.
- On an active clinician participant, send a state/timeline snapshot and mark joined only
  when an escalation was actually requested.
- On clinician disconnect, mark lost and broadcast it.
- Test malformed metadata, non-clinicians, duplicate active events, proactive joins,
  requested joins, disconnects, empty timelines, and publish/timeline failures.

### T3 — real-room smoke run

- Start the agent and web app from the configured local `.env` without copying secrets.
- Join responder and clinician browsers to one incident.
- Execute a short arrest scenario and an unsupported-number gate scenario.
- Record pass/fail evidence, not credentials or patient data.

### T4 — reconciliation and final push

- Bring `README.md`, `BUILD-PLAN.md`, and the requirements/run instructions into agreement
  with the actual Gemini + Deepgram runtime.
- Run the full gate again, independently review each implementation diff, commit, run the
  secret-history audit, and push.

## Explicitly outside tonight's claim

- This does **not** create an external on-call paging transport. Tonight's clinician joins
  the incident ID manually; the application must say "requested" until that participant is
  actually present.
- The pure L2 assessment engine is not yet driven from live free text. Wiring it requires a
  separately tested L3 extraction/certainty boundary; rushing that boundary would turn an
  unverified model inference into deterministic clinical authority.
- The two recorded STT meaning-loss failures remain open. They are visible, counted, and
  must not be described as solved by downstream logic.
- This remains a demonstration system, not a clinically validated medical device.
