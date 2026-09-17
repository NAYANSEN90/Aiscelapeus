# Work log

A running transcript of what was done, in reverse-chronological order (newest first), so
past work can be verified without re-deriving it.

**Conventions.** One entry per session or per meaningful unit of work. Each entry records
what changed, what was *proved* (with the artifact), what was decided, and what is still
open. Claims of "works" carry a link to evidence in `docs/evidence/`. Entries are appended,
never rewritten — a correction is a new entry that supersedes an old one.

---

## 2026-09-17 · Session 5 — parallel subsystems; BUILD-PLAN §8.2 item 1 refuted

**Goal (NAYANSEN):** find which gaps can be built in parallel, factor out any shared
subsystem, prove each in isolation before composing.

### Decided — the parallelisation, and the shared subsystem it exposed

Gaps 1 (`alpha`) and 2 (`MossPort` binding) **cannot** run in parallel as framed: both edit
the same two method signatures in `moss_context.py`. But that overlap was the clue — both are
about *how a retrieval call is specified*, not about retrieval behaviour. Factored out
**S0 `retrieval.py`**: a frozen `RetrievalRequest` carrying `query`, `top_k`, `alpha`,
`latency_class`, `categories`. Gap 1 becomes "set a constant"; Gap 2 becomes "the request
already carries the tier".

Decisive evidence for S0: **`QueryOptions(alpha=1.5)` is accepted silently by the SDK.** No
range validation. An out-of-range alpha produces undefined ranking in a clinical path with no
error, so the frozen type is the only place that invariant can live — CLAUDE.md §1.1's
"raise over silent clamp", applied where the vendor will not.

Ran in parallel on disjoint files: **S0** (new files only), **S3** (CI/meta/mypy), **S4**
(read-only measurement). **S1+S2** compose onto S0 sequentially afterwards.

### Refuted — BUILD-PLAN §8.2 item 1

**`alpha` 0.8 → 1.0 is a 5-point regression, not a 12-point gain.** Re-measured against real
Moss on a committed 20-query colloquial set, then **reproduced independently a second time
with identical figures**:

| alpha | top-1 | recall@5 | recall@8 | critical top-1 |
|---|---|---|---|---|
| 0.7 | 12/20 (60 %) | 20/20 | 20/20 | 4/8 |
| **0.8** *(code already had this — best)* | **14/20 (70 %)** | 20/20 | 20/20 | 5/8 |
| 1.0 *(item 1 directed this)* | 13/20 (65 %) | 20/20 | 20/20 | 5/8 |

1.0 fixes no miss and **degrades a life-critical query** — `airway-choking-adult` falls from
rank 2 to rank 4. `moss_context.py:199` was already right. The keyword component earns its
weight; the hypothesis behind 0.8 is supported, not refuted.

**RET-C01 and RET-C02 both FAIL at every alpha** (best critical 5/8 against a required 8/8).

**Root cause — a magnet document.** `drowning-rescue`'s text legitimately contains
*"not breathing", "unresponsive", "rescue breaths", "airway"* and *"CPR"*: a superset of the
vocabulary of both CPR and choking, because drowning management involves all of them. **No
similarity function can rank a specific protocol above a document containing its entire
vocabulary.** This is the structural reason a better embedder was never going to help, and it
is direct evidence for the Session 4 decision to demote retrieval from *router* to
*detail-fetcher*. Observed live on the seeded production index:
*"he's not breathing, what do I do"* → **Drowning**.

Caveat recorded honestly: the prior 25-query set was never committed (the gap RET-C04 exists
to close), so absolute figures across the two sets are **not comparable**. Only the
within-run alpha comparison is trustworthy.

### Proved

| Claim | Result | Evidence |
|---|---|---|
| **Spike 0** — deps resolve on Python 3.13 | **PASS**, exit 0; `cp313` wheels throughout. No pin to 3.12 | `evidence/2026-09-17-spike0-environment.md` |
| Protocol index seeded and queryable | 20 protocols; worst case **9.39 ms** vs 10 ms budget | `seed_moss.py --check` output |
| The meta-gate actually bites | 3 deliberately-bad tests caught by the correct rule each; honest control **not** flagged | independent re-verification |
| mypy clean | `Success: no issues found in 17 source files` | run this session |

### Found — two live defects, both fixed

1. **`MOSS_PROTOCOLS_INDEX` did not exist in Moss Cloud.** `EmergencyContext.connect()` could
   never have succeeded against real Moss. Root cause: `scripts/seed_moss.py` still called the
   pre-refactor `MossConfig.from_env()` with no argument, so the seeder had been broken since
   env-reading became explicit. Nothing caught it — scripts are outside the test import graph
   and no type checker ran. **Fixed; index seeded.**
2. **The Session-2 provider migration never reached the code.** `Settings.load()` still
   *requires* `OPENAI_API_KEY` and `ELEVENLABS_API_KEY` — both vendors dropped — and that
   blocked the seeder outright. `ModelConfig` still defaults `llm_model` to `gpt-4o` and
   `tts_voice_id` to an ElevenLabs id; `main.py` still constructs `openai.LLM` and
   `elevenlabs.TTS`. Scoped the seeder to `MossConfig` only; **the migration itself is left as
   its own subsystem, not a drive-by.** Spike 0 makes it worse than it looks: `openai` now
   installs as a hard transitive dep of `livekit-agents`, so this fails at *runtime on a
   missing key* rather than at *import on a missing module* — the shape that looks wired and
   breaks mid-call.

### Built

- **S0** `retrieval.py` + 42 tests. Two independent reviews found 5 real defects, including
  `categories=('',)` producing a *valid* filter matching zero documents (agent answers with no
  protocol under it), and a `field(compare=False)` mutation that would let a BACKGROUND result
  be cache-served to the 10 ms voice path with **no `TierViolation` possible**.
- **S3** real CI: `tests/meta/` (falsifier + no-vacuous-assert gates, 302 tests), mypy wired
  with every suppression documented and its removal condition stated. `--passWithNoTests`
  removed from both web scripts. `contract` and `harness` jobs **removed rather than stubbed** —
  a fake harness exiting 0 over an empty `scenarios/` would launder FR-013's latency claim.
  3.13 added to the matrix.
- **62 → 414 tests.**

### Corrections to my own claims, both caught by subagents

- I said `pytest -m contract` "passes vacuously, exit 0". It exits **5**
  (`NO_TESTS_COLLECTED`) — the Python half would have *failed*; only the TypeScript half was
  vacuous. I had read rather than run it.
- I instructed S0 to set `alpha=1.0` citing BUILD-PLAN §8.2. S0 detected the concurrent
  measurement, **verified the evidence, kept 0.8, and escalated the conflict** instead of
  silently resolving it. My instruction was the defect.

### Open

- **S1+S2 composition in flight** — thread `RetrievalRequest` through the live path, close the
  `MossPort`/`EmergencyContext` divergence (missing `checkpoint`, absent `latency_class`,
  `dict` vs `FactRecord`), make `TierViolation` reachable from real code.
- **RET-C02 still fails.** Next retrieval work is §8.2 item 4 (corpus augmentation — all three
  critical misses are symptom-phrased queries losing to a topically adjacent doc) plus the
  re-rank layer at **top-8, not top-5**: recall@5 is 100 % but the worst correct rank is
  exactly 5, so there is zero headroom.
- `telemetry.py` real mypy debt: `_CompactConsoleExporter` does not subclass `SpanExporter`
  (`arg-type` override scoped to that module, to be removed with the fix).
- B2 transcript edge, B4 gate, B5 harness, provider migration — all still unstarted.
- Nothing committed this session.

---

## 2026-09-17 · Session 4 — clinical standards sourced

**Goal (NAYANSEN):** find the standard medical diagnostic/triage checklists and build against
them, rather than relying on retrieval to decide what a responder is looking at.

### Added

`docs/CLINICAL-STANDARDS.md` — the published algorithms, with every numeric threshold and
full source citations:

- **ABCDE primary survey** (Resuscitation Council UK / ERC) — assessment order, the
  treat-before-moving-on rule, life-threatening signs per letter, and thresholds: RR 12–20
  normal / >25 concerning, SpO₂ 94–98 % (88–92 % COPD), cap refill <2 s, glucose 4.0 mmol/L,
  ACVPU.
- **START adult MCI triage** — the full decision tree and the **"RPM 30-2-Can Do"** mnemonic:
  RR **30**, cap refill **2 s**, follows commands.
- **JumpSTART paediatric** — ages 1–8, the **rescue-breath branch adult START lacks**,
  RR **15–45**, peripheral pulse, AVPU where *inappropriate* P or U → RED. Plus the
  non-ambulatory-child modification.
- **Four triage colours** and a proposed mapping to this system's `Criticality` 1–5.
- **SAMPLE / OPQRST** secondary survey, and **FAST** for stroke.
- **9 worked MCI scenarios** from the source training deck, as ready-made test fixtures.

Extracted the 55-page START/JumpSTART training PDF locally with `pypdf` after the fetcher
could not read it — that is where the exact thresholds and the worked cases came from.

### Decided

- **Retrieval is demoted from router to detail-fetcher.** A deterministic assessment state
  machine decides *what step comes next*; Moss supplies *how to perform it*. This is the real
  fix for 64 % top-1 — a better embedder was never going to solve a problem that is not a
  similarity question.
- **Scope line for clinical tools:** if a bystander with no equipment cannot perform it, it
  does not belong on the responder path. Excludes FAST-ED / RACE / LAMS / CSTAT / VAN
  (LVO routing, needs a trained neuro exam), shock index (needs a BP cuff), sepsis screens,
  and full GCS (ACVPU is the field equivalent). Recorded as a decision, not an oversight.

### Found — open design questions

1. **BLACK has no representation in `Criticality` 1–5.** A single-casualty field agent
   arguably never assigns it, but a scale that cannot *express* a state is the kind of gap
   `CLAUDE.md` §1.1 says to close in the type system rather than by convention.
2. **`phrases.py` markers currently map to a level.** Under ABCDE they should map to *which
   letter is failing*, which is both more useful and more auditable.
3. **The 9 scenario expected-answers are derived from the algorithms, not read from the
   deck's answer slides.** They must be checked against the published answers before being
   used as test oracles. Cases 4, 6 and 8 are the discriminating ones.
4. Every threshold needs clinician review before any real use.

### Open

- No code written this session. The assessment state machine is designed but not built.
- BUILD-PLAN §8.2 items 1–3 (alpha→1.0, top-5 re-rank, `.score` audit) still not implemented.

---

## 2026-09-17 · Session 3 — retrieval finding overturned by independent review

**Goal:** act on the Moss retrieval problem. Outcome: the diagnosis was refuted before any
code was written on top of it.

### Corrected

The independent verifier dispatched at the end of Session 2 **refuted the Session 2
diagnosis**. Recorded in `docs/evidence/2026-09-17-moss-retrieval-verified.md`; the Session 2
evidence file now carries a SUPERSEDED banner over §3.3–3.7 and is otherwise left intact.

| Session 2 claim | Verified reality |
|---|---|
| 2/5 top-1; *"not breathing no pulse"* → drowning | **Does not reproduce.** Returns `bls-adult-cpr` at rank 1. Probable cause: an ascending score-sort or `max(d.id ...)` in the probe, both of which select `drowning-rescue` from a *correct* result set |
| Scores quantised → embeddings dead | **Reciprocal Rank Fusion outputs** — rank, not similarity. `62/(62+k)` renormalised; `0.775` is the 10th rung |
| `alpha` has no effect | **Works.** The Session 2 corpus (5 docs) was too small for ordering to change |
| `session.model_id` is `None` | **Public attribute does not exist.** Real value at `session._inner.model_id`, correctly `moss-minilm` |

Embeddings proven live: `"cardiac emergency"` (zero word overlap) correctly ranks the
myocardial-infarction doc first on an adversarial corpus.

### Proved

| Metric | Value |
|---|---|
| Top-1, `alpha=1.0` | **64 %** (25 labelled queries, 20-doc corpus) |
| Top-1, `alpha=0.8` *(current code)* | 52 % |
| **Recall@5 / @8** | **88 % / 96 %** |
| `moss-mediumlm` | ties at 64 % — **not a fix** |

Root cause: the embedder clusters by broad topic but discriminates weakly **within** a
topic, and every document here is "medical emergency".

### Decided

- **Latency budget relaxed** until retrieval is correct (NAYANSEN). NFR-001's 10 ms stays a
  design target, not a gate, during this phase.
- **Retrieve-wide-then-re-rank**, not iterative re-querying — because recall@5 is 88 %, the
  answer is already in the candidate set. Bounded multi-pull is the fallback for the
  residual, not the primary mechanism.
- Ordered work: `alpha`→1.0, then top-5 + LLM re-rank, then audit `.score` usage, then
  corpus augmentation. Re-measure between steps.
- **Do not** switch to `moss-mediumlm`; **do not** remove the title prefix in
  `as_documents()` (costs top-1).

### Found — affects a designed safety feature

**`.score` is rank-derived and cannot serve as a confidence signal.** PE-006 specifies a
"low-confidence retrieval floor" — a score threshold. It would have shipped as a safety
feature that looks like it works and cannot. Needs a different confidence mechanism.

### Open

- Items 1–4 of BUILD-PLAN §8.2 — **not yet implemented.** Next code change; requires an
  independent reviewer before commit.
- Spike 0 and Spike 2 still not run.

---

## 2026-09-16 · Session 2 — tool verification (Spike 1)

**Goal:** verify every external tool works before building subsystems on top of them.
Prompted by the discovery that *nothing* was installed and no credentials existed.

### Changed

- `CLAUDE.md` (new) — mandatory engineering rules: SOLID/DRY/patterns, correctness before
  optimisation, **independent reviewer subagent after every code change, no self-review**.
- `docs/BUILD-PLAN.md` (new) — decisions, the B-1 spike phase, subsystem order B2–B6.
- `docs/evidence/2026-09-16-gemini-verification.md` (new)
- `docs/evidence/2026-09-16-tool-verification.md` (new)
- `docs/WORKLOG.md` (new — this file)
- `.env` — populated by NAYANSEN with LiveKit, Moss, Deepgram, Gemini keys. LiveKit key
  rotated after an earlier one was exposed in a chat transcript.
- Removed a stray `agent/.env.local`; `config.py:40` `repo_root()` searches the **repo root
  only**, so a file there is never read.

**No source code changed.** No reviewer required under the rules; this session was
verification and documentation only.

### Proved

| Claim | Result | Evidence |
|---|---|---|
| Moss in-process retrieval ≤ 10 ms | **p50 6.04 ms, p100 7.42 ms** over n=30 | tool-verification §3.2 |
| Moss SDK matches `moss_context.py` assumptions | **yes** — `text=`, `.docs` both already correct | §3.1 |
| Deepgram STT round-trips clinical speech | **0.999 confidence**, exact match, "CPR" correct | §1 |
| Deepgram TTS (Aura-2) works | 31 KB WAV | §1 |
| Gemini calls tools correctly | parallel calls, correct args, Level 5 on cardiac arrest | gemini-verification |
| Python 3.13 runs `moss` 1.12.0 | yes | §6 |

**NFR-001 now has an artifact behind it for the first time.** It was previously marked
BUILT with no execution on record.

### Found — blocking

1. **Moss retrieval quality is 2/5 top-1** on an unambiguous 5-document corpus.
   *"not breathing no pulse"* returns the **drowning** protocol instead of CPR. Scores are
   quantised (`1.0`/`0.8`/`0.775`), `alpha` has no effect, and `session.model_id` reads
   `None` even when set — embeddings appear not to be engaging. Fast and wrong is worse than
   slow and right: the output gate cannot catch this, because the instruction *is* correctly
   cited, to the wrong document. See tool-verification §3.3–3.7 for the action list.

2. **Gemini LLM turn measured 2.3–2.6 s.** NFR-002 budgets **500 ms end-to-end**. Cold,
   unstreamed, single-shot, India→US, so streaming will improve it — but not 5×. NFR-002
   must be re-derived from measurement, not restated.

3. **`gemini-2.5-flash` is retired** (404 for new users). Any doc naming it is stale.
   `gemini-3.6-flash` is the pick; `gemini-3.8-flash` returned 503, so the provider config
   needs a fallback chain.

### Decided

- **ElevenLabs dropped.** Deepgram Aura-2 covers TTS on the STT key. Published gap is
  40–50 ms TTFB against a budget the LLM overruns by ~2 s — not worth a second vendor.
- **OpenAI → Gemini `gemini-3.6-flash`** (no OpenAI credits available).
- **Stack is four vendors:** LiveKit (transport), Deepgram (STT+TTS), Gemini (LLM),
  Moss (retrieval). Down from six.
- Ollama (`nemotron-3-nano:4b`, already installed) is the intended backend for the test
  suite and SOAP generation, to keep the default run free and hermetic. Not yet wired.

### Open

- `MOSS_PROTOCOLS_INDEX`, `DEEPGRAM_MODEL`, `LLM_MODEL`, `NEXT_PUBLIC_LIVEKIT_URL` are blank
  in `.env` — non-secret config dropped during credential entry.
- Spike 0 (dependency install, `livekit-agents==1.8.1` on Python 3.13) — **not yet run**.
- Spike 2 (LiveKit room + data channel) — not yet run.
- D1 scope, D2 live-infra, D3 deep-tier — still open in BUILD-PLAN §5.

---

## 2026-09-16 · Session 1 — survey and plan

**Goal:** read the design, establish real state, produce a build plan.

### Found

- `docs/DESIGN.md` (1017 lines) describes an 8-layer architecture; `docs/reviews/` holds
  three multi-persona readiness reviews scoring **PROD 1/5 across 17 consecutive personas**.
- **Domain layer is genuinely done and tested** — 62 tests pass in 0.17 s, hermetic.
  `triage.py` and `phrases.py` import nothing external.
- **`transcript.py` is written, tested by nothing, and wired into nothing.**
  `main.py:129` still reads the STT event inline with `getattr`. Consequence:
  `hard_escalation_triggered` has one call site, inside a tool, fed text **the model chose
  to pass**. The deterministic net meant to outrank the model is gated on the model.
- **Nothing was installed** — no `moss`, no `livekit-agents`, no plugins. The integration
  half of the codebase had never been executed on this machine. The green suite proved only
  that the pure domain layer was correct.

### Decided

- Insert **Phase B-1 (tool verification spikes)** before any subsystem work.
- Subsystem order **B2 → B6**, following data flow: transcript → triage → gate → harness →
  audit/retrieval.
- B2 (wire `normalize_transcript`, run the classifier at the transcript edge) is the
  highest-severity open item and is cheap.
