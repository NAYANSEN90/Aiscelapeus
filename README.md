# Aiscelapeus

Real-time first-aid triage over voice, for first responders and bystanders whose
hands are busy and whose screen is out of reach.

A responder opens a call and talks. The agent listens full-duplex, retrieves
verified first-aid protocol from **Moss** in single-digit milliseconds, records
every vital and intervention into a live incident index, scores criticality on a
1–5 scale, and requests a human clinician the moment the case gets past what
field first aid should decide. A clinician joins the incident room manually in
this demo. When it is over, the same recorded
timeline produces the SOAP note.

Built for the **YC Fall 2026 × Moss Zero Latency Builder Sprint**, Real-Time Voice
and Conversational AI track.

---

## Why Moss is the architecture, not a dependency

The bottleneck in a voice agent is not the model, it is the hop to the retrieval
layer. Every network round trip to a vector database lands in the middle of a
conversation where a person is waiting to be told what to do with their hands.

Aiscelapeus uses Moss in two distinct ways, and neither involves a vector database:

**1. Protocol index — built in the cloud, resident in the agent process.**
`load_index` pulls the verified first-aid corpus into memory once, before the
responder says a word. Every lookup after that is in-process. No hop.

**2. Session index — created live, in memory, per incident.**
`MossClient.session()` opens a local index for one emergency. Vitals,
interventions and observations are written to it *mid-conversation* and queried
semantically a few seconds later — "when did the tourniquet go on", "has
adrenaline been given". This is what lets the agent answer questions about the
incident instead of asking the responder to repeat themselves while they are
doing compressions. At the end of the call `push_index()` promotes it to Moss
Cloud, where it becomes the audit trail and the source for the SOAP note.

Every retrieval is traced. The responder UI shows last / best / worst latency
live, and the agent logs a warning whenever a lookup exceeds the 10 ms budget, so
the latency claim is evidenced rather than asserted.

---

## Architecture

```
  Responder (browser, hands-free)                     Clinician (dashboard)
            │  WebRTC audio                                    │  WebRTC audio
            ▼                                                  ▼
  ┌───────────────────────────── LiveKit SFU ─────────────────────────────┐
  └───────────────────────────────┬───────────────────────────────────────┘
                                  │  audio in / audio out / data channel
                                  ▼
                       ┌──────────────────────┐
                       │  Aiscelapeus agent   │
                       │  (livekit-agents)    │
                       └──────────┬───────────┘
        ┌────────────────┬────────┴────────┬──────────────────┐
        ▼                ▼                 ▼                  ▼
   Deepgram STT      Gemini + CRISPE   Deepgram TTS      OpenTelemetry
   nova-3-medical    tools + triage    turbo v2.5        every stage
   diarization                                           traced
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
      Moss protocol index      Moss session index
      (in-process, loaded)     (in-process, live)
              │                        │
              └────────► Moss Cloud ◄──┘
                     build + archive
```

Five tools are exposed to the model, and all clinical content flows through them:

| Tool | What it does |
|---|---|
| `lookup_protocol` | Hybrid search over the verified corpus. Called before any clinical instruction. |
| `record_finding` | Writes a vital, intervention or observation into the live session index. |
| `recall_state` | Semantic recall over what has already happened, including elapsed time. |
| `assess_criticality` | Sets the 1–5 level. Ratchets upward; never silently downgrades. |
| `escalate_to_clinician` | Records and broadcasts a clinician request. A clinician joins the incident room manually. |

---

## Repository layout

```
agent/
  aiscelapeus/
    config.py         all configuration from the environment (12-factor)
    telemetry.py      OpenTelemetry setup, prompt hashing, span helpers
    prompts.py        CRISPE-structured prompts, versioned
    protocols.py      the seed first-aid corpus
    moss_context.py   both Moss indexes, with latency instrumentation
    triage.py         criticality model and the escalation rule
    agent.py          the Agent subclass and its five tools
    soap.py           post-incident SOAP note generation
    main.py           LiveKit AgentServer entrypoint
  scripts/seed_moss.py  builds the protocol index, then latency-checks it
web/
  app/page.tsx           responder view (hands-free, live latency readout)
  app/doctor/page.tsx    clinician dashboard (join mid-incident, full timeline)
  app/api/token/route.ts LiveKit token minting — the auth boundary
  lib/                   typed event stream shared by both views
docs/                    PRD, architecture notes, mentor feedback
```

---

## Running it

### 1. Configure

```bash
cp .env.example .env.local          # agent
cp .env.example web/.env.local      # web app
```

Fill in LiveKit, Moss, Deepgram and Gemini credentials.

### 2. Build the protocol index (once)

```bash
cd agent
.venv/Scripts/python scripts/seed_moss.py          # Windows
# python scripts/seed_moss.py                      # macOS / Linux
```

This builds the index in Moss Cloud, then loads it and runs six realistic
responder queries, printing the retrieval latency for each and a pass/fail
against the 10 ms budget.

### 3. Run the agent

```bash
cd agent
.venv/Scripts/python -m aiscelapeus.main dev
```

### 4. Run the web app

```bash
cd web
npm run dev
```

Open http://localhost:3000, press **Start emergency call**, and talk. Open
http://localhost:3000/doctor in a second window and paste the incident ID to join
as a clinician.

---

## Observability

Every turn opens a span tree under one session-scoped correlation ID:

- `moss.connect`, `moss.query.protocols`, `moss.query.session_state`,
  `moss.write.session_state`, `moss.push_index` — each carrying `moss.wall_ms`,
  Moss's own `moss.time_taken_ms`, hit count, and a `moss.within_budget` flag
- LiveKit's own STT / LLM / TTS spans, fed the same tracer provider
- `triage.escalation`, `triage.hard_escalation`, `soap.generate` — with token
  usage, model version and the prompt hash

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to send these to Arize Phoenix, LangSmith,
Grafana Tempo or Jaeger. Leave it blank and they render as one line per span in
the agent log. Transcripts are patient data: `allow_pii` is on only in
development.

---

## Safety posture

This is a hackathon build. It is not a medical device, it is not clinically
validated, and the protocol corpus is paraphrased public first-aid guidance
rather than an authority. It is built to escalate to a human rather than to
substitute for one:

- Criticality ratchets upward within an incident and never silently downgrades.
- A deterministic phrase-level rule forces Level 5 and an immediate clinician
  request on reports of no breathing, no pulse, unresponsiveness or drowning —
  independent of the model's judgement.
- The agent is instructed to refuse and escalate rather than improvise any
  instruction not returned by protocol retrieval.
- The SOAP generator writes "not recorded" rather than inferring anything absent
  from the timeline.

---

## Known gaps

Tracked openly rather than hidden — these are the next commits, not oversights:

- **Encryption at rest** for audio and transcripts (AES-256) is specified but not
  implemented; TLS 1.3 in transit is inherited from LiveKit and Vercel.
- **Rate limiting and OAuth2** in front of `/api/token`. The route validates and
  scopes tightly, but it does not yet authenticate the caller.
- **Local-first fallback** for a total cellular blackout: cache the protocol index
  on-device and queue transcripts for deferred sync. Moss's WebAssembly SDK
  (`@moss-dev/moss-web`) is the intended path.
- **Prosody-driven escalation.** Distress scoring is modelled in `TriageState`
  but is not yet wired to an acoustic signal.
- **External clinician dispatch.** Escalation currently records and broadcasts a request;
  the clinician joins the incident ID manually. The UI distinguishes requested from joined.
- **Live L2 translation.** The typed assessment state machine and headless harness are built,
  but free-text-to-typed-finding extraction is not yet wired into the live agent.
- **STT robustness.** Two committed synthetic urgent-speech turns lose safety markers before
  any downstream rule can see them; they remain strict expected failures in the corpus.
