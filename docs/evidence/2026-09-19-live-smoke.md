# Real-room release smoke — 19 September 2026

No credential value or patient data is recorded here. Identities and incident names were
synthetic and short-lived.

## What ran

- Next.js standalone production server on localhost.
- LiveKit production worker named `aiscelapeus`, registered in India South.
- A responder RTC client using a responder-scoped token minted by `/api/token`.
- A clinician RTC client using a clinician-scoped token for the same incident.
- Configured Moss, Gemini, and Deepgram services.

## Observed pass signals

1. Responder token dispatch created exactly one named `aiscelapeus` job; clinician token
   minting did not dispatch a duplicate worker.
2. Both clients reached `CONN_CONNECTED` and saw the agent participant. The responder
   subscribed to the agent audio track.
3. The responder received `triage.state`; the late clinician received a targeted
   `triage.snapshot`.
4. Moss loaded the protocol index and created the per-incident session index.
5. Gemini completed the opening turn after the fallback timeout was raised above its
   10-second minimum. Deepgram completed TTS, and LiveKit recorded agent speaking/playout.
6. On shutdown, Gemini generated a 208-character SOAP note from the empty synthetic
   timeline. This verified removal of the stale implicit OpenAI credential dependency.
7. A second ordering run joined the clinician first. That token created the room and
   produced the named job request before the responder joined; when the responder arrived,
   the worker explicitly linked RoomIO to `responder-second`, delivered the clinician
   snapshot/state, and published an agent audio track to the responder. This closes the
   room-configuration-on-create counterfactual.

The first run found two genuine production defects: the fallback adapter's 5-second
attempt timeout was rejected by Gemini with HTTP 400, and SOAP still constructed an
OpenAI client. Both were reproduced, covered by regression tests, fixed, and then passed
in the second run.

## Measured reality and limits

- Cold Moss connection was about 6.4 seconds, including about 3.9 seconds loading the
  protocol index.
- The successful cold opening turn recorded about 3.4 seconds for the Gemini request and
  about 2.0 seconds for Deepgram TTS. This is functional evidence, not evidence that the
  design's latency targets are met.
- The RTC smoke client did not publish human microphone audio. Browser-control startup
  failed on this host, so browser permission, microphone capture, audible speaker output,
  interruption, and the spoken arrest/gate scenarios still require the runbook's manual
  rehearsal.
- External clinician paging remains absent. The clinician joined by incident ID; the UI
  stays at requested/waiting until a clinician participant is actually active.
- The local Docker daemon was unavailable. Compose configuration, both Dockerfiles, the
  Python wheel, and the standalone web artifact were validated, but a local container
  image build is not claimed by this evidence.
