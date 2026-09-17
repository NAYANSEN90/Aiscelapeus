# Clinical safety review — assessment design

**Date:** 17 Sept 2026
**Reviewer:** `readiness-field` persona, adversarial, read-only
**Subjects:** `docs/CLINICAL-STANDARDS.md`, `docs/ARCHITECTURE-ASSESSMENT.md`
**Scope:** clinical content only — is the medicine right, and right *for this product*
**Outcome:** design revised before implementation. No code had been written against it.

| Bar | Score |
|---|---|
| DEMO | 3/5 |
| PROD | **1/5** |

> Two findings, if implemented as written, produce a bystander being told an unconscious
> patient is expectant, and a bystander being sent down a pulse-check branch lay rescuers
> demonstrably cannot perform. Both are fatal-outcome paths.

---

## Blocker 1 — BLACK/expectant is clinically invalid in this product

`CLINICAL-STANDARDS.md` §3.3, §3.5, §4.1, §7 case 4

START and JumpSTART exist to ration one responder across many casualties. **BLACK means "I
am walking past this person to reach someone I can save."** That calculus does not exist
when a bystander has one patient and nowhere else to go.

§3.3 takes an apneic child, checks a pulse, and on "no pulse" terminates at BLACK. §7 case 4
enshrines this as a **test oracle**: *apneic toddler, remains apneic with jaw thrust, no
pulse → BLACK*.

In a single-patient incident that presentation is **paediatric cardiac arrest**, and the
correct action is **5 rescue breaths then CPR** — the one intervention with a real survival
curve in children. The project's own corpus already holds the right answer
(`protocols.py:18-31` adult CPR, `:99-110` drowning's 5-breaths-first), so the algorithm
layer would be **overriding correct content with an MCI rationing decision**.

Concretely: the agent says the equivalent of *"there is nothing more you can do"* to a
parent kneeling over their child.

> **§4.1 is backwards.** It flags `Criticality`'s inability to express BLACK as a gap to
> close in the type system. **That inability is the correct property of the type.**
> `triage.py:38-43` stays as it is.

**Fix:** delete BLACK from the responder path. Apneic + pulseless = Criticality 5 + CPR,
always. BLACK re-enters only behind an explicit multi-casualty mode with a trained-responder
gate — never for a bystander.

## Blocker 2 — the pulse check gates life on an assessment lay rescuers cannot perform

`CLINICAL-STANDARDS.md` §3.3

Lay-rescuer pulse detection is documented as roughly coin-flip accurate with long check
times. **This is exactly why ILCOR/AHA/ERC removed the pulse check from lay BLS**, replacing
it with *"unresponsive and not breathing normally → compressions."* §3.3 reinstates it as a
life-or-death branch point.

Two symmetric harms, both terminating in no resuscitation:

- **False "no pulse"** on a child with a weak pulse → BLACK → death.
- **False "pulse present"** → the "still apneic after 5 breaths → BLACK" leaf → still no CPR.

**Both leaves of the branch converge on "do not resuscitate," reached through a measurement
the operator cannot make.**

This violates the dividing line the same document states at §5.4: *"if a bystander with no
equipment cannot perform it, it does not belong on the responder path."* Pulse palpation on
a trapped toddler fails that test more clearly than anything §5.4 actually excluded.

`phrases.py:132-141` already treats "can't find a pulse" as `no_pulse` → CRITICAL
escalation, which is correct. **The L2 design would invert the meaning of the same utterance
depending on which layer processed it.**

**Fix:** the 5 rescue breaths become unconditional; the pulse check stops being a branch
point.

## Blocker 3 — agonal breathing has no handling

`ABSENT:` searched §1.2, §2.1 step 2, §3.3 step 2 — no mention of agonal, gasping, or
"not breathing normally" as a distinct state in any algorithm branch.

§1.2 says "not breathing normally" (correct RCUK phrasing), but every downstream algorithm
collapses to a **binary `Breathing? YES/NO`**.

**Roughly 40 % of witnessed arrests present with agonal gasps**, and the untrained caller's
answer to *"is he breathing?"* is **yes** — they see chest movement and hear noise. A binary
question routes that patient to respiratory-rate assessment instead of CPR.

> This is the classic dispatcher-CPR failure mode and **the most probable way this specific
> product kills someone**, because it fires on the most common cardiac arrest presentation.

`phrases.py:120-129` catches it *if the responder volunteers the word*. **But the agent's own
question shapes the answer.**

**Fix:** three states — `normal` / `abnormal-or-gasping` / `none`; the latter two both route
to CPR. The agent must never ask a bare breathing question.

## Blocker 4 — "always take the worse branch" is unsafe as a blanket policy

`ARCHITECTURE-ASSESSMENT.md` §5.1

"Over-triage is recoverable, under-triage is not" holds for **category assignment**. It does
not hold once a category drives a **physical instruction**, and this architecture couples
them.

- **"Ambiguous → JumpSTART"** is backwards. JumpSTART contains the pulse check whose no-pulse
  leaf is BLACK. Defaulting an ambiguous adolescent there routes them toward the expectant
  leaf; under START they get RED. **The worse branch is worse in the wrong direction.**
- **"Cannot determine capillary refill → RED"** fires on effectively *every* patient, because
  bystanders can never determine it. An input that is always ASSUMED_WORST carries zero
  information. **Universal over-triage is signal loss, not caution** — it destroys the
  ability to distinguish the genuinely critical patient.
- **The ratchet latches assumptions permanently** (`triage.py:192-206`). A patient
  ASSUMED_WORST → 5 at minute one cannot be corrected at minute three, even by a clinician on
  the bridge. **Assumption-driven and evidence-driven CRITICAL become indistinguishable at
  the ratchet** — the exact conflation §6's certainty type exists to prevent. The type is
  defined but the ratchet never consumes it.

**Fix:** scope the rule. Worse-branch is correct for **escalation and summoning a clinician**.
For **instructions to a bystander**, the rule is *"give the intervention that is safe under
both hypotheses"* — for airway/breathing uncertainty that is almost always CPR, and for
algorithm ambiguity it is START, not JumpSTART.

## Blocker 5 — clinician-only assessments and treatments sit unscoped on the responder path

`CLINICAL-STANDARDS.md` §1.3–§1.7

**Cannot be performed by a bystander:** capillary refill (needs 5 s timed pressure, heart
level, good light, warm environment — and §3.2 says it is unreliable when cool, then §2.1
uses it as the adult RED/YELLOW discriminator anyway); counted respiratory rate (needs a
timepiece and an unaware patient); tracheal position, percussion, auscultation, JVP (needs a
stethoscope and training); pulse pressure and urine output (needs a cuff and a catheter —
urine output is an ICU trend measure); pupil exam and blood glucose (needs a glucometer, so
the 4.0 mmol/L threshold is unreachable).

**Cannot be given by a bystander:** 15 L/min oxygen via reservoir mask, bag-mask ventilation,
large-bore IV access and crystalloid boluses.

§1.5 correctly marks the IV block *"out of scope for field first aid, recorded for the
clinician bridge."* **That qualifier appears nowhere else**, so an LLM consuming this
document has no structural signal distinguishing *"tell the bystander this"* from *"record
this for the clinician."*

> **The risk of asking anyway is worse than omission.** Every undoable assessment costs
> 30–60 s of the golden window, erodes caller compliance, and — worst — produces a
> **fabricated answer**. A panicking caller asked to count breaths will guess. That guess
> enters L2 as INFERRED and drives a deterministic branch. **Far more dangerous than
> UNKNOWN, because it looks like data.**

Also noted: `ARCHITECTURE-ASSESSMENT.md` §7 concedes "nobody counts breaths in a crisis",
then keeps RR as the primary discriminator in both algorithms. "Panting" cannot distinguish
RR 28 from RR 34, and **30 is the START RED boundary**.

---

## Accuracy audit — transcription was sound

The numbers were right; the defects are qualifiers and context. Verified correct: ABCDE
RR 12–20/>25, SpO₂ 94–98 % / 88–92 %, cap refill <2 s, glucose 4.0 mmol/L, ACVPU including
the newer C; START RR 30 / cap refill 2 s / obeys commands and "RPM 30-2-Can Do";
JumpSTART ages 1–8, RR 15–45, 5 breaths, AVPU where inappropriate-P or U → RED.

Two omissions and one error:

- **`ABSENT:`** published START permits **absent radial pulse** as the alternative perfusion
  check when cap refill is unusable (dark, cold). Its omission matters because cap refill is
  the more bystander-hostile of the two.
- **§4.1 maps RED → Criticality 4–5.** RED is a single category; splitting it means the same
  algorithm output produces two escalation behaviours depending on an LLM's read of "arrest
  markers" — **L3 resolving an output**, which `ARCHITECTURE-ASSESSMENT.md` §5 forbids.
- The 5 rescue breaths are faithfully reproduced as pulse-gated, which is correct for MCI and
  is exactly the problem here (Blocker 2).

## Dangerous omissions, ranked

1. **Agonal breathing** — Blocker 3. Highest-probability killer.
2. **`ABSENT:` opioid overdose / naloxone.** Searched `CLINICAL-STANDARDS.md`,
   `protocols.py`, `prompts.py` — no match for naloxone, opioid or overdose. Leading cause of
   out-of-hospital death in the 20–45 bracket; presents exactly as this system's existing
   triggers ("unresponsive", "barely breathing", "blue lips" — **all three already in
   `phrases.py`**); reversible in seconds; antidote increasingly carried in public kits.
   **The system will meet this patient and has nothing to say.**
3. **`ABSENT:` choking-vs-arrest differentiation in the algorithms.** Witnessed choking
   collapse → back blows/thrusts *before* CPR; unwitnessed → compressions immediately. Wrong
   branch either wastes the window or fails to clear the obstruction.
4. **`ABSENT:` a blocking scene-safety gate.** One line in §1.2 and a good entry in
   `protocols.py:273-284`, but **no DR in the DRABC**. The bystander is the operator; if they
   become casualty two, the patient dies too. Must be a hard precondition, not a retrievable
   document.
5. **`ABSENT:` "when NOT to move" as a cross-cutting constraint.** §1.6 says "unconscious →
   lateral position" with **no spinal caveat**, and START step 1 asks "can walk?" — an
   instruction to a possibly spine-injured patient to stand. Acceptable in MCI; gratuitous
   harm for one patient with time available. The airway-overrides-spine rule should be
   stated, not implicit.
6. **`ABSENT:` recovery position in any algorithm terminal state.** The single most valuable
   thing a lone bystander does for an unconscious-but-breathing patient.
7. **`ABSENT:` "call emergency services first".** §1.1's "call for help early" is a
   *clinician* instruction meaning summon the resus team. **The agent is not the ambulance.**
8. **`ABSENT:`** what happens when the bystander *is* the patient; no CPR-coaching metronome
   path despite `protocols.py:18-31` holding the rate.

## What the review found already correct

- **`protocols.py` is markedly more fit for a layperson than the standards document** —
  correct, imperative, one action per line, scoped to what an untrained person can do:
  recovery position *with* the spinal caveat (`:86-97`), drowning's 5-breaths-first
  (`:99-110`), anaphylaxis including the 5-minute second dose (`:166-178`), hypothermia's
  rough-handling warning (`:245-258`).
- **`phrases.py` encodes the right clinical instincts** — agonal/gasping as arrest
  (`:120-129`), drowning as arrest until proven otherwise (`:183-187`), negation and
  past-tense suppression (`:191-228`). *"The parts of this system I would trust at a real
  scene."*
- **§5.4 is exemplary** — excluding FAST-ED/RACE/LAMS, shock index, qSOFA and full GCS with
  reasons. The failure is that §1.5–§1.6 and §3.3 violate that very line.
- **§5.3's insistence that FAST's T is the part not to lose** — the sharpest clinical
  judgement in the document.
- **§7 flags its own test oracles as unverified**, which is what made case 4 findable at
  design time rather than after implementation.

---

## Disposition

**ADR-009 recorded** (`CLINICAL-STANDARDS.md` banner): START/JumpSTART demoted to
`CLINICIAN-BRIDGE` reference. The **ABCDE spine plus `protocols.py`** is the responder-path
foundation.

This goes further than the reviewer proposed — they suggested rewriting §2/§3 as
single-patient algorithms. Removing them from the responder path entirely removes the
category error at the root rather than patching its symptoms.

### Lesson for the record

**The error was importing an algorithm's conclusions without its preconditions.**
START/JumpSTART are valid under scarcity, with a trained triage officer and a queue of other
patients. Strip those and BLACK stops meaning "unsalvageable" and starts meaning "we gave up".

The document also **stated the correct rule in §5.4 and then violated it three times**.
Writing a principle down is not the same as applying it — which is the argument for external
review over more careful self-review.
