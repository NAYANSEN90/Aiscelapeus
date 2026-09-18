"""L2 - the assessment state machine. Pure domain logic beside `triage.py`.

Given a set of established inputs this returns a branch, deterministically: no
network, no model, no clock beyond the injected one. It owns the ABCDE ordering
constraint, the treat-before-moving-on rule, the numeric thresholds as named
constants with citations, and the certainty handling.

It does NOT own phrasing. `question_key` names what must be established; L3
turns that into words a panicking bystander can answer, and `protocols.py`
supplies how to perform an action. Restating either here would put a second
answer to one question in a second place.

WHAT THIS MODULE IS NOT ALLOWED TO BE
=====================================
`docs/reviews/2026-09-17-clinical-safety.md` scored the design this replaces
PROD 1/5 and found two paths that end in a death. Five blockers came out of it,
each one now an ASM row, and each one is a constraint on this file rather than a
suggestion. They are restated where they bite, because a constraint recorded
only in a review is a constraint the next author does not see.

1. BLACK/expectant does not exist on this path. `Criticality` cannot express it
   and that inability is the CORRECT property of the type - see `triage.py` and
   `ARCHITECTURE-ASSESSMENT.md` §11.1. Apneic plus pulseless is cardiac arrest
   in a single-patient incident: Criticality 5 and CPR, always. MCI is out of
   scope, so there is no rationing decision to encode. The START/JumpSTART
   thresholds are adopted below as *recognition cues*; their *categories* are
   not, because a category like BLACK encodes a triage decision rather than a
   clinical finding.
2. The lay pulse check is never a branch point. Lay pulse detection is roughly
   coin-flip accurate, which is why ILCOR/AHA/ERC removed it from lay BLS. See
   `PulseReport` below: the type exists so the finding can be RECORDED, and
   `decide` never reads it to choose a branch.
3. Breathing is a three-state input, not a bool. See `Breathing`.
4. Worse-branch is scoped, not blanket (`ARCHITECTURE-ASSESSMENT.md` §5.1 as
   amended). Worse branch for category and escalation; for an INSTRUCTION, the
   intervention safe under BOTH hypotheses. For algorithm ambiguity, START.
5. No assessment a bystander cannot perform. The excluded list is
   `UNPERFORMABLE_ASSESSMENTS`, and it is enforced by a test walking every
   reachable branch rather than by this docstring.

WHAT THE SECOND CLINICAL REVIEW FOUND
=====================================
`docs/reviews/2026-09-18-l2-clinical.md` reviewed the first implementation of
this module and scored it DEMO 4/5, PROD 2/5, on three reachable paths where a
bystander WITH THE CORRECT INFORMATION was given the wrong next action. Those
are not gaps where the machine lacked data - they are branches taken on
complete inputs, which is the worse kind. Each fix is commented where it bites:

1. Catastrophic haemorrhage was DEFERRED BEHIND QUESTIONS. The C block sat
   after the responsiveness and breathing gates, so a bystander kneeling on a
   femoral bleed was asked to assess breathing. An ESTABLISHED bleed is now a
   pre-A gate, in the same shape as the scene gate (C-ABC / MARCH). The
   QUESTION stays in the C block; only the established finding is hoisted.
2. A BLEEDING ARREST was told to compress and never told to stop the blood.
   `_arrest_branch` now has a bleeding-aware leaf emitting a COMBINED
   instruction.
3. The SCENE GATE WAS AN ABSORBING STATE. `SceneSafety` is now three states so
   a committed responder advances; the gate no longer lowers a known arrest's
   criticality (`_gate_floor`); and the question's phrasing is pinned.
4. RECOVERY POSITION fired with no mechanism-of-injury input. `SpinalRisk` is
   held and the technique named is now distinguishable.
5. TWO DEAD STEPS, AND CHOKING HAD NO REPRESENTATION - see below.

THE DEAD-STEP DECISION, AND WHY THE THREE PARTS DIFFER
======================================================
`ASK_AGE_BAND` and `INSTRUCT_OPEN_AIRWAY` were declared and never returned, and
`airway_opened` was an input nothing read. That is safe today and misleading
tomorrow: a future author reads a member plus a matching field as a designed
slot. All three are resolved, and not all three the same way, because they are
not the same defect.

- `ASK_AGE_BAND` is DELETED. Age is never worth a turn: an unestablished age
  resolves to the ADULT algorithm exactly as an explicit UNCERTAIN does
  (ASM-08), so the question could not change a branch, and a question that
  cannot change a branch spends the golden window for nothing. The only thing
  age decides is compressions-first versus breaths-first, and a bystander
  volunteers "it's a little boy" without being asked. `age_band` stays as an
  INPUT because L3 does supply it from volunteered speech.
- `INSTRUCT_OPEN_AIRWAY` and `airway_opened` are DELETED rather than wired.
  Reintroducing them would recreate the defect the first clinical review killed
  in its Blocker 2: a manoeuvre gate in front of resuscitation, with an
  outcome a lay rescuer cannot assess ("is the airway open now?"). Head tilt
  belongs inside the CPR and recovery-position instructions, where
  `protocols.py` already carries it, not as a step with a state a bystander has
  to report back.
- CHOKING is IMPLEMENTED, not recorded as a gap. This is the substantive half
  of the decision. A witnessed choking collapse arrived as
  ABNORMAL_OR_GASPING or NONE and routed straight to INSTRUCT_CPR;
  compressions will not shift a lodged bolus. It is common, fast and
  REVERSIBLE, the corpus already owns the wording
  (`protocols.py:airway-choking-adult`), and the thing that was missing is a
  TRANSITION on consciousness - which is a state change on a named input, so it
  is L2's job by construction rather than something retrieval can be asked to
  cover. See `AirwayObstruction`.

WHAT THE CLINICAL RE-REVIEW FOUND
=================================
A second `readiness-field` clinical pass re-scored the module DEMO 4/5, PROD
2/5 - unchanged, for different reasons. It confirmed four of the five original
blockers convincingly closed and singled out
`INSTRUCT_CONTROL_BLEEDING_THEN_CPR` as the best clinical work in the module,
genuinely fixed. It then found four new defects, each reproduced by execution
and each closed where it bites:

1. THE RESPONSIVENESS GATE WAS APPLIED TOO WIDELY. `routes_to_cpr` gated BOTH
   non-normal breathing states on responsiveness, so a patient reported as not
   breathing at all but still called "alert" was told to sit upright and use a
   reliever inhaler at Criticality 4. Only ABNORMAL_OR_GASPING takes the
   conjunction now; NONE routes to the arrest path whatever responsiveness was
   reported, and the contradiction is re-tested inside the instruction rather
   than resolved in favour of the reassuring half. See `Breathing.routes_to_cpr`
   for the decision and for why re-asking was rejected.
2. THE DISTRESS LEAF BROKE THE PROVENANCE CONTRACT. It was the only instruction
   leaf recording no assumptions at all, so its Criticality 4 was uncorrectable
   by later evidence - ASM-14 inverted on the leaf whose patient is most likely
   to change state. Fixed there, and a walk-level test now covers every leaf.
3. THE ARREST PATH DISCARDED A WITNESSED FOREIGN BODY. `_arrest_branch` never
   read `airway_obstruction`, so a bystander who watched food go in and the
   patient collapse was never told to look in the mouth. Blocker 2's shape
   exactly, fixed for bleeding and not for choking. See `_mouth_check_clause`.
4. THE ANAPHYLAXIS POSITIONING CONTRADICTED THE CORPUS. An unconditional,
   capitalised "do NOT lay them flat" outranked
   `protocols.py:anaphylaxis`'s flat-with-legs-raised rule, which exists
   because these patients arrest when they sit or stand up. Now conditional,
   with the per-condition detail deferred to the protocol; the decision is
   recorded at the leaf.

RECORDED GAPS - NOT CLOSED HERE
===============================
The clinical reviews' non-blocking findings, recorded so the next author does
not rediscover them as surprises. A test asserts each one stays recorded.

- THE HAZARD CLASS GAP IS CLOSED, and it is described here rather than deleted
  outright because the next author needs to know that two of the items that
  used to sit in this list are now mechanisms above, not gaps. BLOCKER 3 OF THE
  THIRD CLINICAL REVIEW. `SceneSafety` collapsed every hazard into one UNSAFE
  value while the pinned question already enumerated traffic, fire and
  electricity - so the discriminating information was collected and then
  discarded, the same arity defect the enum was widened to fix, one level down.
  It was LATENT while UNSAFE blocked patient contact outright and Blocker
  3(a)'s three-state fix - correct, and required by the absorbing-loop death -
  made it REACHABLE all the way to INSTRUCT_CPR. Closed by `HazardClass`, by
  `SceneSafety`'s per-class members, by `INSTRUCT_BREAK_ELECTRICAL_CONTACT`
  standing in front of every touch instruction, and by `_hazard_suffix` being
  per-class. The committed escape hatch is now scoped to ADJACENT by the
  ABSENCE of a member, which is what stops it being re-widened.

  AND THE FIRST FIX OF IT LEFT THE CONSUMING CLASS DEAD, WHICH AN INDEPENDENT
  CLINICAL REVIEW OF THIS VERY CHANGE FOUND. `_hazard_suffix` was made
  per-class and `INSTRUCT_MAKE_SCENE_SAFE` was left with one hazard-agnostic
  string - and that instruction is the ONLY step a consuming scene reaches, so
  every word of the fire guidance existed in the module and reached no caller
  while 96 tests passed. The string it did send offered the committed escape
  hatch, which `UNSAFE_CONSUMING` has no member to honour, so it invited an
  answer the machine could only meet by repeating itself: Blocker 3(a)'s
  absorbing loop rebuilt on fire with an invitation attached. Closed by
  `_move_to_safety_rationale`, which dispatches on the class exactly as
  `_hazard_suffix` does, and pinned by BRANCH-LEVEL rows rather than by a
  direct call to the suffix helper - the direct call is what let dead clinical
  content pass as tested.

  WHAT REMAINS OF IT, AND IT IS NOT NOTHING. One residue, narrower than the
  original:
  - FIRE AND GAS GET THE RIGHT WORDS AND NOT YET THE RIGHT MECHANISM.
    `HazardClass.CONSUMING` is expressible, and the caller is now told the
    hazard is a DOSE, that no position beside it stays survivable, and that the
    priority is getting out to clear air - but dose-over-time really wants a
    CLOCK and an extraction decision, and L2 has no clock beyond the injected
    one and no expression for "you have been in there too long". A consuming
    scene therefore blocks patient contact like any other approach hazard,
    which is defensible and is not the same as modelling the exposure.

    THE ESCAPE HATCH IS REFUSED FOR IT DELIBERATELY, and that decision is
    recorded rather than left looking like an oversight, because it is the one
    place Blocker 3(a)'s absorbing-loop objection has real force and is being
    overruled on clinical grounds. There is no committed variant of
    `UNSAFE_CONSUMING`: a responder who will not leave a smoke-filled room is
    not accepting a survivable risk, they are taking a dose alongside the
    patient, and the instruction now says so and tells them to get clear and
    let the fire service reach a patient who cannot be moved. The reviewer
    accepted the refusal and blocked only on the WORDS, which is what changed.
- THE COMMITTED-UNSAFE STATE WAS UNREACHABLE BY THE PINNED PHRASING, AND THAT
  HALF IS NOW FIXED as a side effect of Blocker 3's question rewrite: the
  ASK_SCENE_SAFE rationale now offers "already beside them and not leaving" as
  an answer in its own right, so the escape hatch is no longer described only
  behind the wall it opens. It also now asks the one follow-up that decides the
  hazard CLASS - whether the patient is still touching the conductor - because
  a class the question cannot elicit is a class the type holds and never sees.
  What is NOT fixed is that L2 pins the WORDS and cannot verify L3 asked them;
  that is the general limit on every pinned phrasing here, not specific to this
  one.
- `_hazard_suffix` IS NO LONGER A CONSTANT STRING, AND THE TUNE-OUT PROBLEM IS
  ONLY HALF CLOSED. It is now per-hazard-class, so the warning is about the
  hazard the responder actually reported rather than being the wrong warning for
  two of the three classes - that was the reachable-harm half and it is fixed.
  The REPETITION half stands: identical repetition is what gets tuned out, and
  within a class the sentence is still identical on turn two and on turn twenty,
  because L2 is pure and holds no turn history by construction (ASM-01) so there
  is no expression for "say it differently this time". Closing the rest needs a
  turn-aware layer above L2, which is a scoped change somewhere else. This is
  now an attention problem only, which is what it was always claimed to be.
- THERE IS NO "BLEEDING CONTROLLED" INPUT. An established PRESENT bleed returns
  INSTRUCT_CONTROL_BLEEDING forever, because nothing can ever report that the
  pressure worked. That is the same absorbing-loop shape the scene-gate fix
  killed, still live on the C letter.
- `_contradiction_clause`'s WORDING IS A DEFECT ALTHOUGH ITS ROUTING IS RIGHT.
  Found by the third clinical review. The decision to re-test the contradiction
  inside the instruction rather than spend a question turn on it is correct and
  is not what is being recorded here - the WORDS are. "NOTE THE CONTRADICTION
  AND SAY IT OUT LOUD", "those two things cannot both be true" and "tell them
  what they told you" instruct the operator to RELITIGATE THE CALLER'S REPORT
  mid-compression, with a bystander who is already at the limit of what they can
  do and who now has to defend what they said. A real dispatcher re-tests
  invisibly: they ask a question whose answer settles it without ever announcing
  that the caller was wrong, which is exactly what `ASK_AIRWAY_OBSTRUCTION`
  already does one block earlier with "can he answer you?" - and the module
  therefore already contains the pattern this clause should have used. Closing
  it is a rewrite of the clause's words to an invisible re-test in that shape,
  and it changes no routing, which is why it is recorded rather than bundled
  into a routing fix.
- LEG-RAISING ON DETERIORATION IS NOT CLEARLY RIGHT FOR A PENETRATING CHEST
  INJURY. Defect 4's fix gives one deterioration rule - if they go pale, grey,
  clammy or faint, lower them and raise the legs - because L2 cannot tell which
  of the four presentations it is looking at and that rule is what the corpus's
  anaphylaxis positioning actually protects against. An independent review
  flagged that the stabbed chest is the weakest of the four for it: some
  chest-trauma guidance prefers semi-recumbent or wounded-side-down, and
  raising the legs of a developing tension pneumothorax increases venous return
  into a thorax that cannot accommodate it. The rule is KEPT, because shock
  from a chest wound is far more often hypovolaemia than tension physiology and
  because "they went grey and we sat them up" is the failure this rule exists
  to prevent in all four - but it is the one part of Defect 4's fix that is a
  judgement rather than a derivation, and a clinician should settle it.
- AND THE DETERIORATION RULE IS WRONG FOR PULMONARY OEDEMA, which is a separate
  finding from the stab wound above and runs the OTHER way. The third clinical
  review found it. The gap above concedes the tension-pneumothorax objection for
  the penetrating chest injury and does not address pulmonary oedema at all, and
  the mechanism argument there does not merely weaken - it reverses. For the
  stabbed chest, the greyness and clamminess are usually hypovolaemia, which is
  why raising the legs is defensible. For cardiogenic pulmonary oedema THE
  GREYNESS AND CLAMMINESS ARE THE FAILURE ITSELF - they are the sympathetic
  response to a left ventricle that cannot clear its preload - so the trigger
  this rule fires on is not a sign of a patient who needs more venous return but
  the sign of one who is already drowning in it. Laying them flat and raising
  the legs increases venous return into that failing ventricle and WORSENS the
  oedema; these patients sit bolt upright for a physiological reason, and the
  rule as written takes them out of the position that is keeping them alive.
  This is the one of the four presentations for which the rule is not merely the
  weakest option but actively harmful, and closing it needs either an input that
  distinguishes cardiogenic failure or a deterioration rule that does not
  command a position. Recorded, not fixed: it is a clinical decision with an
  owner and it is not this change's scope.
- THE SPINAL LEAF'S TECHNIQUE IS CONTESTED, and this one is a genuine clinical
  DISAGREEMENT with the previous round's fix rather than a gap. That fix has
  `INSTRUCT_AIRWAY_WITH_SPINAL_CARE` coach a jaw thrust with manual in-line
  stabilisation. The re-review judges that wrong for an untrained operator on
  three grounds: it is a two-handed clinician technique whose untrained failure
  mode pushes the tongue INTO the airway; it commits both of a lone responder's
  hands permanently, so they cannot do anything else ever again; and modern lay
  guidance puts a suspected-spine unconscious patient in the recovery position
  with the head supported and the body turned as a unit. The counter-argument,
  which is the previous round's, is that a full roll by one untrained person is
  the improvisation to avoid and that both leaves protect the airway. RECORDED
  AS CONTESTED rather than silently resolved either way: changing it is a
  clinical decision with a named owner, not a refactor, and the next author
  must see that both positions have a clinician behind them.

- COMPENSATED SHOCK HAS NO DISCRIMINATOR. Excluding capillary refill was right
  - a fabricated timed measurement is worse than UNKNOWN - but it loses the
  pale, clammy, tachycardic patient who is still talking. They now exit at
  Criticality 2 with nothing to flag them. The replacement is NOT capillary
  refill; it is the layperson-observable cluster: cold sweaty skin, grey or
  ashen colour, "he's gone white", drowsiness, thirst. That is a new input and
  a new branch, and it is not built.
- THE RATCHET STOPS TRACKING A PATIENT WHO GENUINELY IMPROVES. Post-ictal,
  hypoglycaemic after sugar, a resolved faint: improvement is
  evidence-lowering-evidence and is refused by design (see `triage.py`). The
  review judged that the right default for a field agent, because the ratchet
  exists so the record survives a calm five minutes - but the criticality
  number stops tracking the patient after the first evidenced peak, and the
  clinician on the bridge has no channel to resolve it. That channel does not
  exist; building one is a scoped change to `triage.py`, not to this module.

WHY THE INPUTS ARE THE SHAPE THEY ARE
=====================================
Every input is a value plus how we came to hold it (`ARCHITECTURE-ASSESSMENT.md`
§6). That is not bookkeeping: a RED assigned because breathing was genuinely
absent and a RED assigned because breathing could not be established are
clinically different events which would otherwise look identical in the record,
and the clinician joining the bridge needs to know which one they are walking
into. `Certainty` carries it, and - closing ASM-14 - the ratchet in `triage.py`
now reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Mapping, TypeVar

from .triage import Criticality, Provenance

__all__ = [
    "ADULT_ALGORITHM_FLOOR_YEARS",
    "AgeBand",
    "AirwayObstruction",
    "AssessmentBranch",
    "AssessmentInputs",
    "AssessmentStep",
    "Breathing",
    "Certainty",
    "Consciousness",
    "Finding",
    "HazardClass",
    "LETTERS_IN_ORDER",
    "Letter",
    "RESCUE_BREATHS_BEFORE_COMPRESSIONS",
    "PulseReport",
    "Responsiveness",
    "SceneSafety",
    "SevereBleeding",
    "SpinalRisk",
    "UNPERFORMABLE_ASSESSMENTS",
    "decide",
    "is_authentic",
]


# --------------------------------------------------------------------- certainty


class Certainty(Enum):
    """How we came to hold an input's value.

    `ARCHITECTURE-ASSESSMENT.md` §6. Ordered weakest-to-strongest so
    `is_evidence` is one comparison rather than a set membership test that
    could drift out of step with the ordering.

    The distinction that matters for ASM-14 is the line between ASSUMED_WORST -
    nobody established this, we took the worse branch on purpose - and
    everything at or above INFERRED, where a human actually reported something.
    A level reached by assumption must be correctable by later evidence; a level
    reached by evidence must not be.
    """

    UNKNOWN = 0
    ASSUMED_WORST = 1
    INFERRED = 2
    ESTABLISHED = 3
    OBSERVED = 4

    @property
    def is_evidence(self) -> bool:
        """Whether a human reported something, as opposed to us assuming it.

        INFERRED counts. "He's panting" is a responder's report about the
        patient, translated by L3 but originating with someone who can see the
        chest. UNKNOWN and ASSUMED_WORST do not: nothing was reported.
        """
        return self.value >= Certainty.INFERRED.value

    @property
    def provenance(self) -> Provenance:
        """How the ratchet in `triage.py` should treat a level reached from this.

        The mapping lives here rather than in `triage.py` because `Certainty` is
        the richer type and this module owns it. `triage.py` only needs the
        two-way distinction, which is why `Provenance` has two members and not
        five: a third would invite a partial ordering nobody has defined.
        """
        return Provenance.EVIDENCE if self.is_evidence else Provenance.ASSUMPTION


#: Classic `TypeVar` rather than PEP 695 `class Finding[T]`: `pyproject.toml`
#: sets `requires-python = ">=3.11"` and mypy's `python_version = "3.11"`, and
#: the newer syntax is a hard parse error there. The generic is not decoration -
#: it is what makes `Finding[Breathing]` and `Finding[Responsiveness]` distinct
#: to the type checker, so a breathing value cannot be passed where a
#: consciousness value is required.
_T = TypeVar("_T")


@dataclass(frozen=True)
class Finding(Generic[_T]):
    """One input: a value, and how we came to hold it.

    Generic so `Finding[Breathing]` and `Finding[Consciousness]` are distinct to
    the type checker, which is what stops a breathing value being passed where a
    consciousness value is required.

    An UNKNOWN finding carries no value at all - `value is None` - rather than a
    sentinel member on every value enum. A `Breathing.UNKNOWN` member would be
    an illegal state expressible in the type: every `match` over breathing would
    then need a branch for it, and the one that forgot would route an
    unestablished patient somewhere silently.
    """

    value: _T | None
    certainty: Certainty = Certainty.UNKNOWN

    def __post_init__(self) -> None:
        if self.certainty is Certainty.UNKNOWN and self.value is not None:
            raise ValueError(
                "an UNKNOWN finding cannot carry a value; "
                f"got {self.value!r}. Report the certainty you actually have"
            )
        if self.certainty is not Certainty.UNKNOWN and self.value is None:
            raise ValueError(
                f"a {self.certainty.name} finding must carry a value; "
                "a valueless finding is UNKNOWN"
            )

    @property
    def is_established(self) -> bool:
        """Whether this input has a value we may branch on at all."""
        return self.value is not None

    @property
    def is_assumption(self) -> bool:
        """Whether any level reached from this is correctable by later evidence."""
        return not self.certainty.is_evidence

    @classmethod
    def unknown(cls) -> "Finding[_T]":
        return cls(value=None, certainty=Certainty.UNKNOWN)


def _unknown() -> Finding[_T]:
    """A default-factory for an UNKNOWN field of any `Finding` type.

    Every field on `AssessmentInputs` defaults to this rather than to a shared
    module-level constant. A frozen dataclass instance would be safe to share,
    but the shared-default habit is not, and the cost of a fresh object per
    field is nothing next to a future mutable field silently aliasing across
    every incident on the process.
    """
    return Finding(value=None, certainty=Certainty.UNKNOWN)


# ------------------------------------------------------------------ input values


class Breathing(Enum):
    """BLOCKER 3. Breathing is three states, and never a bool.

    Roughly 40% of witnessed arrests present with agonal gasps, and the
    untrained caller's answer to "is he breathing?" is *yes* - they see chest
    movement and hear noise. A binary input routes the most common arrest
    presentation to a respiratory assessment instead of to CPR, which the
    clinical review called the most probable way this specific product kills
    someone.

    So the type has no boolean reading. ABNORMAL_OR_GASPING is a first-class
    member sitting between NORMAL and NONE, and the CPR decision is a method on
    the value rather than a condition a caller writes - a caller writing
    `if breathing is Breathing.NONE` would reintroduce the binary underneath the
    three-state type, and there is now nowhere for that expression to be correct.

    `routes_to_cpr` takes responsiveness, because the guideline is a
    conjunction for ABNORMAL_OR_GASPING - see its docstring for why that
    signature is the safety mechanism rather than an inconvenience, and why
    NONE is deliberately NOT subject to the same conjunction.
    """

    NORMAL = "normal"
    ABNORMAL_OR_GASPING = "abnormal_or_gasping"
    NONE = "none"

    @property
    def is_normal(self) -> bool:
        """Whether this is normal, regular, effective breathing."""
        return self is Breathing.NORMAL

    def routes_to_cpr(self, responsiveness: Responsiveness) -> bool:
        """Whether this breathing state, in this patient, indicates CPR. ASM-13.

        A METHOD TAKING RESPONSIVENESS, not a bare property, and that signature
        is the safety mechanism. ASM-13 was written as "both non-normal states
        route to CPR" and implemented as a property of the breathing value
        alone - which is a conclusion imported without its precondition, the
        exact error `docs/reviews/2026-09-17-clinical-safety.md` closes with.

        The guideline is a CONJUNCTION, and this repo states it three times:
        "unresponsive AND not breathing normally -> compressions"
        (`CLINICAL-STANDARDS.md` §1.2, quoted at the A/B block below and in
        `PulseReport` above). ILCOR/AHA/ERC phrase it that way because agonal
        respiration is a brainstem reflex of a dead circulation - it does not
        coexist with A on ACVPU.

        Dropping the first half told an ALERT patient with abnormal breathing to
        receive chest compressions. That is not merely useless but harmful: the
        conscious asthmatic, the anaphylaxis, the pulmonary oedema and the
        partial obstruction are all common, all awake, and all made worse by
        being laid flat and compressed. An independent clinical review found it
        after the choking path was added, and it was reachable on COMPLETE
        inputs - the worst kind.

        Blocker 3 is NOT reintroduced by this scoping. Its target was the BINARY
        QUESTION, and its failure mode is a COLLAPSED patient whose caller says
        "yes he's breathing". An unconscious patient with either non-normal
        state still routes here with no gate whatsoever, which is what that row
        protects. Taking responsiveness as a parameter is what makes the
        precondition impossible to forget: there is no expression that reaches
        CPR without naming the patient's consciousness.

        WHY ONLY ABNORMAL_OR_GASPING TAKES THE CONJUNCTION
        =================================================
        A SECOND independent clinical review (`docs/reviews/`, 2026-09-18, the
        re-review) found this method's first correct-looking fix applied the
        responsiveness gate to BOTH non-normal states, and that the wider
        application was its lead finding. Reproduced: SAFE scene, ALERT,
        obstruction NONE, breathing NONE returned the conscious-distress leaf at
        Criticality 4 - a patient reported as NOT BREATHING AT ALL told to sit
        upright and use a reliever inhaler.

        The conjunction was imported faithfully and applied too widely. The
        failure it protects against - compressions on a talking asthmatic - only
        ever existed for ABNORMAL_OR_GASPING, which is the state a conscious
        patient in respiratory distress ACTUALLY presents with. Nobody who is
        awake presents with NONE: apnoea in a patient the caller still calls
        "alert" is not conscious distress, it is a stale or wrong responsiveness
        report, and that is the most likely error in the first sixty seconds of
        a panicking call.

        So NONE ignores the reported responsiveness entirely and routes to the
        arrest path. The alternative the reviewer preferred - returning to
        ASK_RESPONSIVENESS to surface the contradiction rather than resolve it -
        was considered and REJECTED, and the reasoning is recorded because the
        reviewer's instinct about the contradiction is right even though the
        mechanism is not:

        - IT IS AN ABSORBING LOOP, and L2 is pure. This module holds no turn
          history by construction (ASM-01), so there is no expression for "ask
          once, then proceed". A caller who described a posturing or agonal
          casualty as alert will re-report the same two values when asked again,
          and `decide` - being a function of its inputs alone - MUST return the
          same branch forever. That is Blocker 3(a)'s absorbing scene gate
          rebuilt on the one patient with the least time to spare, and the
          module already carries a RECORDED GAP for the same shape on bleeding.
        - THE CONTRADICTION IS RE-TESTED ANYWAY, without spending the turn.
          Re-testing and compressions are not alternatives, exactly as Blocker 2
          resolved bleeding-versus-compressions with "do both; do not choose":
          `_arrest_branch` carries the contradiction in its own rationale, so
          the caller is told what we were told and asked to look again WHILE
          compressions start. A question costs the golden window; a sentence
          inside the instruction costs nothing.
        - IT IS SAFE UNDER BOTH HYPOTHESES, which is what §5.1 actually
          requires of an instruction. If the apnoea is real, compressions are
          the only survivable action. If the responsiveness report was wrong -
          the likely case - compressions are still right. And the third case,
          a genuinely alert patient with genuinely absent breathing, is not a
          state that physically persists: no air movement ends alertness in
          seconds, so the contradiction resolves itself toward arrest.
        - IT MAKES BOTH SIDES OF THE MODULE AGREE. `_is_arrest` already encodes
          this asymmetry for the CATEGORY, excluding only established-and-awake;
          the instruction side did not, which is why the same patient came out
          at Criticality 4. One rule needs one shape.

        ABNORMAL_OR_GASPING keeps the full conjunction. That is where the awake
        asthmatic, anaphylaxis, pulmonary oedema and partial obstruction live,
        and the conscious-distress leaf still owns them.

        ONE CASE RECONCILES THE CONTRADICTION INSTEAD OF BEING ONE, and `decide`
        handles it upstream rather than this method: an ESTABLISHED WITNESSED
        FOREIGN BODY in a patient reported responsive. A complete obstruction is
        exactly the state where "awake" and "moving no air" are both accurate,
        and the correct action is still back blows and thrusts, because
        compressions cannot shift a lodged bolus (Blocker 5). So the choking
        block keeps that patient, and its instruction carries the transition to
        compressions if they lose consciousness. The contradiction re-test
        applies where there is no explanation, and stands aside where there is.
        """
        if self is Breathing.NONE:
            # No conjunction. See the docstring: an "alert" report alongside
            # absent breathing is a contradiction to re-test inside the arrest
            # instruction, never one to resolve in favour of the reassuring half.
            return True
        return not self.is_normal and responsiveness.is_unconscious


class Responsiveness(Enum):
    """ACVPU, the field consciousness scale (`CLINICAL-STANDARDS.md` §1.6).

    ACVPU rather than GCS deliberately: §5.4 excludes full GCS because it
    invites a precision the setting cannot support. A bystander can report
    "awake", "confused", "only when I shout", "only when I pinch", "nothing".
    """

    ALERT = "alert"
    CONFUSED = "confused"
    VOCAL = "vocal"
    PAIN = "pain"
    UNRESPONSIVE = "unresponsive"

    @property
    def is_unconscious(self) -> bool:
        """Whether the airway is unprotected and CPR is on the table.

        P and U both. A patient responding only to pain cannot protect their own
        airway, and `CLINICAL-STANDARDS.md` §1.6 puts "unprotected airway plus
        unconscious" into the recovery position for exactly that reason.
        """
        return self in (Responsiveness.PAIN, Responsiveness.UNRESPONSIVE)


#: Kept as an alias because "consciousness" is what the rest of the system calls
#: this, and renaming the concept at the module boundary would cost a reader a
#: lookup for nothing.
Consciousness = Responsiveness


class PulseReport(Enum):
    """BLOCKER 2. A pulse finding exists to be RECORDED, never to branch on.

    Lay pulse detection is roughly coin-flip accurate with long check times,
    which is why ILCOR/AHA/ERC removed it from lay BLS and replaced it with
    "unresponsive and not breathing normally -> compressions". The clinical
    review found both leaves of a pulse branch converging on "do not
    resuscitate", reached through a measurement the operator cannot make.

    This type therefore has no `routes_to_*` property and no ordering. It is
    carried so a responder's volunteered "I can't find a pulse" reaches the
    record and the clinician bridge - `phrases.py` already escalates that
    utterance, correctly, and L2 must not invert the meaning of the same words
    by processing them differently.

    `decide` never reads it. That is asserted by a test, not by this docstring.
    """

    REPORTED_PRESENT = "reported_present"
    REPORTED_ABSENT = "reported_absent"
    UNSURE = "unsure"


class SevereBleeding(Enum):
    """Catastrophic external haemorrhage - the one C finding a bystander can see.

    `CLINICAL-STANDARDS.md` §1.5's C assessments are capillary refill, pulse
    pressure and urine output, all of which Blocker 5 excludes. What remains is
    what a bystander can actually observe and act on with bare hands: blood
    pouring, pooling, spurting, or soaking through clothing. `protocols.py`
    carries the interventions (`bleed-tourniquet`, `bleed-pressure-packing`).
    """

    NONE = "none"
    PRESENT = "present"


class HazardClass(Enum):
    """WHAT KIND of hazard, because the three the question already asks are not
    alike in the one way that decides whether the patient may be TOUCHED.

    BLOCKER 3 OF THE THIRD CLINICAL REVIEW. The pinned `ASK_SCENE_SAFE` phrasing
    enumerates traffic, fire and electricity, so the discriminating information
    was already being collected - and then discarded into a two-value safe/unsafe
    verdict. That is the same ARITY defect `SceneSafety` was widened to fix, one
    level down, and the three-state fix is what made it lethal rather than
    latent: `UNSAFE_RESPONDER_COMMITTED` advances by design, all the way to
    INSTRUCT_CPR, so a bystander who said "there's a live cable, I'm already next
    to him" was told to start compressions.

    THE AXIS IS NOT SEVERITY. It is the hazard's RELATIONSHIP TO THE PATIENT,
    which is what decides whether patient contact is survivable for the
    responder. Ordering these by how dangerous they feel would be a different
    axis with no action attached to it, so there is deliberately no ordering
    here and no comparison operator.

    ADJACENT - a hazard you can work BESIDE. Traffic is the case: probabilistic
    and EXTERNAL to the patient. A committed rescuer genuinely can work inside
    it, which is why the escape hatch exists and why "keep watching, get clear
    if it closes in" is exactly the right warning for it.

    IN_PATIENT - a hazard that is IN THE PATIENT. Live electricity is the case.
    Touching the patient completes the circuit, so COMPRESSIONS ARE THE
    MECHANISM OF INJURY: "safe to approach" and "safe to touch" come apart here
    and nothing in a two-value verdict could express the difference. This is the
    one class for which the committed escape hatch is WRONG - see
    `SceneSafety.UNSAFE_IN_PATIENT` for why advancing anyway is not an option
    here when it is everywhere else.

    CONSUMING - a hazard that is CONSUMING THE SCENE. Fire and gas are
    dose-over-time rather than probabilistic: "committed" does not mean "working
    inside an acceptable risk", it means dead in minutes along with the patient.
    The action is extraction, not a standing warning.

    NO MEMBER MEANS "UNKNOWN", deliberately, and the reasoning is `Breathing`'s
    verbatim: a sentinel member is an illegal state expressible in the type, and
    every `match` over it would need a branch for the sentinel that the one
    forgetting it routes somewhere silently. An unestablished scene is
    `Finding.unknown()` on `scene_safe`, which is where "we have not asked yet"
    already lives - and a SAFE scene has no hazard class at all, which is
    expressed by `SceneSafety.SAFE.hazard` being None rather than by a NONE
    member here.
    """

    ADJACENT = "adjacent"
    IN_PATIENT = "in_patient"
    CONSUMING = "consuming"


class SceneSafety(Enum):
    """BLOCKER 3(a), then BLOCKER 3 OF THE THIRD CLINICAL REVIEW.

    The DR gate itself is correct and stays: the bystander is the operator, and
    a rescuer electrocuted across a downed cable produces two dead people rather
    than one. One turn is the right price for that.

    WHAT WAS WRONG THE FIRST TIME WAS THE ARITY. `Finding[bool]` had exactly two
    answers, and `False` looped forever: a caller already kneeling beside a
    patient in a lay-by who truthfully answers "no, cars are going past" was
    told to move to safety and never advanced. The gate then kills the patient
    it exists to protect, and it does so precisely when the bystander has
    ALREADY taken the risk and is not going to un-take it.

    So the committed state is not "unsafe, but carry on anyway" as a permission
    - it is the distinct, real situation a dispatcher meets constantly: the
    hazard persists AND the responder is already committed to the position. That
    advances, carrying a live warning rather than a blocking loop.

    AND WHAT WAS WRONG THE SECOND TIME WAS THE ARITY AGAIN, ONE LEVEL DOWN. One
    `UNSAFE` value covered every hazard class, and the classes are not alike in
    the way that matters: traffic is external and survivable to work beside,
    live electricity is IN THE PATIENT so compressions are the mechanism of
    injury, and fire is dose-over-time. See `HazardClass`. The fix pairs the
    verdict with the class in ONE value rather than adding a second field.

    WHY THE CLASS IS A REFINEMENT OF THE VERDICT AND NOT AN ORTHOGONAL INPUT,
    which is the shape decision and is load-bearing twice over:

    - IT KEEPS ILLEGAL STATES UNREPRESENTABLE. A separate
      `Finding[HazardClass]` field makes `SAFE` with a live cable expressible
      and meaningless, and `UNSAFE` with no class expressible and undecidable -
      which is the "bool plus a separate `committed` flag" shape this docstring
      already records as rejected, rebuilt on the hazard instead of on the
      commitment. The class exists only WHERE a hazard exists, so it belongs
      inside the value that says a hazard exists.
    - IT KEEPS THE ASM-09 WALK AFFORDABLE, and that is a measured constraint
      rather than a preference. The exhaustive walk is the product of the input
      domains: a new field multiplies it, an extra member on an existing field
      adds one slice. Measured on the development machine - a separate
      three-class field took 373k combinations to 1.12M and the walk from 4.9 s
      to ~14.7 s against ASM-09's 8.0 s bound, while these two extra members
      take it to 560k and ~7.4 s. A correctness fix that forces the safety walk
      to be sampled would trade this defect for a worse one.

    THE COMMITTED ESCAPE HATCH IS PER-CLASS, NOT GLOBAL, and that asymmetry is
    the clinical content of this change. It exists for ADJACENT and for nothing
    else. There is therefore no `UNSAFE_IN_PATIENT_COMMITTED` member and no
    `UNSAFE_CONSUMING_COMMITTED` member: "the responder is already beside them
    and staying" is a reason to ADVANCE past traffic and is not a reason to put
    hands on an energised patient, and an absent member is how that is made
    unrepresentable rather than merely unrecommended.
    """

    SAFE = "safe"
    #: Traffic and the like: external, probabilistic, survivable to work beside.
    #: Blocks the approach, because the responder is not yet committed.
    UNSAFE_ADJACENT = "unsafe_adjacent"
    #: The Blocker 3(a) escape hatch, now scoped to the ONE class it is right
    #: for: hazard present, responder already at the patient's side and staying.
    UNSAFE_ADJACENT_COMMITTED = "unsafe_adjacent_committed"
    #: THE HAZARD IS IN THE PATIENT - a live conductor still in contact. This
    #: state has NO committed variant, and that absence is the fix. Advancing
    #: would mean compressions on a patient in circuit, which electrocutes the
    #: responder: the DR gate's own founding case, reached THROUGH the gate.
    #: `INSTRUCT_BREAK_ELECTRICAL_CONTACT` is what this reaches instead.
    UNSAFE_IN_PATIENT = "unsafe_in_patient"
    #: Fire, smoke, gas: dose-over-time, so there is no dose at which standing
    #: beside it is a stable position. No committed variant either - "committed"
    #: here means dying alongside the patient rather than accepting a risk.
    UNSAFE_CONSUMING = "unsafe_consuming"

    @property
    def hazard(self) -> HazardClass | None:
        """Which class of hazard this scene holds, or None for a safe scene.

        Returning None for SAFE rather than adding a `HazardClass.NONE` member
        is the same decision `Finding` makes for an unestablished value, and for
        the same reason: a NONE member would be a branch every match has to
        remember, and the one that forgets routes a safe scene through hazard
        handling.
        """
        return _HAZARD_CLASS_OF[self]

    @property
    def blocks_patient_contact(self) -> bool:
        """Whether the assessment must stop here and move the responder first.

        A property of the value rather than a condition callers write, for the
        same reason as `Breathing.routes_to_cpr`: `if scene is SceneSafety.SAFE`
        written at a call site is the two-state reading restored underneath the
        richer type, and there is nowhere for that expression to be correct.

        TRUE FOR THREE OF THE FIVE MEMBERS NOW, and the two it is false for are
        the two where patient contact is actually survivable: a safe scene, and
        a responder already committed beside an ADJACENT hazard. In particular
        it is TRUE for `UNSAFE_IN_PATIENT` with no committed escape, which is
        Blocker 3 of the third clinical review: the responder may be standing
        right there and it is still not safe to TOUCH, because the touching is
        what injures them.
        """
        return self is not SceneSafety.SAFE and not self.responder_committed

    @property
    def responder_committed(self) -> bool:
        """Whether the responder is already at the patient's side and staying.

        One member, but read through a property so the escape hatch is a
        question asked of the value rather than a member name matched at a call
        site - which is what stops a future author extending the hatch to
        another class by widening an `in (...)` test somewhere downstream.
        """
        return self is SceneSafety.UNSAFE_ADJACENT_COMMITTED

    @property
    def hazard_persists(self) -> bool:
        """Whether a hazard is live and must be restated with every instruction.

        True for every non-safe state: advancing is not the same as the hazard
        being gone, and the branch that advances has to say so.
        """
        return self is not SceneSafety.SAFE

    @property
    def is_in_the_patient(self) -> bool:
        """Whether touching the patient is itself the mechanism of injury.

        The single question the rest of the module asks about a hazard class,
        named once here rather than compared against `HazardClass.IN_PATIENT` at
        each site. A call site writing that comparison is how the next hazard
        class with the same property - a patient in a confined space full of
        slurry gas, a patient under a live rail - gets classified by one
        function's memory of the enum instead of by the type.
        """
        return self.hazard is HazardClass.IN_PATIENT


#: Which hazard class each scene verdict carries. A mapping rather than a method
#: body full of comparisons, and exhaustive by construction: a member added to
#: `SceneSafety` without a class here raises a KeyError the first time `hazard`
#: is read on it, which the exhaustive walk reaches immediately. A `match` with a
#: fallback arm would silently classify a new member as safe instead.
_HAZARD_CLASS_OF: Mapping[SceneSafety, HazardClass | None] = {
    SceneSafety.SAFE: None,
    SceneSafety.UNSAFE_ADJACENT: HazardClass.ADJACENT,
    SceneSafety.UNSAFE_ADJACENT_COMMITTED: HazardClass.ADJACENT,
    SceneSafety.UNSAFE_IN_PATIENT: HazardClass.IN_PATIENT,
    SceneSafety.UNSAFE_CONSUMING: HazardClass.CONSUMING,
}


class SpinalRisk(Enum):
    """BLOCKER 4. Mechanism of injury, to the precision a bystander can supply.

    The unconscious motorcyclist and the unconscious fainter reached an
    identical unconditional "roll them into the recovery position". The spinal
    caveat's WORDING stays in `protocols.py:airway-recovery-position` - one home
    for one rule - but the caveat is a CONDITIONAL, and its condition was an
    input L2 did not hold. Whether it survived into speech therefore depended on
    retrieval landing the right document at 64% top-1, so one call in three
    rolled an unstable C-spine.

    Airway still beats spine, and nothing here changes that: both leaves protect
    the airway. What differs is WHICH TECHNIQUE IS NAMED - jaw thrust with
    manual in-line stabilisation versus a full roll - and only one of those is
    safe for an untrained person to improvise.

    SUSPECTED is the worse branch for an UNKNOWN mechanism only where the type
    says so at the branch; `decide` asks rather than assuming, because the
    question is one turn and answerable ("did they fall from a height, come off
    a bike, dive into water, or crash?").
    """

    NONE = "none"
    SUSPECTED = "suspected"


class AirwayObstruction(Enum):
    """BLOCKER 5. Choking, which had no representation at all.

    A witnessed choking collapse arrived as ABNORMAL_OR_GASPING or NONE and
    routed straight to INSTRUCT_CPR. Compressions will not shift a lodged bolus,
    and back blows plus abdominal thrusts in a still-conscious choker is the
    intervention with the survival curve. For an untrained bystander with one
    patient this is a common, fast, REVERSIBLE death, and the machine had one
    answer for it and it was the wrong one.

    WITNESSED_FOREIGN_BODY is deliberately narrow: it means somebody SAW the
    obstruction happen - eating, then clutching the throat, then unable to speak.
    An unwitnessed collapse is not a choking presentation and must not be
    treated as one, because back blows on a primary cardiac arrest cost the
    window that compressions needed. That is the discriminator the clinical
    review names in both directions.

    `protocols.py:airway-choking-adult` owns the wording, including the
    transition ("if they go unconscious, lower them down and start CPR") and its
    own eligibility scope ("this protocol applies only while they are still
    responsive"). L2 owns the TRANSITION, which is a state change and therefore
    exactly L2's job: consciousness is what decides whether thrusts or
    compressions are the next action, and that is not a phrasing question.
    """

    NONE = "none"
    WITNESSED_FOREIGN_BODY = "witnessed_foreign_body"


class AgeBand(Enum):
    """Which algorithm applies, to the precision a bystander can supply.

    `CLINICAL-STANDARDS.md` §3.1's operative rule is "if a victim appears to be
    a child use JumpSTART, if a young adult use START - do not try to establish
    exact age". UNCERTAIN is a real answer to that, not a missing one, which is
    why it is a member here rather than an UNKNOWN certainty.

    BLOCKER 4 / ASM-08 INVERTED. UNCERTAIN resolves to the ADULT algorithm, the
    opposite of the original design. JumpSTART's pulse branch has a BLACK leaf,
    so "ambiguous -> JumpSTART" routed an ambiguous adolescent toward expectant
    where START gives RED. The worse branch was worse in the wrong direction.
    """

    ADULT = "adult"
    CHILD = "child"
    UNCERTAIN = "uncertain"

    @property
    def resolved(self) -> "AgeBand":
        """The algorithm actually used. UNCERTAIN resolves to ADULT. ASM-08."""
        return AgeBand.ADULT if self is AgeBand.UNCERTAIN else self


# ------------------------------------------------------------------- thresholds


#: Lower bound, in years, at which the adult algorithm applies.
#: `CLINICAL-STANDARDS.md` §3.1: JumpSTART is designed for ages 1-8 because
#: paediatric airway physiology approaches adult by about 8.
#:
#: Named and cited rather than written as a literal at a comparison, per
#: CLAUDE.md: one source of truth for a rule. It is used by `AgeBand` callers
#: at the boundary, where an age in years is available; `decide` itself never
#: sees a number, because `AgeBand` is the parsed form.
ADULT_ALGORITHM_FLOOR_YEARS: int = 8

#: Unconditional rescue breaths before compressions in a paediatric or
#: submersion arrest. `CLINICAL-STANDARDS.md` §3.3 ("the jumpstart") and
#: `protocols.py:drowning-rescue`.
#:
#: BLOCKER 2: five is the count, and it is UNCONDITIONAL. The published
#: algorithm gates these on a palpated pulse. That gate is removed here, and the
#: conflict is deliberate and recorded: the published precondition is a
#: mass-casualty rationing step, and a bystander cannot perform the measurement
#: it depends on.
RESCUE_BREATHS_BEFORE_COMPRESSIONS: int = 5

#: Assessments excluded from the responder path. Blocker 5's list, verbatim.
#:
#: This is a value, not a comment, because ASM-15 is asserted by walking every
#: reachable branch and checking its `question_key` against this set. A rule
#: whose only home is prose is a rule the next author restores by accident.
#:
#: Note what is in here that a published algorithm relies on: capillary refill
#: is START's adult RED/YELLOW discriminator and counted respiratory rate is the
#: primary discriminator in both algorithms. Both are excluded anyway. A
#: bystander cannot time a 5-second press at heart level in good light, and
#: cannot count breaths without a timepiece and an unaware patient - and the
#: risk of asking anyway is worse than omission, because a panicking caller asked
#: to count breaths will GUESS, and that guess enters L2 looking like data.
UNPERFORMABLE_ASSESSMENTS: frozenset[str] = frozenset(
    {
        "capillary_refill",
        "respiratory_rate_counted",
        "auscultation",
        "percussion",
        "tracheal_position",
        "jvp",
        "pupils",
        "blood_glucose",
        "pulse_pressure",
        "urine_output",
        "blood_pressure",
        # The pulse check is not on Blocker 5's list as an *equipment* problem -
        # it needs no equipment at all - but it fails §5.4's test for the same
        # reason and with a worse consequence, so it is excluded here too. This
        # is what makes ASM-12 and ASM-15 one mechanism rather than two.
        "pulse_check",
    }
)


# ------------------------------------------------------------------- the letters


class Letter(Enum):
    """The ABCDE letters, in assessment order.

    An IntEnum was rejected: the ordering matters but arithmetic on it does not,
    and `Letter.A + 1` being legal invites a caller to compute the next letter
    rather than ask for it. `order` and `successor` are the two things callers
    actually need.
    """

    A = "airway"
    B = "breathing"
    C = "circulation"
    D = "disability"
    E = "exposure"

    @property
    def order(self) -> int:
        return _LETTER_ORDER[self]

    def precedes(self, other: "Letter") -> bool:
        return self.order < other.order


_LETTER_ORDER: Mapping[Letter, int] = {
    letter: index for index, letter in enumerate(Letter)
}

#: The letters in assessment order. Derived from the enum's own declaration
#: order rather than written out a second time.
LETTERS_IN_ORDER: tuple[Letter, ...] = tuple(Letter)


class AssessmentStep(Enum):
    """What L2 says must happen next.

    Two kinds of step, and the distinction is Blocker 4's: a step that ESTABLISHES
    an input, and a step that DELIVERS an instruction. The worse-branch rule
    applies differently to each, so they must be distinguishable without reading
    a string.

    NO MEMBER HERE IS UNREACHABLE. A declared-but-never-returned step is safe
    today and misleading tomorrow, because the next author reads a member plus a
    matching input field as a designed slot and builds on a promise nobody kept.
    `ASK_AGE_BAND` and `INSTRUCT_OPEN_AIRWAY` were exactly that and are gone -
    see the module docstring for why each one is absent rather than implemented.
    A test asserts every member is reachable, so re-adding a decorative member
    fails the suite rather than waiting for a future author.
    """

    # Establish an input.
    ASK_SCENE_SAFE = "ask_scene_safe"
    ASK_RESPONSIVENESS = "ask_responsiveness"
    ASK_BREATHING_QUALITY = "ask_breathing_quality"
    ASK_SEVERE_BLEEDING = "ask_severe_bleeding"
    ASK_AIRWAY_OBSTRUCTION = "ask_airway_obstruction"
    ASK_SPINAL_RISK = "ask_spinal_risk"
    ASK_EXPOSURE_FINDINGS = "ask_exposure_findings"

    # Deliver an instruction.
    INSTRUCT_MAKE_SCENE_SAFE = "instruct_make_scene_safe"
    #: BLOCKER 3 OF THE THIRD CLINICAL REVIEW. The hazard is IN THE PATIENT, so
    #: the action is to break the contact or isolate the supply BEFORE any
    #: touch. Distinct from INSTRUCT_MAKE_SCENE_SAFE because the action is
    #: different in kind: that one says move AWAY, this one says the responder
    #: may well be in the right place and must not make CONTACT yet.
    INSTRUCT_BREAK_ELECTRICAL_CONTACT = "instruct_break_electrical_contact"
    INSTRUCT_CPR = "instruct_cpr"
    INSTRUCT_RESCUE_BREATHS_THEN_CPR = "instruct_rescue_breaths_then_cpr"
    #: BLOCKER 2. Haemorrhage control AND compressions, in one instruction.
    INSTRUCT_CONTROL_BLEEDING_THEN_CPR = "instruct_control_bleeding_then_cpr"
    #: BLOCKER 5. Back blows and abdominal thrusts, while still responsive.
    INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION = "instruct_clear_airway_obstruction"
    #: BLOCKER 1 OF THE THIRD CLINICAL REVIEW. An AWAKE choker who is ALSO
    #: bleeding catastrophically. Hands-free pressure, then thrusts - the same
    #: resolution `INSTRUCT_CONTROL_BLEEDING_THEN_CPR` already gives this
    #: patient's unconscious twin, because the conflict is the same one and the
    #: module has already proved it is not a forced choice.
    INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION = (
        "instruct_control_bleeding_then_clear_airway_obstruction"
    )
    #: An AWAKE patient breathing badly. Never compressions. See `routes_to_cpr`.
    INSTRUCT_SUPPORT_SEVERE_BREATHING_DIFFICULTY = (
        "instruct_support_severe_breathing_difficulty"
    )
    INSTRUCT_CONTROL_BLEEDING = "instruct_control_bleeding"
    INSTRUCT_RECOVERY_POSITION = "instruct_recovery_position"
    #: BLOCKER 4. Airway protection without a roll, for a suspected spine.
    INSTRUCT_AIRWAY_WITH_SPINAL_CARE = "instruct_airway_with_spinal_care"
    INSTRUCT_MONITOR = "instruct_monitor"

    @property
    def is_instruction(self) -> bool:
        return self.name.startswith("INSTRUCT_")

    @property
    def question_key(self) -> str:
        """What this step establishes, as a key L3 phrases and ASM-15 checks.

        An instruction establishes nothing, so its key is its own name. This is
        what lets the ASM-15 walk ask one question of every reachable step
        without special-casing.
        """
        if self.is_instruction:
            return self.value
        return self.value.removeprefix("ask_")


# --------------------------------------------------------------- the branch type


#: The witness that blocks ordinary construction of `AssessmentBranch`.
#:
#: ASM-06 is "L3 cannot override an L2 branch - structurally, by the type
#: signature, not by convention". A frozen dataclass alone does not satisfy the
#: row: L3 could construct its own `AssessmentBranch(...)` with whatever step it
#: preferred and hand it onward, and nothing in the type would notice.
#: Frozen-ness stops MUTATION and does nothing about FORGERY, and a forged
#: branch is the more dangerous of the two because it arrives looking authentic.
#:
#: WHAT THIS WITNESS DOES AND DOES NOT DO. Stated precisely, because an
#: independent review found the first version of this comment claiming more than
#: the code delivered - and an overstated safety claim is worse than an absent
#: one, since the next author trusts it.
#:
#: It blocks: `AssessmentBranch(...)` written anywhere outside this module. That
#: is the form an author actually reaches for, and it now raises.
#:
#: It does NOT block, and cannot: `object.__new__(AssessmentBranch)` followed by
#: `object.__setattr__` per field, which never runs `__init__` and so never
#: reaches `__post_init__`; and `dataclasses.replace`, which carries the witness
#: forward. Subclassing is blocked separately by `__init_subclass__` below.
#: Python has no mechanism that makes a value unforgeable against an author
#: willing to write `object.__new__`, so a claim of unforgeability would be
#: false whatever this module did.
#:
#: So the row is met by a DIFFERENT mechanism, one that is actually decidable:
#: `is_authentic` below. Every branch `decide` mints is registered, and a
#: consumer asks whether the branch it was handed is one of them. Forgery is
#: therefore not prevented - it is DETECTED, at the boundary, by a call that
#: cannot be satisfied by a convincing-looking object. That is the property the
#: wire-up step will enforce, and it is tested here.
_WITNESS = object()

#: Identities of every branch `decide` has minted, so `is_authentic` can answer.
#:
#: Keyed by `id()` into a set rather than holding the branches themselves: this
#: must not keep a whole incident's branches alive, and a branch is immutable so
#: its identity is all that is needed. `id()` can be reused after an object is
#: collected, which would make `is_authentic` answer True for a forged object
#: that happens to land on a recycled address - so the branch is ALSO held, in a
#: bounded ring, which is what keeps the identity valid while it can still be
#: asked about.
#:
#: Bounded deliberately: an unbounded registry in a long-running agent process
#: is a leak, and a leak in the safety layer is how the safety layer gets
#: removed. A branch older than the ring is reported inauthentic, which is the
#: safe direction - a consumer that cannot verify must re-ask L2 rather than
#: trust what it holds.
_REGISTRY_SIZE = 64
_recent_branches: list["AssessmentBranch"] = []


@dataclass(frozen=True)
class AssessmentBranch:
    """The branch L2 took. Minted only by `decide`, and verifiable as such.

    Frozen, witnessed against ordinary construction, closed to subclassing, and
    - the part that actually carries ASM-06 - verifiable via `is_authentic`.
    See `_WITNESS` for precisely which forgeries each mechanism does and does
    not stop; the short version is that construction cannot be locked down in
    Python, so the row is met by detection at the boundary rather than by a
    claim of unforgeability.

    Carries the reasoning as well as the decision, because an escalation nobody
    can attribute afterwards is one an audit cannot review: `unresolved_letter`
    says which letter forced this, `assumed_inputs` says what we took on trust,
    and `provenance` says whether a later evidence-grade finding may correct the
    level this branch asks for.
    """

    step: AssessmentStep
    letter: Letter
    criticality: Criticality
    rationale: str
    #: Which letter is currently unresolved. Equal to `letter` for an assessment
    #: step; for an instruction it is the letter the instruction treats.
    unresolved_letter: Letter
    #: Input names taken as ASSUMED_WORST or left UNKNOWN on the way here. Sorted
    #: so two runs over the same inputs produce an identical branch (ASM-01).
    assumed_inputs: tuple[str, ...]
    _witness: object = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse subclasses.

        Without this, `class Forged(AssessmentBranch)` with an overridden
        `__post_init__` forges a branch that passes `isinstance` - so every
        consumer checking the type is satisfied while the witness never runs.
        Found by independent review, which reached it in one line.
        """
        raise TypeError(
            "AssessmentBranch may not be subclassed: a subclass can override "
            "__post_init__ and forge a branch that still passes isinstance "
            "(ASM-06)"
        )

    def __post_init__(self) -> None:
        if self._witness is not _WITNESS:
            raise TypeError(
                "AssessmentBranch is constructible only by assessment.decide(); "
                "L3 may change the INPUTS and ask again, and may never author a "
                "branch (ARCHITECTURE-ASSESSMENT.md §5, ASM-06)"
            )

    @property
    def provenance(self) -> Provenance:
        """Whether later evidence may correct the level this branch asks for.

        An assumption anywhere on the path makes the whole branch an assumption:
        the level was reached partly because something could not be established,
        so a clinician who later establishes it must be able to correct it.
        ASM-14.
        """
        return Provenance.ASSUMPTION if self.assumed_inputs else Provenance.EVIDENCE

    @property
    def is_instruction(self) -> bool:
        return self.step.is_instruction


def is_authentic(branch: object) -> bool:
    """Whether `branch` is a branch this module actually computed.

    THE ACTUAL ASM-06 MECHANISM. The witness blocks the construction an author
    would write; this is what a consumer calls, and it is the part that holds
    against `object.__new__`. Identity cannot be forged: an object convincing in
    every field is still not the object `decide` returned.

    Returns False rather than raising, for anything at all - a forgery, a
    replaced copy, a string, None. A consumer's correct response to False is to
    re-ask L2, and a raise here would push that consumer into a try/except
    around its safety check, which is where safety checks get commented out.

    Reports False for a branch older than the bounded registry. A consumer that
    cannot verify must re-ask rather than trust, so the stale direction is the
    safe one.
    """
    if not isinstance(branch, AssessmentBranch):
        return False
    return any(held is branch for held in _recent_branches)


def _branch(
    step: AssessmentStep,
    letter: Letter,
    criticality: Criticality,
    rationale: str,
    assumed: tuple[str, ...] = (),
    unresolved: Letter | None = None,
) -> AssessmentBranch:
    """The only place a branch is built. Keeps `_WITNESS` at one call site."""
    branch = AssessmentBranch(
        step=step,
        letter=letter,
        criticality=criticality,
        rationale=rationale,
        unresolved_letter=unresolved if unresolved is not None else letter,
        assumed_inputs=tuple(sorted(assumed)),
        _witness=_WITNESS,
    )
    # Registered so `is_authentic` can recognise it. This is the one piece of
    # module-level mutable state in L2, and it is deliberately invisible to
    # `decide`'s result: the branch returned for a given set of inputs does not
    # depend on what is in the registry, so purity and determinism (ASM-01) are
    # untouched. Asserted by the 1000-run determinism test, which shares one
    # process and therefore one registry across all of its runs.
    _recent_branches.append(branch)
    if len(_recent_branches) > _REGISTRY_SIZE:
        del _recent_branches[:-_REGISTRY_SIZE]
    return branch


# ----------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class AssessmentInputs:
    """Everything L2 is allowed to branch on, with the certainty of each.

    Frozen: a turn produces a NEW inputs object rather than mutating one, so a
    branch computed from an earlier set cannot be retrospectively invalidated by
    a later write. Same inputs, same branch, forever (ASM-01).

    Every field defaults to UNKNOWN. A fresh incident knows nothing, and the
    machine's first act must be to say what to establish first - not to assume a
    well patient.

    `pulse` is deliberately present and deliberately unread; see `PulseReport`.
    It is the ONLY such field. `airway_opened` was a second one - an input
    nothing read, beside a step nothing returned - and it is gone: an unread
    field implies a designed slot, and the next author builds on the implication.
    """

    scene_safe: Finding[SceneSafety] = field(default_factory=_unknown)
    responsiveness: Finding[Responsiveness] = field(default_factory=_unknown)
    breathing: Finding[Breathing] = field(default_factory=_unknown)
    severe_bleeding: Finding[SevereBleeding] = field(default_factory=_unknown)
    #: BLOCKER 5. Read by `decide`, and the reason the choking path exists.
    airway_obstruction: Finding[AirwayObstruction] = field(default_factory=_unknown)
    #: BLOCKER 4. Mechanism of injury, read at the recovery-position branch.
    spinal_risk: Finding[SpinalRisk] = field(default_factory=_unknown)
    age_band: Finding[AgeBand] = field(default_factory=_unknown)
    submersion: Finding[bool] = field(default_factory=_unknown)
    exposure_reviewed: Finding[bool] = field(default_factory=_unknown)
    #: Recorded for the clinician bridge. Never a branch point. BLOCKER 2.
    pulse: Finding[PulseReport] = field(default_factory=_unknown)

    def assumed(self, *names: str) -> tuple[str, ...]:
        """Which of `names` are assumptions rather than reported findings.

        Used to build a branch's `assumed_inputs`, which is what makes ASM-14's
        correctability decidable from the branch alone.

        KEPT, BUT NO LONGER THE WAY A LEAF BUILDS ITS SET. Every leaf used to
        call this with a hand-written list of names, and BLOCKER 2 of the third
        clinical review is what that cost: sixteen of the eighteen reachable
        steps under-recorded at least one input they had actually read, because
        the list at the call site is a second copy of the path and the two
        drifted every time the routing changed. `_Consulted` below is the one
        source of truth now; this method remains as the primitive it uses, and
        as the explicit form for a set that is genuinely not the path's.
        """
        return tuple(
            name
            for name in names
            if getattr(self, name).is_assumption
        )


#: Every input name a path can consult. Derived from the dataclass rather than
#: listed, so a field added later is covered by `_Consulted` without a second
#: edit - which is the failure mode this whole mechanism exists to remove.
_INPUT_NAMES: tuple[str, ...] = tuple(AssessmentInputs.__dataclass_fields__)


class _Consulted:
    """The inputs a single `decide` call actually read, accumulated as it runs.

    BLOCKER 2 OF THE THIRD CLINICAL REVIEW, and the reason it is a mechanism
    rather than sixteen corrected lists.

    The guard test commissioned to catch a leaf that reads an input without
    recording it could not catch one: it skipped every leaf whose
    `assumed_inputs` omitted the name, so it only ever fired on leaves that had
    already got it right. Inverting it into an assertion exposed that SIXTEEN of
    the EIGHTEEN reachable steps were under-recording - `ASK_RESPONSIVENESS`
    read `scene_safe` and recorded only `("responsiveness",)`, the bleed hoist
    reported `assumed_inputs=()` with A and B unknown, and so on.

    The consequence closed is ASM-14 INVERTED: a level reached with the scene,
    the airway or the bleed merely ASSUMED reported `provenance=EVIDENCE`, so
    the clinician on the bridge who later established that input could not
    correct the level. On a CRITICAL that is uncorrectable by the one person
    able to correct it.

    WHY AN ACCUMULATOR AND NOT SIXTEEN CORRECTED LISTS. A hand-written list at
    the call site is a SECOND COPY of the path, and the path is not even
    constant per leaf: `_gate_floor` consults `responsiveness` only when
    `_is_arrest` needs it, so `ASK_SCENE_SAFE` genuinely reads a different set
    depending on the breathing value. A list cannot track a short-circuit, which
    means the corrected lists would have to over-record - naming inputs the path
    never touched and making every branch an assumption, which destroys the
    distinction ASM-14 rests on. Recording the read WHERE THE READ HAPPENS is
    the only form with one source of truth.

    PURITY IS UNTOUCHED (ASM-01). This is a per-call accumulator created inside
    `decide` and dropped when it returns: no module state, no ordering
    dependence, and the branch for a given set of inputs is identical on every
    call. It records which fields were consulted; it cannot change what any of
    them says. Asserted by the 1000-run determinism test, which compares
    `assumed_inputs` across repeated calls on fresh inputs objects.

    ONE TYPED ACCESSOR PER INPUT, not a single `get(name: str)`. A string-keyed
    accessor would have to return `Finding[object]`, which erases exactly the
    generic that `Finding` exists for - a `Finding[Breathing]` could then be
    passed where a `Finding[Responsiveness]` is required and mypy would not
    notice. The accessors are more lines and they keep the property; a recorded
    read is not worth paying for with a lost type. `pulse` deliberately has NO
    accessor, which is how ASM-12's "never a branch point" becomes a fact about
    the reachable API rather than a rule the next author has to remember.
    """

    __slots__ = ("_inputs", "_names")

    def __init__(self, inputs: AssessmentInputs) -> None:
        self._inputs = inputs
        self._names: set[str] = set()

    def _read(self, name: str) -> None:
        self._names.add(name)

    @property
    def scene_safe(self) -> Finding[SceneSafety]:
        self._read("scene_safe")
        return self._inputs.scene_safe

    @property
    def responsiveness(self) -> Finding[Responsiveness]:
        self._read("responsiveness")
        return self._inputs.responsiveness

    @property
    def breathing(self) -> Finding[Breathing]:
        self._read("breathing")
        return self._inputs.breathing

    @property
    def severe_bleeding(self) -> Finding[SevereBleeding]:
        self._read("severe_bleeding")
        return self._inputs.severe_bleeding

    @property
    def airway_obstruction(self) -> Finding[AirwayObstruction]:
        self._read("airway_obstruction")
        return self._inputs.airway_obstruction

    @property
    def spinal_risk(self) -> Finding[SpinalRisk]:
        self._read("spinal_risk")
        return self._inputs.spinal_risk

    @property
    def age_band(self) -> Finding[AgeBand]:
        self._read("age_band")
        return self._inputs.age_band

    @property
    def submersion(self) -> Finding[bool]:
        self._read("submersion")
        return self._inputs.submersion

    @property
    def exposure_reviewed(self) -> Finding[bool]:
        self._read("exposure_reviewed")
        return self._inputs.exposure_reviewed

    @property
    def assumed(self) -> tuple[str, ...]:
        """The consulted inputs that were assumptions rather than reports.

        This is what every leaf passes as `assumed=`. It is computed from the
        reads that actually happened on the way to that leaf, so a leaf cannot
        under-record and cannot over-record.

        Iterates `_INPUT_NAMES` rather than `self._names` so the result is in a
        fixed order independent of read order, which is one of the two things
        that keep `assumed_inputs` identical across runs (`_branch` sorts as
        well). Reads the findings directly instead of delegating to
        `AssessmentInputs.assumed`: that delegation built an intermediate
        generator and re-walked the names, and it cost ~1.1us of a ~9.5us
        decision - measured, not guessed, after the exhaustive walk's ASM-09
        budget went marginal on the first version of this class.
        """
        inputs = self._inputs
        names = self._names
        return tuple(
            name
            for name in _INPUT_NAMES
            if name in names and getattr(inputs, name).is_assumption
        )


# ---------------------------------------------------------------------- the rule


def decide(inputs: AssessmentInputs) -> AssessmentBranch:
    """The branch to take, given what is established. Pure.

    ABCDE in order, and the order is structural: each block below returns, so no
    later letter can be reached while an earlier one is unresolved (ASM-03).
    There is no path that falls through to D while A is open, because there is no
    expression for it - the function has already returned.

    Deterministic by construction: reads only `inputs`, holds no state, calls
    nothing that observes time, randomness or the filesystem (ASM-01, ASM-09).

    EVERY INPUT IS READ THROUGH `_Consulted`, and that is Blocker 2 of the third
    clinical review. A leaf's `assumed=` is `path.assumed` - the assumptions
    among the inputs this call actually consulted - never a hand-written list,
    because the list is a second copy of the path and sixteen of the eighteen
    steps had already drifted from it. See `_Consulted`.

    A THIN WRAPPER OVER `_decide`, which takes the accumulator rather than
    building one. That seam is what lets a test ask the MODULE which inputs a
    path consulted instead of modelling the path a second time in the test file
    - and a second model of the path is precisely what Blocker 2 was.
    """
    return _decide(_Consulted(inputs))


def _decide(path: _Consulted) -> AssessmentBranch:
    """`decide`'s body, over an accumulator the caller owns. See `decide`."""
    # ---- DR: scene safety. Blocker 5's "no blocking scene-safety gate".
    #
    # Before A, and blocking. The bystander is the operator: if they become
    # casualty two, the patient dies too. The clinical review found this present
    # as one line of prose and one retrievable document, and nowhere in any
    # algorithm - "no DR in the DRABC".
    # BLOCKER 3(b) and (c) are both in these two branches.
    #
    # (b) The gate must not LOWER the criticality of a known arrest. An arrest
    # waiting behind an unresolved scene is still an arrest, so the category is
    # the arrest's, and only the STEP is the scene's. `_gate_floor` computes it
    # from what is already established rather than pinning SEVERE.
    #
    # (c) The phrasing is pinned here, not left to L3. This is the one gate
    # between a reported arrest and compressions, and "is the scene safe?"
    # invites a freeze ("I don't know... I think so?") or a reflexive "yes"
    # carrying no information. The answerable form is a closed hazard list, and
    # the rationale names it the way the breathing question's does - which was
    # pinned, while this one was not.
    if not path.scene_safe.is_established:
        return _branch(
            AssessmentStep.ASK_SCENE_SAFE,
            Letter.A,
            _gate_floor(path),
            "Scene safety is not established. Nothing else may be advised "
            "first: a responder who becomes the second casualty cannot help "
            "the first. Ask it as a CLOSED HAZARD LIST answerable yes or no - "
            "traffic, fire, electricity - and about where the responder is "
            "standing right now. Never the open form 'is the scene safe?', "
            "which is answered with a freeze or a reflexive yes. THEN ASK THE "
            "ONE THING THAT DECIDES WHETHER THE PATIENT MAY BE TOUCHED AT ALL: "
            "if they named electricity, ask whether the patient is still "
            "TOUCHING the cable, the rail or the machine, because a hazard "
            "that is IN the patient makes touching them the injury and it is "
            "handled differently from one they are merely standing near. Offer "
            "'already beside them and not leaving' as an answer in its own "
            "right, because a committed responder is a real answer and not a "
            "refusal.",
            assumed=path.assumed,
        )

    scene = path.scene_safe.value
    assert scene is not None  # narrowed by is_established above

    # BLOCKER 3 OF THE THIRD CLINICAL REVIEW. THE HAZARD IS IN THE PATIENT.
    #
    # Checked BEFORE the generic move-to-safety gate, because it is not the same
    # instruction: that one says move AWAY from a hazard the responder is
    # standing near, and this one says the responder may be in exactly the right
    # place and must not make CONTACT until the circuit is broken. Told to "move
    # to safety" instead, a bystander already at the patient's side reads it as
    # advice they have already declined and does the thing they came to do.
    #
    # WHY THIS IS NOT A WARNING ON THE COMPRESSIONS INSTRUCTION. Reproduced
    # before the fix: UNSAFE_RESPONDER_COMMITTED + UNRESPONSIVE + breathing NONE
    # returned INSTRUCT_CPR at CRITICAL, with a `_hazard_suffix` that spoke of
    # watching the hazard and getting clear "if it closes in" and never
    # mentioned TOUCHING the patient. Compressions on a patient still in circuit
    # ARE the mechanism of injury, so a warning attached to that instruction is a
    # caveat on an action that must not happen yet. Two casualties instead of
    # one, which is the exact outcome the DR gate exists to prevent - reached
    # THROUGH the gate.
    #
    # AND THIS IS THE ONE PLACE THE COMMITTED ESCAPE HATCH MUST NOT APPLY.
    # Blocker 3(a)'s escape is right and stays right for traffic: the absorbing
    # loop killed the patient it protected, and a responder who has taken a
    # probabilistic external risk will not un-take it. That argument turns on
    # the hazard being survivable to work beside, and it does not transfer here -
    # "advance anyway" on an energised patient does not accept a risk to the
    # responder, it transfers the arrest to them, and then nobody is compressing
    # anybody. The type is what enforces it: there is no
    # UNSAFE_IN_PATIENT_COMMITTED member to reach this from, so the exception
    # cannot be reintroduced by a condition downstream.
    #
    # `_gate_floor`, shared with both other pre-A gates. Blocker 3(b) is that a
    # gate must not LOWER the criticality of a known arrest, and the Blocker 1
    # hoist reintroduced it verbatim later - so a third gate written with a
    # pinned SEVERE would be the third instance of one defect. The step is this
    # gate's; the category is not this gate's to cap, and an arrest behind a live
    # conductor is still an arrest.
    if scene.is_in_the_patient:
        return _branch(
            AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT,
            Letter.A,
            _gate_floor(path),
            "THE HAZARD IS IN THE PATIENT: they are still in contact with a "
            "live conductor, so TOUCHING THEM COMPLETES THE CIRCUIT and chest "
            "compressions would be the mechanism of injury rather than the "
            "treatment. DO NOT TOUCH THE PATIENT YET - not to check them, not "
            "to start compressions - however close the responder already is. "
            "This is the one hazard where being committed beside the patient "
            "does not mean carrying on. BREAK THE CONTACT FIRST, and there are "
            "four ways in falling order of preference. IF THE PATIENT IS "
            "AWAKE, SHOUT AT THEM TO LET GO AND GET CLEAR FIRST - it costs "
            "nothing and risks nobody - but do NOT read a failure to obey as "
            "unconsciousness or as refusal: a current across the hand clamps "
            "the grip shut, so somebody who is fully awake often CANNOT let go "
            "of what is electrocuting them, and telling them again will not "
            "change it. Then switch the supply off at the plug, the breaker or "
            "the isolator if it can be reached. If it is a cable, a rail or "
            "overhead lines, that is not a switch anybody at the scene can "
            "throw: TELL THE EMERGENCY SERVICES NOW AND SAY IT IS AN "
            "ELECTRICAL INCIDENT, so the network operator or the railway can "
            "isolate the supply and confirm it dead - that call IS the "
            "intervention for this one, not a formality alongside it. Or, ONLY "
            "for ordinary household voltage and only if the responder is "
            "standing on something dry and insulating, push the casualty or "
            "the cable clear with something dry and non-conducting - a wooden "
            "broom handle, a plastic chair - never a bare hand and never "
            "anything metal or damp. IF THERE IS WATER AT THIS SCENE, TREAT "
            "THE WHOLE WET AREA AS LIVE and do not step into it or reach "
            "across it: water carries the current out to whoever is standing "
            "in it, so the push-clear method is OFF unless the responder is "
            "out of the wet and on something dry, and isolating the supply is "
            "the only thing that makes the area safe. IF NONE OF THAT IS "
            "POSSIBLE, SAY SO PLAINLY AND DO NOT IMPROVISE: for high voltage, "
            "overhead lines or a rail, stay well "
            "back - the ground itself can be live several metres out - and "
            "wait for the supply to be confirmed dead, because a rescuer down "
            "beside the patient means nobody is resuscitating anybody. THE "
            "MOMENT THE CONTACT IS BROKEN OR THE SUPPLY IS OFF, SAY SO: this "
            "patient is then an ordinary arrest and compressions start "
            "immediately, and electrical arrest is one of the most survivable "
            "kinds there is."
            + _hazard_suffix(scene),
            assumed=path.assumed,
        )

    if scene.blocks_patient_contact:
        return _branch(
            AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE,
            Letter.A,
            _gate_floor(path),
            _move_to_safety_rationale(scene),
        )

    # BLOCKER 1. CATASTROPHIC HAEMORRHAGE, HOISTED TO PRE-A. C-ABC / MARCH.
    #
    # This block sat after the responsiveness and breathing gates, which meant a
    # bystander kneeling on a femoral bleed was asked to assess breathing. They
    # then take their hands off the wound to look at the chest, or they answer
    # and wait. Exsanguination is 2-3 minutes; the airway of a talking patient
    # is self-evidently patent.
    #
    # The hoist is deliberately NARROW, and the narrowness is the whole safety
    # argument. ABCDE is NOT reordered wholesale. This is a pre-A gate on an
    # ESTABLISHED finding only - `is_established` and PRESENT - in exactly the
    # shape of the scene gate above. The QUESTION "is there catastrophic
    # bleeding?" stays down in the C block where it was, because hoisting the
    # question would delay the breathing question behind a bleeding question and
    # simply move the defect one letter over.
    #
    # An arrest is NOT overtaken here: breathing is not read yet, so a patient
    # with an established bleed AND established non-normal breathing falls
    # through to `_arrest_branch`, which owns that combination (Blocker 2).
    #
    # WHAT THIS GATE DOES OUTRANK, recorded because an independent review found
    # the ordering undiscussed and therefore indistinguishable from an accident
    # of line order. It precedes the conscious-distress leaf, and it precedes
    # the choking block for a patient whose breathing is anything other than an
    # ESTABLISHED NONE - because a responsive patient with SOME air movement is
    # what `protocols.py:airway-choking-adult` means by scoping itself to "only
    # while they are still responsive". Exsanguination is two to three minutes
    # and cannot be put back. Pinned by
    # `test_a_catastrophic_bleed_outranks_a_still_responsive_choker`.
    #
    # AND THE ONE CASE IT DOES NOT OUTRANK - BLOCKER 1 OF THE THIRD CLINICAL
    # REVIEW, WHICH WAS A REACHABLE DEATH. Reproduced: ALERT + breathing NONE +
    # WITNESSED_FOREIGN_BODY + bleeding PRESENT returned this leaf, whose
    # rationale never mentions the airway, the obstruction, thrusts or
    # compressions and tells the bystander to keep both hands on the wound
    # "while answering anything else". A conscious person with a lodged bolus
    # and zero air movement arrests in front of them. That is Blocker 5's death
    # reached through the bleed gate instead of the arrest route.
    #
    # WHY THE PREVIOUS ROUND'S RECORDED JUDGEMENT DID NOT COVER IT, which is the
    # load-bearing part. The judgement was justified by "a patient who is STILL
    # RESPONSIVE is by definition still moving some air", and the gate's own
    # rationale said "a patient who can be asked about is a patient with an
    # airway". That premise is sound for an UNESTABLISHED breathing value - the
    # case it was written for - and FALSE for breathing ESTABLISHED as NONE. The
    # cell was the recorded judgement applied outside the premise that justified
    # it, and no test pinned the established-NONE variant. Both statements of the
    # false premise are corrected: the one in the rationale below, and the one in
    # the named test's comment.
    #
    # THE CHOICE IS NOT FORCED, WHICH IS WHY THIS IS NOT A PRIORITY DECISION.
    # `INSTRUCT_CONTROL_BLEEDING_THEN_CPR` already solves this exact
    # single-rescuer conflict for the UNCONSCIOUS twin of this patient: pressure
    # held by a knee, body weight, or a tight bandage or tourniquet over packing
    # frees both hands. Back blows and abdominal thrusts need both hands. So the
    # module has already proved the choice is avoidable, and a forced choice here
    # would not be honest. Blocker 2's resolution applies unchanged: do both, do
    # not choose.
    #
    # THE NARROWER ALTERNATIVE WAS REJECTED BY THE REVIEWER. Letting
    # `_reaches_choking_block` stand the hoist aside on established NONE would
    # drop the BLEED instead, which is the same defect wearing the other hat.
    # The bleed is carried into the choking block, not abandoned at it.
    if (
        path.severe_bleeding.is_established
        and path.severe_bleeding.value is SevereBleeding.PRESENT
        and not _routes_to_arrest_path(path)
        and not _airway_outranks_the_bleed_hoist(path)
    ):
        return _branch(
            AssessmentStep.INSTRUCT_CONTROL_BLEEDING,
            Letter.C,
            # Same rule as the scene gate, and for the same reason: a gate must
            # not LOWER the criticality of a possible arrest waiting behind it.
            # The step is the bleed's; the category is not the bleed's to cap.
            _gate_floor(path),
            "Catastrophic external haemorrhage, established. Direct pressure "
            "now, then packing or a tourniquet. This precedes the "
            "responsiveness and breathing questions rather than following "
            "them: nothing reported about this patient says the airway is "
            "blocked or that they have stopped moving air, and exsanguination "
            "is measured in two to three minutes. Keep the hands on the wound "
            "while answering anything else - and if they STOP being able to "
            "breathe or to speak, or if somebody SAW something go into their "
            "airway, say so immediately, because either one changes the whole "
            "instruction."
            + _hazard_suffix(scene),
            # DEFECT 2, AND THEN BLOCKER 2 OF THE THIRD CLINICAL REVIEW - AND
            # THE COUNT WENT UP EACH TIME SOMETHING COULD ACTUALLY LOOK. The
            # re-review named the distress leaf as the only leaf recording no
            # assumptions; adding `assumed=` to the leaves that passed none
            # raised it to six. Inverting the guard test that was supposed to
            # catch the rest - it had a `continue` where an assertion belonged,
            # so it could only fire on leaves already correct - showed SIXTEEN
            # of the EIGHTEEN reachable steps still under-recording, this leaf
            # among them: it named only `scene_safe` and `severe_bleeding`
            # while `_gate_floor` had also read breathing and responsiveness.
            # Hence `path.assumed` rather than any list. See `_Consulted`.
            assumed=path.assumed,
            unresolved=Letter.C,
        )

    # ---- A/B: responsiveness and breathing.
    #
    # Assessed together and before anything else, because the one finding that
    # short-circuits the whole algorithm is the pair: "unresponsive and not
    # breathing normally -> start CPR immediately"
    # (`CLINICAL-STANDARDS.md` §1.2). Splitting them into two letters here would
    # mean a patient with no breathing waiting for the A letter to close before
    # the B letter could ask for compressions.
    if not path.responsiveness.is_established:
        return _branch(
            AssessmentStep.ASK_RESPONSIVENESS,
            Letter.A,
            Criticality.SEVERE,
            "Responsiveness is not established. It decides whether the airway is "
            "protected and whether CPR is on the table.",
            assumed=path.assumed,
        )

    responsiveness = path.responsiveness.value
    assert responsiveness is not None  # narrowed by is_established above

    # ---- A: airway obstruction. BLOCKER 5, and it sits HERE for a reason.
    #
    # Before the breathing question, because a choking patient is exactly the
    # patient whose breathing answer is ambiguous - they are making noise and
    # moving their chest, and "is he breathing" collects a useless yes. And
    # after responsiveness, because responsiveness is what decides which of the
    # two choking actions applies. That is the transition
    # `protocols.py:airway-choking-adult` states in its closing line and scopes
    # in its own text ("this protocol applies only while they are still
    # responsive"); L2 owns the transition itself because a state change on a
    # named input is a branch, not a phrasing.
    #
    # Only asked of a RESPONSIVE patient. An unresponsive patient is handled by
    # the arrest path below, and asking a bystander to reconstruct whether an
    # already-collapsed casualty was eating costs the window that compressions
    # need. So the question is not universal - it is scoped to the state where
    # its answer changes the action.
    #
    # AND NOT OF A PATIENT ALREADY ROUTING TO THE ARREST PATH. Defect 1's fix
    # decoupled the two: absent breathing now reaches `_arrest_branch` whatever
    # responsiveness was reported, so "responsive" and "not going to the arrest
    # path" stopped being the same condition. Without this clause an ALERT
    # report plus absent breathing asked the choking question - which both
    # delays compressions and, when a bleed is also established, DEFERS THE
    # BLEED BEHIND A QUESTION, the exact defect the pre-A hoist exists to close.
    # Caught by the established-bleed walk rather than by inspection.
    #
    # `_routes_to_arrest_path` is reused rather than restating the condition:
    # one rule, one place, and it is the same predicate the bleed hoist above
    # consults, so the two cannot drift apart again.
    #
    # EXCEPT FOR AN ESTABLISHED WITNESSED OBSTRUCTION, which is the one reading
    # that RECONCILES the contradiction instead of being one. Defect 1's whole
    # argument is that "awake" plus "no breathing" cannot both be true - but a
    # complete foreign-body obstruction is precisely the case where they can:
    # the patient is conscious and moving no air, the caller's "he's not
    # breathing at all" is ACCURATE, and the correct action is still back blows
    # and thrusts because compressions cannot shift a lodged bolus. Blocker 5's
    # named test asserts this, correctly, and it is not a test to rescope.
    #
    # So the contradiction re-test applies where there is no explanation for
    # the contradiction, and stands aside where there is one. The choking leaf
    # already carries the transition to compressions on loss of consciousness,
    # which is the path that reaches the arrest branch when it should.
    #
    # The exemption is not written here. `_reaches_choking_block` holds
    # it, and `_routes_to_arrest_path` consults THAT - so a witnessed
    # obstruction already makes the predicate below answer False, and naming
    # the exemption a second time at this site would be dead logic that reads
    # like a live rule. Mutation testing caught the redundant form: removing
    # the extra clause changed no behaviour, which is the definition of a
    # condition that is not carrying its weight.
    #
    # THE QUESTION IS NOT GATED ON THE ARREST ROUTE, AND THE INSTRUCTION IS.
    # That asymmetry is the fix for a defect an INDEPENDENT REVIEW found in
    # Defect 1's first implementation, which gated both on
    # `not _routes_to_arrest_path(inputs)` together. Reproduced: ALERT +
    # breathing NONE + airway_obstruction UNESTABLISHED - the realistic FIRST
    # TURN for this presentation - made `_routes_to_arrest_path` answer True
    # (because the predicate then covered only the
    # ESTABLISHED-witnessed case and this patient's obstruction was unasked), skipped the whole block, and went straight to
    # INSTRUCT_CPR. A conscious complete-obstruction choker was therefore
    # denied thrusts and told to compress, and was never asked the one question
    # that would have found them - Blocker 5's death, reintroduced through
    # Defect 1's routing, in exactly the case where the obstruction question is
    # most valuable.
    #
    # The question must therefore win over the arrest route, because it is the
    # thing that DECIDES the arrest route: `ASK_AIRWAY_OBSTRUCTION`'s pinned
    # phrasing does double duty by design - it is the choking discriminator AND
    # it re-tests the alert report ("can he answer you?"). It is safe under
    # both hypotheses, which is what §5.1 requires: one turn, and the answer
    # routes to thrusts or to compressions on the next turn either way. The
    # INSTRUCTION below keeps the arrest check, so an established NONE
    # obstruction still falls through to the arrest path rather than looping.
    if not responsiveness.is_unconscious:
        if not path.airway_obstruction.is_established:
            return _branch(
                AssessmentStep.ASK_AIRWAY_OBSTRUCTION,
                Letter.A,
                # `_gate_floor`, not a pinned SEVERE. Now that this question can
                # stand in front of an established absent-breathing report (see
                # the asymmetry above), pinning SEVERE here would be Blocker
                # 3(b) exactly: a gate LOWERING the criticality of a reported
                # arrest. The step is the question's; the category is not the
                # question's to cap. `_is_arrest` excludes established-and-awake
                # for ABNORMAL_OR_GASPING, so the conscious asthmatic waiting
                # behind this question still asks for SEVERE and only the
                # apnoeic patient lifts it to CRITICAL.
                _gate_floor(path),
                "The patient is responsive, so a mechanical airway obstruction "
                "is still treatable by hand and must be excluded before the "
                "breathing question - a choking patient makes noise and moves "
                "their chest, so 'is he breathing' collects a useless yes. Ask "
                "whether somebody SAW this start: eating or drinking, then "
                "clutching the throat, unable to speak or cough properly. A "
                "collapse nobody witnessed is not a choking presentation. "
                "ASK IN THE SAME BREATH WHETHER THE PATIENT IS TALKING TO THEM "
                "RIGHT NOW, and pin that phrasing: 'can he answer you?'. It "
                "does two jobs. It is the choking discriminator, and it "
                "re-tests the report that this patient is awake at all - a "
                "caller who described a posturing or agonal casualty as alert "
                "corrects themselves on that question without ever being told "
                "they were wrong, and a genuinely alert patient talks."
                + _bleeding_while_asking_clause(path),
                assumed=path.assumed,
            )
        if (
            path.airway_obstruction.value
            is AirwayObstruction.WITNESSED_FOREIGN_BODY
        ):
            # BLOCKER 1 OF THE THIRD CLINICAL REVIEW. AN AWAKE CHOKER WHO IS
            # ALSO BLEEDING CATASTROPHICALLY GETS BOTH, IN ONE INSTRUCTION.
            #
            # This combination used to be taken by the pre-A bleed hoist, whose
            # rationale never mentioned the airway and told the bystander to
            # keep both hands on the wound while a conscious person with a
            # lodged bolus and no air movement arrested in front of them. The
            # hoist now stands aside for it (`_airway_outranks_the_bleed_hoist`)
            # and the bleed arrives HERE rather than being dropped, because
            # dropping it would be the same defect with the other patient dying.
            #
            # REACHED ON EVERY BREATHING VALUE, not only an established NONE,
            # and an independent review is why. A WITNESSED foreign body is a
            # finding already in hand: with every input established there is no
            # question left to ask, so a bare bleed instruction would be an
            # ABSORBING STATE on a reported obstruction rather than a deferral
            # that a later turn repairs. See `_airway_outranks_the_bleed_hoist`
            # for the full argument and for the case that is deliberately left
            # with the hoist.
            #
            # Checked first, and CONDITIONAL rather than a suffix, because this
            # is a different instruction and not the same one with a sentence
            # added: the order of actions changes, the hands-free method has to
            # be named before the thrusts, and both transitions - loss of
            # consciousness AND the pressure failing - have to be carried.
            #
            # THE SHAPE IS `INSTRUCT_CONTROL_BLEEDING_THEN_CPR`'S, deliberately.
            # That leaf resolved this identical single-rescuer conflict for the
            # unconscious twin of this patient with a knee, body weight, or a
            # tight bandage or tourniquet over packing - pressure that holds
            # without hands. Back blows and thrusts need both hands, so the same
            # method makes the same conflict performable here, and the module
            # having already proved that is why a forced choice would not be an
            # honest answer.
            if (
                path.severe_bleeding.is_established
                and path.severe_bleeding.value is SevereBleeding.PRESENT
            ):
                return _branch(
                    AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION,
                    Letter.A,
                    # `_gate_floor`, not a pinned SEVERE, and for the same
                    # reason the choking question uses it: this patient's
                    # breathing may be an established NONE, and capping a
                    # reported arrest at SEVERE is Blocker 3(b).
                    _gate_floor(path),
                    "Witnessed foreign-body airway obstruction in a patient who "
                    "is still responsive, WITH catastrophic external "
                    "haemorrhage. Both, and in this order - the choice is not "
                    "forced, because the pressure does not need your hands. Get "
                    "pressure onto the wound and KEEP it there without using "
                    "your hands - your knee, your weight, or a tight bandage or "
                    "tourniquet over packing - and then give back blows and "
                    "abdominal thrusts, which need both of your hands free. "
                    "Compressions cannot shift a lodged bolus, so the thrusts "
                    "are the intervention with the survival curve. Do both; do "
                    "not choose. If a second person is there, one holds the "
                    "wound and the other does the thrusts. If they go "
                    "UNCONSCIOUS, stop the thrusts, lower them down and start "
                    "chest compressions WITHOUT letting the pressure off the "
                    "wound - report that change immediately, because it changes "
                    "the whole instruction. If the bleeding breaks through, put "
                    "your weight back on it and say so."
                    + _hazard_suffix(scene),
                    assumed=path.assumed,
                    # The airway is what is being treated and the bleed is
                    # outstanding underneath it, so C is what remains unresolved
                    # - the same reading the hoist gives and the reason a later
                    # turn still comes back to it.
                    unresolved=Letter.C,
                )
            return _branch(
                AssessmentStep.INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION,
                Letter.A,
                Criticality.SEVERE,
                "Witnessed foreign-body airway obstruction in a patient who is "
                "still responsive. Back blows and abdominal thrusts now: "
                "compressions cannot shift a lodged bolus, and this is the "
                "intervention with the survival curve. If they go UNCONSCIOUS, "
                "stop the thrusts, lower them down and start chest "
                "compressions - report that change immediately, because it "
                "changes the whole instruction. This protocol applies only "
                "while they are still responsive."
                + _hazard_suffix(scene),
                # Blocker 2's mechanism, not a list - see `_Consulted`.
                assumed=path.assumed,
            )

    if not path.breathing.is_established:
        # BLOCKER 4, and this is the exact case §5.1 was amended for. Breathing
        # is unestablished, so the CATEGORY takes the worse branch - SEVERE, a
        # clinician is worth summoning - while the INSTRUCTION does not jump to
        # CPR. The step is a QUESTION, and it is the three-state question: the
        # intervention safe under both hypotheses is to find out which hypothesis
        # holds, and it costs one turn.
        #
        # Compare the alternative the blanket rule would have given: assume the
        # worst, instruct compressions on a patient who is breathing normally.
        # That is a physical instruction chosen on no evidence.
        return _branch(
            AssessmentStep.ASK_BREATHING_QUALITY,
            Letter.B,
            Criticality.SEVERE,
            "Breathing quality is not established. Ask about NORMAL, regular "
            "breathing and probe explicitly for occasional, noisy or gasping "
            "breaths - a bare yes/no question is answered 'yes' for agonal "
            "gasps.",
            assumed=path.assumed,
            unresolved=Letter.B,
        )

    breathing = path.breathing.value
    assert breathing is not None  # narrowed by is_established above

    if breathing.routes_to_cpr(responsiveness):
        # BLOCKER 1, BLOCKER 2, BLOCKER 3, all one branch.
        #
        # Both non-normal breathing states arrive here, for an UNCONSCIOUS
        # patient - the conjunction `CLINICAL-STANDARDS.md` §1.2 states and
        # `routes_to_cpr` now enforces by signature. There is no pulse check
        # between this line and the instruction, and no leaf below it that
        # withholds resuscitation: `_arrest_branch` cannot return anything but
        # Criticality 5 plus compressions. Whatever `inputs.pulse` says.
        return _arrest_branch(path, breathing, scene)

    if not breathing.is_normal:
        # AWAKE, AND BREATHING BADLY. The patient is A, C or V on ACVPU - so
        # they have a circulation by definition, because they are perfusing
        # their brain well enough to respond - and their breathing is abnormal
        # or gasping. Obstruction has already been excluded above, or we would
        # not be here.
        #
        # This leaf exists because the alternative reached `_arrest_branch` and
        # told an awake, talking patient to receive chest compressions. The
        # conscious asthmatic, the anaphylaxis, the pulmonary oedema and the
        # partial obstruction are all here, all common, and all made WORSE by
        # being laid flat and compressed.
        #
        # SEVERE, not CRITICAL, and the distinction is load-bearing: they are
        # awake. The ratchet lets this rise the moment they stop being, and the
        # instruction's own closing clause is what makes that transition get
        # reported.
        # DEFECT 4 OF THE RE-REVIEW: THE POSITIONING IMPERATIVE.
        #
        # This leaf said "Sit them upright... and do NOT lay them flat",
        # unconditionally and in capitals, while `protocols.py:anaphylaxis` says
        # "Lay them flat with legs raised; sit them up only if breathing is
        # hard." Both are defensible - flat-with-legs-raised exists because
        # anaphylaxis patients who sit or stand up arrest, the empty-ventricle
        # phenomenon - but L2 outranks retrieval, so the unconditional
        # imperative won and took out the one positioning rule that has killed
        # people.
        #
        # FOUR PRESENTATIONS REACH THIS LEAF and the fix has to be right for
        # all four: the conscious asthmatic, anaphylaxis, pulmonary oedema and
        # a penetrating chest injury. L2 cannot distinguish them - it holds no
        # input that would - so the resolution cannot be a per-condition rule.
        #
        # WHAT WAS DECIDED, and why not the other two options:
        #
        # - NOT a new input to distinguish anaphylaxis. It would cost a question
        #   in the golden window whose answer does not change THIS leaf's
        #   action: every patient here has hard breathing by construction, and
        #   the corpus's own rule sits them up in exactly that case. It would
        #   only change what happens AFTER deterioration, which is a transition
        #   and already routes elsewhere.
        # - NOT deferring positioning wholesale to the protocol. "Find the
        #   position you can breathe in" is the one positioning statement that
        #   is correct for all four AND needed in the next ten seconds; handing
        #   it to retrieval at 64% top-1 is the Blocker 4 failure mode.
        # - CONDITIONAL, which is what this is. The imperative becomes a
        #   position of COMFORT rather than a commanded posture, the capitalised
        #   prohibition on lying flat is GONE, and the real content of the
        #   corpus rule is restored in the form that is true for every
        #   presentation here: the danger is being sat or stood up once they go
        #   pale, grey or faint, and that applies to the shocky stab wound just
        #   as much as to anaphylaxis. Positioning detail beyond that is left to
        #   the cited protocol, which is where the per-condition wording lives.
        #
        # The auto-injector wording also changes. "If they have one" versus the
        # corpus's "ask for an auto-injector and check pockets and bags" is the
        # same defect in miniature - a passive clause where the corpus is
        # active - so this now tells them to look.
        return _branch(
            AssessmentStep.INSTRUCT_SUPPORT_SEVERE_BREATHING_DIFFICULTY,
            Letter.B,
            Criticality.SEVERE,
            "Awake but breathing badly, with a foreign body excluded. Do NOT "
            "start compressions: they have a circulation, and compressions on "
            "an awake patient in respiratory distress harm them. Let them take "
            "the position they can breathe in and support them there - most "
            "people in this state choose to sit up and lean forward, and that "
            "is usually the one that helps. Do not force them flat while they "
            "are fighting for air, and do not make them stand or walk. IF THEY "
            "GO PALE, GREY, CLAMMY OR FAINT, sitting or standing them up is "
            "dangerous in its own right - lower them back down and raise their "
            "legs, and follow the positioning in the protocol for what is "
            "actually causing this, which is where the per-condition rule "
            "lives. If this could be a severe allergic reaction, ASK FOR AN "
            "ADRENALINE AUTO-INJECTOR AND GET THEM TO CHECK POCKETS AND BAGS "
            "for it - theirs, not somebody else's - and help them use their "
            "own reliever inhaler if they have one. Loosen anything tight at "
            "the neck, keep them calm, and stay with them. THE THING TO WATCH "
            "FOR: if they stop being able to talk, go limp, or stop "
            "responding, that is a different emergency - lower them down and "
            "start chest compressions, and report the change immediately."
            + _hazard_suffix(scene),
            # DEFECT 2 OF THE RE-REVIEW: THE PROVENANCE CONTRACT.
            #
            # This was the ONLY instruction leaf calling neither
            # `inputs.assumed(...)` nor passing `assumed=`. Reached with
            # `scene_safe` at ASSUMED_WORST it reported provenance=EVIDENCE and
            # assumed_inputs=(), which made its Criticality 4 UNCORRECTABLE by
            # later evidence - the exact inversion ASM-14 exists to prevent, on
            # the leaf whose patient is most likely to change state.
            #
            # All four inputs on the path here, not just the ones this leaf
            # reads last: the level was reached partly because each of them held
            # the value it did, so an assumption in any of them is an assumption
            # in the branch. A walk-level test now asserts EVERY leaf records
            # its assumptions, so this cannot recur one leaf at a time.
            assumed=path.assumed,
            unresolved=Letter.B,
        )

    # Breathing is NORMAL from here down. Every branch below is a patient with a
    # patent airway and spontaneous respiration.

    # ---- C: catastrophic haemorrhage.
    if not path.severe_bleeding.is_established:
        return _branch(
            AssessmentStep.ASK_SEVERE_BLEEDING,
            Letter.C,
            Criticality.SEVERE,
            "Severe external bleeding is not established. It is the one "
            "circulation finding a bystander can both see and treat.",
            assumed=path.assumed,
            unresolved=Letter.C,
        )
    # An established PRESENT bleed has already been treated by the pre-A gate
    # above, so the only value that reaches here is NONE. Left unasserted rather
    # than re-tested: a second condition on the same finding in a second place
    # is the DRY failure the hoist exists to avoid.

    # ---- D: disability. ACVPU is already established as responsiveness.
    if responsiveness.is_unconscious:
        # Breathing normally, unconscious, no catastrophic bleed. The single most
        # valuable thing a lone bystander does - and the clinical review found it
        # absent from every algorithm terminal state.
        #
        # BLOCKER 4. The spinal caveat's WORDING stays in
        # `protocols.py:airway-recovery-position` - one home for one rule - but
        # the caveat is a CONDITIONAL and its condition is an input, so L2 has
        # to hold it. Without it the unconscious motorcyclist and the
        # unconscious fainter got one identical unconditional imperative, and
        # whether the caveat reached speech depended on retrieval landing the
        # right document at 64% top-1.
        if not path.spinal_risk.is_established:
            return _branch(
                AssessmentStep.ASK_SPINAL_RISK,
                Letter.D,
                Criticality.SEVERE,
                "Unconscious and breathing normally, so the airway must be "
                "protected - but the TECHNIQUE depends on the mechanism, and "
                "only one of the two is safe to improvise. Ask what happened: "
                "a fall from a height, off a bike or a horse, a dive into "
                "water, a vehicle crash, or a blow to the head or neck.",
                assumed=path.assumed,
                unresolved=Letter.D,
            )
        if path.spinal_risk.value is SpinalRisk.SUSPECTED:
            # Airway still beats spine, and both leaves protect the airway. What
            # differs is which technique is NAMED: a full roll by one untrained
            # person on an unstable C-spine is the improvisation to avoid, and a
            # jaw thrust with manual in-line stabilisation is the one that is not.
            return _branch(
                AssessmentStep.INSTRUCT_AIRWAY_WITH_SPINAL_CARE,
                Letter.D,
                Criticality.SEVERE,
                "Unconscious, breathing normally, and a mechanism that "
                "suspects a spinal injury. Do NOT roll them. Kneel at the "
                "head, hold it steady in line with the body with both hands, "
                "and open the airway with a jaw thrust rather than a head "
                "tilt. The airway still comes first: if it cannot be kept "
                "clear this way - vomit, or breathing that stops being normal "
                "- then turn them, keeping the head, neck and spine in one "
                "line, because an airway lost outranks a spine protected."
                + _hazard_suffix(scene),
                # Blocker 2's mechanism, not a list - see `_Consulted`.
                assumed=path.assumed,
                unresolved=Letter.D,
            )
        return _branch(
            AssessmentStep.INSTRUCT_RECOVERY_POSITION,
            Letter.D,
            Criticality.SEVERE,
            "Unconscious and breathing normally, with no mechanism suggesting "
            "a spinal injury. Recovery position to protect the airway, then "
            "stay and watch the breathing."
            + _hazard_suffix(scene),
            # Blocker 2's mechanism, not a list - see `_Consulted`.
            assumed=path.assumed,
            unresolved=Letter.D,
        )

    # ---- E: exposure.
    if not path.exposure_reviewed.is_established:
        return _branch(
            AssessmentStep.ASK_EXPOSURE_FINDINGS,
            Letter.E,
            _conscious_floor(responsiveness),
            "A to D are resolved. Look for what has been missed, and keep the "
            "patient warm.",
            assumed=path.assumed,
            unresolved=Letter.E,
        )

    return _branch(
        AssessmentStep.INSTRUCT_MONITOR,
        Letter.E,
        _conscious_floor(responsiveness),
        "Primary survey complete and no life threat outstanding. Keep watching "
        "breathing and responsiveness, and reassess from A on any change."
        + _hazard_suffix(scene),
        # Blocker 2's mechanism, not a list - see `_Consulted`. This leaf
        # matters most: it is the terminal "nothing found" branch, so a LOW or
        # MODERATE reached partly by assumption is exactly the level a
        # clinician needs to be able to raise later.
        assumed=path.assumed,
        unresolved=Letter.E,
    )


def _arrest_branch(
    path: _Consulted, breathing: Breathing, scene: SceneSafety
) -> AssessmentBranch:
    """Cardiac arrest. Criticality 5 and compressions, always.

    BLOCKER 1 (of the FIRST clinical review) is this function's entire shape.
    There is no parameter by which it can decline to resuscitate, no leaf
    returning anything below CRITICAL, and no expression here that reads
    `inputs.pulse`. A single-patient incident has no rationing decision to make,
    so an apneic patient is a patient in arrest and not a patient to walk past.

    The choice is compressions-first, five-rescue-breaths-first, or
    bleeding-control-plus-compressions - which are choices about HYPOXIC versus
    PRIMARY CARDIAC versus TRAUMATIC arrest, and not about whether to
    resuscitate. Every leaf resuscitates.

    `scene` is passed rather than re-read from `inputs` because `decide` has
    already narrowed it, and re-deriving a narrowed value is where the two
    copies drift.

    READS `airway_obstruction`. The re-review's third defect: this function
    never read it, so UNRESPONSIVE + NONE + WITNESSED_FOREIGN_BODY returned a
    plain INSTRUCT_CPR whose rationale never mentioned the mouth or the object.
    That is Blocker 2's defect shape exactly - an established finding the arrest
    path did not read - fixed for bleeding and not for choking. A bystander who
    watched food go in and the patient collapse was never told to look. See
    `_mouth_check_clause`.

    CARRIES THE RESPONSIVENESS CONTRADICTION. The re-review's lead defect: an
    established-awake reading alongside absent breathing now reaches here rather
    than the conscious-distress leaf (see `Breathing.routes_to_cpr`), and the
    contradiction is re-tested in the rationale instead of being resolved
    silently. See `_contradiction_clause`.
    """
    # Hypoxic arrest - submersion, or a child - gets the five breaths first.
    # `CLINICAL-STANDARDS.md` §3.2: an apneic child is more likely to have a
    # primary respiratory problem, and `protocols.py:drowning-rescue` already
    # holds the five-breaths-first sequence for submersion.
    #
    # BLOCKER 2 / ASM-12: unconditional. The published algorithm gates these on
    # a palpated pulse and that gate is gone. Both leaves below start
    # resuscitation, so the removed check could not have changed the outcome
    # even if a bystander could perform it.
    # The two conditional clauses every leaf below carries, computed once. Same
    # shape as `_hazard_suffix`: a rule that applies to every arrest leaf is
    # computed in one place, because a per-leaf rule enforced by the author's
    # memory is a rule the next leaf omits - which is how Defect 3 happened.
    mouth = _mouth_check_clause(path)
    contradiction = _contradiction_clause(path, breathing)

    band = (
        path.age_band.value.resolved
        if path.age_band.value is not None
        # ASM-08: uncertainty resolves to the ADULT algorithm. Unestablished is
        # at least as uncertain as an explicit UNCERTAIN, so it resolves the
        # same way.
        else AgeBand.ADULT
    )
    hypoxic = band is AgeBand.CHILD or path.submersion.value is True


    # BLOCKER 2. A BLEEDING ARREST IS TOLD TO STOP THE BLOOD *AND* COMPRESS.
    #
    # This function read `severe_bleeding` nowhere, so ABNORMAL_OR_GASPING plus
    # an established PRESENT bleed returned bare INSTRUCT_CPR and
    # INSTRUCT_CONTROL_BLEEDING was unreachable from here. The bystander got
    # compressions-only guidance with no mention of the blood.
    #
    # "Arrest outranks a limb bleed" is right as a PRIORITY and wrong for
    # TRAUMATIC arrest: haemorrhage control is PART OF the resuscitation, not a
    # later letter, and compressions on an uncontrolled arterial bleed pump the
    # remaining circulating volume onto the road. It is the one situation where
    # CPR is futile without controlling the source.
    #
    # The single-rescuer conflict is genuine - one person cannot pack a wound
    # and compress at once - and that is exactly why it is resolved HERE rather
    # than left for the LLM to improvise. Silence is the worst available option.
    # The resolution is a combined instruction that names the order and the
    # hands-free method: weight or a knee on the wound, which holds pressure
    # while both hands compress.
    #
    # Checked before the hypoxic split, and checked FIRST, because a bleeding
    # arrest is a bleeding arrest whether it is a drowned child or an adult
    # under a car. The bleeding and the breaths are not alternatives: the
    # rationale carries the breath count too when the presentation is hypoxic,
    # so nothing the hypoxic leaf provides is lost by arriving here.
    #
    # Criticality stays CRITICAL and the step is still a resuscitation step, so
    # ASM-11's "apneic plus pulseless is always 5 plus CPR" and the no-pulse-gate
    # property are untouched: this leaf resuscitates, like every other leaf here.
    if (
        path.severe_bleeding.is_established
        and path.severe_bleeding.value is SevereBleeding.PRESENT
    ):
        breaths = (
            f"Give {RESCUE_BREATHS_BEFORE_COMPRESSIONS} rescue breaths first, "
            "then compressions. "
            if hypoxic
            else ""
        )
        return _branch(
            AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR,
            Letter.B,
            Criticality.CRITICAL,
            f"{breathing.value} breathing WITH catastrophic external "
            "haemorrhage - a traumatic arrest, where controlling the bleeding "
            "is part of the resuscitation and not a later letter. "
            "Compressions on an uncontrolled arterial bleed pump out what "
            "volume is left. Get pressure onto the wound and KEEP it there "
            "without using your hands - your knee, your weight, or a tight "
            "bandage or tourniquet over packing - and then start chest "
            f"compressions. {breaths}Do both; do not choose. If a second "
            "person is there, one holds the wound and the other compresses. "
            "No pulse check."
            + mouth
            + contradiction
            + _hazard_suffix(scene),
            assumed=path.assumed,
            unresolved=Letter.B,
        )

    if hypoxic:
        return _branch(
            AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR,
            Letter.B,
            Criticality.CRITICAL,
            f"{breathing.value} breathing in a hypoxic-arrest presentation. "
            f"{RESCUE_BREATHS_BEFORE_COMPRESSIONS} rescue breaths, then chest "
            "compressions. The breaths are unconditional: no pulse check."
            + mouth
            + contradiction
            + _hazard_suffix(scene),
            assumed=path.assumed,
            unresolved=Letter.B,
        )

    return _branch(
        AssessmentStep.INSTRUCT_CPR,
        Letter.B,
        Criticality.CRITICAL,
        f"{breathing.value} breathing. Start chest compressions immediately. "
        "Not breathing normally is the whole indication: no pulse check."
        + mouth
        + contradiction
        + _hazard_suffix(scene),
        assumed=path.assumed,
        unresolved=Letter.B,
    )


def _is_arrest(path: _Consulted) -> bool:
    """Whether an arrest is already ESTABLISHED from what we hold.

    Deliberately conservative in the other direction from everything else in
    this module: it answers True only on an established non-normal breathing
    value, never on an unknown one. An UNKNOWN breathing finding is not an
    arrest, it is an unanswered question, and treating it as an arrest here
    would let the bleeding hoist be skipped on no evidence.

    Reads `breathing` through `is_normal` rather than comparing members, so a
    fourth breathing state added later is classified by the type rather than by
    this function's memory of the enum.

    Deliberately does NOT require an established UNRESPONSIVE reading, unlike
    `routes_to_cpr`. The two answer different questions and the asymmetry is
    intentional. `routes_to_cpr` decides whether to put hands on a chest, so it
    demands the full conjunction. This function decides whether the CATEGORY
    should be CRITICAL while a gate holds, and whether the bleed hoist should
    stand aside for the arrest path - both of which are worse-branch decisions
    about a category, where an unestablished consciousness must not be allowed
    to lower a reported arrest (`ARCHITECTURE-ASSESSMENT.md` §5.1 as amended:
    worse branch for the category, safe-under-both for the instruction).

    So an ALERT patient breathing ABNORMALLY is not an arrest here either - the
    conscious-distress leaf owns them - but an unknown consciousness with absent
    breathing is treated as one for categorisation, and after Defect 1's fix so
    is an ESTABLISHED-AWAKE reading with absent breathing, because that pair is
    a contradiction in which apnoea wins on both the instruction and the
    category. See the body for why the witnessed-obstruction case is not
    excluded from that.
    """
    if not path.breathing.is_established or path.breathing.value is None:
        return False
    if path.breathing.value.is_normal:
        return False
    # ABSENT BREATHING IS AN ARREST FOR THE CATEGORY WHATEVER WAS REPORTED
    # ABOUT RESPONSIVENESS, matching `routes_to_cpr` after Defect 1's fix.
    #
    # Without this the gate in front of the choking question LOWERED a reported
    # arrest to SEVERE: an ALERT report plus absent breathing is a contradiction
    # in which apnoea wins the INSTRUCTION (see `Breathing.routes_to_cpr`), so
    # letting the reassuring half win the CATEGORY would be the same defect
    # wearing the other hat, and Blocker 3(b) is explicit that a gate must not
    # cap the criticality of a possible arrest waiting behind it.
    #
    # The witnessed-obstruction case is deliberately NOT excluded here. A
    # conscious complete obstruction is minutes from being an arrest, so
    # CRITICAL is the right category even while the INSTRUCTION is thrusts -
    # which is precisely the category/instruction split §5.1 draws.
    if path.breathing.value is Breathing.NONE:
        return True
    responsiveness = path.responsiveness.value
    # For ABNORMAL_OR_GASPING, established-and-awake is the one reading that
    # rules an arrest out - the conscious-distress leaf owns that patient.
    return responsiveness is None or responsiveness.is_unconscious


def _routes_to_arrest_path(path: _Consulted) -> bool:
    """Whether `decide` will reach `_arrest_branch` on THIS turn.

    Distinct from `_is_arrest`, and the difference is the whole reason both
    exist. `_is_arrest` answers a question about the CATEGORY and counts an
    unknown consciousness as an arrest, so a gate cannot lower a reported one.
    This answers a question about CONTROL FLOW - will the arrest path actually
    be taken now - and therefore requires the full conjunction that
    `routes_to_cpr` requires, because that is the condition the code below
    really tests.

    The bleed hoist needs THIS one. Using `_is_arrest` there left a hole found
    by the exhaustive walk: an established bleed plus abnormal breathing plus an
    UNKNOWN consciousness stood the hoist aside as "an arrest", then asked the
    responsiveness question - deferring the established bleed behind a question,
    which is the exact defect the hoist exists to close. With this predicate the
    bleed is treated on that turn, which is safe under both hypotheses: pressure
    on a wound is right whether or not the patient turns out to be in arrest,
    and the consciousness answer arrives on the next turn and re-routes to the
    combined leaf if it needs to.

    ACCOUNTS FOR THE CHOKING BLOCK, which sits between the bleed hoist and the
    arrest path. Defect 1's fix made absent breathing route to CPR regardless of
    the reported responsiveness, and that made this predicate briefly LIE: it
    answered True for a responsive patient with an established witnessed foreign
    body, who actually reaches the choking instruction and not `_arrest_branch`.
    The bleed hoist then stood aside for an arrest path that was never taken,
    and the established bleed was deferred behind the choking instruction -
    caught by the established-bleed walk, which is now the second time that walk
    has found a hole in THIS predicate rather than in the code it guards.

    This function answers a question about CONTROL FLOW, so it has to model the
    control flow it is named for. `_is_arrest` is the one that answers about the
    category and is deliberately blunter; the difference between them is the
    whole reason both exist, and this is the second thing that lives only here.
    """
    if _reaches_choking_block(path):
        return False
    return (
        path.breathing.is_established
        and path.breathing.value is not None
        and path.responsiveness.value is not None
        and path.breathing.value.routes_to_cpr(path.responsiveness.value)
    )


def _reaches_choking_block(path: _Consulted) -> bool:
    """Whether the choking block will own this patient on THIS turn.

    One predicate, named once, consulted by both the choking block itself and
    `_routes_to_arrest_path`. Written out at either site instead, the two
    readings drifted apart the moment Defect 1's fix landed - which is how an
    established bleed ended up deferred behind the choking instruction, twice.

    TRUE FOR A RESPONSIVE PATIENT WHOSE OBSTRUCTION IS EITHER AN ESTABLISHED
    WITNESSED FOREIGN BODY OR NOT ESTABLISHED AT ALL, because the block owns
    both: the instruction in the first case, the question in the second. An
    earlier version covered only the instruction, and an independent review
    found the hole - a first-turn ALERT-plus-absent-breathing patient skipped
    the block entirely and was never asked the choking discriminator.

    The established-witnessed case is the one reading which RECONCILES an awake
    report with an absent-breathing report rather than contradicting it, because
    a complete obstruction genuinely moves no air while the patient is still
    conscious - see `Breathing.routes_to_cpr`. The unestablished case is the
    question that decides which of those two worlds we are in, and it is worth
    one turn because its pinned phrasing also re-tests the awake report itself.
    """
    responsiveness = path.responsiveness.value
    if responsiveness is None or responsiveness.is_unconscious:
        return False
    if not path.airway_obstruction.is_established:
        # The block asks the question.
        return True
    # The block instructs only on a witnessed obstruction; an established NONE
    # falls through to the breathing question and the arrest path.
    return (
        path.airway_obstruction.value is AirwayObstruction.WITNESSED_FOREIGN_BODY
    )


def _airway_outranks_the_bleed_hoist(path: _Consulted) -> bool:
    """Whether the choking block takes this patient ahead of the bleed hoist.

    BLOCKER 1 OF THE THIRD CLINICAL REVIEW. The hoist's recorded justification
    is that a responsive patient is still moving some air, so the airway can
    wait one turn while the bleeding is treated. That is sound while breathing
    is UNESTABLISHED - and false once breathing is ESTABLISHED as NONE, which is
    the reading that says in so many words that this patient is moving no air at
    all. Applying the judgement there was applying it outside its own premise,
    and it produced a bleed instruction that never mentioned the airway on a
    conscious patient with a lodged bolus.

    TRUE ON TWO READINGS, AND THE TEST IS NOT THE BREATHING VALUE. It is whether
    deferring the airway for a turn can still be repaired by a later turn. The
    first version of this fix used `breathing is ESTABLISHED NONE` alone, and an
    INDEPENDENT REVIEW found the hole that left - which is recorded here because
    the hole was the same defect one step to the left, and the narrow test is
    what hid it.

    (1) AN ESTABLISHED WITNESSED FOREIGN BODY, whatever the breathing value. The
        reviewer reproduced ALERT + ABNORMAL_OR_GASPING + WITNESSED_FOREIGN_BODY
        + bleeding PRESENT returning the bare bleed instruction, whose rationale
        never mentions the obstruction the caller has already reported. Every
        input in that cell is ESTABLISHED, so there is no question left to ask
        and nothing new can arrive: `decide` is pure, so the same facts return
        the same bare instruction forever. That is an ABSORBING STATE on a known
        obstruction - Blocker 3(a)'s shape, which this module has already killed
        twice - and it is worse than the NONE case in one respect, because the
        machine is not waiting to find out. It knows, and says nothing.

        Air movement past a partial obstruction is not evidence the obstruction
        is benign, and a witnessed foreign body with gasping is the trajectory
        INTO complete obstruction that the choking block exists to intercept
        early. The premise the hoist rests on - "a responsive patient is still
        moving some air, so the airway can wait" - answers a question about
        whether the airway is patent ENOUGH FOR NOW; it does not license
        discarding a reported obstruction outright.

    (2) BREATHING ESTABLISHED AS NONE, which is the originally reported cell and
        covers the case where the obstruction has NOT been asked about. Here the
        hoist's premise is not merely strained but false: the reading says in so
        many words that this patient is moving no air at all. The choking
        question is worth the turn because it is what decides thrusts versus
        compressions, and its pinned phrasing also re-tests the awake report.

    WHAT IS DELIBERATELY NOT COVERED: an UNESTABLISHED obstruction with any
    breathing value other than NONE. Nothing has reported an obstruction and
    nothing has reported that the air has stopped, so the hoist's premise is
    intact, the bleed is treated first, and the round-2 judgement stands exactly
    as recorded. Treating an unasked question as an apnoea report would let the
    hoist be skipped on no evidence, the same error `_is_arrest` refuses to
    make.

    AND THE HONEST VERSION OF WHY THAT CASE DIFFERS FROM (1), because the first
    draft of this docstring overstated it. It said the bleed instruction "is
    followed by the discriminator on a later turn", which is NOT true unaided:
    there is no "bleeding controlled" input (a recorded gap in the module
    docstring), so nothing the responder reports about the wound re-routes this
    patient, and L2 never asks the airway question while the hoist holds. The
    escape is therefore not automatic - it is that a CALLER REPORT reaches the
    airway, and both reports that would do it are named in the hoist's own
    rationale, which tells the responder to say so immediately if the patient
    stops being able to breathe or speak, or if somebody saw something go into
    the airway. Either one lands in (2) or in (1) on the next turn, verified by
    execution. The difference from (1) is thus narrower than "absorbing versus
    not": in (1) the report has ALREADY ARRIVED and is being discarded, which no
    further report can repair, whereas here the information has not arrived and
    the instruction actively solicits it. That is a real difference and it is the
    one this predicate rests on - but it rests on the rationale's wording doing
    its job, which is weaker than a routing guarantee and is written down here
    so the next author does not read more safety into it than there is.

    `_reaches_choking_block` rather than a restated condition, so this cannot
    drift from the block it defers to - the same reason `_routes_to_arrest_path`
    consults it. It is ALSO what makes reading (1) as "an established
    obstruction" correct rather than sloppy: that predicate already answers
    False for an established NONE, so the only established value that survives
    it is WITNESSED_FOREIGN_BODY. Naming the member again below would be dead
    logic reading like a live rule - mutation testing caught exactly that form
    here, widening the member test to any established value and changing no
    behaviour, which is the same finding the choking block itself records one
    block up.

    THIS DOES NOT DROP THE BLEED, and that distinction is why the reviewer
    rejected the narrower fix. Standing the hoist aside is only half: the
    choking block carries the haemorrhage into its own instruction, exactly as
    `_arrest_branch` carries it into `INSTRUCT_CONTROL_BLEEDING_THEN_CPR`.
    Blocker 2's rule is unchanged - do both, do not choose - and it is
    performable because hands-free pressure frees the hands that thrusts need.
    """
    if not _reaches_choking_block(path):
        return False
    if path.airway_obstruction.is_established:
        # (1) A finding already in hand - and past `_reaches_choking_block` the
        # only established value it can be is WITNESSED_FOREIGN_BODY. Nothing
        # later can add to it, so deferring it defers it forever.
        return True
    # (2) No obstruction reported yet, so only an established apnoea overrides
    # the hoist - and the block's answer is the question, not an instruction.
    return (
        path.breathing.is_established
        and path.breathing.value is Breathing.NONE
    )


def _gate_floor(path: _Consulted) -> Criticality:
    """The criticality to ask for while a pre-A gate holds the assessment.

    BLOCKER 3(b), generalised. The scene gate was pinned at SEVERE, so a
    reported arrest waiting behind an unresolved scene asked for 4 rather than 5
    - the gate LOWERING the criticality of a known arrest. The step is rightly
    the gate's; the category is not the gate's to cap, and an arrest behind a
    hazard is still an arrest.

    Shared by BOTH pre-A gates rather than written twice. The bleed hoist had
    the identical defect the moment it was added - it pinned SEVERE while a
    possible arrest sat behind it - and it was caught by the arrest walk rather
    than by inspection. One function means the next pre-A gate cannot
    reintroduce it by copying the wrong line.

    SEVERE remains the floor: the responder is at an incident with an
    unresolved life threat, which is worth a clinician whatever else is true.
    """
    if _is_arrest(path):
        return Criticality.CRITICAL
    return Criticality.SEVERE


def _hazard_suffix(scene: SceneSafety) -> str:
    """The standing hazard warning carried by every post-gate instruction.

    BLOCKER 3(a). Advancing past a live hazard is not the same as the hazard
    being gone. The committed state advances - that is the whole point, because
    the absorbing loop killed the patient it was protecting - so every
    instruction downstream of it has to keep saying so. A responder who answered
    "no, cars are going past" is still working next to traffic three minutes
    later.

    Empty for a safe scene, so the common path carries no noise.

    BLOCKER 3 OF THE THIRD CLINICAL REVIEW: IT USED TO BE ONE CONSTANT STRING,
    AND THAT WAS WRONG IN TWO SEPARATE WAYS.

    WRONG IN CONTENT, which was the reachable harm. The constant said "keep them
    watching for it... get clear if it closes in" - a description of a hazard
    that MOVES TOWARD YOU. That is true of traffic and of fire and it is the
    opposite of the truth for a hazard that is in the patient, where nothing
    closes in and the danger is the contact the responder is about to make.
    Watching a cable does not help. This is now per-class, so each class gets a
    warning that is true of it, and `HazardClass` is what makes that expressible
    at all - the recorded gap said "L2 never holds WHICH hazard it was", and it
    does now, so that half of the gap is closed rather than re-recorded.

    WRONG IN FORM, which is the tune-out problem and is the half this addresses
    only partly - stated plainly rather than claimed as closed. Identical
    repetition on every downstream instruction is the definition of an alarm
    that gets tuned out, and per-class text does not fix repetition: a responder
    working beside traffic still gets the same adjacent-class sentence on turn
    two and on turn twenty, because L2 is pure and holds no turn history by
    construction (ASM-01), so there is no expression here for "say it
    differently the fourth time". What this change does deliver is that the
    sentence is ABOUT the hazard the responder actually reported, which is the
    difference between an alarm that is merely repetitive and one that is also
    irrelevant. The remaining half stays a recorded gap, re-weighted, and it
    needs a turn-aware layer that L2 is not.

    THE IN_PATIENT ARM IS DEFENCE-IN-DEPTH, NOT A LIVE DISCRIMINATOR, and it is
    recorded as such because a reader would otherwise take it for one. The
    in-the-patient gate returns before any instruction that touches the patient
    can be reached, so the only leaf that calls this with an IN_PATIENT scene is
    that gate's own. It is written anyway: a future routing change that let an
    in-the-patient scene reach a touch instruction would otherwise attach a
    traffic warning to compressions on an energised patient, which is precisely
    the defect this blocker is.
    """
    hazard = scene.hazard
    if hazard is None:
        return ""
    if hazard is HazardClass.IN_PATIENT:
        return (
            " THE HAZARD IS STILL IN THE PATIENT and nothing here is safe to do "
            "by hand until that changes: the danger is the CONTACT, not "
            "something approaching, so there is nothing useful to watch for and "
            "moving back is not the answer either. Restate that the supply must "
            "be off or the contact broken first, and ask them to say the moment "
            "it is."
        )
    if hazard is HazardClass.CONSUMING:
        return (
            " THE FIRE OR GAS AT THIS SCENE IS STILL BURNING AND HAS NOT BEEN "
            "RESOLVED, and this hazard gets worse with every second of exposure "
            "rather than staying the same: smoke and gas are a dose, so there "
            "is no position beside it that stays survivable. Restate it, and "
            "tell them the priority is getting the patient and themselves OUT "
            "to clear air - treatment continues there, not here."
        )
    return (
        " The hazard reported at this scene has NOT been resolved and the "
        "responder is working inside it: restate it, keep them watching for it, "
        "and tell them to get clear if it closes in."
    )


def _move_to_safety_rationale(scene: SceneSafety) -> str:
    """The move-to-safety instruction, per hazard class.

    FOUND BY THE INDEPENDENT CLINICAL REVIEW OF BLOCKER 3'S OWN FIX, and it is
    the same defect Blocker 3 was - fixed for one hazard class and left open on
    another, which is why it is worth recording rather than quietly patching.

    `_hazard_suffix` was made per-class, and `INSTRUCT_MAKE_SCENE_SAFE` was left
    with one constant string because that leaf deliberately carries no suffix -
    the reasoning being that the move-to-safety instruction IS the hazard
    warning. EXECUTION FALSIFIED THAT: the string named no hazard at all, so a
    caller in a smoke-filled room and a caller beside traffic were read
    byte-identical words, and every word of the CONSUMING class's clinical
    content - dose-over-time, no survivable standing position, get out to clear
    air - existed in `_hazard_suffix` and reached no caller, because the only
    step a consuming scene reaches is this one. Dead clinical content behind 96
    passing tests, which is this project's signature failure mode.

    AND THE CONSTANT STRING ACTIVELY INVITED AN ANSWER THE TYPE REFUSES. It
    offered "if they are ALREADY beside the patient and will not leave, that is
    a different answer and the assessment continues" - true for ADJACENT, which
    has a committed member, and false for CONSUMING and IN_PATIENT, which
    deliberately do not. So a fire caller was invited to say "I'm not leaving
    him" and the module had nothing to do with the answer but repeat itself.
    That is Blocker 3(a)'s absorbing loop rebuilt on fire with an invitation
    attached, so the escape is now offered only by the class that can honour it.

    Dispatches on `scene.hazard` like `_hazard_suffix`, so there is no new field
    and no extra slice on the ASM-09 walk. `IN_PATIENT` never reaches this leaf
    - the in-the-patient gate returns first - so its arm is defence-in-depth for
    the same reason `_hazard_suffix`'s is, and is written rather than omitted so
    a future routing change cannot hand an energised patient the traffic words.

    TWO PER-CLASS STRING TABLES, DELIBERATELY, AND THE DRY QUESTION WAS ASKED.
    An independent review noted that this and `_hazard_suffix` both dispatch on
    `scene.hazard` and both say something about each class, so a future clinical
    correction to "what fire requires" has two homes. They are kept separate
    because they are answers to two different questions and merging them would
    need a `context` parameter selecting which half to return, which is one
    function pretending to be two. This one is the INSTRUCTION for a responder
    who has not yet committed - what to DO about the hazard. `_hazard_suffix` is
    the standing REMINDER appended to instructions about the PATIENT, for a
    responder who has advanced past the hazard. A rule that belongs to both -
    which class is which, and whether a class may be advanced past - lives in
    `HazardClass` and `SceneSafety` and is consulted by both, so the part that
    is genuinely one rule does have one home.
    """
    hazard = scene.hazard
    if hazard is HazardClass.CONSUMING:
        return (
            "There is FIRE, SMOKE OR GAS at this scene and the responder is "
            "not yet committed to the patient's side. GET OUT, AND TAKE THE "
            "PATIENT IF THEY CAN BE MOVED - this hazard is not a risk that "
            "stays the same while you work, it is a DOSE that gets worse every "
            "second, so there is no position beside it that stays survivable "
            "and no amount of care that makes staying safe. Smoke kills far "
            "more people than flame does. Do not go in, and do not go back in "
            "for anything. Get to clear air, well away and upwind, and "
            "treatment continues there rather than here - being out is the "
            "treatment for now. If the patient cannot be moved, do NOT stay "
            "with them: say so, get clear, and let the fire service reach "
            "them, because two casualties in the smoke is how both die."
        )
    if hazard is HazardClass.IN_PATIENT:
        # Defence-in-depth: the in-the-patient gate returns before this leaf is
        # reachable. Written so a future routing change cannot fall through to
        # the traffic words on a patient who is still in circuit.
        return (
            "The hazard is IN THE PATIENT and the contact has not been broken. "
            "Do not make contact with them, and do not move them by hand. The "
            "supply has to be off or the contact broken first."
        )
    return (
        "The scene is unsafe and the responder is not yet committed to the "
        "patient's side. Move to safety before any patient contact. If "
        "they are ALREADY beside the patient and will not leave, that is a "
        "different answer and the assessment continues with a standing "
        "hazard warning."
    )


def _bleeding_while_asking_clause(path: _Consulted) -> str:
    """The keep-the-pressure-on clause for the choking QUESTION.

    BLOCKER 1 OF THE THIRD CLINICAL REVIEW, second half. The hoist now stands
    aside for a responsive patient with an established NONE breathing report and
    an obstruction that has not been asked about yet, because that question is
    what decides between thrusts and compressions and its pinned phrasing also
    re-tests the awake report. That precedence is the previous round's and is
    right - but standing the hoist aside would otherwise leave the established
    haemorrhage unmentioned for the turn the question costs, which is the exact
    deferral the hoist exists to prevent.

    So the bleed is not deferred, it is carried: the question is asked WITH the
    pressure already on the wound, hands-free, which is the same resolution
    `INSTRUCT_CONTROL_BLEEDING_THEN_CPR` and the combined choking leaf use. One
    turn, nothing dropped, and no forced choice.

    Returns "" unless the bleed is an established PRESENT, so the ordinary
    choking question carries no extra words. Same shape as `_hazard_suffix` and
    `_mouth_check_clause`, which is this module's existing answer to a
    conditional clause on a rationale.
    """
    if not path.severe_bleeding.is_established:
        return ""
    if path.severe_bleeding.value is not SevereBleeding.PRESENT:
        return ""
    return (
        " AND THERE IS CATASTROPHIC BLEEDING, WHICH IS NOT WAITING FOR THE "
        "ANSWER. Get pressure onto the wound and KEEP it there without using "
        "your hands - your knee, your weight, or a tight bandage or tourniquet "
        "over packing - so that both hands are free the moment the answer comes "
        "back, and ask while that pressure is already on."
    )


def _mouth_check_clause(path: _Consulted) -> str:
    """The look-in-the-mouth step, for an arrest with a WITNESSED foreign body.

    DEFECT 3 of the re-review. `_arrest_branch` never read `airway_obstruction`,
    so an unconscious choking victim got a plain compressions instruction whose
    rationale never mentioned the mouth or the object. Blocker 2's defect shape
    exactly - an established finding the arrest path did not read - closed for
    bleeding and left open for choking.

    Current guidance for an unconscious choking victim is compressions PLUS
    checking the mouth for a now-displaced visible object before ventilations,
    removing it with fingers only if it can be SEEN. The compressions themselves
    raise intrathoracic pressure and often shift the bolus up where it becomes
    visible and retrievable, which is why the check belongs after them and why
    it is worth a sentence at all.

    DOES NOT COACH A BLIND FINGER SWEEP, and the distinction is the whole
    clinical content of this clause. A blind sweep pushes an unseen obstruction
    deeper, can impact it in the larynx, and injures the soft palate of a
    patient who may not have had an obstruction at all. "Only if you can SEE it"
    is therefore stated as a precondition rather than as advice.

    Scoped to WITNESSED_FOREIGN_BODY. An unwitnessed collapse is not a choking
    presentation - `AirwayObstruction` says why in both directions - and adding
    a mouth-check sentence to every arrest would spend the one thing this
    instruction has, which is the responder's attention on compressions.

    Returns "" for every other case, so the common arrest path carries no extra
    words. Same shape as `_hazard_suffix`, which is the module's existing answer
    to "a conditional clause appended to several leaves".
    """
    if not path.airway_obstruction.is_established:
        return ""
    if path.airway_obstruction.value is not AirwayObstruction.WITNESSED_FOREIGN_BODY:
        return ""
    return (
        " SOMEBODY SAW SOMETHING GO INTO THE AIRWAY, so the compressions have a "
        "second job: they raise the pressure in the chest and often push the "
        "object back up where it can be reached. Each time you stop to give "
        "breaths, LOOK IN THE MOUTH first. If you can SEE the object, hook it "
        "out with your fingers. If you cannot see anything, do NOT put your "
        "fingers in and sweep blindly - that pushes it further down and can "
        "wedge it - just carry on with compressions."
    )


def _contradiction_clause(path: _Consulted, breathing: Breathing) -> str:
    """The re-test for a caller who reported an AWAKE patient with no breathing.

    DEFECT 1 of the re-review, and the half of that fix which is not a routing
    change. `Breathing.routes_to_cpr` now sends absent breathing to the arrest
    path whatever responsiveness was reported, because the reported pair is a
    contradiction and apnoea is the half that cannot wait. This clause is what
    stops that from being a SILENT resolution: the contradiction is handed back
    to the caller to re-test, in the same breath as the instruction, rather than
    spent as a question turn (see `routes_to_cpr` for why re-asking would be an
    absorbing loop in a pure function).

    Scoped narrowly to the contradiction. It fires only on an ESTABLISHED awake
    reading with absent breathing - not on gasping, where an awake reading is
    clinically coherent and the conscious-distress leaf owns the patient
    anyway, and not on an unconscious or unestablished reading, where there is
    no contradiction to re-test.

    THE BREATHING GUARD IS DEFENCE-IN-DEPTH, NOT A LIVE DISCRIMINATOR, and that
    is recorded because a reader would otherwise take it for one. Mutation
    testing widened it to `breathing.is_normal` and no test failed: an awake
    patient with ABNORMAL_OR_GASPING cannot reach `_arrest_branch` at all,
    because the conscious-distress leaf owns them, so the responsiveness guard
    below already excludes every case this one would. It is kept anyway - the
    clause states a claim about absent breathing specifically, and a future
    routing change that brought an awake gasping patient here would otherwise
    have them told their reports contradict when they do not.
    """
    if breathing is not Breathing.NONE:
        return ""
    responsiveness = path.responsiveness.value
    if responsiveness is None or responsiveness.is_unconscious:
        return ""
    return (
        " NOTE THE CONTRADICTION AND SAY IT OUT LOUD: this patient was "
        f"described as {responsiveness.value.upper()} and as not breathing at "
        "all, and those two things cannot both be true. Do not stop "
        "compressions to settle it. Tell them what they told you and ask them "
        "to look again at the chest and the face - a patient who is genuinely "
        "talking to them is not in arrest and the instruction changes, and a "
        "patient who is not needs the compressions that are already under way."
    )


def _conscious_floor(responsiveness: Responsiveness) -> Criticality:
    """The criticality floor for a conscious patient with no outstanding threat.

    Not MINOR: the agent has been called to an incident, and this function is
    only reached with A to D resolved and nothing found. A new confusion is the
    C of ACVPU and is a red flag in its own right, so it does not share a floor
    with a fully alert patient.
    """
    if responsiveness is Responsiveness.ALERT:
        return Criticality.LOW
    return Criticality.MODERATE
