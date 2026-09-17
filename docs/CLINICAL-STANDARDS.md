# Clinical assessment standards

**Purpose.** The retrieval layer cannot be the thing that decides what a responder is
looking at. These are the published, deterministic assessment algorithms the system adheres
to. They are checklists with fixed order and fixed numeric thresholds — auditable, testable,
and independent of any model.

**Status:** reference. Sourced 17 Sept 2026. Not yet implemented in code.

**Safety note.** Paraphrased from published guidance for engineering purposes. This project
is not a medical device and is not clinically validated. Every threshold below must be
reviewed by a qualified clinician before any real use.

---

> # ⚠ REVISION IN PROGRESS — read this first
>
> An adversarial clinical review on 17 Sept found **three defects in §2 and §3 that could
> kill a patient**. Findings and the full review are in
> `docs/reviews/2026-09-17-clinical-safety.md`. **§2 (START) and §3 (JumpSTART) must not be
> implemented as written.**
>
> ### ADR-009 — START/JumpSTART are demoted to clinician-bridge reference
>
> **Decision (NAYANSEN, 17 Sept):** the **ABCDE spine plus the existing layperson corpus in
> `agent/aiscelapeus/protocols.py`** is the responder-path foundation. START and JumpSTART
> are retained below as **`CLINICIAN-BRIDGE` reference only** and drive no responder-facing
> state machine.
>
> **Why.** START and JumpSTART are *mass-casualty* tools. Their purpose is rationing one
> responder across many victims, and their preconditions are scarcity, a trained triage
> officer, and a queue of other patients. This product is a **single-patient** agent talking
> to **one untrained bystander** who has nowhere else to go. Strip those preconditions and
> BLACK stops meaning "unsalvageable" and starts meaning "we gave up".
>
> The tell is §3.5: BLACK patients are reassessed *after* RED and YELLOW — a step that only
> exists when there is a queue.
>
> **The error was importing an algorithm's conclusions without its preconditions.**
>
> ### The three fatal defects
>
> | # | Defect | Consequence |
> |---|---|---|
> | 1 | **BLACK on the responder path** (§3.3, and §7 case 4 as a *test oracle*) | Apneic pulseless toddler = paediatric cardiac arrest. Correct action is 5 rescue breaths then CPR. The agent would tell a parent there is nothing more to do |
> | 2 | **Pulse check as a branch point** (§3.3) | Lay pulse detection is ~coin-flip; ILCOR/AHA/ERC removed it from lay BLS for this reason. **Both leaves converge on "do not resuscitate"** via a measurement the operator cannot make |
> | 3 | **Binary `Breathing? YES/NO`** (§1.2, §2.1, §3.3) | ~40 % of witnessed arrests present with agonal gasps. Ask an untrained caller "is he breathing?" and they say **yes**. Routes the most common arrest presentation away from CPR |
>
> ### Mandated corrections
>
> 1. **Delete BLACK from the responder path.** Apneic + pulseless → `Criticality.CRITICAL`
>    and a CPR instruction, always.
> 2. **The 5 rescue breaths become unconditional.** Remove the pulse check as a branch point.
> 3. **`Breathing?` becomes three states** — `normal` / `abnormal-or-gasping` / `none`. The
>    latter two both route to CPR. **The agent must never ask a bare "is he breathing?"** —
>    it must ask about *normal, regular* breathing and explicitly probe for occasional,
>    noisy or gasping breaths.
> 4. **Every assessment and treatment gets a scope tag** — `RESPONDER` (bystander, bare
>    hands, a phone) or `CLINICIAN-BRIDGE` (recorded for the doctor, never spoken to the
>    caller).
> 5. **Add the missing killers:** opioid overdose / naloxone, a *blocking* scene-safety gate
>    (there is no **DR** in the current DRABC), choking-vs-arrest differentiation, and
>    recovery position as a terminal state.
>
> ### Corrections to statements made elsewhere in this document
>
> - **§4.1 is backwards.** It calls `Criticality`'s inability to express BLACK "a gap to
>   close in the type system". **That inability is the correct property of the type.**
>   `triage.py:38-43` stays as it is.
> - **§7 case 4 is not a valid test oracle.** It encodes the BLACK defect.
> - **§1.5–§1.6 violate the dividing line §5.4 states.** 15 L/min oxygen, bag-mask
>   ventilation, percussion, auscultation, JVP, tracheal position, pulse pressure, urine
>   output, IV fluids and the glucometer threshold are all `CLINICIAN-BRIDGE`. Capillary
>   refill and counted respiratory rate belong in **neither** column for an untrained caller.
>
> ### What the review found already correct
>
> **`agent/aiscelapeus/protocols.py` is more fit for a layperson than this document** —
> recovery position *with* the spinal caveat, drowning's 5-breaths-first, anaphylaxis with
> the 5-minute second dose. `phrases.py` already encodes the right instincts: agonal/gasping
> as an arrest presentation, drowning as arrest until proven otherwise, negation and
> past-tense suppression.
>
> This document went to the literature and produced something **worse than what the project
> already had**, by transcribing clinician-facing guidance without asking who performs it.

---

## Why this document exists

Moss retrieval measures **64 % top-1** (`docs/evidence/2026-09-17-moss-retrieval-verified.md`).
Semantic similarity over free text is the wrong mechanism for deciding *which assessment step
comes next*, because that order is not a similarity question — it is a **fixed protocol**.

The division of labour this implies:

| Job | Mechanism | Why |
|---|---|---|
| What do I assess next? | **Deterministic algorithm (this document)** | Fixed published order; no inference needed |
| Is this immediately life-threatening? | **Deterministic thresholds + `phrases.py`** | Must not depend on a model or an embedder |
| How exactly do I perform this action? | Moss retrieval | Genuinely a lookup; wrong answer is recoverable if the step is right |

Retrieval stops being the router and becomes what it is good at: fetching detail **once the
algorithm has already decided what is needed**.

---

## 1. ABCDE — the primary survey

The standard structured approach for any deteriorating or critically ill patient
(Resuscitation Council UK; European Resuscitation Council).

### 1.1 The governing rules

1. Assess in order **A → B → C → D → E**.
2. **Treat life-threatening problems before moving to the next letter.**
3. Reassess from the top after any intervention.
4. Call for help early.
5. Initial aim is survival, not diagnosis.
6. Allow time for a treatment to work before re-judging it.

> Rule 2 is the one the agent must enforce structurally. An agent that discusses D while A is
> unresolved is wrong regardless of how good its retrieval was.

### 1.2 First steps — the 30-second rapid look

- Ensure personal safety (scene safety before patient contact).
- Conscious? Ask *"How are you?"*. Unresponsive? Shake and ask *"Are you alright?"*.
- **Unresponsive and not breathing normally → start CPR immediately.** This short-circuits
  the whole algorithm.

### 1.3 A — Airway

| Assess | Life-threatening signs |
|---|---|
| Air movement at mouth/nose | Complete obstruction: no breath sounds |
| Chest/abdominal movement | Paradoxical "see-saw" respiration |
| Accessory muscle use | Present |
| Noise | Stridor, gurgling, snoring = partial obstruction |
| Colour | Central cyanosis (**late** sign) |

**Act before B:** airway opening manoeuvre, suction, positioning. High-concentration oxygen,
15 L/min via reservoir mask. Depressed consciousness itself obstructs the airway.

**Target SpO₂: 94–98 %**, or **88–92 %** where hypercapnic respiratory failure is a risk (COPD).

### 1.4 B — Breathing

| Measure | Normal | Concerning |
|---|---|---|
| **Respiratory rate** | **12–20 /min** | **> 25 /min**, or rising |
| SpO₂ | 94–98 % | below target |
| Chest expansion | equal bilaterally | unequal |

Also: rhythm and depth, tracheal position (deviation = mediastinal shift), percussion
(hyper-resonant = pneumothorax; dull = fluid/consolidation), auscultation (wheeze, stridor,
absent sounds), JVP.

**Immediately life-threatening:** tension pneumothorax, massive haemothorax, acute severe
asthma, pulmonary oedema.

**Act before C:** oxygen; bag-mask ventilation if rate or depth is inadequate or absent.

### 1.5 C — Circulation

> *"In almost all medical and surgical emergencies, consider hypovolaemia to be the primary
> cause of shock until proven otherwise."*

| Measure | Normal | Concerning |
|---|---|---|
| **Capillary refill** | **< 2 s** (5 s pressure, at heart level) | **> 2 s** |
| Pulse | present, regular | barely palpable central = poor output; bounding = possible sepsis |
| Limb temperature / colour | warm, pink | cool, pale, mottled, blue |
| Pulse pressure | 35–45 mmHg | narrowed = vasoconstriction |
| Urine output | > 0.5 mL/kg/h | oliguria |

**Immediately life-threatening:** massive or continuing haemorrhage, cardiac tamponade,
septic shock.

**Act before D:** control external haemorrhage. (Clinical setting: large-bore IV access,
500 mL crystalloid bolus over < 15 min — **250 mL** in cardiac failure or trauma; reassess
every 5 min; target systolic > 100 mmHg. Out of scope for field first aid, recorded for the
clinician bridge.)

### 1.6 D — Disability

**ACVPU:** **A**lert · **C**onfused (new) · **V**ocal · **P**ain · **U**nresponsive.

Also: pupils (size, equality, reaction), blood glucose, reversible drug causes.

**Hypoglycaemia < 4.0 mmol/L in an unconscious patient** is immediately life-threatening.

Check A, B and C first — hypoxia and hypotension are the common causes of reduced
consciousness. Unprotected airway + unconscious → lateral (recovery) position.

### 1.7 E — Exposure

Full examination as needed to find what has been missed. Maintain normothermia. Preserve
dignity.

---

## 2. START — adult mass-casualty triage

Simple Triage And Rapid Treatment. Newport Beach Fire & Marine Dept. + Hoag Hospital. The
US gold standard for field adult MCI triage. **30–60 seconds per patient.**

### 2.1 The algorithm

```
Step 1  Can walk?                    ── YES ──> GREEN (minor)
Step 2  Breathing?
          NO  → open airway
                still apneic         ─────────> BLACK (dead/expectant)
                starts breathing     ─────────> RED (immediate)
          YES → step 3
Step 3  Respiratory rate > 30/min?   ── YES ──> RED
          ≤ 30 → step 4
Step 4  Capillary refill > 2 s?      ── YES ──> RED   (control bleeding)
          ≤ 2 s → step 5
Step 5  Follows simple commands?
          NO                         ─────────> RED
          YES                        ─────────> YELLOW (delayed)
```

### 2.2 The mnemonic

| | | |
|---|---|---|
| **R** | Respirations | **30** |
| **P** | Perfusion | **2** |
| **M** | Mental status | **Can Do** |

"**RPM 30-2-Can Do**" — worth reusing verbatim; it is what responders are trained on.

---

## 3. JumpSTART — paediatric mass-casualty triage

Lou Romig MD, 1995. The most widely used paediatric MCI triage tool in the US.

### 3.1 Which algorithm to use

Designed for **ages 1–8**. Below 1 is less likely to be ambulatory; paediatric airway
physiology approaches adult by about 8.

> **Operative rule:** *"If a victim appears to be a **child**, use JumpSTART. If a victim
> appears to be a **young adult**, use START."* Do not try to establish exact age.

### 3.2 Why a separate algorithm exists

1. **An apneic child is more likely to have a primary respiratory problem than an adult.**
   Perfusion may persist briefly and the child may be salvageable — hence the rescue-breath
   step, which adult START does not have.
2. RR ± 30 over- or under-triages children depending on age.
3. Capillary refill is unreliable in a cool environment.
4. "Obeys commands" is not a valid mental-status gauge for younger children.

### 3.3 The algorithm

```
Step 1  Can walk?                     ── YES ──> GREEN
Step 2  Breathing?
          NO  → open airway (positioning)
                breathing resumes     ─────────> RED
                still apneic → check PERIPHERAL PULSE
                    no pulse          ─────────> BLACK
                    pulse present → give 5 mouth-to-barrier ventilations   <- "the jumpstart"
                        breathing resumes  ────> RED
                        still apneic       ────> BLACK
          YES → step 3
Step 3  Respiratory rate 15-45/min?
          NO (< 15, > 45, or irregular) ───────> RED
          YES → step 4
Step 4  Peripheral pulse palpable? (least injured limb)
          NO                          ─────────> RED
          YES → step 5
Step 5  AVPU
          A, V, or APPROPRIATE P      ─────────> YELLOW
          INAPPROPRIATE P, or U       ─────────> RED
```

### 3.4 Non-ambulatory children

Covers infants too young to walk, developmental delay, chronic disability, and injuries
predating the incident.

- Evaluate with the JumpSTART algorithm.
- Any **RED** criterion → **RED**.
- Meets **YELLOW** criteria → **YELLOW** if significant external injury (deep penetrating
  wounds, severe bleeding, severe burns, amputation, distended tender abdomen);
  otherwise **GREEN**.

### 3.5 BLACK category

Unless injuries are clearly incompatible with life, BLACK patients are **reassessed** once
critical interventions for RED and YELLOW are complete.

---

## 4. Triage categories

| Colour | Name | Meaning |
|---|---|---|
| **RED** | Immediate | Life-threatening; needs care within the hour |
| **YELLOW** | Delayed | Not life-threatening; can wait hours |
| **GREEN** | Minor / walking wounded | Non-urgent |
| **BLACK** | Deceased / expectant | Dead or unsalvageable |

### 4.1 Mapping to this system's `Criticality` 1–5

`triage.py` uses a 1–5 scale; START/JumpSTART use four colours. **They are not the same
scale and must not be silently conflated.** Proposed mapping, to be confirmed:

| START/JumpSTART | `Criticality` | Note |
|---|---|---|
| GREEN | 1–2 | MINOR / LOW |
| YELLOW | 3 | MODERATE |
| RED | 4–5 | SEVERE / CRITICAL — 5 when arrest markers present |
| BLACK | — | **No equivalent.** The 1–5 scale cannot express it |

> **Open design question.** BLACK has no representation in `Criticality`. A single-casualty
> field agent arguably never assigns it, but the scale being unable to *express* it is the
> kind of gap §1.1 of `CLAUDE.md` says to close in the type system rather than by convention.

---

## 5. Secondary survey — SAMPLE and OPQRST

After the primary survey, once life threats are managed.

### 5.1 SAMPLE — history

| | |
|---|---|
| **S** | Signs and symptoms |
| **A** | Allergies |
| **M** | Medications |
| **P** | Past pertinent medical history |
| **L** | Last oral intake (solid and liquid) |
| **E** | Events leading to the incident |

### 5.2 OPQRST — pain assessment

| | |
|---|---|
| **O** | Onset — when it began, and how it progressed |
| **P** | Provocation / palliation — what worsens or relieves it |
| **Q** | Quality — sharp, dull, crushing, burning |
| **R** | Region / radiation — where, and where it spreads |
| **S** | Severity — scale |
| **T** | Time — duration and pattern |

Conventional order: primary survey → OPQRST (if pain is the chief complaint) → SAMPLE.

These map directly onto `FactKind.SYMPTOM` and the structured questions the agent should ask
once the patient is stable — and give the SOAP note's **S**ubjective section a defined shape
instead of free text.

---

## 5.3 FAST — stroke recognition

The one condition-specific scale that belongs in a layperson-facing field agent, because it
is already taught to the public and is time-critical.

| | | Positive finding |
|---|---|---|
| **F** | Face | Facial droop — ask them to smile; one side does not move |
| **A** | Arms | Arm drift — both arms raised, one drifts down |
| **S** | Speech | Slurred, wrong words, or unable to speak |
| **T** | Time | **Note the time symptoms started** and call emergency services |

**T is the part the agent must not lose.** Thrombolysis eligibility is determined by time of
onset, so "when were they last seen well?" is a question the agent should ask and record as a
timestamped fact. This is a concrete, high-value job for `FactKind.SYMPTOM` that no retrieval
step provides.

The clinical scale underneath (Cincinnati Prehospital Stroke Scale) is FAST scored 0–3.

### 5.4 Deliberately out of scope

These are real, published, and **wrong for this product**. Recorded so the exclusion is a
decision rather than an oversight:

| Tool | Purpose | Why excluded |
|---|---|---|
| FAST-ED, RACE, LAMS, CSTAT, VAN | Large-vessel-occlusion detection for **hospital destination routing** | Requires trained neuro exam; a responder cannot score it, and the agent does not route ambulances |
| Shock index (HR/SBP) | Occult haemorrhage detection | Needs a blood-pressure cuff |
| Sepsis screens (qSOFA etc.) | In-hospital deterioration | Not a field first-aid decision |
| GCS (full 3–15) | Neuro assessment | **ACVPU is the field equivalent** and is already in §1.6. GCS invites a precision the setting cannot support |

The dividing line: **if a bystander with no equipment cannot perform it, it does not belong
on the responder path.** It may still belong in the clinician bridge view.

---

## 6. What this changes in the build

1. **A deterministic assessment state machine**, not a retrieval guess. ABCDE ordering and
   the START/JumpSTART branches are code with tests, in the domain layer beside
   `triage.py` — pure, no I/O, no model.
2. **Numeric thresholds become named constants** with citations: RR 30 (adult) / 15–45
   (paediatric), cap refill 2 s, glucose 4.0 mmol/L, SpO₂ 94–98 % / 88–92 %.
3. **Retrieval is demoted from router to detail-fetcher.** The algorithm decides the step;
   Moss supplies how to perform it. This is the real fix for 64 % top-1 — not a better
   embedder.
4. **The 9 worked scenarios in the source training deck become test fixtures** (§7).
5. **`phrases.py` gains structure.** Its markers currently map to a level; under ABCDE they
   map to *which letter is failing*, which is more useful and more auditable.

---

## 7. Ready-made test fixtures

The source training package includes worked cases with published answers — a school-bus MCI.
These become golden fixtures for the assessment state machine. Verbatim:

| # | Presentation | Expected |
|---|---|---|
| 1 | School-aged boy, RR **10**, good distal pulse, groans to painful stimuli | *(JumpSTART: RR < 15 → RED)* |
| 2 | Adult kneeling, too dizzy to walk, RR **20**, CR **2 s**, obeys commands | *(START: non-ambulatory, RR ≤ 30, CR ≤ 2 s, obeys → YELLOW)* |
| 3 | School-aged girl walks toward you crying, clothing torn, no obvious bleeding | *(ambulatory → GREEN)* |
| 4 | Toddler trapped, **apneic**, remains apneic with jaw thrust, **no pulse** | *(JumpSTART: apneic + no pulse → BLACK)* |
| 5 | Adult female driver trapped, RR **24**, cap refill **4 s**, moans to verbal | *(START: CR > 2 s → RED)* |
| 6 | Toddler, RR **50**, palpable distal pulse, withdraws from pain | *(JumpSTART: RR > 45 → RED)* |
| 7 | Woman carrying infant, walking, RR 20, CR 2 s, obeys commands | *(ambulatory → GREEN)* |
| 8 | Infant carried by #7, quiets to RR **34**, good pulse, focuses on rescuer, reaches for mother, no significant injury | *(non-ambulatory child, all YELLOW criteria, no significant external injury → GREEN)* |
| 9 | School-aged boy propped up, RR **28**, good distal pulse, answers questions, obvious deformity both lower legs | *(JumpSTART: RR 15–45, pulse, alert → YELLOW)* |

> Expected answers in parentheses are **derived from the algorithms above, not read from the
> source deck's answer slides.** They must be checked against the published answers before
> being used as test oracles. Cases 4, 6 and 8 are the discriminating ones — they exercise
> the rescue-breath branch, the paediatric RR ceiling, and the non-ambulatory-child
> modification respectively.

---

## Sources

- [The ABCDE Approach — Resuscitation Council UK](https://www.resus.org.uk/library/abcde-approach)
- [The START and JumpSTART MCI Triage Tools](https://www.nm.org/-/media/northwestern/resources/for-medical-professionals/ems-training/ems-education/start-jumpstart-mci-triage.pdf) — Lou Romig MD, 2006; Newport Beach Fire & Marine Dept.
- [JumpSTART Pediatric Triage Algorithm — HHS REMM](https://remm.hhs.gov/startpediatric.htm)
- [SAMPLE history](https://en.wikipedia.org/wiki/SAMPLE_history) · [OPQRST](https://en.wikipedia.org/wiki/OPQRST)
- jumpstarttriage.com — materials free to download; start-triage.com — materials for purchase
