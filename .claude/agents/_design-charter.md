# Shared design charter (included by every persona in a design-council session)

**If you were dispatched for a design session, read this file instead of
`_readiness-rubric.md`.** Your agent definition tells you to read that one; this
overrides it. You are designing, not scoring. Do not return DEMO/PROD scores.

## The project

**Aiscelapeus** — real-time first-aid voice triage for first responders. A
LiveKit voice agent (Python) plus a Next.js dual UI, with Moss as an in-process
retrieval layer rather than a network-hop vector database. Built for the
YC Fall 2026 × Moss Zero Latency Builder Sprint.

## Invariants — no design may break these

Quantitative, and load-bearing. A proposal that breaks one of these is not a
trade-off to weigh; it is out of bounds until the invariant itself is debated.

1. **Nothing but Moss on the synchronous voice path.** Class-0 retrieval is
   in-process only. Redis is fast; "fast" is not "in-process." (RET-001)
2. **p95 end-to-end voice round trip <= 500 ms**, final transcript -> first TTS
   byte. (NFR-002)
3. **p100 Moss retrieval <= 10 ms.** (NFR-001)
4. **Criticality ratchets upward.** No design may introduce a silent downgrade.
5. **Deterministic hard triggers outrank the model.** (ADR-004)
6. **No clinical instruction reaches TTS unvalidated.** Every numeric appears
   verbatim in a cited protocol. (PE-005)
7. **Observability never sits on the voice path.** Export is async and batched.
8. **No PHI in the cache tier**, by construction. (ADR-008)
9. **Transcribed audio and retrieved corpus text are untrusted input.**
10. **Not a medical device.** Nothing in a design may imply clinical validation.

## Blast-radius thresholds — these force a STRUCTURAL debate

- Touches a frozen interface — signature, schema, wire format, or semantics
- Crosses or changes a latency class or a stated budget
- Adds or removes a datastore, service, or external dependency
- Changes escalation, gating, refusal, or fallback behaviour
- Changes an auth, data-classification, encryption, or retention boundary
- Invalidates an accepted ADR
- Touches **> 8 files** or **> 5 requirement IDs**

## Living design document

`docs/DESIGN.md`. Diffed, never regenerated. Requirement IDs are never
renumbered. ADRs are amended by a new ADR that names what it supersedes and why —
see ADR-001/ADR-007 and ADR-003/ADR-008 for the established pattern. Both halves
stay; the reasoning that changed is worth as much as the conclusion.

## Evidence rule (load-bearing)

Every claim in a position, challenge, or concession carries one of:

- `path/to/file.py:123` — a concrete citation, or
- `ABSENT: <what you looked for and where you searched>`, or
- `UNVERIFIED CONCERN` — ranked below everything evidenced.

Never restate the README's own "Known gaps" as a finding unless you verified it
in code. If you verified it, cite the code, not the README.

## Requirement ID prefixes in use

`FR` functional · `NFR` non-functional · `OBS` observability · `SEC` security and
compliance · `PE` prompt engineering · `OFF` offline and degraded · `AUD` audit
and persistence · `RET` retrieval · `TEST` harness · `SCN` scenario ·
`RET-R##` retrieval test case · `ADR` architecture decision

New requirements take the next free number in the right prefix. Every one needs a
quantitative, testable acceptance criterion — "encryption at rest" is not one;
"AES-256-GCM on all audio objects, verified by policy scan in CI" is.

## Constraints you are designing under

- **Deadline 20 Sept 2026, 23:59 IST.** One builder.
- **Paid connectors are metered** — Moss, LiveKit, ElevenLabs, Deepgram, OpenAI.
  Designs that require live runs to validate are more expensive than they look.
- **The cut list in DESIGN.md section 10 is real.** If your proposal only works
  when nothing gets cut, say so.

## Stay in your lens

Another persona covers what you were told to leave alone. Duplicating them wastes
the round. Where your lens genuinely overlaps another's, challenge them in
Round 2 rather than absorbing their territory in Round 1.
