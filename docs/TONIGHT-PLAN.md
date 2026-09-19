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
   current state plus the recorded timeline, or an explicit warning that history is unavailable.
5. The clinician join and disconnect move the escalation lifecycle through
   `REQUESTED -> CLINICIAN_JOINED -> CLINICIAN_LOST`; the UI never says "bridged" merely
   because a request exists.
6. A real smoke scenario proves the deterministic transcript-edge escalation survives
   model inaction and the output gate refuses an unsupported clinical number.
7. The demo runbook records the exact commands, URLs, expected signals, recovery steps,
   and remaining non-production limitations.
8. A fresh checkout has a secret-free, repeatable delivery path: Compose starts the
   production agent and web images, readiness fails closed on missing configuration, and
   a tagged release publishes downloadable artifacts.

## Counterfactual failure matrix

The order below is driven by the failure that would occur if each assumption were false.

| Assumption | Counterfactual | Observable test | Required response |
|---|---|---|---|
| "Clinician requested" means a clinician is present | Nobody has joined, but the responder stops acting because the UI says "bridged" | Request escalation without a clinician participant | UI says **requested / waiting**, never joined |
| A late clinician sees the incident | LiveKit data messages sent before join are gone | Join after findings and escalation already exist | Targeted snapshot contains state and the full current timeline, or explicitly marks history unavailable |
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

### T1 — make the web boundary testable and honest — DONE

- Extract the data-channel reducer as a pure function and test every topic, malformed
  envelope handling, bounded buffers, snapshot replacement, and latency aggregation.
- Add token-route tests for every reject path and the exact room-scoped grant.
- Reject unknown roles instead of silently treating them as responders.
- Remove the remote Google-font build dependency.
- Enable the web test job in CI once it has real tests.

### T2 — clinician lifecycle and late-join catch-up — DONE

- Parse participant metadata defensively at one boundary.
- On an active clinician participant, send a state/timeline snapshot and mark joined only
  when an escalation was actually requested.
- On clinician disconnect, mark lost and broadcast it.
- Test malformed metadata, non-clinicians, duplicate active events, proactive joins,
  requested joins, disconnects, empty timelines, and publish/timeline failures.
- Register durable shutdown before any external await, keep responder media identity stable
  for retries, and rotate to a fresh incident after an explicit call end.

### T3 — real-room smoke run — TRANSPORT PROVED, BROWSER INPUT OPEN

- Started the production worker and standalone web server from ignored local
  configuration without copying secrets.
- Joined responder and clinician RTC clients to one real incident. Named dispatch,
  agent audio publication, `triage.state`, targeted `triage.snapshot`, Gemini reply,
  Deepgram synthesis/playout, and Gemini SOAP generation all completed.
- Browser automation was unavailable on this host, so microphone capture, audible
  browser playback, and a spoken arrest/gate scenario remain a manual rehearsal item.
- Evidence: `docs/evidence/2026-09-19-live-smoke.md`.

### T4 — reconciliation and final push — DONE

- `README.md`, `BUILD-PLAN.md`, requirements, and run instructions agree with the actual
  Gemini + Deepgram runtime.
- The wheel and standalone web build were produced; Compose configuration validates. The
  local Docker daemon was unavailable, so image execution remains an explicit limitation.
- The full gate passed, the final implementation received an independent **SAFE TO COMMIT**
  verdict, commits `45bce9c` and `23fdd9d` were pushed to `origin/main`, and the post-commit
  history audit found no configured credential value or tracked dotenv beyond
  `.env.example`.

### T5 — judge delivery — LOCAL PATH DONE, HOSTED DEPLOY OPEN

- `/demo` contains a permanently labeled recorded simulation with emergency and
  non-emergency scenarios, replay controls, progress, and accessible status.
- Every replay frame crosses the production JSON parser and reducer; malformed fixtures
  fail visibly rather than silently becoming a convincing mock.
- The documented target is Vercel for the zero-credential replay and a long-lived
  container host for the persistent LiveKit worker. No public hosted URL has been
  deployed or evidenced from this workspace yet.
- Compose, production Dockerfiles, readiness, a tagged-release workflow, Python wheel,
  and standalone Next archive provide the download-to-run and downloadable paths.

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
