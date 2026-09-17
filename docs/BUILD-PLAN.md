# Aiscelapeus — Build Plan

**Status:** active
**Created:** 16 Sept 2026
**Owner:** NAYANSEN
**Supersedes:** the day-by-day roadmap in `docs/DESIGN.md` §10, which assumed an
environment that turned out not to exist (see B-1 below)

This document records what was decided, why, and in what order it gets built. It is
the operational companion to `docs/DESIGN.md` — the design says *what* the system is,
this says *how it gets built and proved*.

---

## 1. Engineering rules

These are mandatory and apply to every change. The normative copy lives in the repo root
`CLAUDE.md` so it binds every session and every subagent; it is summarised here for
readers of the plan.

### 1.1 Foundations first

Code is grounded in **design patterns, DRY and SOLID**. In this repo that means:

- **Dependency inversion.** `agent/aiscelapeus/ports.py` defines `MossPort` and
  `PublisherPort` as `Protocol`s. Nothing in the domain or agent layer imports a vendor
  SDK directly. A new external dependency gets a port before it gets a caller.
- **Single responsibility at the edges.** Vendor objects are parsed once, at the
  boundary, into frozen domain types (`Utterance`, `Settings`, `FactRecord`). Logic
  downstream receives parsed values, never raw SDK objects.
- **Make illegal states unrepresentable.** An enum over a bool (`EscalationStatus`), a
  result object over a flag (`LevelChange`), a raise over a silent clamp.
- **DRY is one source of truth for a rule**, not merely no repeated text. Escalation
  markers live in `phrases.py`; prompt, tests and docs derive from that constant.

### 1.2 Correct first, fast second

Write correct code. Test it for correctness. **Only then** optimise.

A performance number with no correctness evidence under it is not an achievement. This
project already has that failure on record: a sub-10 ms retrieval claim, marked BUILT,
that had never been executed.

### 1.3 Independent review after every code change

After every code change, **spawn an independent reviewer subagent** before committing.

- Use a fresh `Agent` call — `feature-dev:code-reviewer` for implementation diffs, the
  `readiness-*` personas for design-level passes.
- **Self-review does not satisfy this rule.** The author's model of the code is exactly
  what missed the defect. Two confirmed bugs here (`set_level`'s no-op mutation, the
  `last_trace` cross-task race) survived self-review and were caught only independently.
- Findings are reported honestly, including those not acted on, and why.

**Granularity:** one review per subsystem, before commit — not per file edit. At that
point the diff is coherent and the reviewer can judge design rather than a fragment.

### 1.4 Per-subsystem workflow

```
fakes → tests → implementation → wire → integration test → independent review → commit
```

No subsystem starts until the previous one's suite is green and its review is addressed.
Integration follows isolated proof; it never precedes it.

### 1.5 Evidence discipline

A requirement is `BUILT` only when something that runs asserts it. Never mark a row BUILT
whose evidence column names a mechanism another row marks NEW.

---

## 2. Why B-1 exists: the environment finding

The original plan went straight to building subsystems against fakes. An environment
check on 16 Sept found:

| Check | Result |
|---|---|
| `moss` installed | **NOT INSTALLED** |
| `livekit-agents` installed | **NOT INSTALLED** |
| `openai`, all `livekit-plugins-*` | **NOT INSTALLED** |
| `qdrant-client`, `redis` | **NOT INSTALLED** |
| `.env.local` / `.env` present | **ABSENT** (at the time) |
| Python | **3.13.13**, while `pyproject.toml` declares `>=3.11` and CI tests only 3.11/3.12 |

**The 62 passing tests prove only that the pure domain layer is correct.** `triage.py`
and `phrases.py` import nothing external. The integration half of this codebase had never
been executed on this machine.

Two consequences:

1. **ADR-001 is unverified.** The architecture deletes Qdrant from the voice path because
   Moss is in-process and sub-10 ms. That rests entirely on a claim about a library nobody
   had ever imported.
2. **Install-time risk is unmeasured.** `moss>=1.11.0` is an unbounded floor — the API may
   have drifted from what `moss_context.py` assumes. `livekit-agents==1.8.1` is a hard pin
   that may have no wheel for Python 3.13.

**Decision:** verify every external tool with a throwaway spike *before* building on it.
Discovering a broken assumption on day one costs an hour; discovering it on the 19th costs
the submission.

---

## 3. Phase B-1 — Tool verification spikes

Each spike is a throwaway script in `agent/spikes/`, answering one falsifiable question.
**No spike imports from `agent/aiscelapeus/`** — they verify the vendor, not our code.
Each writes its result to `docs/evidence/`, which is what finally puts an artifact behind
the latency claim.

| # | Spike | Question it answers | Credentials | Status |
|---|---|---|---|---|
| **0** | Environment | Can the declared dependency set install at all? Is Python 3.13 viable, or do we pin 3.12? | none | **ready** |
| **1** | **Moss** | Does the real SDK match what `moss_context.py` assumes? Is in-process retrieval actually ≤ 10 ms? Does `load_index` return `loaded_doc_count` as `ConnectOutcome` expects? | `MOSS_*` | blocked |
| **2** | LiveKit | Token mint → room connect → publish a data message → receive it in a second client. | `LIVEKIT_*` | **ready** |
| **3** | Media path | Deepgram STT and ElevenLabs TTS on one short utterance. What does one turn cost? | `DEEPGRAM_*`, `ELEVENLABS_*`, `OPENAI_*` | blocked |
| **4** | Qdrant + Redis | Does the compose stack come up on Windows? Upsert and query both. | none (Docker) | deferred — see §5 |

**Spike 1 is the load-bearing one.** It either validates ADR-001 or kills it. Everything
downstream — the tier discipline in `ports.py`, the two-tier retrieval design, the demo's
central claim — assumes its answer.

### Exit criteria

B-1 is done when spikes 0, 1 and 2 have run and their output is committed to
`docs/evidence/`. Spike 3 runs once before the first live rehearsal. Spike 4 only if the
deep tier stays in scope.

---

## 4. Subsystem build order

Built one at a time, each proved in isolation before integration. The order follows data
flow — transcript feeds triage, triage feeds the gate, the gate feeds TTS — because
building out of order means testing against a contract with no producer.

### B0 / B1 — Complete (16 Sept)

Pure domain and seams. 62 tests, hermetic, 0.17 s.

- `triage.py` — `Criticality`, `EscalationStatus`, `LevelChange`, ratcheting `TriageState`
- `phrases.py` — deterministic escalation markers, 26 phrasings
- `ports.py` — `MossPort`, `PublisherPort`, and `LatencyClass`/`TierViolation`, which
  encode the ADR-001 rule **as a function argument** so a Class-0 call to a network store
  raises at the call site rather than tripping an alert after the fact
- `config.py`, `clock.py`, `transcript.py`

### B2 — Transcript edge + escalation at the boundary — **DONE** (17 Sept, `a1ae682`)

> **Built and proved.** `escalation.py` holds the rule's action once; both the tool path
> (`agent.py`) and the transcript edge (`main.py`) call it, so the two cannot drift.
> `normalize_transcript` is wired, the speaker role is mapped instead of hardcoded, and the
> 26-phrasing corpus now runs through the **applier** rather than only the matcher — the
> earlier green proved recognition and said nothing about action.
>
> Mutation-tested: disabling the edge call fails **33** tests, including
> `test_a_life_threat_escalates_from_raw_speech_with_no_tool_call`.
>
> Also fixed en route: `ctx.create_task` does not exist on `JobContext`, so the **first final
> transcript of every session raised `AttributeError`** and no transcript ever reached the UI.
> It hid behind `ModuleNotFoundError: livekit` — the module could not be imported, so the
> broken line was unreachable by every gate.
>
> **⚠ B2 changed WHERE the rule runs, not WHAT it recognises.** See the new blocker below.

### B2.1 — `phrases.py` net defects — **DONE** (18 Sept, `7e64624`)

> **Nine holes closed, all verified by execution.** The five from the review, three more
> found by independent review of that fix, and one more found by reviewing that review.
> Every one was *silence on a reported life threat*.
>
> `_NEGATION_WINDOW = 3` is deleted. The review's recommended clause-scoped lookback was
> implemented and then **deleted too**: reverting it changed no outcome anywhere in the
> corpus, because the leftward walk already halts at the first content word. A safety
> mechanism no test can distinguish from its own absence is not evidence.
>
> Tests 1394 → 1707; corpus 38 → 68 entries; 15 mutants all killed by a named test.
>
> **Remaining wart, flagged not hidden:** `_contradicted_later`'s `.{0,40}?` is still a flat
> character window — same class as the deleted `_NEGATION_WINDOW`. On the *suppressing* side
> a too-short window over-fires, which is the safe direction, and probing found no further
> defect. Low priority, not zero.

### B2.1 — original entry, retained for the record

`docs/reviews/2026-09-17-phrases-negation-defects.md` records **five defects verified by
execution** against the real module, found by writing the expected markers for 47 responder
turns *before* running anything and then executing `find_markers` over them.

Two are CRITICAL, and both are *silence on a reported life threat*:

| # | Defect | Severity |
|---|---|---|
| F-001 | The negation window crosses clause boundaries | **CRITICAL** |
| F-005 | First match only — a suppressed match drops the marker for the whole utterance | **CRITICAL** |
| F-003 | `unresponsive` misses "can't wake" phrasing | HIGH |
| F-004 | `drowning` requires the literal word "water" | HIGH |
| F-002 | A contracted past tense makes a recovery narrative escalate | LOW |

Reproduced independently: **`"I can't wake him"` fires no marker at all** — a bystander
reporting unresponsiveness in the most natural possible phrasing, and the net stays silent.

This composes badly with B2 in a way worth stating plainly: **B2 widened the net's reach, and
this review shows the net has holes.** Wiring a leaky net more thoroughly does not make it
watertight. `phrases.py` is not a heuristic — its own docstring says it exists specifically to
catch model failure, and that putting a model in front of it reintroduces the failure mode.
So these cannot be delegated to the LLM.

The error asymmetry decides the priority: a missed escalation can kill a patient, a false one
is stood down by a clinician. **Fix before any further subsystem work.**

### B2 — original entry, retained for the record

**Was the highest-severity open item, and cheap.**

`transcript.py` exists, is tested by nothing, and is **wired into nothing** — `main.py:129`
still reads the STT event inline with `getattr`. So `hard_escalation_triggered` has exactly
one call site, inside a tool, fed the text *the model chose to pass*.

A responder saying "he's not breathing" escalates only if the model decides to call
`record_finding`. The deterministic net that is supposed to outrank the model is gated on
the model.

- Wire `normalize_transcript` into `main.py`
- Run the classifier on every final transcript; keep the model path as an independent
  second trigger
- Tests: speaker-index mapping, interim rejection, malformed SDK events, and the
  26-phrasing corpus driven through **the edge** rather than the tool

### B3 — Agent tool layer + Moss context against fakes

Drive all five tools through `FakeMoss`: tool-call sequences, the
`LevelChange.should_persist` path, `TierViolation` on a Class-0 call to a network store,
doc-ID collision on resume via `ConnectOutcome.resumed_existing_session`.

### B4 — Output schema gate

`ClinicalTurn` Pydantic model, numeric-citation check against retrieved text, pre-rendered
safe line on reject with **zero extra LLM calls**. Highest-leverage new component in the
design, and it has no dependency on the audit or offline work.

Tests: uncited dosage rejected, empty `protocol_ids` on a clinical instruction rejected,
rejection path adds no model call.

### B5 — Headless scenario harness

YAML scenarios injected at the transcript boundary — which only works *because* B2 made
that boundary a pure function. This is what converts DESIGN.md §8's `BUILT` claims from
assertion into evidence.

### B6 — FastAPI audit plane, then two-tier retrieval

Last. Both are large; both sit on the design's own cut list.

---

## 5. Open decisions

| # | Decision | Options | Recommendation | Status |
|---|---|---|---|---|
| D1 | Scope | Full §8 register vs the "never cut" set (gate, auth'd tokens, offline tier, FR-007 assertions, trace spine, RET-001) | **Never-cut set.** The full register is not four days of work; the design's own cut list concedes this | **open** |
| D2 | Live infrastructure | Run Moss early vs stay headless | Done — Spike 1 ran 16 Sept | **closed** |
| D3 | Spike 4 (Qdrant + Redis) | Verify now vs defer | Defer unless the deep tier is in scope — items 6 and 7 on the cut list | **open** |
| D4 | **Latency vs correctness priority** | Hold the 10 ms budget vs relax it until retrieval is correct | **Relax the budget.** Directed by NAYANSEN 17 Sept — see §8 | **decided** |

---

## 8. Retrieval correctness — the B1.5 phase

**Directed by NAYANSEN, 17 Sept 2026.** Spike 1 measured Moss at p100 7.42 ms (budget PASS)
but poor retrieval accuracy.

> **Corrected 17 Sept by independent verification.** The original "2/5 top-1, embeddings not
> engaging" diagnosis was **wrong** — see `docs/evidence/2026-09-17-moss-retrieval-verified.md`.
> Embeddings are live; the headline failure did not reproduce; the three diagnostic
> "signals" were all misreadings of normal SDK behaviour.
>
> **The real numbers:** **64 % top-1** (not 2/5) at `alpha=1.0`, 52 % at the current 0.8.
> **Recall@5 = 88 %, recall@8 = 96 %.**
>
> **The real cause:** the embedder clusters by broad topic but discriminates weakly *within*
> a topic — and every document in this corpus is "medical emergency". `moss-mediumlm` does
> not fix it.
>
> The direction below still holds: one-shot top-1 is not sufficient. But the right shape is
> **retrieve wide then re-rank**, not iterative re-querying — because the correct protocol
> is already in the candidate set 88–96 % of the time.

### 8.1 The decision

**The latency requirement is relaxed until retrieval is correct.** NFR-001's 10 ms budget
stands as a design target but is **not a gate** during this phase. Rationale, and it is the
§1.2 rule applied to the architecture itself: a retrieval layer that answers the wrong
question in 7 ms has negative value in a clinical system. Correct first, fast second.

This is not permission to abandon the budget. It is sequencing: establish correctness,
measure what it costs, then optimise back toward 10 ms with a known-good baseline.

### 8.2 Ordered work, cheapest real win first

> ### ⛔ Item 1 is REFUTED — do not do it. Corrected 17 Sept, session 5.
>
> `alpha` 0.8 → 1.0 is a **regression**, not a 12-point gain. Re-measured against real
> Moss on a committed, colloquially-phrased 20-query set
> (`agent/tests/data/retrieval_queries.yaml`, which also closes RET-C04), and reproduced
> independently twice with identical figures:
>
> | alpha | top-1 | recall@5 | recall@8 | critical top-1 |
> |---|---|---|---|---|
> | 0.7 | 12/20 (60 %) | 20/20 | 20/20 | 4/8 |
> | **0.8** *(current code — best)* | **14/20 (70 %)** | 20/20 | 20/20 | 5/8 |
> | 1.0 *(item 1 directed this)* | 13/20 (65 %) | 20/20 | 20/20 | 5/8 |
>
> Going to 1.0 fixes no top-1 miss and **degrades a life-critical query**:
> `airway-choking-adult` falls from rank 2 to rank 4. The hypothesis behind 0.8 — that
> keyword recall earns its weight on verbatim terms like "tourniquet" and "AED" — is
> **supported**, not refuted. `moss_context.py:199` was already correct.
>
> Why the old number was wrong: the earlier 25-query set was never committed (the gap
> RET-C04 exists to close), so it cannot be re-run or audited. Absolute figures across the
> two sets are **not comparable** — only the within-run alpha comparison is trustworthy.
>
> **RET-C01 and RET-C02 both FAIL at every alpha.** Best critical top-1 is 5/8 against a
> required 8/8. Three stable misses, all symptom-phrased queries losing to a topically
> adjacent document:
>
> | Query intent | Returns | Correct doc at |
> |---|---|---|
> | not breathing, collapsed | `drowning-rescue` | rank 5 |
> | choking, can't speak | `drowning-rescue` | rank 2 |
> | bleeding, needs packing | `minor-wound` | rank 3 |
>
> **`drowning-rescue` is a magnet document.** Its text legitimately contains
> *"not breathing", "unresponsive", "rescue breaths", "airway"* and *"CPR"* — a superset of
> the vocabulary of both CPR and choking, because drowning management genuinely involves all
> of them. **No similarity function can rank a specific protocol above a document that
> contains its entire vocabulary.** This is the structural reason a better embedder was never
> going to fix this, and it is the evidence for the Session 4 decision to demote retrieval
> from *router* to *detail-fetcher*.
>
> Evidence: `docs/evidence/2026-09-17-retrieval-alpha-measurement.md`.
> Harness: `agent/spikes/spike1_retrieval_accuracy.py`.
>
> **Also measured:** the §3.2 claim "p100 7.42 ms budget PASS" did not reproduce as stated.
> Wall-clock is p50 ≈ 12 ms / p100 ≈ 29 ms at production `top_k=3`; the SDK's own `moss_ms`
> reads 0–1 ms. The original figure likely reported `moss_ms`, not wall time. A fresh
> `seed_moss.py --check` run measures 9.39 ms worst case against the 10 ms budget.

| # | Action | Measured effect | Where |
|---|---|---|---|
| 1 | ~~**`alpha` 0.8 → 1.0**~~ **REFUTED — see above. Leave at 0.8.** | ~~52 % → 64 % top-1~~ **70 % → 65 %, a 5-point regression** | `moss_context.py:199` was already right |
| 2 | **Retrieve top-5/8; the LLM re-ranks** | 64 % → **88–96 % coverage** | agent tool layer |
| 3 | **Audit every use of `.score` as confidence** | removes a meaningless safety signal | PE-006 and anywhere thresholding |
| 4 | Corpus augmentation — a **symptom/presentation line** per protocol in responder vocabulary | unmeasured; do after 1–2 and re-measure | `protocols.py` |

**Do not** switch to `moss-mediumlm` (ties at 64 %), and **do not** remove the title prefix
in `as_documents()` — dropping it costs top-1 (4/10 → 3/10). Current code is right there.

### 8.3 The re-rank design, not a re-query loop

The multi-pull direction holds, but the evidence reshapes it. **Recall@5 is 88 % and
recall@8 is 96 %, against 64 % at top-1** — the right protocol is nearly always *in* the
candidate set, just not first. So the job is **select from candidates**, not *query again*.

**Primary: retrieve wide, re-rank once.** One retrieval at top-5/8, then the LLM picks with
the responder's actual words in view. One extra reasoning step, no extra retrieval round
trip, and it targets the measured failure mode directly.

**Fallback: bounded multi-pull.** For the residual ~4 % where the answer is not in the
candidate set at all — reformulate and re-query, bounded, then give up honestly.

Constraints either shape must respect:

- **Off the Class-0 voice path.** Otherwise the voice path stops being in-process and
  ADR-001 dies for a different reason. Re-rank is Class 1/2 work (`ports.py:LatencyClass`);
  the responder hears card-level guidance while refinement runs.
- **Bounded, with a defined exhaustion outcome** — state uncertainty and escalate, never
  improvise.
- **Every pull and every re-rank is a span**, or it becomes an unauditable black box in a
  clinical decision path.
- **Deterministic escalation still outranks it.** `phrases.py` fires on the raw utterance
  regardless of what any retrieval step concludes.

### 8.4 Sequencing

**Do 1–3 first and re-measure before building anything in §8.3.** Item 1 is a two-line
change worth 12 points. Building a re-rank layer on top of an un-fixed `alpha` would credit
the layer with a gain that a config value already supplied — and leave the cheap fix
undone.

### 8.5 Acceptance criteria

| ID | Criterion |
|---|---|
| RET-C01 | ≥ 18/20 top-1 on a held-out query set of responder-phrased utterances, one per protocol |
| RET-C02 | **100 %** top-1 on the escalation-critical categories: cardiac, airway, haemorrhage, drowning |
| RET-C03 | Retrieval returning the wrong protocol is a **test failure**, not a relevance warning |
| RET-C04 | The query set lives in the repo and runs in CI against a fake, and on demand against real Moss |

RET-C02 is the one that matters. A wrong protocol on a cardiac arrest is the failure mode
this system exists to prevent, and no downstream component can catch it — the output gate
(B4) validates that an instruction **cites** a protocol, not that the cited protocol is the
**right** one. An instruction sourced from the wrong document is correctly formed, correctly
cited, and wrong.

---

## 6. Credentials

`config.py:40` resolves `repo_root()` from the module path, and `read_env` searches
**the repository root only**, in order: `.env.local`, then `.env`. A file at
`agent/.env.local` is never read — one was created there by mistake on 16 Sept and removed.

Precedence: the real environment wins over the dotenv file.

| Group | State |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | **set** |
| `NEXT_PUBLIC_LIVEKIT_URL` | **blank** — the web app needs it client-side for Spike 2 |
| `MOSS_PROJECT_ID`, `MOSS_PROJECT_KEY` | **blank** — blocks Spike 1 |
| `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `OPENAI_API_KEY` | **blank** — blocks Spike 3 |

`.env`, `.env.local` and `.env*.local` are gitignored; `.env.example` is explicitly
preserved by a negation. Verified with `git check-ignore`.

> **Note.** The LiveKit API secret was shared in a chat transcript during setup. LiveKit
> shows a secret only once at creation, so it cannot be re-read from their dashboard.
> Rotating the key and pasting the replacement directly into `.env` is the clean move
> whenever convenient.

---

## 7. Known risks carried from the reviews

From `docs/reviews/2026-09-15-readiness-3.md`, still open:

1. **Escalation notifies no human.** The lifecycle now has the five states it needs
   (`EscalationStatus`), but no dispatch transport writes into them, and no
   participant-lifecycle listener exists.
2. **The latency claim is unproven and was displayed green before measurement.** Spike 1
   plus the `docs/evidence/` artifact closes the first half.
3. **No type checker runs on either surface.** No mypy or pyright in `agent/`; TypeScript
   has `strict: true` but no `typecheck` script.
4. **Five BUILT rows in §8 assert criteria no code computes** — their evidence column
   names the harness, which FR-013 itself marks NEW.
