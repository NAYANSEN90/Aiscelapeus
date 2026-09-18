# Architecture — the assessment core

**Status:** design, **revised 18 Sept after adversarial clinical review**. Still no code
written against it - which is the point: `docs/reviews/2026-09-17-clinical-safety.md` scored
the original PROD 1/5 and found two paths that end in a death, so the revision had to land
before L2 was built. See §5.1, the ASM table, and §11.
**Date:** 17 Sept 2026
**Depends on:** `docs/CLINICAL-STANDARDS.md` (the algorithms and thresholds)
**Supersedes:** the implicit "LLM decides, retrieval supports" model in `docs/DESIGN.md` §7.3

> This is the heart of the system. Everything else — retrieval, the output gate, the audit
> trail, offline mode — is in service of getting this right or proving it was right.

---

## 1. The idea in one line

**The published algorithm provides the structure. The agent handles the uncertainty.**

ABCDE, START and JumpSTART are written for a trained clinician standing over a patient. They
assume the assessor can already see what they are assessing. Nothing in them says how to get
from *"there's blood everywhere and he's not waking up"* to a checkbox.

That gap is the agent's entire job — and it is the only job it has.

---

## 2. Why the algorithm cannot do it alone

| The algorithm says | The gap |
|---|---|
| "Assess airway patency" | A bystander does not know what patency is |
| "Respiratory rate > 30?" | Nobody counts breaths in a crisis |
| "Capillary refill > 2 s" | Requires knowing the test exists, and a clock |
| "Follows simple commands" | Is *"he sort of mumbled"* V or P? |
| One patient | *"There are three of them"* |
| Linear progression | The patient deteriorates mid-assessment |

## 3. Why the agent cannot do it alone

Measured, in this repo:

- Moss retrieval: **64 % top-1** (`docs/evidence/2026-09-17-moss-retrieval-verified.md`).
- The LLM turn: **2.3–2.6 s** against a 500 ms budget
  (`docs/evidence/2026-09-16-gemini-verification.md`).
- 17 consecutive readiness personas scored PROD 1/5, the recurring theme being that safety
  properties rested on the model choosing to invoke them.

A model that is right most of the time is the wrong mechanism for a decision that must be
right every time and explainable afterwards.

---

## 4. The three layers

```
┌──────────────────────────────────────────────────────────────┐
│ L1  phrases.py — deterministic markers                        │
│     Fires on the raw transcript. Outranks everything below.   │
│     "not breathing" -> CRITICAL, whatever anything else said. │
└──────────────────────────────────────────────────────────────┘
                              ▲ outranks
┌──────────────────────────────────────────────────────────────┐
│ L2  Assessment state machine — ABCDE / START / JumpSTART      │
│     Pure. No I/O, no model. Given established inputs, the     │
│     branch taken is fixed and testable.                       │
└──────────────────────────────────────────────────────────────┘
                              ▲ feeds inputs to
┌──────────────────────────────────────────────────────────────┐
│ L3  The agent — handles uncertainty                           │
│     Elicits, translates, infers, re-asks, disambiguates.      │
│     May FILL an input. May never OVERRIDE a branch.           │
└──────────────────────────────────────────────────────────────┘
```

Each layer has a different trust level, and trust flows **upward only**. L3 is the least
trusted and does the most work; L1 is the most trusted and does the least.

### 4.1 L1 — deterministic markers

`phrases.py`, already built and tested (26 phrasings, negation and past-tense handling).

Fires on the **raw transcript at the edge**, not on text a model chose to pass. It is the
backstop for total failure of everything above it.

> **Currently defective.** `hard_escalation_triggered` has one call site — inside a tool,
> fed the text the *model* selected. The net meant to catch model failure is gated on the
> model. B2 fixes this; this architecture depends on it being fixed.

### 4.2 L2 — the assessment state machine

Pure domain logic beside `triage.py`. No network, no model, no clock beyond the injected one.

Owns: the ABCDE ordering constraint, the treat-before-moving-on rule, the START/JumpSTART
branches, the numeric thresholds as named constants with citations, and the
adult-vs-paediatric algorithm choice.

Given a set of established inputs, the output is **a pure function**. Same inputs, same
branch, every time, testable without a network.

### 4.3 L3 — the agent

Owns everything the algorithm cannot specify: phrasing a question a panicking bystander can
answer, translating *"he's panting"* into a respiratory-rate estimate, deciding whether
*"he sort of mumbled"* is V or P, noticing a second patient, noticing deterioration.

This is real work and it is where an LLM genuinely earns its place. It is also the only
layer permitted to be non-deterministic.

---

## 5. The asymmetric boundary — the core rule

> **The agent may resolve uncertainty about *inputs*.
> The agent may never resolve uncertainty about *outputs*.**

| Question | Whose job |
|---|---|
| *"Is the respiratory rate above or below 30?"* | **L3.** Gather, infer, ask again, coach a count |
| *"RR is 34, therefore RED"* | **L2.** Deterministic, in code, not negotiable |

"The agent handles uncertainty" must not become "the agent decides". Once an input is
established, the branch is arithmetic.

### 5.1 The failure direction is fixed — **but scoped**

> **Amended 18 Sept 2026.** The blanket form of this rule was **Blocker 4** in
> `docs/reviews/2026-09-17-clinical-safety.md`, and the three examples it originally gave
> were all wrong. Recorded rather than rewritten silently, because "always take the worse
> branch" reads as obviously safe and is the kind of principle that gets restored by
> someone who has not read the review.

**The rule holds for category assignment and for summoning a clinician. It does NOT hold
once a category drives a physical instruction, and this architecture couples them.**

| Uncertainty about… | Rule |
|---|---|
| **Escalation / criticality category** | Take the worse branch. Over-triage is recoverable; under-triage is not. |
| **An instruction to a bystander** | Give the intervention that is **safe under both hypotheses**. For airway or breathing uncertainty that is almost always **CPR**. |
| **Which algorithm applies** | **START**, never JumpSTART. |

Why each original example was wrong:

- ~~*Ambiguous between START and JumpSTART → JumpSTART*~~ — **backwards.** JumpSTART
  contains the pulse check whose no-pulse leaf is BLACK, so defaulting an ambiguous
  adolescent there routes them toward the **expectant** leaf, where START would give them
  RED. The worse branch was worse *in the wrong direction*.
- ~~*Cannot determine capillary refill → RED*~~ — fires on **every** patient, because a
  bystander can never determine capillary refill (it needs 5 s timed pressure at heart
  level, good light and a warm environment). An input that is *always* assumed-worst
  carries **zero information**: universal over-triage is signal loss, not caution, and it
  destroys the ability to pick out the genuinely critical patient.
- ~~*Cannot determine breathing rate → RED*~~ — a counted respiratory rate needs a
  timepiece and an unaware patient, so it is unavailable for the same reason. Breathing is
  assessed as the three states in Blocker 3 — `normal` / `abnormal-or-gasping` / `none` —
  with the latter two both routing to CPR, not as a rate.

**And the ratchet must consume the certainty type.** `triage.py`'s ratchet latches, so a
patient assumed-worst to 5 at minute one cannot be corrected at minute three *even by a
clinician on the bridge* — which makes assumption-driven and evidence-driven CRITICAL
indistinguishable, the exact conflation §6's certainty type exists to prevent. The type is
defined and the ratchet never reads it. Closing that is part of L2, not a later polish.

Every such decision is still recorded as an assumption, not as a finding (§6).

### 5.2 What L3 must never do

1. Override a branch L2 computed.
2. Assign a triage category directly.
3. Downgrade criticality — the ratchet in `TriageState` already forbids this; L3 gets no
   exception.
4. Skip an ABCDE letter because it "seems fine".
5. Report an assumed input as an established one.

---

## 6. Input certainty — a first-class type

An input is not a value. It is **a value plus how we came to hold it.**

```
Certainty:
    UNKNOWN         not yet asked, or asked and not answered
    ASSUMED_WORST   could not establish; worst-case taken, deliberately
    INFERRED        derived from responder description ("panting" -> RR high)
    ESTABLISHED     responder gave a usable direct answer
    OBSERVED        measured (future: device or count-along)
```

**Why this matters more than it looks.** A RED assigned because RR was genuinely 34, and a
RED assigned because RR could not be determined, are clinically different events that
currently would look identical in the record. The clinician joining the bridge needs to know
which one they are walking into.

Every input carries its certainty into:

- the **span attributes** (so the trace explains the decision),
- the **SOAP note** (so the record distinguishes observed from assumed),
- the **clinician catch-up summary** (so the first thing they see is what is *not* known).

### 6.1 A real confidence signal — replacing PE-006

PE-006 specifies a "low-confidence retrieval floor" — escalate when the retrieval score
falls below a threshold. **That mechanism cannot work:** Moss `.score` is
Reciprocal-Rank-Fusion output and encodes rank, not similarity
(`docs/evidence/2026-09-17-moss-retrieval-verified.md` §1.2). A threshold on it is
meaningless.

**Replacement, computed from state rather than from an embedding:**

```
assessment_confidence = f(count of UNKNOWN and ASSUMED_WORST inputs,
                          which letters they fall under,
                          how long they have been unresolved)
```

An A or B input still `UNKNOWN` after two turns is a stronger and more honest escalation
trigger than any similarity score, and it does not depend on Moss at all.

---

## 7. How a turn actually flows

```
transcript (final)
   │
   ├─> L1 markers on the RAW text ──── hit ──> CRITICAL + escalate. Independent path.
   │                                            Does not wait for L2 or L3.
   │
   ├─> L3 extracts candidate inputs from the utterance
   │      "he's breathing really fast, like panting"
   │        -> respiratory_rate: INFERRED, high
   │
   ├─> L2 receives inputs, recomputes state
   │      - which ABCDE letter is currently unresolved
   │      - whether a triage branch is now determinable
   │      - what the next required assessment is
   │
   ├─> L3 phrases the next question OR the next instruction
   │      guided by what L2 says is needed, not by what L3 finds interesting
   │
   └─> Moss retrieves HOW to perform the step L2 selected
          (detail-fetch, not routing — see §8)
```

**L1 runs on every transcript regardless of what L2 and L3 are doing.** It is not a step in
the pipeline; it is a parallel tripwire.

---

## 8. What this does to retrieval

Retrieval is **demoted from router to detail-fetcher**, and that is the real fix for 64 %
top-1.

| Before | After |
|---|---|
| Free-text utterance → Moss → hope the right protocol is first | L2 names the required step → Moss fetches how to perform *that* |
| A similarity question | A lookup with a known key |
| Wrong answer = wrong protocol spoken confidently | Wrong answer = imprecise detail on a step already known to be correct |

A better embedder was never going to fix this, because *"what step comes next"* is not a
similarity question. It is a published protocol. This also explains why `moss-mediumlm` tied
with `moss-minilm` at 64 % — both were being asked the wrong question.

The retrieval fixes in `BUILD-PLAN.md` §8.2 (`alpha`→1.0, top-5 + re-rank) still apply and
still help. They are simply no longer load-bearing for safety.

---

## 9. Consequences for existing components

| Component | Change |
|---|---|
| `phrases.py` | Markers should map to **which ABCDE letter is failing**, not only to a level. More useful and more auditable |
| `triage.py` | `Criticality` 1–5 cannot express **BLACK**. Either add it or state why a single-casualty field agent never assigns it |
| `transcript.py` | Becomes the L1 entry point — the fix B2 was already going to make |
| PE-006 | Re-specified per §6.1; the score-threshold mechanism is unbuildable |
| Output gate (B4) | Gains a second job: reject an instruction that skips an unresolved earlier ABCDE letter |
| SOAP | **S**ubjective gains structure from SAMPLE/OPQRST; every claim carries its certainty |

---

## 10. What must be proved before this is trusted

Battle-testing means these are green, not that the design reads well.

| ID | Assertion |
|---|---|
| **ASM-01** | L2 is a pure function: identical inputs produce an identical branch, 1000 randomised runs, no I/O |
| **ASM-02** | ~~All 9 worked MCI scenarios produce the published category~~ **WITHDRAWN.** The deck's own answers are not a valid oracle here: case 4 (apneic pulseless toddler → BLACK) is the fatal-outcome path Blocker 1 names, and START/JumpSTART categories presuppose the rationing decision a single-casualty bystander never makes. Replaced by ASM-11/12/13 |
| **ASM-03** | ABCDE ordering cannot be violated: no reachable path advises on a later letter while an earlier one is unresolved |
| **ASM-04** | **SCOPED, see §5.1.** An unestablished input takes the worse branch for *category and escalation only*. For an *instruction*, the assertion is that the chosen intervention is safe under **both** hypotheses. Property-tested across the input space, with instructions and categories asserted separately |
| **ASM-05** | L1 fires on the raw transcript independently of L2 and L3 — proven by driving a transcript with L3 stubbed to return nothing |
| **ASM-06** | L3 cannot override an L2 branch — structurally, by the type signature, not by convention |
| **ASM-07** | Certainty survives into span attributes, SOAP, and the catch-up summary. 0 inputs reported as ESTABLISHED that were ASSUMED_WORST |
| **ASM-08** | **INVERTED.** Adult/paediatric ambiguity defaults to **START**, never JumpSTART: JumpSTART's pulse-check branch has a BLACK leaf, so the old default routed an ambiguous adolescent toward *expectant* where START gives RED |
| **ASM-09** | The full L2 suite runs with **no network and no model**, in CI, in under 5 s |
| **ASM-10** | A deteriorating patient forces re-entry at A; the ratchet is never violated by re-assessment |
| **ASM-11** | `Criticality` still cannot express BLACK, and no path assigns an expectant category. Apneic + pulseless is **always** Criticality 5 + CPR |
| **ASM-12** | No reachable path makes a pulse check a branch point. The 5 rescue breaths are unconditional |
| **ASM-13** | **SCOPED, AND RE-SCOPED 18 Sept — see below.** Breathing is a three-state input - `normal` / `abnormal-or-gasping` / `none`. `abnormal-or-gasping` routes to CPR **only in a patient who is also unconscious** (ACVPU P or U): an *awake* patient breathing abnormally is never compressed and reaches the conscious-distress leaf. `none` routes to CPR **whatever responsiveness was reported**, because "awake" plus "not breathing at all" is a contradiction in which apnoea wins; the contradiction is re-tested inside the arrest instruction rather than resolved silently. The one exception is a *responsive* patient with an established **witnessed foreign body**, who reaches thrusts - a complete obstruction is the single state where both reports are accurate. No reachable path asks a bare yes/no breathing question |
| **ASM-14** | The ratchet reads the certainty type: a level reached by assumption is correctable by later evidence, a level reached by evidence is not |
| **ASM-15** | No assessment a bystander cannot perform appears on the responder path (Blocker 5's list: capillary refill, counted RR, auscultation, JVP, pupils, glucose, pulse pressure) |

### Why ASM-13 is scoped (18 Sept)

The row originally read *"both non-normal states route to CPR"*, full stop, and it was
implemented that way — as a property of the breathing value alone. That is **a conclusion
imported without its precondition**, the exact error `2026-09-17-clinical-safety.md`'s own
closing lesson names, and it reached a harmful branch on complete inputs: an **ALERT**
patient with abnormal or gasping breathing was told to receive chest compressions.

The guideline is a **conjunction**, and this repo already stated it three times —
`CLINICAL-STANDARDS.md` §1.2, `PulseReport`'s docstring, and the 17 Sept review itself:
*"unresponsive **and** not breathing normally → compressions."* Agonal respiration is a
brainstem reflex of a dead circulation; it does not coexist with A on ACVPU. The awake
patients this caught — the asthmatic, the anaphylaxis, the pulmonary oedema, the partial
obstruction — are common, and all are made **worse** by being laid flat and compressed.

**Blocker 3 is not reintroduced by this scoping.** Its target was the *binary question*,
and its failure mode is a **collapsed** patient whose caller answers "yes he's breathing".
An unconscious patient with either non-normal state still routes to CPR with no gate at
all. The precondition is now enforced by the **signature**: `routes_to_cpr` takes
responsiveness, so there is no expression that reaches the CPR decision without naming the
patient's consciousness. Pinned by
`test_no_reachable_branch_compresses_the_chest_of_an_awake_patient`, which walks the whole
input space, and by `test_the_cpr_decision_cannot_be_made_without_naming_consciousness`.

### Why ASM-13 was RE-scoped, same day (18 Sept)

The scoping above was applied **too widely**, and a second `readiness-field` clinical pass
made it the lead finding. Gating *both* non-normal states on responsiveness meant a patient
reported as **not breathing at all** but still called "alert" reached the conscious-distress
leaf at **Criticality 4** — told to sit upright and use a reliever inhaler. Reproduced by
execution, on complete inputs.

The harm the conjunction prevents — compressions on a talking asthmatic — **only ever
existed for `abnormal-or-gasping`**, which is how a conscious patient in respiratory
distress actually presents. Nobody awake presents with `none`: apnoea beside an "alert"
report is a stale or wrong responsiveness report, the likeliest error in the first sixty
seconds of a panicking call, and resolving it toward the reassuring half is the fatal
direction. So `none` ignores the reported responsiveness.

**The contradiction is re-tested, not resolved silently.** Returning to
`ASK_RESPONSIVENESS` was the reviewer's preferred option and was rejected: L2 is pure and
holds no turn history (ASM-01), so a caller re-reporting the same two values gets the same
question forever — Blocker 3(a)'s absorbing gate rebuilt on the patient with the least
time. Instead the arrest rationale names the contradiction, quotes it back, and asks for
the re-check **while compressions run**, which is Blocker 2's "do both; do not choose"
applied to information rather than to bleeding.

**One exception, and an independent review found it was needed twice over.** A *responsive*
patient with an established **witnessed foreign body** keeps the choking instruction: a
complete obstruction is the one state where "awake" and "moving no air" are both accurate,
and compressions cannot shift a lodged bolus. The review then found that the *question*
`ASK_AIRWAY_OBSTRUCTION` also has to outrank the arrest route — gating it on the arrest
route meant a first-turn ALERT-plus-apnoea patient skipped the block and was never asked
the discriminator at all, reintroducing Blocker 5's death through this very fix. The
question decides the route, so it precedes it; `_reaches_choking_block` holds that fact
once, and `_routes_to_arrest_path` consults it.

`_is_arrest` carries the same asymmetry for the **category**, so the gate in front of the
question cannot cap a reported arrest at SEVERE (Blocker 3(b)). Pinned by
`test_absent_breathing_reaches_the_arrest_path_whatever_responsiveness_says`,
`test_a_responsive_patient_is_asked_the_choking_question_before_compressions`,
`test_a_witnessed_obstruction_still_reconciles_awake_with_no_breathing` and
`test_the_apnoea_contradiction_is_not_re_asked_as_a_question`.

---

## 11. Open questions

1. ~~**BLACK in `Criticality`.**~~ **ANSWERED, 17 Sept, by the clinical review — and the
   question was posed backwards.** `Criticality`'s inability to express BLACK is the
   **correct property of the type**, not a gap to close. BLACK means *"I am walking past
   this person to reach someone I can save"*, and that calculus does not exist for a
   bystander with one patient and nowhere else to go. `triage.py` stays exactly as it is.
   In a single-casualty incident an apneic pulseless casualty is **cardiac arrest**:
   Criticality 5 and CPR, always. Pinned by ASM-11.
2. ~~**Multiple patients.**~~ **ANSWERED by the same review, and it decides more than it
   looks.** MCI is **out of scope**: one incident, one `TriageState`, no rationing. The
   thresholds are adopted as *recognition* cues; the **categories are not**, because a
   category like BLACK encodes a triage decision rather than a clinical finding. This is
   why ASM-02 is withdrawn rather than reworded — the 9 worked MCI scenarios cannot be an
   oracle for a system that does not triage between patients. BLACK re-enters only behind
   an explicit multi-casualty mode with a trained-responder gate, which is not this
   product.
3. **Does L3 propose inputs, or extract them?** Extraction is safer and testable;
   proposal ("sounds like maybe RR 30-ish") is more useful and less verifiable. Leaning
   extraction-only, with `INFERRED` as the ceiling.
4. **Where does the treat-before-moving-on rule bite?** Blocking all other conversation is
   clinically correct and may be unusable if the responder wants to talk about something
   else. Needs a stated policy.
5. **Certainty decay.** Does an ESTABLISHED input go stale? A respiratory rate from four
   minutes ago is not current. Probably yes, per-input, per ABCDE letter.
6. Every threshold still needs **clinician review** before real use.
