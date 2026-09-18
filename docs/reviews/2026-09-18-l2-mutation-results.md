# L2 mutation results — every safety rule, reintroduced as a defect

**Date:** 18 Sept 2026
**Subject:** `agent/aiscelapeus/assessment.py`, and the ASM-14 change in
`agent/aiscelapeus/triage.py` / `agent/aiscelapeus/escalation.py`
**Method:** each blocker reintroduced as a minimal source edit, applied to the real
file, `pytest tests/unit/test_assessment.py` run, edit reverted. A rule whose test does
not fail when the rule is removed is not proven.

Baseline for every run: **48 passed** in `tests/unit/test_assessment.py`; full suite
**2754 passed, 14 skipped**; `mypy aiscelapeus` clean.

## Results — 14 mutants, 0 survivors

| Mutant | Reintroduced defect | Caught by |
|---|---|---|
| M1 | **Blocker 1 / ASM-11** — an expectant leaf: apneic + pulseless returns `INSTRUCT_MONITOR` at SEVERE | `test_apneic_and_pulseless_is_always_critical_plus_cpr` |
| M2 | **Blocker 2 / ASM-12** — the 5 rescue breaths gated on `pulse is REPORTED_PRESENT` | `test_the_pulse_report_cannot_change_any_branch` |
| M3 | **Blocker 3 / ASM-13** — `routes_to_cpr` narrowed to `NONE`, so agonal gasps stop being an arrest | `test_apneic_and_pulseless_is_always_critical_plus_cpr` |
| M4 | **Blocker 4 / ASM-04** — blanket worse-branch: unestablished breathing drives a CPR *instruction* | `test_an_unresolved_earlier_letter_always_wins_over_a_later_one` |
| M5 | **ASM-08** — ambiguity re-inverted to JumpSTART, the algorithm with the BLACK leaf | `test_algorithm_ambiguity_resolves_to_the_adult_algorithm` |
| M6 | **Blocker 5 / ASM-15** — `capillary_refill` and `respiratory_rate_counted` removed from the exclusion list | `test_the_excluded_list_covers_every_assessment_blocker_5_named` |
| M7 | **Blocker 5 / ASM-15** — a reachable step asks the bystander for capillary refill | `test_no_step_asks_the_responder_to_check_a_pulse` |
| M8 | **ASM-03** — an assessment step advises a letter later than the open one | `test_no_reachable_branch_advises_a_later_letter_while_an_earlier_is_open` |
| M9 | **ASM-06** — the witness check disabled, so L3 can construct its own branch | `test_a_branch_cannot_be_constructed_outside_the_module` |
| M10 | **ASM-14** — the original defect: the ratchet stops reading certainty and latches assumptions permanently | `test_an_assumed_level_is_correctable_by_later_evidence` |
| M11 | **ASM-14 inverted** — blanket "evidence may lower", so an evidenced CRITICAL can be stood down | `test_reassessment_never_lowers_an_evidenced_level` |
| M12 | **ASM-05** — L1 escalates as ASSUMPTION, so L2 arithmetic can stand down a reported life threat | `test_l1_outranks_l2_even_when_l2_computes_a_lower_level` |
| M13 | **ASM-14** — a later assumption softens an evidenced level back to correctable | `test_an_assumption_cannot_soften_an_evidenced_level_back_to_correctable` |
| M14 | **Illegal state** — an `UNKNOWN` finding may carry a value | `test_an_unknown_breathing_finding_cannot_carry_a_value` |

## M12 survived the first run, and that is the useful finding

On the first sweep **M12 survived**: flipping `escalation.py` to escalate as an
`ASSUMPTION` — which lets L2's arithmetic stand down a `phrases.py` marker-driven
CRITICAL, inverting the layer ordering the whole architecture rests on — broke nothing.

The cause was the test, not the code. `test_l1_outranks_l2_even_when_l2_computes_a_lower_level`
hand-rolled L1's `set_level` call with `provenance=Provenance.EVIDENCE` written into the
test body, so it asserted the *rule* while never exercising the code path that has to obey
it. The test and the mutant were independent.

Fixed by driving the real `apply_hard_escalation` and asserting on
`state.level_provenance`, after which M12 is caught. This is exactly the class of defect
the repo already has twice on record — a mechanism whose only implementation was its own
description — and it was invisible to a green suite.

## Second sweep — the five clinical blockers, 18 mutants, 0 survivors

Run after `docs/reviews/2026-09-18-l2-clinical.md`'s five blockers were closed. Same
method, same harness shape. Baseline for every run: **74 passed** in
`tests/unit/test_assessment.py`; full suite **2895 passed, 14 skipped**; `mypy` clean.

| Mutant | Reintroduced defect | Caught by |
|---|---|---|
| M15 | **Blocker 1** — the bleed hoist deleted, C back behind the A/B gates | `test_no_reachable_branch_defers_an_established_catastrophic_bleed` |
| M16 | **Blocker 1 narrowed** — hoist fires only once breathing is known, so the femoral-bleed-plus-unknown-breathing case defers again | same, plus `test_an_established_bleed_precedes_the_responsiveness_and_breathing_gates` |
| M17 | **Blocker 2** — the arrest path stops reading `severe_bleeding`, so a bleeding arrest gets bare CPR | `test_a_bleeding_arrest_is_told_to_control_the_haemorrhage_and_compress` |
| M18 | **Blocker 2 half-done** — the combined leaf drops the compressions | `test_no_reachable_branch_withholds_resuscitation_from_an_arrest` |
| M19 | **Blocker 3(a)** — the committed state blocks too, so the gate is absorbing again | `test_a_committed_responder_advances_past_a_live_hazard` |
| M20 | **Blocker 3(b)** — the scene gate pins SEVERE, lowering a known arrest | `test_the_scene_gate_never_lowers_the_criticality_of_a_known_arrest` |
| M21 | **Blocker 3(c)** — the scene question's phrasing is unpinned | `test_the_scene_question_pins_a_closed_hazard_list_phrasing` |
| M22 | **Blocker 4** — the spinal branch collapses; motorcyclist and fainter get one roll | `test_the_unconscious_motorcyclist_and_the_unconscious_fainter_differ` |
| M23 | **Blocker 4 half-done** — a roll ordered with no mechanism established | `test_the_recovery_position_is_never_ordered_without_a_mechanism` |
| M24 | **Blocker 5** — the choking instruction unreachable, so a conscious choker routes to CPR | `test_a_conscious_choker_gets_thrusts_and_not_compressions` |
| M25 | **Blocker 5 over-reach** — thrusts coached for an unresponsive patient | `test_an_unwitnessed_collapse_is_not_treated_as_choking`, and 20 others |
| M26 | **Dead member** — a declared step nothing returns | `test_no_assessment_step_is_declared_and_never_returned` |
| M27 | **Unread input** — `airway_opened` restored, read by nothing | `test_no_input_field_is_declared_and_never_read` |
| M28 | **ASM-13 unscoped** — `routes_to_cpr` ignores responsiveness, so an awake patient is compressed | `test_no_reachable_branch_compresses_the_chest_of_an_awake_patient` |
| M29 | The conscious-distress leaf deleted, so an awake bad-breather falls through to the arrest path | same |
| M30 | The terminal branch drops the hazard suffix, so a responder in traffic is never reminded after a full survey | `test_every_instruction_past_a_live_hazard_restates_it` |
| M31 | **Blocker 3(b) in the other gate** — the bleed gate pins SEVERE, lowering a possible arrest behind it | `test_no_reachable_branch_withholds_resuscitation_from_an_arrest` |
| M32 | Expensive work in `_branch`, to prove the ASM-09 walk budget is crossable rather than decorative | `test_the_exhaustive_walk_itself_stays_inside_the_suite_budget` |

### Two findings from this sweep

**M25 is caught by 21 tests, and that breadth is the finding.** Scoping the choking
question to responsive patients is not a local feature of the choking path — it is
load-bearing for the arrest path, because an unresponsive patient who is asked to
reconstruct a choking history is an unresponsive patient not receiving compressions.

**M31 was written because M20 existed.** Blocker 3(b) — a gate lowering the criticality of
an arrest behind it — was reintroduced verbatim by the Blocker 1 bleed hoist the moment it
was added, and was caught by the arrest walk rather than by inspection. The two gates now
share `_gate_floor`, so the next pre-A gate cannot reintroduce it by copying the wrong
line. A defect fixed in one place and recreated in the next is the DRY argument stated as
an incident.

## What is NOT proven here

- **ASM-06 is a detection property, not a prevention property.** M9 proves the witness
  blocks ordinary construction. It does not, and cannot, block `object.__new__` plus
  `object.__setattr__`, `dataclasses.replace`, `deepcopy` or `pickle`; an independent
  review demonstrated all four. The row is carried by `is_authentic`, and
  `test_every_forgery_that_construction_cannot_block_is_detected` pins each of those four
  forgeries as *detected*. No claim of unforgeability is made, because Python affords none.
- **Level and provenance are not restart-safe.** `main.py` builds a fresh `TriageState`
  per session and only `escalation` is rehydrated, so a restart mid-incident resets an
  EVIDENCE-established level. Pre-existing, not introduced by L2, and not closed by it.
- The thresholds themselves still need clinician review before real use, per
  `CLINICAL-STANDARDS.md`'s standing safety note.

## Reproducing

The mutants are minimal string substitutions; the harness applies each to the real file,
runs the L2 suite, and reverts in a `finally` block. It is not committed to the repo —
a script that edits source files in place does not belong in a suite that runs on every
change — so the table above is the record.

---

# Round 4 - the third clinical review's two blockers

**Date:** 18 Sept 2026
**Subject:** `agent/aiscelapeus/assessment.py` - Blocker 1 (the bleed hoist outranking the
airway of a patient who has no airway) and Blocker 2 (a guard test structurally incapable
of finding the provenance defect it was commissioned for).
**Method:** as above - each fix's rule reintroduced as a minimal edit to the real file,
`pytest tests/unit/test_assessment.py` run, edit reverted.

Baseline for every run: **88 passed** in `tests/unit/test_assessment.py`; full suite
**3032 passed, 14 skipped**; `mypy aiscelapeus` clean, 21 source files.

## Results - 17 mutants, 0 surviving at the end of the round

| Mutant | Reintroduced defect | Caught by |
|---|---|---|
| N1 | **Blocker 1, verbatim** - the bleed hoist stops standing aside, so the awake apnoeic choker with a bleed gets the bleed instruction that never mentions the airway | `test_no_reachable_branch_defers_an_established_catastrophic_bleed`, `test_an_awake_choker_who_is_moving_no_air_gets_the_bleed_and_the_airway`, `test_a_known_obstruction_is_never_discarded_by_the_bleed_hoist` |
| N2 | **The independent reviewer's finding** - the exemption narrowed back to established-NONE only, so a *reported* witnessed obstruction is discarded forever | `test_a_known_obstruction_is_never_discarded_by_the_bleed_hoist` |
| N3 | The exemption stops requiring an ESTABLISHED obstruction, so an unasked airway skips the hoist on every breathing value | `test_an_established_bleed_precedes_the_responsiveness_and_breathing_gates`, `test_a_catastrophic_bleed_outranks_a_still_responsive_choker` |
| N4 | The exemption treats an UNESTABLISHED breathing value as apnoea, skipping the hoist on no evidence | `test_an_established_bleed_precedes_the_responsiveness_and_breathing_gates` + 2 more |
| N5 | The combined leaf drops the BLEED: the awake choker is routed to plain thrusts | `test_no_reachable_branch_defers_an_established_catastrophic_bleed` + 2 more |
| N6 | The combined leaf drops the hands-free method, so the single-rescuer choice becomes forced again | `test_an_awake_choker_who_is_moving_no_air_gets_the_bleed_and_the_airway` |
| N7 | The combined leaf pins SEVERE, capping a reported arrest - Blocker 3(b) | `test_an_awake_choker_who_is_moving_no_air_gets_the_bleed_and_the_airway` |
| N8 | The choking QUESTION stops instructing the pressure, deferring the bleed behind a question | `test_no_reachable_branch_defers_an_established_catastrophic_bleed` + 1 |
| N9 | **Blocker 2's reported defect, verbatim** - `ASK_RESPONSIVENESS` hand-writes `assumed=inputs.assumed("responsiveness")` again, omitting the `scene_safe` it read | `test_every_leaf_records_the_assumptions_on_its_path`, `test_no_instruction_leaf_omits_the_assumed_argument` |
| N10 | The bleed hoist reports no assumptions at all (Blocker 2's second reported leaf) | `test_no_instruction_leaf_omits_the_assumed_argument` |
| N11 | `_Consulted.assumed` reports EVERY input rather than the consulted ones - over-recording, which destroys the distinction ASM-14 rests on | `test_a_branch_resting_on_any_assumption_is_correctable`, `test_every_leaf_records_the_assumptions_on_its_path` |
| N12 | `_Consulted` stops recording reads, so every branch claims EVIDENCE | `test_a_branch_resting_on_any_assumption_is_correctable` + 2 more |
| N13 | The accumulator becomes module-level state, so reads leak across calls (purity broken) | `test_a_branch_resting_on_any_assumption_is_correctable` |
| N14 | The combined leaf loses the loss-of-consciousness transition to compressions | `test_an_awake_choker_who_is_moving_no_air_gets_the_bleed_and_the_airway` |
| N15 | The combined leaf reports `unresolved=A`, so the outstanding bleed stops being flagged | `test_an_awake_choker_who_is_moving_no_air_gets_the_bleed_and_the_airway` |
| N16 | `_consulted_by` claims nothing is on any path, so the inverted guard asserts about nothing | `test_no_input_field_is_declared_and_never_read`, `test_every_leaf_records_the_assumptions_on_its_path` |
| N17 | The hoist's rationale stops soliciting the two reports that are the only escape from it | `test_an_established_bleed_precedes_the_responsiveness_and_breathing_gates` |

## Two mutants survived their first run, and both were the useful findings

**N15 survived**, and it was a real gap rather than an equivalent mutant. Flipping the
combined leaf's `unresolved_letter` from C to A - so the haemorrhage that is treated but
not resolved stops being the open letter, and no later turn is obliged to come back to it -
broke nothing. Nothing in the suite asserted that leaf's `unresolved_letter` at all. Closed
by adding the assertion to the named test; N15 is caught now. Same shape as round 3's M12:
the rule was real and the test for it did not exist.

**N3 survived, and is an EQUIVALENT MUTANT, which is a different finding.** Widening
reading (1) of `_airway_outranks_the_bleed_hoist` from `WITNESSED_FOREIGN_BODY` to any
established obstruction value changed no behaviour - because `_reaches_choking_block`
already answers False for an established `NONE`, so the member test was
unreachable-by-value and therefore dead. That is the same discovery the choking block
itself already records one block up, for the same reason. The member test was REMOVED
rather than kept: a condition that reads like a live rule and carries no weight is what a
future author builds on. The coupling it now relies on - that the only established
obstruction surviving `_reaches_choking_block` is `WITNESSED_FOREIGN_BODY`, the enum
having exactly two members - is pinned by the reported-absence loop in
`test_a_known_obstruction_is_never_discarded_by_the_bleed_hoist`, so if that filtering ever
changes, the simplification fails a named test rather than silently sending a patient with
no obstruction into the choking block. N3 as re-stated against the simplified form is
caught.

## Blocker 2 needed a PAIRED experiment, because a test cannot be proven by mutating itself

Restoring the guard test's `continue` in place of the assertion does **not** fail the
suite, and it never can: weakening a test is invisible to the suite that contains it. The
experiment that actually proves the inversion was worth making is the pair -

- **weak guard (`continue` restored) + real module defect (N9):** the guard is silent. The
  defect is caught only by `test_no_instruction_leaf_omits_the_assumed_argument`, the
  structural AST row, and *only because* that row was strengthened in the same change to
  require `assumed=path.assumed`. Against the original AST row, which checked merely that
  `assumed=` was present, the pair would have passed clean - which is how the reported
  defect survived a clinical review in the first place.
- **inverted guard + the same defect:** caught, by name, with the message
  `AssessmentStep.ASK_RESPONSIVENESS read scene_safe on its path but did not record it as
  assumed`.

That message is the whole point of the change, and it is the one the third clinical review
predicted the inversion would produce.

## What the inversion exposed, and what it did not

Inverting the `continue` into an assertion reported **16 of the 18 reachable steps**
under-recording at least one input they had read - not the two leaves the review reported.
Only `INSTRUCT_AIRWAY_WITH_SPINAL_CARE` and `INSTRUCT_RECOVERY_POSITION` were already
correct.

The **naive** inversion also over-fires, and that part is not a defect in the code: it
demands `ASK_SCENE_SAFE` record `age_band`, which that path never reads. Recording an input
off the path would make every branch an assumption and destroy the distinction ASM-14 rests
on. So the guard is scoped to the inputs the path actually consulted, asked of the module's
own accumulator rather than of a table in the test file - because a second model of the
path in the test is the defect Blocker 2 *was*.

## What is NOT proven here

- **The hoist's deferral of an unasked airway is not repaired by routing.** There is no
  "bleeding controlled" input, so `ALERT + bleeding PRESENT + obstruction UNKNOWN +
  breathing NORMAL` returns the same bleed instruction indefinitely, and L2 never asks the
  airway question while the hoist holds. The escape is a CALLER REPORT, and both reports
  that would produce one are solicited in the hoist's own rationale - pinned by N17. That
  is weaker than a routing guarantee, it is recorded as such in
  `_airway_outranks_the_bleed_hoist`, and the first draft of that docstring overstated it.
- **The three recorded gaps added this round are recorded, not closed** - the re-weighted
  live-electricity hazard, `_contradiction_clause`'s wording, and the deterioration rule's
  reversal for pulmonary oedema. `test_the_recorded_gaps_stay_recorded` asserts each stays
  written down; none of them is fixed, and the pulmonary-oedema one is actively harmful on
  the leaf it reaches.
- Blockers 3, 4 and 5 of the third clinical review are **out of scope** for this round and
  are not addressed by any mutant here.
