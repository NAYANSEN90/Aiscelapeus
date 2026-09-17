# Architecture — the assessment core

**Status:** design, pending adversarial review. No code written against this yet.
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

### 5.1 The failure direction is fixed

**When an input cannot be established, the system does not guess. It takes the worse branch
and says so.**

- Cannot determine breathing rate → treat as RED, tell the responder why.
- Cannot determine capillary refill → treat as RED.
- Ambiguous between START and JumpSTART → **JumpSTART**, because it has the rescue-breath
  branch that can still save a child.

Over-triage is recoverable. Under-triage is not. Every such decision is recorded as an
assumption, not as a finding (§6).

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
| **ASM-02** | All 9 worked MCI scenarios (`CLINICAL-STANDARDS.md` §7) produce the published category. **Cases 4, 6, 8 are discriminating** |
| **ASM-03** | ABCDE ordering cannot be violated: no reachable path advises on a later letter while an earlier one is unresolved |
| **ASM-04** | Every unestablished input results in the **worse** branch, never the better one. Property-tested across the input space |
| **ASM-05** | L1 fires on the raw transcript independently of L2 and L3 — proven by driving a transcript with L3 stubbed to return nothing |
| **ASM-06** | L3 cannot override an L2 branch — structurally, by the type signature, not by convention |
| **ASM-07** | Certainty survives into span attributes, SOAP, and the catch-up summary. 0 inputs reported as ESTABLISHED that were ASSUMED_WORST |
| **ASM-08** | Adult/paediatric selection defaults to **JumpSTART** when ambiguous |
| **ASM-09** | The full L2 suite runs with **no network and no model**, in CI, in under 5 s |
| **ASM-10** | A deteriorating patient forces re-entry at A; the ratchet is never violated by re-assessment |

---

## 11. Open questions

1. **BLACK in `Criticality`.** Add a value, add a separate axis, or document why a
   single-casualty field agent never assigns it?
2. **Multiple patients.** START/JumpSTART are *mass-casualty* tools; the current system
   models one incident with one `TriageState`. Is multi-casualty in scope, or do we adopt the
   thresholds while explicitly not supporting MCI?
3. **Does L3 propose inputs, or extract them?** Extraction is safer and testable;
   proposal ("sounds like maybe RR 30-ish") is more useful and less verifiable. Leaning
   extraction-only, with `INFERRED` as the ceiling.
4. **Where does the treat-before-moving-on rule bite?** Blocking all other conversation is
   clinically correct and may be unusable if the responder wants to talk about something
   else. Needs a stated policy.
5. **Certainty decay.** Does an ESTABLISHED input go stale? A respiratory rate from four
   minutes ago is not current. Probably yes, per-input, per ABCDE letter.
6. Every threshold still needs **clinician review** before real use.
