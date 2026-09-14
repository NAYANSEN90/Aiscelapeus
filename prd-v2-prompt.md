# PRD v2 Prompt — Architecture Copilot (HiDevs)

Paste the block below into the Architecture Copilot chat where the v1 architecture
and PRD were built. It extends v1 to v2 against the mentor evaluation of
13 Sept 2026, working the four gaps as one connected chain rather than four
independent patches.

---

```
CONTEXT LOCK — do not drift from this.
Track: Real-Time Voice and Conversational AI. Mandatory stack: Moss (retrieval),
LiveKit (WebRTC), Next.js, Python, FastAPI. Project: Aiscelapeus — real-time
emergency voice AI for first responders. You are extending an EXISTING PRD to v2.
Do not regenerate v1. Do not re-open settled architecture.

═══════════════════════════════════════════
PART 0 — BASELINE (LOCKED, DO NOT REDESIGN)
═══════════════════════════════════════════
v1 is approved and scored "Strongly Aligned." Keep all of it intact:
• 7-layer architecture: Client / Edge & Media / Intelligence / Context & Knowledge
  / Escalation / Audit & Review / Testing & Resilience
• Voice loop: WebRTC full-duplex → Deepgram STT with prosody → LangGraph
  orchestrator → Moss (sub-10ms session state) + Qdrant RAG → ElevenLabs Turbo TTS
• Escalation: Level 1–5 criticality thresholds → LiveKit SFU bridges a doctor →
  live dashboard showing AI state and history
• Audit: TimescaleDB millisecond transcripts + prosody markers, SOAP notes within
  30s, synced audio/transcript playback
• Scenario Test Harness simulating responder + doctor with variable dialogue/timing
• Budgets: sub-10ms Moss retrieval, sub-500ms end-to-end voice round trip

═══════════════════════════════════════════
PART 1 — WHAT THE EVALUATION FOUND
═══════════════════════════════════════════
Mandatory stack: all MET. 8 of 10 problem requirements covered. Praised:
multi-party WebRTC bridging, sub-10ms retrieval, prosody analysis, Redis-backed
session cache, SOAP generation, test harness.

Four gaps, in the priority I want you to work them:

P0 [HIGH — Technical Depth] No LLM observability. No OpenTelemetry tracing, token
   tracking, prompt hashing, or latency monitoring. Blocks auditing and debugging
   of life-critical failures.
P1 [HIGH — Security & Compliance] Vague HIPAA. No API Gateway controls (rate
   limiting, input validation, injection protection). No encryption standards
   named (AES-256 at rest, TLS 1.3 in transit).
P2 [MEDIUM — Prompt Engineering] No structured prompt framework (CRISPE),
   constraints, or few-shot examples for the triage and SOAP agents. Risks
   hallucinated or non-compliant medical advice.
P3 [MEDIUM — Architecture] No local-first fallback for cellular blackout. NOTE:
   this is one of only two MISSED problem requirements, so treat it as higher
   weight than its Medium label suggests.

Also flagged: the PRD defines no unique requirement IDs and no quantitative
acceptance criteria. Apply the fix for this to EVERYTHING you write below.

═══════════════════════════════════════════
PART 2 — THE CONNECTING THREAD
═══════════════════════════════════════════
Do not solve these as four independent sections. They share one spine:

A trace context (trace_id + session_id + correlation_id) is minted when an
emergency session opens, and every later element attaches to it:
  • P0 creates it and propagates it across WebSocket → FastAPI → gRPC MossService
    → Qdrant → TTS, so any triage decision can be reconstructed end to end.
  • P1 attaches authenticated identity and role to that same context, and defines
    the encryption boundary for each artifact the trace references.
  • P2 emits prompt_hash and model_version into that same context, so a bad
    instruction is traceable to the exact prompt version that produced it.
  • P3 buffers that same context offline and replays it on reconnect, so an
    offline session reconciles into one continuous audit trail.

State this spine explicitly in the PRD. Each section must reference what the
previous one established.

═══════════════════════════════════════════
PART 3 — WORK ORDER (build in this sequence)
═══════════════════════════════════════════

▸ P0 — OBSERVABILITY LAYER (new 8th layer)
Add OpenTelemetry auto-instrumentation for LangGraph, Pydantic AI, and FastAPI.
Define: the span tree for one emergency session; span attributes (token usage,
per-hop latency, prompt_hash, model_version, moss_retrieval_ms, criticality_level,
escalation_decision and its inputs); correlation ID propagation across the
WebSocket and gRPC boundaries; export to Arize Phoenix or LangSmith.
CONSTRAINT: export must be async and batched — it may not sit on the voice path
or consume the 500ms budget. State the sampling policy (100% retention for
Level 4–5, sampled below). Show how a single trace satisfies Responsible AI
explainability for one automated triage decision.

▸ P1 — SECURITY & COMPLIANCE (rewrite PRD §11, extend Voice Gateway)
Building on the trace context from P0, add identity to it. Specify:
JWT/OAuth2 with short-lived tokens and role claims (responder / doctor / auditor),
scoped LiveKit room tokens; API Gateway rate limiting per identity; strict input
validation via Pydantic at the edge; injection protection — treat transcribed
audio as UNTRUSTED input reaching the RAG and prompt path; TLS 1.3 for all
transit including gRPC and DTLS-SRTP for media; AES-256 at rest for S3 audio,
TimescaleDB transcripts, Redis session cache, Moss context, and any on-device
cache. Add a PHI data-classification table (artifact → classification → encryption
→ retention → deletion), an RBAC matrix (role × resource × action), and audit-log
immutability. Map each control to HIPAA and GDPR.

▸ P2 — PROMPT ENGINEERING SPECIFICATION (new section)
Write a full CRISPE spec (Context, Role, Instructions, Specifics, Personality,
Experiment) for BOTH agents separately:
  1. Emergency Voice Agent
  2. SOAP Note Generator
Include: few-shot examples covering triage Levels 1 through 5; explicit
prohibitions (no diagnosis, no dosage outside retrieved protocol, no improvisation
beyond the Qdrant corpus); mandatory escalation triggers; Pydantic output schemas
as a hard gate before any instruction reaches TTS; behavior when RAG retrieval
returns low confidence. Define prompt versioning and state that prompt_hash and
model_version emit into the P0 spans.

▸ P3 — OFFLINE & DEGRADED-MODE OPERATION (new section)
Define three tiers with explicit entry/exit conditions and user-facing behavior:
  TIER 1 FULL — normal cloud loop
  TIER 2 DEGRADED (3G / low bandwidth) — codec and bitrate adaptation, reduced
         TTS quality, text fallback, what gets dropped first and in what order
  TIER 3 OFFLINE (cellular blackout) — encrypted, signed, versioned local subset
         of the Qdrant protocol corpus on the responder device; local guidance
         from cached protocols only, with clearly stated capability limits;
         voice recordings, transcripts, and Moss state deltas queued for deferred
         sync; P0 spans buffered locally and replayed on reconnect under the same
         correlation_id; conflict resolution rules on reconnection.
Cache encryption follows the P1 standard. In this same section, close the three
TODOs left open in PRD §15: network degradation, doctor drop-off (AI resumes lead),
and late join (Moss-driven catch-up summary for a doctor entering mid-incident).

═══════════════════════════════════════════
PART 4 — APPLY TO ALL OUTPUT
═══════════════════════════════════════════
1. Give every requirement — existing and new — a unique ID:
   FR-xxx (functional), NFR-xxx (non-functional), OBS-xxx, SEC-xxx, PE-xxx,
   OFF-xxx. Retrofit IDs onto the v1 requirements in §5 and §6.
2. Give every requirement a quantitative, testable acceptance criterion.
   "Encryption at rest" is not acceptable. "AES-256-GCM on all S3 audio objects,
   verified by automated policy scan in CI" is.
3. Extend the Scenario Test Harness section with the new cases each fix implies:
   trace completeness, auth failure paths, injection attempts via transcribed
   speech, prompt-schema violations, blackout entry/exit and queue replay.
4. Rewrite §13 Success Metrics so each metric names its measurement method and
   the telemetry source it reads from.
5. Update the Mermaid architecture diagrams to show the observability layer, the
   security boundary, and the offline path.
6. Update §14 Timeline to place this work across the existing phases.

═══════════════════════════════════════════
PART 5 — GUARDRAILS
═══════════════════════════════════════════
• Never trade the sub-500ms voice round trip or sub-10ms Moss retrieval for any
  of the above. If a recommendation threatens either, say so explicitly and
  propose the trade-off rather than silently absorbing the cost.
• Do not introduce stack elements outside the mandatory set without justifying
  them against it.
• Apply 12-Factor App methodology: statelessness, horizontal scalability, clean
  config boundaries. Call out where v1 violates it.
• Output the deliverable as PRD v2 sections that slot into the existing numbering,
  not as a standalone document. Mark every section as AMENDED or NEW.
• If any two requirements conflict, surface the conflict rather than resolving it
  silently.

Begin with P0. Do not skip ahead.
```

---

## Notes on two judgment calls

**Requirement IDs and acceptance criteria** were not in the mentor's numbered
recommendations — the point sits in the "what was delivered" prose. Because it is
cross-cutting and cheap, the prompt makes it a rule applied to every section
rather than a fifth sequential task.

**Local-first fallback** is labeled MEDIUM in the recommendations, but it is one
of only two flatly MISSED problem requirements. The prompt instructs the agent to
weight it above its label.
