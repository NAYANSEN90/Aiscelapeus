"""L2 - the assessment state machine, and the five clinical blockers on it.

Every test's first body comment is its falsifier, per tests/meta. Here that
convention earns its keep more than anywhere else in the suite: the clinical
review that produced these rows found two paths ending in a death, so a test
whose author cannot name the real-world failure probably has not understood
which death it prevents.

Assertions observe the returned branch and the resulting state, never a flag
whose value happens to be right. Several functions under test return tuples, so
emptiness is compared as emptiness rather than against `[]`.

The ASM rows from `docs/ARCHITECTURE-ASSESSMENT.md` §10 are named in the section
headers, and every safety rule here has a recorded mutant - see
`docs/reviews/2026-09-18-l2-mutation-results.md` for what was reintroduced and
which named test caught it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import itertools
import random
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from aiscelapeus.assessment import (
    ADULT_ALGORITHM_FLOOR_YEARS,
    LETTERS_IN_ORDER,
    RESCUE_BREATHS_BEFORE_COMPRESSIONS,
    UNPERFORMABLE_ASSESSMENTS,
    AgeBand,
    AirwayObstruction,
    AssessmentBranch,
    AssessmentInputs,
    AssessmentStep,
    Breathing,
    Certainty,
    Finding,
    HazardClass,
    Letter,
    PulseReport,
    Responsiveness,
    SceneSafety,
    SevereBleeding,
    SpinalRisk,
    decide,
    is_authentic,
)
# Private on purpose: `_Consulted` is the module's own record of which inputs a
# path read, and `_decide` is the seam that lets a test ask it. A public mirror
# of either would be a second model of the path, which is the defect Blocker 2
# of the third clinical review closed.
from aiscelapeus.assessment import _Consulted, _decide
from aiscelapeus.clock import ManualClock
from aiscelapeus.escalation import SOURCE_TRANSCRIPT, apply_hard_escalation
from aiscelapeus.triage import Criticality, Provenance, TriageState

# --------------------------------------------------------------------- fixtures

#: An established safe scene. Every test past the DR gate needs it, and writing
#: it inline everywhere would bury the finding each test is actually about.
SAFE = Finding(SceneSafety.SAFE, Certainty.ESTABLISHED)

#: An established `True` for the plain boolean findings (`exposure_reviewed`,
#: `submersion`). Separate from SAFE since the scene stopped being a bool.
YES = Finding(True, Certainty.ESTABLISHED)

#: No mechanism of injury, and no obstruction. Every test that wants to reach a
#: terminal state past D needs both, and neither is what those tests are about.
NO_SPINAL_RISK = Finding(SpinalRisk.NONE, Certainty.ESTABLISHED)
NO_OBSTRUCTION = Finding(AirwayObstruction.NONE, Certainty.ESTABLISHED)


def established(value: object) -> Finding:
    """An input a responder actually reported."""
    return Finding(value, Certainty.ESTABLISHED)


async def _noop() -> None:
    """The broadcast callback `escalation.py` injects. Nothing to observe here."""
    return None


async def _noop_escalate(reason: str, category: str) -> None:
    """The paging callback. The L1 test below asserts on state, not on paging."""
    return None


def _breathing_case(
    breathing: Breathing,
    *,
    age: AgeBand = AgeBand.ADULT,
    pulse: PulseReport | None = None,
    submersion: bool | None = None,
    bleeding: SevereBleeding | None = None,
) -> AssessmentInputs:
    """An unresponsive patient with the given breathing state.

    One builder rather than a fixture per case, because the tests below vary
    exactly one axis at a time and the point of most of them is that the OTHER
    axes cannot change the outcome.

    `bleeding` defaults to UNKNOWN rather than NONE deliberately: the arrest
    path must not depend on the bleeding question having been asked, and
    defaulting it to NONE here would have hidden Blocker 2 from every test that
    uses this builder.
    """
    return AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        breathing=established(breathing),
        age_band=established(age),
        pulse=Finding.unknown() if pulse is None else established(pulse),
        submersion=(
            Finding.unknown() if submersion is None else established(submersion)
        ),
        severe_bleeding=(
            Finding.unknown() if bleeding is None else established(bleeding)
        ),
    )


def _well_patient() -> AssessmentInputs:
    """A fully assessed, alert, breathing, non-bleeding patient."""
    return AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.ALERT),
        breathing=established(Breathing.NORMAL),
        severe_bleeding=established(SevereBleeding.NONE),
        airway_obstruction=NO_OBSTRUCTION,
        spinal_risk=NO_SPINAL_RISK,
        exposure_reviewed=YES,
        age_band=established(AgeBand.ADULT),
    )


#: Every value each input can take, for the exhaustive walks below. Built from
#: the enums themselves rather than listed, so a member added later is walked
#: without anyone remembering to add it here - which is the difference between a
#: property test and a list of cases that happens to be complete today.
#:
#: `airway_opened` is gone because the input is gone; `airway_obstruction` and
#: `spinal_risk` are new, and the scene went from two values to three. That
#: takes the product from ~93k combinations to ~373k, and the walk is still
#: EXHAUSTIVE rather than sampled - see `_every_reachable_branch` for what had
#: to change to keep it affordable.
#:
#: BLOCKER 3 OF THE THIRD CLINICAL REVIEW took the scene from three values to
#: FIVE, which takes this full product to ~560k. Built from `SceneSafety`
#: itself, so the two new members are walked without anyone remembering to add
#: them - and that is also why the hazard class is a refinement of this input
#: rather than a separate field: a separate three-value `Finding[HazardClass]`
#: field would have MULTIPLIED this to ~1.12M, where two extra members on an
#: existing field only add two slices.
#:
#: THE MAIN WALK CROSSES `_DECIDING_DOMAINS`, NOT THIS, and at ~560k this is why
#: - see `_CROSSED_OUT_OF_THE_WALK`. This mapping remains the full domain of
#: every input, which is what the pulse row varies over and what the per-name
#: `_FINDING_PAIRS` cache is built from; the walk crosses the subset a branch
#: can actually depend on. The measured numbers are in
#: `test_the_exhaustive_walk_itself_stays_inside_the_suite_budget`.
_INPUT_DOMAINS: dict[str, tuple[object, ...]] = {
    "scene_safe": (None, *SceneSafety),
    "responsiveness": (None, *Responsiveness),
    "breathing": (None, *Breathing),
    "severe_bleeding": (None, *SevereBleeding),
    "airway_obstruction": (None, *AirwayObstruction),
    "spinal_risk": (None, *SpinalRisk),
    "age_band": (None, *AgeBand),
    "submersion": (None, True, False),
    "exposure_reviewed": (None, True, False),
    "pulse": (None, *PulseReport),
}

#: The one input CROSSED OUT of the main walk, and the only one that may ever be
#: - so it is named here as a set of one rather than left as a special case
#: inside the walk, and the reason it is sound is asserted rather than asserted
#: about.
#:
#: WHY THIS EXISTS. Blocker 3 of the third clinical review took `SceneSafety`
#: from three members to five, which took the product from 373k to 560k and the
#: walk from ~4.9 s to ~8.0 s against ASM-09's 8.0 s bound - measured, and
#: FAILING intermittently at 8.04-8.05 s on two runs out of three. A flaky
#: safety test is one that gets deleted, and raising the bound to hide a real
#: regression is the failure mode CLAUDE.md names, so neither was acceptable.
#:
#: WHY `pulse` AND NOTHING ELSE. It is the one input that provably cannot change
#: a branch, and the proof is structural rather than empirical: `_Consulted`
#: deliberately has NO `pulse` accessor, so there is no expression by which any
#: path can read it (ASM-12). Crossing it into the product therefore multiplies
#: every safety row's work by four and adds no reachable state at all. That is
#: not sampling - nothing decision-relevant is dropped - and the distinction
#: matters because sampling the walk was considered and rejected when the space
#: last grew.
#:
#: WHAT STILL WALKS IT AT FULL STRENGTH. `test_the_pulse_report_cannot_change_
#: any_branch` crosses all four pulse values against the whole rest of the
#: space, which is the row that would notice if the premise above ever stopped
#: holding, and `test_no_input_field_is_declared_and_never_read` asserts `pulse`
#: is the only unread field. So the claim is not assumed here - it is asserted
#: by the two rows that own it, and this set is what they license.
_CROSSED_OUT_OF_THE_WALK: frozenset[str] = frozenset({"pulse"})

#: The domains the main walk actually crosses: everything a branch can depend on.
_DECIDING_DOMAINS: dict[str, tuple[object, ...]] = {
    name: values
    for name, values in _INPUT_DOMAINS.items()
    if name not in _CROSSED_OUT_OF_THE_WALK
}

#: One `Finding` per (name, value) pair, shared across the whole walk, as a
#: per-name list of (value, Finding) pairs. `Finding` is frozen, so sharing is
#: safe, and building 3.7 million of them instead of 40 was most of the walk's
#: cost. Held as pairs rather than a dict keyed by (name, value) because the
#: walk is hot enough that hashing an enum 2.2 million times to look up a
#: constant showed up in a profile.
#:
#: The determinism test still builds a FRESH inputs object per run, which is
#: where "L2 does not mutate what it was given" is actually asserted - this
#: cache is a walk optimisation and must not be used there.
_FINDING_PAIRS: dict[str, tuple[tuple[object, Finding], ...]] = {
    name: tuple(
        (
            value,
            Finding.unknown()
            if value is None
            else Finding(value, Certainty.ESTABLISHED),
        )
        for value in values
    )
    for name, values in _INPUT_DOMAINS.items()
}


def _inputs_from(assignment: dict[str, object]) -> AssessmentInputs:
    """Build inputs from a name -> value mapping, `None` meaning UNKNOWN."""
    return AssessmentInputs(
        **{
            name: (
                Finding.unknown()
                if value is None
                else Finding(value, Certainty.ESTABLISHED)
            )
            for name, value in assignment.items()
        }
    )


def _consulted_by(assignment: dict[str, object]) -> frozenset[str]:
    """Which inputs `decide` actually reads on the path this assignment takes.

    Asks the module's own accumulator rather than re-deriving the path from the
    source or from a table in this file: `_Consulted` is what the leaves build
    `assumed=` from, so a second model of the path here could disagree with the
    one the code uses and the disagreement would be invisible. Reaching into the
    private name is deliberate for exactly that reason - a public mirror of it
    would be the second copy.
    """
    path = _Consulted(_inputs_from(assignment))
    _decide(path)
    return frozenset(path._names)


#: What each row below actually reads off a branch. The walk keeps THIS rather
#: than the `AssessmentBranch`, because holding 140k live branches costs far
#: more memory than the assertions need - and, worse, would pin every one of
#: them in `assessment._recent_branches`' bounded ring semantics in a way the
#: registry was never sized for.
class _Walked(NamedTuple):
    step: AssessmentStep
    letter: Letter
    criticality: Criticality
    unresolved_letter: Letter
    assumed_inputs: tuple[str, ...]
    is_instruction: bool
    #: Kept because two rows assert on the WORDS a branch carries and not only
    #: on its step: the live-hazard walk, and - after Blocker 1 of the third
    #: clinical review - the established-bleed walk, where a question is allowed
    #: to stand in front of the bleed only if it instructs the pressure in the
    #: same breath. Holding the string costs memory the walk can afford; a
    #: second decide() per assertion costs the ASM-09 budget.
    rationale: str


def _every_reachable_branch() -> list[tuple[dict[str, object], _Walked]]:
    """Exhaustively enumerate the whole input space and the branch each gives.

    The space is small enough to walk completely - the product of the domains
    above - which is stronger than sampling it: "no reachable path does X" is
    then a statement about every path rather than about the ones a seed happened
    to visit.

    Two new inputs and a third scene state took the product from ~93k to ~373k,
    which made the naive version of this function cost 6 s and several hundred
    MB - over ASM-09's budget for the whole L2 suite. Kept exhaustive by
    sharing the frozen `Finding` objects and by collapsing each result to a
    `_Walked` tuple instead of retaining the branch. Sampling was the
    alternative and was rejected: "no reachable branch defers an established
    catastrophic bleed" is worth nothing as a statement about 5% of paths.

    CROSSES `_DECIDING_DOMAINS` RATHER THAN `_INPUT_DOMAINS`, which is what kept
    it inside the budget when Blocker 3 of the third clinical review took the
    scene to five members. `pulse` is held at UNKNOWN here because no path can
    read it - see `_CROSSED_OUT_OF_THE_WALK` for the structural proof and for
    the two rows that still walk that axis at full strength. Every assignment
    below still carries a `pulse` key, so the rows that group or filter on it
    read the same shape they always did; the value is simply the only one the
    decision path could ever have seen.

    STILL EXHAUSTIVE OVER EVERYTHING A BRANCH CAN DEPEND ON, which is the
    property the safety rows need. "No reachable branch does X" remains a
    statement about every reachable branch, because an input nothing reads
    generates no additional reachable branches.
    """
    names = list(_DECIDING_DOMAINS)
    results: list[tuple[dict[str, object], _Walked]] = []
    append = results.append
    build = AssessmentInputs
    #: The crossed-out inputs, at the only value the decision path can see.
    fixed = {name: None for name in _CROSSED_OUT_OF_THE_WALK}

    for combination in itertools.product(*(_FINDING_PAIRS[n] for n in names)):
        # `combination` is a tuple of (value, Finding) pairs in `names` order,
        # so the assignment a test reads and the inputs `decide` gets are built
        # from the same tuple in one pass - no second lookup, no rebuilding of
        # the frozen Findings.
        branch = decide(
            build(**{
                name: pair[1] for name, pair in zip(names, combination)
            })
        )
        append(
            (
                {
                    **{name: pair[0] for name, pair in zip(names, combination)},
                    **fixed,
                },
                _Walked(
                    step=branch.step,
                    letter=branch.letter,
                    criticality=branch.criticality,
                    unresolved_letter=branch.unresolved_letter,
                    assumed_inputs=branch.assumed_inputs,
                    is_instruction=branch.is_instruction,
                    rationale=branch.rationale,
                ),
            )
        )
    return results


#: Computed once: the walk is ~140k combinations and every structural row below
#: asks a different question of the same set.
#:
#: Timed here rather than by a test re-walking the space, because re-walking
#: would spend the budget it is checking and would measure a second, warmer run
#: instead of the one CI actually pays for on every change. ASM-09 asserts on
#: this number.
_WALK_STARTED = time.perf_counter()
ALL_BRANCHES = _every_reachable_branch()
ALL_BRANCHES_SECONDS = time.perf_counter() - _WALK_STARTED


# ------------------------------------------------- ASM-01: L2 is a pure function


def test_identical_inputs_give_an_identical_branch_over_many_random_runs() -> None:
    # falsifier: the state machine consults something outside its inputs - a
    # clock, a random seed, module-level mutable state, a cached previous answer
    # - so the same patient gets a different instruction on two different turns
    # and the audit trail cannot explain which one was right. ASM-01.
    rng = random.Random(20260918)
    names = list(_INPUT_DOMAINS)

    for _ in range(1000):
        assignment = {
            name: rng.choice(_INPUT_DOMAINS[name]) for name in names
        }
        first = decide(_inputs_from(assignment))
        second = decide(_inputs_from(assignment))
        # A fresh inputs object each time, so this also catches a machine that
        # mutates what it was given and answers differently on a second read.
        third = decide(_inputs_from(assignment))

        assert first.step is second.step is third.step, assignment
        assert first.criticality is second.criticality, assignment
        assert first.letter is second.letter, assignment
        assert first.assumed_inputs == second.assumed_inputs, assignment


def test_deciding_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    # falsifier: a branch reaches for a file, a socket or the wall clock, so the
    # one layer the system claims is deterministic and offline-safe stops being
    # either - and it fails at a scene with no signal, which is when it is most
    # needed. ASM-01, ASM-09.
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("L2 performed I/O")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    monkeypatch.setattr(random, "random", forbidden)

    branch = decide(_breathing_case(Breathing.NONE))
    assert branch.step is AssessmentStep.INSTRUCT_CPR


def test_the_module_imports_nothing_that_could_do_io() -> None:
    # falsifier: someone adds `import requests`, `from .moss import ...` or
    # `import datetime` to the pure layer. The tests above would still pass -
    # an unused import performs no I/O - while the purity claim quietly becomes
    # false for the next author who uses it.
    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    # `triage` is permitted and is the ONLY sibling that is: it is the other
    # pure domain module, and L2 needs `Criticality` and `Provenance` from it.
    # Anything else in this list - `moss_context`, `retrieval`, `agent`,
    # `config` - would drag a vendor SDK or the filesystem into the pure layer.
    permitted = {"dataclasses", "enum", "typing", "triage", "__future__", ""}
    assert imported <= permitted, (
        f"L2 must import only pure stdlib typing machinery; found {imported - permitted}"
    )
    # And the one permitted sibling must itself stay pure of vendor imports,
    # or the restriction above is satisfied while the purity claim is not.
    from aiscelapeus import triage

    triage_source = Path(inspect.getfile(triage)).read_text(encoding="utf-8")
    for vendor in ("livekit", "deepgram", "google.genai", "requests", "httpx"):
        assert vendor not in triage_source, f"triage.py reached for {vendor}"


# ------------------------------------------ ASM-03: ABCDE ordering is unviolatable


def test_no_reachable_branch_advises_a_later_letter_while_an_earlier_is_open(
) -> None:
    # falsifier: the agent discusses D - or tells the responder to keep the
    # patient warm - while the airway is still unresolved. The clinical spine of
    # the whole product is "treat life-threatening problems before moving to the
    # next letter", and an agent that breaks it is wrong no matter how good its
    # retrieval was. ASM-03.
    for assignment, branch in ALL_BRANCHES:
        assert branch.letter is branch.unresolved_letter or branch.is_instruction, (
            f"{assignment} produced assessment step {branch.step} on "
            f"{branch.letter} while {branch.unresolved_letter} was unresolved"
        )


def test_an_unresolved_earlier_letter_always_wins_over_a_later_QUESTION() -> None:
    # falsifier: breathing is unknown but the bleeding QUESTION is unanswered,
    # and the machine picks the bleeding question because it is answerable - so
    # a patient in arrest gets asked about blood while nobody starts
    # compressions. ASM-03.
    #
    # This test used to assert the same thing for an ESTABLISHED bleed, and the
    # second clinical review found that exact behaviour to be Blocker 1: a
    # bystander kneeling on a femoral bleed was asked to assess breathing. The
    # rule it was really defending is narrower than it was written, and the
    # narrower rule is the correct one - an unresolved earlier letter outranks a
    # later QUESTION, never a later established life threat. The established
    # case is now `test_no_reachable_branch_defers_an_established_catastrophic_bleed`.
    inputs = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        # B unknown, and C unknown too - so C has only a question to offer.
        exposure_reviewed=YES,
    )
    branch = decide(inputs)
    assert branch.unresolved_letter is Letter.B
    assert branch.step is AssessmentStep.ASK_BREATHING_QUALITY

    # And the same when C is established as NONE: there is nothing to treat, so
    # the earlier letter's question still wins.
    no_bleed = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            severe_bleeding=established(SevereBleeding.NONE),
            exposure_reviewed=YES,
        )
    )
    assert no_bleed.step is AssessmentStep.ASK_BREATHING_QUALITY


def test_scene_safety_blocks_every_letter_including_a_reported_arrest() -> None:
    # falsifier: there is no DR in the DRABC - the clinical review's ranked
    # omission 4. The responder is the operator; if they walk into live wires or
    # traffic to reach the patient, there are two casualties and nobody to help
    # either. Today this exists as one line of prose and a retrievable
    # document, which is not a gate.
    inputs = AssessmentInputs(
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        breathing=established(Breathing.NONE),
    )
    branch = decide(inputs)
    assert branch.step is AssessmentStep.ASK_SCENE_SAFE, (
        "an unestablished scene must block even a reported arrest"
    )

    unsafe = AssessmentInputs(
        scene_safe=established(SceneSafety.UNSAFE_ADJACENT),
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        breathing=established(Breathing.NONE),
    )
    assert decide(unsafe).step is AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE


# ----------------------------- BLOCKER 3: the scene gate is not absorbing


def test_a_committed_responder_advances_past_a_live_hazard() -> None:
    # falsifier: THE ABSORBING GATE. A caller already kneeling beside a patient
    # in a lay-by who answers "no, cars are going past" TRUTHFULLY is told to
    # move to safety and never advances - so the arrest behind the gate never
    # gets compressions and the gate kills the patient it exists to protect,
    # precisely when the bystander has already taken the risk and will not
    # un-take it. Verified as reachable before the fix: scene_safe=False +
    # UNRESPONSIVE + breathing=NONE returned INSTRUCT_MAKE_SCENE_SAFE forever.
    committed = AssessmentInputs(
        scene_safe=established(SceneSafety.UNSAFE_ADJACENT_COMMITTED),
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        breathing=established(Breathing.NONE),
        age_band=established(AgeBand.ADULT),
    )
    branch = decide(committed)
    assert branch.step is AssessmentStep.INSTRUCT_CPR, (
        "a committed responder must ADVANCE, not loop on the scene gate"
    )
    assert branch.criticality is Criticality.CRITICAL

    # And advancing is not the same as the hazard being gone: every instruction
    # past the gate must keep saying so, or the responder stops watching for it.
    assert "hazard" in branch.rationale.lower()
    assert SceneSafety.UNSAFE_ADJACENT_COMMITTED.hazard_persists is True
    assert SceneSafety.UNSAFE_ADJACENT_COMMITTED.blocks_patient_contact is False


# --------- BLOCKER 3 OF THE THIRD CLINICAL REVIEW: the hazard in the patient


def test_a_patient_still_in_circuit_is_never_told_to_start_compressions() -> None:
    # falsifier: THE REPRODUCED DEFECT, AND IT KILLS THE RESPONDER.
    # scene_safe=UNSAFE_RESPONDER_COMMITTED + UNRESPONSIVE + breathing NONE
    # returned INSTRUCT_CPR at CRITICAL, with a hazard warning that spoke of
    # "watching" the hazard and getting clear "if it closes in" and never
    # mentioned TOUCHING the patient. A bystander who said "there's a live
    # cable, I'm already next to him" was told to put both hands on an
    # energised chest: compressions ARE the mechanism of injury there, so the
    # result is two casualties instead of one - the exact outcome the DR gate
    # exists to prevent, reached THROUGH the gate because Blocker 3(a)'s
    # three-state fix made the committed state advance by design.
    in_circuit = AssessmentInputs(
        scene_safe=established(SceneSafety.UNSAFE_IN_PATIENT),
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        breathing=established(Breathing.NONE),
        age_band=established(AgeBand.ADULT),
    )
    branch = decide(in_circuit)
    assert branch.step is AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT, (
        f"a patient still in contact with a live conductor must reach the "
        f"break-the-contact instruction, not {branch.step}"
    )
    lowered = branch.rationale.lower()
    # The prohibition has to name the ACTION, not merely the hazard: "be
    # careful of electricity" alongside compressions is what the old suffix
    # amounted to, and it is what killed the rescuer.
    assert "do not touch the patient" in lowered, (
        "the instruction must forbid patient CONTACT explicitly; a warning "
        "about the hazard next to a compressions order is the defect itself"
    )
    assert "completes the circuit" in lowered, (
        "and say why, because a bystander who does not know the mechanism "
        "will reach for the chest anyway"
    )
    # And it must say what to DO instead, in the order a bystander can act on.
    # An instruction that only forbids leaves the patient in arrest.
    for method in ("plug", "breaker", "isolat"):
        assert method in lowered, (
            f"the break-the-contact methods must be named; {method!r} missing"
        )
    assert "non-conducting" in lowered or "non-conductor" in lowered, (
        "pushing the casualty clear with a dry non-conductor is the one "
        "method a lone bystander can often manage"
    )
    # THE TETANIC GRIP, and the zero-risk method that has to come first.
    # Found by the independent clinical review of this fix: the instruction
    # spoke only to the responder, so the free method - shout at an awake
    # patient to let go - was missing, and a responder who shouted and got no
    # response would read a fully conscious patient as unconscious. A current
    # across the hand clamps the grip shut, which is the classic presentation.
    assert "let go" in lowered, (
        "telling an awake patient to let go costs nothing and risks nobody, "
        "so it must be the first method offered"
    )
    assert "cannot let go" in lowered, (
        "and the reason they may fail to comply must be stated, or the "
        "responder misreads a tetanic grip as unconsciousness"
    )
    # WHO TO CALL. For a cable, a rail or overhead lines the isolation is not a
    # switch anybody at the scene can throw, so the one action that makes that
    # method performable at all is naming the call. Found by the same review:
    # the instruction said "get somebody to have it isolated" and named nobody.
    assert "emergency services" in lowered, (
        "network isolation is not performable by a bystander alone; the call "
        "that makes it happen must be named, or the method is an instruction "
        "to do something impossible"
    )
    # ENERGISED WATER. Water carries the current out to whoever stands in it,
    # so the push-clear method is off and the whole wet area is live. Found by
    # the same review, which reproduced `submersion=True` changing not one byte.
    assert "as live" in lowered, (
        "a wet scene must be treated as live throughout, or a responder "
        "standing in the same puddle pushes the casualty clear and is "
        "electrocuted through the water"
    )
    # The honest half: sometimes they cannot, and the agent must say so rather
    # than leave them improvising on a rail or overhead lines.
    assert "stay well back" in lowered, (
        "if the contact cannot be broken the answer is to stay back and wait "
        "for isolation - this is the one place 'advance anyway' is wrong"
    )
    # And the moment it IS broken, this is an ordinary and survivable arrest.
    assert "compressions start" in lowered, (
        "the transition out of this state must be stated, or the gate becomes "
        "the absorbing loop Blocker 3(a) killed"
    )


def test_the_in_patient_gate_does_not_lower_the_criticality_of_an_arrest() -> None:
    # falsifier: BLOCKER 3(b), FOR THE THIRD TIME IN THIS MODULE. The scene gate
    # did it first (SEVERE behind an unresolved scene with an arrest waiting),
    # the Blocker 1 bleed hoist reintroduced it verbatim, and a third pre-A gate
    # written with a pinned SEVERE would be the third instance of one defect -
    # the clinician on the bridge sees a 4 and pages accordingly while the
    # patient is pulseless behind a live cable. `_gate_floor` is shared, so this
    # asserts the share rather than a copied line.
    arrest = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.UNSAFE_IN_PATIENT),
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
        )
    )
    assert arrest.step is AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT
    assert arrest.criticality is Criticality.CRITICAL, (
        f"the in-the-patient gate lowered a known arrest to {arrest.criticality}"
    )
    # And with no arrest behind it the gate keeps its own floor, so the rule
    # above is not just "this gate always says 5".
    no_arrest = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.UNSAFE_IN_PATIENT),
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.NORMAL),
        )
    )
    assert no_arrest.step is AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT
    assert no_arrest.criticality is Criticality.SEVERE


def test_no_reachable_branch_touches_a_patient_who_is_still_the_hazard() -> None:
    # falsifier: the case above is fixed for the reported cell and some other
    # combination of inputs still reaches a hands-on instruction with the
    # hazard in the patient - the bleed hoist, the choking thrusts, the recovery
    # position roll, any of which put a responder's hands on an energised
    # casualty just as surely as compressions do. Walks the entire input space,
    # so this is a statement about every path rather than about the one cell the
    # reviewer happened to reproduce. That distinction is the whole reason this
    # is a walk: Blocker 3 was reported on the CPR leaf, and the defect was in
    # the GATE, which sits in front of all of them.
    hands_on = {
        AssessmentStep.INSTRUCT_CPR,
        AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING,
        AssessmentStep.INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION,
        AssessmentStep.INSTRUCT_RECOVERY_POSITION,
        AssessmentStep.INSTRUCT_AIRWAY_WITH_SPINAL_CARE,
        AssessmentStep.INSTRUCT_SUPPORT_SEVERE_BREATHING_DIFFICULTY,
    }
    checked = 0
    for assignment, walked in ALL_BRANCHES:
        scene = assignment["scene_safe"]
        if scene is None or not scene.is_in_the_patient:
            continue
        checked += 1
        assert walked.step not in hands_on, (
            f"{assignment} reached {walked.step}, which puts a responder's "
            f"hands on a patient who is still completing a circuit"
        )
        # And it is not spared by being asked a question either: every cell
        # here must reach the one instruction that resolves the hazard, because
        # a question would spend a turn without removing the thing that is
        # killing both of them.
        assert (
            walked.step is AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT
        ), f"{assignment} reached {walked.step} rather than breaking the contact"
    assert checked > 1000, (
        f"the in-the-patient space must actually be walked; only {checked} "
        f"matched"
    )


def test_the_hazard_warning_is_true_of_the_hazard_it_warns_about() -> None:
    # falsifier: `_hazard_suffix` is one CONSTANT STRING for every hazard class,
    # so two of the three classes get a warning that describes something else.
    # "Keep them watching for it, get clear if it closes in" describes a hazard
    # that MOVES TOWARD YOU: true of traffic, and the opposite of the truth for
    # a hazard that is in the patient, where nothing closes in, watching does
    # not help, and moving back is not the answer. The reviewer's point is that
    # a warning which is wrong for the hazard is worse than no warning, because
    # the responder acts on it.
    adjacent = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.UNSAFE_ADJACENT_COMMITTED),
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
        )
    ).rationale.lower()
    assert "closes in" in adjacent, (
        "the work-beside class keeps the warning that is true of it - traffic "
        "really does close in, and getting clear really is the answer"
    )

    in_patient = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.UNSAFE_IN_PATIENT),
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
        )
    ).rationale.lower()
    assert "closes in" not in in_patient, (
        "a hazard that is IN the patient does not close in, and telling the "
        "responder to watch for it to do so is the wrong warning entirely"
    )
    assert "the contact, not something approaching" in in_patient, (
        "the in-the-patient warning must name what the danger actually is"
    )

    # THE CONSUMING CLASS IS READ OFF A DECIDED BRANCH, NOT OFF `_hazard_suffix`,
    # AND AN INDEPENDENT CLINICAL REVIEW IS WHY. The first version of this row
    # called `_hazard_suffix(SceneSafety.UNSAFE_CONSUMING)` directly and
    # justified it with "a consuming scene only reaches INSTRUCT_MAKE_SCENE_SAFE,
    # which carries no suffix by design because it IS the hazard warning".
    # EXECUTION FALSIFIED THAT justification: the move-to-safety string named no
    # hazard at all, so the fire caller and the traffic caller were read
    # byte-identical words and every word of the consuming class's clinical
    # content reached nobody. 96 tests passed over dead clinical content, which
    # is exactly the "mechanism whose only implementation is its own
    # description" pattern this repo has five instances of. Asserting through
    # the branch is what makes the content's REACHABILITY part of the claim.
    consuming = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.UNSAFE_CONSUMING),
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
        )
    ).rationale.lower()
    # Fire and gas are dose-over-time, so "it might close in" understates them:
    # committed means dead in minutes, and the action is extraction.
    assert "dose" in consuming, (
        "a consuming hazard is dose-over-time and the warning must say so, "
        "rather than describing a risk that stays the same while you work"
    )
    assert "clear air" in consuming, (
        "and the action for it is extraction, not a standing watch"
    )
    for owed in ("fire", "smoke"):
        assert owed in consuming, (
            f"the fire caller must hear the hazard NAMED; {owed!r} missing, "
            f"which is how a traffic-written string reached them instead"
        )

    # The three warnings are genuinely different text, which is what "L2 holds
    # WHICH hazard it was" buys. Identical strings would pass every assertion
    # above that only checks for a substring.
    assert len({adjacent, in_patient, consuming}) == 3, (
        "the per-class warnings must differ; identical text is the constant "
        "string wearing three names"
    )


def test_every_hazard_class_has_a_warning_that_is_not_the_adjacent_one() -> None:
    # falsifier: a hazard class is added to the enum later and `_hazard_suffix`
    # falls through to the traffic text for it, which is exactly how the
    # original defect existed - one class's warning applied to all of them. This
    # asserts the mapping is total over the class enum rather than trusting a
    # final `return` to be right for a member nobody has thought about yet.
    from aiscelapeus.assessment import _hazard_suffix

    adjacent_text = _hazard_suffix(SceneSafety.UNSAFE_ADJACENT)
    seen: dict[HazardClass, str] = {}
    for member in SceneSafety:
        hazard = member.hazard
        text = _hazard_suffix(member)
        if hazard is None:
            assert text == "", "a safe scene must carry no warning at all"
            continue
        assert text, f"{member.name} carries a hazard but no warning"
        # Same class, same warning - the class is what the warning is about.
        if hazard in seen:
            assert seen[hazard] == text, (
                f"{member.name} warns differently from another member of the "
                f"same hazard class, so the text is keyed on the member rather "
                f"than on the class it is about"
            )
        seen[hazard] = text
        if hazard is not HazardClass.ADJACENT:
            assert text != adjacent_text, (
                f"{hazard.name} falls through to the work-beside warning, "
                f"which is the constant-string defect for that class"
            )
    assert set(seen) == set(HazardClass), (
        f"every hazard class must be reachable from some scene state and have "
        f"its own warning; missing {set(HazardClass) - set(seen)}"
    )


def test_the_committed_answer_is_offered_only_where_it_can_be_honoured() -> None:
    # falsifier: FOUND BY THE INDEPENDENT CLINICAL REVIEW OF THIS VERY FIX, and
    # it is Blocker 3(a)'s absorbing loop rebuilt on fire WITH AN INVITATION
    # ATTACHED. The move-to-safety instruction was one constant string that
    # offered "if they are ALREADY beside the patient and will not leave, that
    # is a different answer and the assessment continues" - which is true for
    # the work-beside class, because it has a committed member, and FALSE for
    # fire and for a patient in circuit, which deliberately do not. So a caller
    # in a smoke-filled room was invited to say "I'm not leaving him" and the
    # module had nothing to do with that answer but repeat itself forever. An
    # escape offered by a machine that cannot honour it is worse than one not
    # offered, because the caller stakes their life on the offer.
    invitation = "will not leave"
    for member in SceneSafety:
        if not member.blocks_patient_contact:
            continue
        branch = decide(
            AssessmentInputs(
                scene_safe=established(member),
                responsiveness=established(Responsiveness.UNRESPONSIVE),
                breathing=established(Breathing.NONE),
            )
        )
        offers = invitation in branch.rationale.lower()
        # The class may offer the committed answer only if some member of the
        # SAME hazard class can actually represent it.
        honourable = any(
            other.responder_committed and other.hazard is member.hazard
            for other in SceneSafety
        )
        assert offers == honourable, (
            f"{member.name} "
            f"{'offers' if offers else 'does not offer'} the committed answer "
            f"while the type "
            f"{'can' if honourable else 'cannot'} honour it - an escape the "
            f"machine refuses must not be dangled, and one it accepts must be "
            f"described where it is reachable"
        )


def test_every_blocking_hazard_class_names_its_own_hazard() -> None:
    # falsifier: the mirror of the row above, and the half that made the
    # consuming class's clinical content DEAD. `_hazard_suffix` was made
    # per-class while INSTRUCT_MAKE_SCENE_SAFE kept one hazard-agnostic string -
    # so for the one class whose ONLY reachable step is that instruction, every
    # word of the correct guidance existed in the module and reached no caller.
    # A walk-level row rather than a per-class case, because the defect was
    # precisely that one class was forgotten while the others were handled.
    # SCOPED TO THE BLOCKING STATES, which is what the row's name says and what
    # the defect was about. The COMMITTED state advances by design, so it
    # reaches sixteen different steps and its hazard wording is carried by
    # `_hazard_suffix` on each of them - asserted by
    # `test_every_instruction_past_a_live_hazard_restates_it` and by
    # `test_the_hazard_warning_is_true_of_the_hazard_it_warns_about`, not here.
    # This row owns the states whose ONLY reachable step is a pre-A gate, which
    # is precisely where a hazard-agnostic string left a class unserved.
    reached: dict[SceneSafety, set[AssessmentStep]] = {}
    for assignment, walked in ALL_BRANCHES:
        scene = assignment["scene_safe"]
        if scene is None or not scene.blocks_patient_contact:
            continue
        reached.setdefault(scene, set()).add(walked.step)

    # Every blocking scene state must be reachable at all, or the assertions
    # below are about nothing.
    assert set(reached) == {m for m in SceneSafety if m.blocks_patient_contact}

    #: The word each hazard class must say for itself, in whatever step it
    #: reaches. Keyed on the class so a new class added later has no entry and
    #: fails here rather than silently inheriting another class's words.
    owed_by_class = {
        HazardClass.ADJACENT: ("hazard",),
        HazardClass.IN_PATIENT: ("contact",),
        HazardClass.CONSUMING: ("smoke",),
    }
    assert set(owed_by_class) == set(HazardClass), (
        "a hazard class with no owed word would inherit another class's "
        "wording unnoticed, which is the defect this row exists for"
    )
    for scene, steps in sorted(reached.items(), key=lambda kv: kv[0].name):
        hazard = scene.hazard
        assert hazard is not None
        # ONE STEP PER BLOCKING SCENE, and that is a property rather than an
        # assumption: the scene block is the first thing `_decide` does and
        # every blocking member returns from it unconditionally, so no other
        # input can change the step. Asserted rather than relied on, because a
        # future gate that made this false would otherwise silently reduce the
        # loop below to whichever step happened to come first.
        assert len(steps) == 1, (
            f"{scene.name} reaches {sorted(s.name for s in steps)}; this row "
            f"assumes a blocking scene returns from the pre-A gate on every "
            f"input, so the per-class wording can be asserted from the scene "
            f"alone. Widen the row before widening the gate."
        )
        for step in steps:
            branch = decide(
                AssessmentInputs(
                    scene_safe=established(scene),
                    # An arrest, so `_gate_floor`'s path is exercised too. The
                    # step cannot depend on these - see the assertion above.
                    responsiveness=established(Responsiveness.UNRESPONSIVE),
                    breathing=established(Breathing.NONE),
                )
            )
            assert branch.step is step, (
                f"{scene.name} reached {branch.step.name} rather than the "
                f"{step.name} the walk recorded for it"
            )
            lowered = branch.rationale.lower()
            for owed in owed_by_class[hazard]:
                assert owed in lowered, (
                    f"{scene.name} reaches {step.name} with a rationale that "
                    f"never says {owed!r}, so the caller is read words written "
                    f"for a different hazard than the one they reported"
                )


def test_the_scene_question_asks_what_decides_whether_touching_is_safe() -> None:
    # falsifier: the hazard CLASS is expressible in the type and the question
    # never elicits it, so the discriminator is collected by nobody and the
    # state is reached only if a caller volunteers "he's still touching the
    # cable" unprompted. That is the recorded "escape hatch behind the wall it
    # opens" defect in a new place: a type that can hold a distinction the
    # question cannot ask is the same information loss the two-value verdict
    # had, moved one layer out.
    branch = decide(AssessmentInputs())
    assert branch.step is AssessmentStep.ASK_SCENE_SAFE
    lowered = branch.rationale.lower()
    # Still the closed hazard list - this must not regress.
    for hazard in ("traffic", "fire", "electricity"):
        assert hazard in lowered
    # And now the follow-up that decides the class. Asserted as the ASK, not as
    # the word "touching" - MUTATION TESTING CAUGHT THAT EXACT WEAKNESS. The
    # first version of this row asserted `"touching" in lowered`, and deleting
    # the whole follow-up question left the later clause "makes touching them
    # the injury" behind, which still matched. So the mutant SURVIVED and the
    # row was checking a word that appears twice rather than the rule it stands
    # for. This asserts the imperative that elicits the class.
    assert "ask whether the patient is still" in lowered, (
        "the question must ASK whether the patient is still in contact with "
        "the conductor - that answer is what decides whether they may be "
        "touched at all, and a type that can hold the distinction while the "
        "question cannot elicit it loses the information all over again"
    )
    # The conductors a bystander would actually name, so the question is
    # answerable rather than abstract.
    assert "cable" in lowered, "and name what contact to look for"
    # And the escape hatch is now offered rather than described only behind the
    # instruction that blocks it.
    assert "not leaving" in lowered, (
        "'already beside them and not leaving' must be offered as an answer, "
        "or the committed state is unreachable by the pinned phrasing"
    )


def test_every_instruction_past_a_live_hazard_restates_it() -> None:
    # falsifier: advancing past a live hazard is not the same as the hazard
    # being gone, so an instruction that omits the warning leaves a responder
    # working next to traffic three minutes later with nobody having mentioned
    # it since. Found by independent review on INSTRUCT_MONITOR specifically -
    # the terminal branch, reached after a full survey, which is exactly when
    # the responder has stopped thinking about the road. Asserted over every
    # instruction rather than that one, because a per-leaf rule enforced by
    # memory is a rule the next leaf omits.
    checked = 0
    for assignment, walked in ALL_BRANCHES:
        scene = assignment["scene_safe"]
        if scene is None or not scene.hazard_persists:
            continue
        if not walked.is_instruction:
            continue
        # The move-to-safety instruction IS the hazard warning, so it is not
        # required to carry a second copy of one.
        if walked.step is AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE:
            continue
        checked += 1
        rationale = decide(_inputs_from(assignment)).rationale.lower()
        assert "hazard" in rationale, (
            f"{walked.step} was instructed past a live hazard without "
            f"restating it: {assignment}"
        )
    assert checked > 100, (
        f"the live-hazard instruction space must actually be walked; only "
        f"{checked} matched"
    )


def test_the_scene_state_is_one_enum_not_a_verdict_plus_a_flag() -> None:
    # falsifier: the escape, or the hazard class, is added as a second field -
    # `scene_safe` plus `responder_committed`, or `scene_safe` plus
    # `hazard_class` - which makes (safe=True, committed=True) and (SAFE, live
    # cable) expressible and meaningless, and forces every reader of the pair to
    # rediscover which of the combinations are real. Same reasoning that made
    # Breathing three-state rather than a bool plus a `gasping` flag, applied a
    # second time to the hazard class.
    assert [member.value for member in SceneSafety] == [
        "safe",
        "unsafe_adjacent",
        "unsafe_adjacent_committed",
        "unsafe_in_patient",
        "unsafe_consuming",
    ]
    fields = set(AssessmentInputs.__dataclass_fields__)
    for smell in (
        "responder_committed",
        "scene_committed",
        "committed",
        "hazard",
        "hazard_class",
        "scene_hazard",
    ):
        assert smell not in fields, f"{smell} is the verdict-plus-flag shape"

    # Every member carries exactly one hazard class, and only the safe one
    # carries none - so there is no member for which "which hazard is this?" is
    # undecidable, which is what the two-value verdict made it.
    assert SceneSafety.SAFE.hazard is None
    for member in SceneSafety:
        if member is SceneSafety.SAFE:
            continue
        assert member.hazard is not None, (
            f"{member.name} is unsafe but carries no hazard class, so the "
            f"warning for it cannot be true of it"
        )

    # And no member of HazardClass means "unknown" - the Breathing rule. An
    # unasked scene is Finding.unknown(); a safe scene has `hazard is None`.
    assert [m.value for m in HazardClass] == [
        "adjacent",
        "in_patient",
        "consuming",
    ]
    for smell in ("UNKNOWN", "NONE", "UNSURE"):
        assert smell not in HazardClass.__members__, (
            f"HazardClass.{smell} is an illegal state expressible in the type: "
            f"every match over it would need an arm the forgetting one routes "
            f"silently"
        )


def test_only_a_hazard_you_can_work_beside_has_a_committed_escape_hatch() -> None:
    # falsifier: BLOCKER 3 OF THE THIRD CLINICAL REVIEW, AS A PROPERTY OF THE
    # TYPE. The escape hatch that Blocker 3(a) added for traffic is extended to
    # every hazard class - so a bystander who said "there's a live cable, I'm
    # already next to him" advances past the gate and is told to compress a
    # patient still in circuit. Two casualties instead of one, which is the
    # exact outcome the DR gate exists to prevent, reached THROUGH the gate.
    # Asserted as an absent member rather than as a routing condition, because a
    # condition downstream can be re-widened and a member that does not exist
    # cannot be reached.
    committed = [m for m in SceneSafety if m.responder_committed]
    assert committed == [SceneSafety.UNSAFE_ADJACENT_COMMITTED], (
        f"the committed escape hatch must exist for the work-beside class and "
        f"for nothing else; got {[m.name for m in committed]}"
    )
    for member in committed:
        assert member.hazard is HazardClass.ADJACENT, (
            f"{member.name} lets a responder advance past a hazard that is not "
            f"the survivable-to-work-beside class"
        )
    # No member for the hatch on the other two classes, by name.
    for forbidden in (
        "UNSAFE_IN_PATIENT_COMMITTED",
        "UNSAFE_CONSUMING_COMMITTED",
    ):
        assert forbidden not in SceneSafety.__members__, (
            f"SceneSafety.{forbidden} would make 'advance anyway' expressible "
            f"for a hazard where advancing transfers the arrest to the rescuer"
        )

    # An in-the-patient hazard blocks patient contact however close the
    # responder already is - which is the whole distinction the two-value
    # verdict could not express.
    assert SceneSafety.UNSAFE_IN_PATIENT.blocks_patient_contact is True
    assert SceneSafety.UNSAFE_ADJACENT_COMMITTED.blocks_patient_contact is False
    # And exactly the two survivable-contact states do not block, so the gate
    # cannot be re-widened into an absorbing one - nor narrowed back into one.
    not_blocking = [m for m in SceneSafety if not m.blocks_patient_contact]
    assert not_blocking == [
        SceneSafety.SAFE,
        SceneSafety.UNSAFE_ADJACENT_COMMITTED,
    ]


def test_the_scene_gate_never_lowers_the_criticality_of_a_known_arrest() -> None:
    # falsifier: an arrest waiting behind an unresolved scene asks for SEVERE
    # (4) rather than CRITICAL (5), so the gate DOWNGRADES a reported arrest -
    # the clinician on the bridge sees a 4 and pages accordingly while the
    # patient is pulseless. Verified before the fix: scene unknown +
    # UNRESPONSIVE + breathing=NONE gave SEVERE.
    for scene in (Finding.unknown(), established(SceneSafety.UNSAFE_ADJACENT)):
        arrest = decide(
            AssessmentInputs(
                scene_safe=scene,
                responsiveness=established(Responsiveness.UNRESPONSIVE),
                breathing=established(Breathing.NONE),
            )
        )
        assert arrest.criticality is Criticality.CRITICAL, (
            f"the scene gate lowered a known arrest to {arrest.criticality}"
        )
        assert not arrest.step.is_instruction or (
            arrest.step is AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE
        )

    # A scene gate with no arrest behind it keeps its own floor, so the rule
    # above is not just "the gate always says 5".
    nothing_known = decide(AssessmentInputs())
    assert nothing_known.step is AssessmentStep.ASK_SCENE_SAFE
    assert nothing_known.criticality is Criticality.SEVERE


def test_the_scene_question_pins_a_closed_hazard_list_phrasing() -> None:
    # falsifier: `question_key="scene_safe"` leaves the ONE gate between a
    # reported arrest and compressions to be phrased by the non-deterministic
    # layer. "Is the scene safe?" invites a freeze ("I don't know... I think
    # so?") or a reflexive "yes" carrying no information, and unlike the
    # breathing question nothing pinned it. The answerable form is a closed
    # hazard list answerable yes or no.
    branch = decide(AssessmentInputs())
    assert branch.step is AssessmentStep.ASK_SCENE_SAFE
    lowered = branch.rationale.lower()
    for hazard in ("traffic", "fire", "electricity"):
        assert hazard in lowered, (
            f"the pinned phrasing must name {hazard} as a closed hazard; "
            f"got {branch.rationale!r}"
        )
    assert "yes or no" in lowered, "and commit L3 to an answerable closed form"
    assert "is the scene safe" in lowered, (
        "and name the open form it must NOT use, the way the breathing "
        "question names the bare yes/no it must not use"
    )


def test_letters_are_ordered_and_the_order_has_one_source() -> None:
    # falsifier: the assessment order is written down twice - once in the enum
    # and once in a list some caller iterates - and the two drift, so C is
    # assessed before B on one code path and nobody notices because both look
    # right in isolation.
    assert LETTERS_IN_ORDER == tuple(Letter)
    assert [letter.order for letter in LETTERS_IN_ORDER] == [0, 1, 2, 3, 4]
    assert Letter.A.precedes(Letter.B)
    assert not Letter.E.precedes(Letter.A)


# ----------------------------------- ASM-11: BLACK/expectant does not exist here


def test_criticality_still_cannot_express_black() -> None:
    # falsifier: someone "closes the gap" by adding an EXPECTANT or BLACK member
    # to Criticality, or a parallel expectancy axis beside it. The type's
    # inability to express BLACK is the CORRECT property - BLACK means "I am
    # walking past this person to reach someone I can save", and a bystander
    # with one patient and nowhere else to go never makes that decision.
    # ASM-11.
    assert [member.name for member in Criticality] == [
        "MINOR",
        "LOW",
        "MODERATE",
        "SEVERE",
        "CRITICAL",
    ]
    assert max(Criticality) is Criticality.CRITICAL


def test_apneic_and_pulseless_is_always_critical_plus_cpr() -> None:
    # falsifier: THE FATAL PATH. An apneic pulseless toddler is categorised
    # expectant and the agent tells a parent kneeling over their child that
    # there is nothing more to do. In a single-patient incident that
    # presentation is paediatric cardiac arrest, and the correct action is five
    # rescue breaths then CPR - the one intervention with a real survival curve
    # in children. ASM-11.
    for age in AgeBand:
        for breathing in (Breathing.NONE, Breathing.ABNORMAL_OR_GASPING):
            branch = decide(
                _breathing_case(
                    breathing, age=age, pulse=PulseReport.REPORTED_ABSENT
                )
            )
            assert branch.criticality is Criticality.CRITICAL, (
                f"{age} {breathing} with no pulse gave {branch.criticality}"
            )
            assert branch.step in (
                AssessmentStep.INSTRUCT_CPR,
                AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR,
            ), f"{age} {breathing} with no pulse gave {branch.step}"


def test_no_reachable_branch_withholds_resuscitation_from_an_arrest() -> None:
    # falsifier: some combination of age, pulse, submersion and bleeding reaches
    # a leaf that neither resuscitates nor escalates - the expectant leaf,
    # arrived at sideways rather than by a member named BLACK. Walks the entire
    # input space, so this is a statement about every path. ASM-11.
    # Every step that actually resuscitates. The combined bleeding-plus-CPR leaf
    # belongs here and is not an exception to the rule: it delivers compressions,
    # at CRITICAL, and additionally controls the source. A step that merely
    # controlled bleeding would NOT belong here, which is what makes this set the
    # real assertion rather than a list widened until the test passed.
    resuscitating = {
        AssessmentStep.INSTRUCT_CPR,
        AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR,
    }
    # The two branches that may legitimately precede resuscitation, and the only
    # two: both are QUESTIONS about an earlier ABCDE letter, so they delay by one
    # turn and cannot terminate. Anything else appearing here - a monitor
    # instruction, a recovery position, a bleeding question - would be a leaf that
    # withholds resuscitation from an arrest, which is the expectant leaf reached
    # sideways. Listed explicitly rather than skipped by a `continue`, because a
    # `continue` would also silently excuse a genuinely wrong branch.
    # Extended by ONE member, and the argument for it is specific rather than a
    # widening to make a test pass.
    #
    # INSTRUCT_CONTROL_BLEEDING appears here for exactly one cell of the space:
    # an established catastrophic bleed with breathing non-normal and
    # consciousness NOT YET established. Two safety rules meet there - "never
    # defer an established bleed behind a question" (Blocker 1) and "never delay
    # resuscitation" (ASM-11) - and something has to go first.
    #
    # The bleed goes first, for three reasons. It is safe under BOTH hypotheses
    # about consciousness, which is the scoped worse-branch rule for an
    # instruction: pressure on a wound is right whether or not this patient is
    # in arrest. Compressions are NOT safe under both, because an awake patient
    # must never be compressed. And the delay is one turn, after which
    # consciousness is established and the combined bleeding-plus-CPR leaf is
    # reached - which is asserted immediately below, so this precursor cannot
    # become a terminal state.
    #
    # The CRITICAL category is unaffected either way, so nothing is
    # under-triaged while this happens: that is asserted for every precursor.
    #
    # AND ONE MORE, ADDED BY BLOCKER 3 OF THE THIRD CLINICAL REVIEW, with the
    # narrowest argument of any of them because it is the only precursor that
    # delays compressions on a patient already established as being in arrest.
    #
    # INSTRUCT_BREAK_ELECTRICAL_CONTACT belongs here because it is not a delay
    # that trades the patient's life for something else - it is the ONLY route
    # by which this patient can be resuscitated at all. Compressions on a
    # casualty still completing a circuit electrocute the responder, so the
    # alternative to this branch is not "compressions sooner", it is "two
    # casualties and nobody compressing". The DR gate's founding case is exactly
    # this one, and Blocker 3(a)'s absorbing-loop objection does not apply
    # because the state is not absorbing: breaking the contact or isolating the
    # supply changes `scene_safe`, and the next turn reaches compressions at
    # CRITICAL - asserted below with the other precursors, so this cannot become
    # a terminal state either.
    #
    # It is also why this is the one hazard class with no committed escape
    # hatch: for traffic, "advance anyway" accepts a risk to a responder who has
    # already accepted it, and here it transfers the arrest to them.
    permitted_precursors = {
        AssessmentStep.ASK_SCENE_SAFE,
        AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE,
        AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT,
        AssessmentStep.ASK_RESPONSIVENESS,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING,
    }
    # AN ARREST IS THE CONJUNCTION, not the breathing value alone. This filter
    # used to be "breathing is non-normal", full stop, which asserted that an
    # ALERT patient with abnormal breathing must eventually be compressed - an
    # independent clinical review found that harmful and traced it to ASM-13's
    # row having dropped the "unresponsive AND" half of the guideline this repo
    # states three times. The arrest space is the unconscious patient, plus the
    # patient whose consciousness is not yet established, who keeps the full
    # strictness because an unestablished input must not lower a reported
    # arrest. The awake patient is covered by the MIRROR test below, so nothing
    # became unasserted - the two together still cover every non-normal
    # breathing path in the space.
    def _is_arrest_space(assignment: dict[str, object]) -> bool:
        breathing = assignment["breathing"]
        if breathing not in (Breathing.NONE, Breathing.ABNORMAL_OR_GASPING):
            return False
        responsiveness = assignment["responsiveness"]
        return responsiveness is None or responsiveness.is_unconscious

    for assignment, branch in ALL_BRANCHES:
        if not _is_arrest_space(assignment):
            continue
        if branch.step in permitted_precursors:
            assert branch.criticality >= Criticality.SEVERE, (
                f"{assignment} delayed resuscitation at a category below SEVERE"
            )
            continue
        assert branch.step in resuscitating, (
            f"{assignment} reached {branch.step} instead of resuscitation"
        )
        assert branch.criticality is Criticality.CRITICAL

    # And with scene and responsiveness both established, there is no precursor
    # left: non-normal breathing in an unconscious patient resuscitates,
    # unconditionally.
    # The scene must be established AND not blocking. Both non-blocking scene
    # states count, which is Blocker 3's escape: a committed responder beside a
    # patient in an unsafe lay-by advances and gets compressions, where the old
    # `scene_safe is True` would have excused the absorbing loop from this
    # assertion entirely.
    covered = 0
    for assignment, branch in ALL_BRANCHES:
        responsiveness = assignment["responsiveness"]
        scene = assignment["scene_safe"]
        if (
            _is_arrest_space(assignment)
            and scene is not None
            and not scene.blocks_patient_contact
            and responsiveness is not None
        ):
            covered += 1
            assert branch.step in resuscitating, (
                f"{assignment} reached {branch.step} instead of resuscitation"
            )
            assert branch.criticality is Criticality.CRITICAL
    assert covered > 1000, (
        f"the unconditional-resuscitation claim must cover real ground; "
        f"only {covered} combinations matched"
    )

    # THE PRECURSORS CANNOT TERMINATE. Each one is only acceptable above because
    # answering it advances; a precursor that loops is the expectant leaf with a
    # question mark on it, which is precisely what Blocker 3's absorbing scene
    # gate was. Proven by answering each one and re-deciding.
    bleeding_arrest_unknown_consciousness = AssessmentInputs(
        scene_safe=SAFE,
        breathing=established(Breathing.ABNORMAL_OR_GASPING),
        severe_bleeding=established(SevereBleeding.PRESENT),
    )
    first = decide(bleeding_arrest_unknown_consciousness)
    assert first.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING
    assert first.criticality is Criticality.CRITICAL, (
        "the bleed-first turn must not lower a possible arrest's category"
    )
    answered = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            severe_bleeding=established(SevereBleeding.PRESENT),
        )
    )
    assert answered.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR, (
        "establishing consciousness must reach resuscitation on the next turn"
    )

    # And the same for the scene precursor, which Blocker 3 is entirely about.
    committed = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.UNSAFE_ADJACENT_COMMITTED),
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
        )
    )
    assert committed.step in resuscitating

    # AND THE IN-THE-PATIENT PRECURSOR, which is the one that most needs this
    # assertion: a gate in front of an established arrest that could not be
    # answered would be the expectant leaf with a warning on it. Breaking the
    # contact is reported as a change to the SCENE - the hazard is no longer in
    # the patient - and both answers a responder can give advance to
    # compressions at CRITICAL.
    in_circuit = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.UNSAFE_IN_PATIENT),
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
        )
    )
    assert in_circuit.step is AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT
    assert in_circuit.criticality is Criticality.CRITICAL, (
        "the in-the-patient gate must not lower a known arrest's category"
    )
    for resolved in (
        SceneSafety.SAFE,
        SceneSafety.UNSAFE_ADJACENT_COMMITTED,
    ):
        freed = decide(
            AssessmentInputs(
                scene_safe=established(resolved),
                responsiveness=established(Responsiveness.UNRESPONSIVE),
                breathing=established(Breathing.NONE),
            )
        )
        assert freed.step in resuscitating, (
            f"breaking the contact into {resolved.name} must reach "
            f"resuscitation on the next turn, or the gate is absorbing"
        )
        assert freed.criticality is Criticality.CRITICAL


def test_no_reachable_branch_compresses_the_chest_of_an_awake_patient() -> None:
    # falsifier: THE MIRROR OF THE ROW ABOVE, and the defect an independent
    # clinical review found in the first version of the choking path. A patient
    # established as ALERT, CONFUSED or VOCAL - awake, therefore perfusing their
    # brain, therefore with a circulation - is told to receive chest
    # compressions because their breathing was reported abnormal. The conscious
    # asthmatic, the anaphylaxis, the pulmonary oedema and the partial airway
    # obstruction all present exactly this way; all are common; and all are made
    # WORSE by being laid flat and compressed, quite apart from the rib
    # fractures and the struggle of a conscious person being compressed.
    # Walks the entire input space, so this is a statement about every path.
    #
    # RE-SCOPED TO ABNORMAL_OR_GASPING, AND THE RE-SCOPING IS THE POINT.
    #
    # This loop originally demanded no compressions ANYWHERE in the awake space,
    # including for breathing=NONE. That pinned a defect as correct, and a
    # second independent clinical review made it the lead finding: an awake
    # report beside an absent-breathing report sent the patient to the
    # conscious-distress leaf at Criticality 4, told to sit upright and use an
    # inhaler. `_is_arrest` already excluded only established-and-awake for the
    # CATEGORY while the instruction side gated on both non-normal states, which
    # is why the same patient came out at 4 rather than 5.
    #
    # The clinical claim this row actually defends is about RESPIRATORY
    # DISTRESS, not about apnoea. The asthmatic, the anaphylaxis, the pulmonary
    # oedema and the partial obstruction named above all present as
    # ABNORMAL_OR_GASPING - that is what "awake and breathing badly" means. None
    # of them presents as NONE, because no air movement ends alertness in
    # seconds. So the row is narrowed to the state it was always about, and the
    # apnoea case is asserted the other way in
    # `test_absent_breathing_reaches_the_arrest_path_whatever_responsiveness_says`.
    #
    # Stated explicitly so a future author who reaches the apnoea case does not
    # have to break two named tests and guess which side was right.
    compressions = {
        AssessmentStep.INSTRUCT_CPR,
        AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR,
    }
    awake = (
        Responsiveness.ALERT,
        Responsiveness.CONFUSED,
        Responsiveness.VOCAL,
    )
    checked = 0
    for assignment, branch in ALL_BRANCHES:
        if assignment["responsiveness"] not in awake:
            continue
        # Absent breathing is the contradiction, not a distress presentation.
        if assignment["breathing"] is Breathing.NONE:
            continue
        checked += 1
        assert branch.step not in compressions, (
            f"{assignment} told a conscious patient to receive compressions "
            f"via {branch.step}"
        )
    assert checked > 1000, (
        f"the awake space must actually be walked; only {checked} matched"
    )

    # And the awake patient breathing badly is not merely spared compressions -
    # they get the right action, and it names the deterioration to watch for.
    # A branch that spared them by saying nothing useful would pass the loop
    # above while leaving a patient in severe distress with no instruction.
    distressed = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.NONE),
        )
    )
    assert (
        distressed.step
        is AssessmentStep.INSTRUCT_SUPPORT_SEVERE_BREATHING_DIFFICULTY
    )
    assert distressed.criticality is Criticality.SEVERE, (
        "awake is not CRITICAL, and the ratchet must be able to raise it when "
        "they stop being awake"
    )
    lowered = distressed.rationale.lower()
    # The position that helps must still be named. Re-worded by Defect 4's fix
    # from a commanded "sit them upright" to the position of COMFORT, because
    # the commanded form contradicted protocols.py:anaphylaxis and outranked it
    # - see `test_the_distress_leaf_states_no_positioning_imperative_it_cannot
    # _justify`. What this row cares about is that A position is named at all,
    # since a leaf that spares them compressions and says nothing useful would
    # otherwise pass.
    assert "sit up" in lowered and "lean forward" in lowered, (
        "the position that helps must be named"
    )
    assert "not" in lowered and "compressions" in lowered, (
        "and the harmful action must be named as the one NOT to take"
    )
    assert "stop responding" in lowered or "go limp" in lowered, (
        "the transition to compressions must be stated, because this patient "
        "is the one who may arrest while the responder watches"
    )


# ------------- RE-REVIEW DEFECT 1: apnoea is never a conscious-distress leaf


def _contradiction_clause_text(branch: AssessmentBranch) -> str:
    """The contradiction clause's own words, isolated FROM A REAL BRANCH.

    Isolation is needed because the structural assertion in
    `test_the_apnoea_re_test_is_invisible_and_does_not_relitigate` bans
    referring to the caller's report, and the SURROUNDING arrest rationale
    legitimately uses some of those words ("not breathing normally is the whole
    indication"). Applying the ban to the whole rationale would therefore fail
    on text this change never touched.

    ISOLATED BY DIFFERENCING TWO `decide` RESULTS, NOT BY CALLING
    `_contradiction_clause`. The blocker-3 round lost a real defect to a test
    that called `_hazard_suffix` directly and so never noticed that the only
    step a fire scene reaches never used it - 98 tests passed over dead
    clinical content. A clause nobody speaks is not fixed, so this reads the
    words a bystander would actually hear: the unconscious twin of the same
    patient reaches the same leaf with no contradiction to re-test, so
    whatever the awake branch says beyond it is the clause and nothing else.
    """
    baseline = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.NONE),
        )
    )
    assert baseline.step is branch.step, (
        "the differencing baseline must reach the SAME leaf, or this "
        f"subtracts the wrong rationale: {baseline.step} vs {branch.step}"
    )
    common = baseline.rationale
    assert branch.rationale.startswith(common), (
        "the clause is documented as APPENDED to the shared arrest rationale; "
        "if that stopped being true this helper is silently measuring nothing"
    )
    clause = branch.rationale[len(common):].strip()
    assert clause, (
        "the awake branch says nothing the unconscious one does not, so the "
        "contradiction is being resolved SILENTLY - the re-review's lead "
        "defect, back"
    )
    return clause.lower()


def test_absent_breathing_reaches_the_arrest_path_whatever_responsiveness_says(
) -> None:
    # falsifier: THE RE-REVIEW'S LEAD FINDING, AND TWO NAMED TESTS PINNED IT AS
    # CORRECT. A caller reports a patient as alert AND as not breathing at all.
    # The responsiveness conjunction was applied to BOTH non-normal breathing
    # states, so the patient went to the conscious-distress leaf at Criticality
    # 4 and the bystander was told to sit them upright and help them use a
    # reliever inhaler. Reproduced exactly that way before the fix.
    #
    # The two reports cannot both be true, and the reassuring half is the one
    # more likely to be wrong: mistaking a posturing or agonal casualty for an
    # awake one is the commonest error in the first sixty seconds of a panicking
    # call, whereas "not breathing" is a hard thing to report by accident. So
    # apnoea wins the contradiction and the patient reaches resuscitation.
    for awake in (
        Responsiveness.ALERT,
        Responsiveness.CONFUSED,
        Responsiveness.VOCAL,
    ):
        branch = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(awake),
                breathing=established(Breathing.NONE),
                airway_obstruction=NO_OBSTRUCTION,
                severe_bleeding=established(SevereBleeding.NONE),
            )
        )
        assert branch.step is AssessmentStep.INSTRUCT_CPR, (
            f"a patient reported {awake} and not breathing at all reached "
            f"{branch.step} instead of resuscitation"
        )
        assert branch.criticality is Criticality.CRITICAL, (
            "and the category must be the arrest's, not the reassuring half's"
        )
        # AND THE CONTRADICTION IS RE-TESTED RATHER THAN RESOLVED SILENTLY.
        # This is the half of the fix that is not a routing change: the reviewer
        # asked for the contradiction to be surfaced, and a branch that simply
        # overrode the awake report would pass the assertions above while
        # hiding the thing a dispatcher most needs to resolve.
        #
        # WHAT IS ASSERTED IS THE OBSERVATION REQUEST, NOT THE WORD
        # "CONTRADICTION". Blocker 4 of the third clinical review: this block
        # used to demand "contradiction", the awake value quoted back, "look
        # again" and "do not stop compressions" - which pinned the RELITIGATING
        # wording as correct, and would have made a future author fixing the
        # register break a named test. The clinical requirement is that the
        # branch ask for an observation whose answer changes the route; it is
        # not that the branch announce the caller was wrong. See
        # `_contradiction_clause` and
        # `test_the_apnoea_re_test_is_invisible_and_does_not_relitigate`.
        lowered = branch.rationale.lower()
        assert "tell me if he tries to speak" in lowered, (
            "the branch must ask for the observation that settles which "
            "patient this is, so the caller corrects themselves rather than "
            "being silently overridden"
        )
        assert "watch the patient's face" in lowered, (
            "and must name where to look, since a responder mid-compression "
            "can only make an observation that needs no hands"
        )
        assert "the instruction changes" in lowered, (
            "and must say that the answer changes the route, which is why the "
            "re-test is worth words at all"
        )


def test_the_apnoea_re_test_is_invisible_and_does_not_relitigate() -> None:
    # falsifier: BLOCKER 4 OF THE THIRD CLINICAL REVIEW. The re-test is spoken
    # as meta-commentary on the caller's own report - "NOTE THE CONTRADICTION
    # AND SAY IT OUT LOUD", "those two things cannot both be true", "tell them
    # what they told you" - so a bystander with both hands on a chest is told
    # they contradicted themselves and has to defend what they said. The
    # concrete harm is a STOP: a responder arguing about their report is a
    # responder not compressing, and no-flow time is the thing that decides
    # whether this patient survives. "Do not stop compressions" does not
    # cancel it, because it is a mitigation bolted onto a sentence that invites
    # the stop.
    #
    # ASSERTED AT THE BRANCH, THROUGH `decide`, NOT BY CALLING THE CLAUSE
    # HELPER. Blocker 3's round cost a real defect exactly that way: a test
    # called `_hazard_suffix` directly and hid the fact that the only step a
    # fire scene reaches never used it, so 98 tests passed over dead clinical
    # content. A clause nothing speaks is a clause that is not fixed.
    #
    # AND THE ASSERTIONS ARE IMPERATIVES, NOT WORDS. The same round's other
    # lesson: a bare substring survived deleting the rule because the word
    # appeared twice elsewhere in the rationale. Each banned string below is a
    # phrase that can ONLY be relitigation, and each required string below is
    # the observation request itself.
    for awake in (
        Responsiveness.ALERT,
        Responsiveness.CONFUSED,
        Responsiveness.VOCAL,
    ):
        branch = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(awake),
                breathing=established(Breathing.NONE),
                airway_obstruction=NO_OBSTRUCTION,
                severe_bleeding=established(SevereBleeding.NONE),
            )
        )
        lowered = branch.rationale.lower()

        # 1. THE RELITIGATION IS GONE, AND THIS IS CHECKED STRUCTURALLY
        # BEFORE IT IS CHECKED BY PHRASE. An INDEPENDENT REVIEW of this very
        # change found that the phrase list below, on its own, checks WORDS AND
        # NOT THE RULE - which is the repo's signature failure mode and the
        # named warning from the blocker-3 round. Its mutant was executed and
        # SURVIVED: prepending "POINT OUT THAT BOTH OF THEIR REPORTS CANNOT BE
        # RIGHT, and make them account for the one they got wrong." while
        # leaving every required substring intact passed all 100 tests in this
        # file. Rephrased relitigation carries the identical harm - a responder
        # arguing about their report instead of compressing - so a denylist of
        # five historical strings is not the assertion this row needs.
        #
        # The structural property is that the clause SPEAKS ONLY ABOUT THE
        # PATIENT, NEVER ABOUT THE CALLER'S REPORT. Relitigation is not a
        # vocabulary, it is a referent: every form of it - "note the
        # contradiction", "point out that both reports cannot be right", "make
        # them account for it" - has to refer to what the caller SAID or TOLD
        # you, or to their REPORT, in order to be relitigation at all. An
        # observation request refers to the patient and to what is visible now.
        # So the reference itself is banned, which no rephrasing can avoid
        # while still doing the harmful thing.
        clause = _contradiction_clause_text(branch)
        for referent in (
            "told",
            "said",
            "say",
            "report",
            "describ",
            "contradic",
            "claim",
            "wrong",
            "not match",
            "account for",
        ):
            assert referent not in clause, (
                f"{awake} + absent breathing refers to the caller's own "
                f"report ({referent!r}), which is relitigation whatever words "
                f"carry it - the clause must speak only about the patient and "
                f"what is visible now. Clause: {clause}"
            )

        # AND THEN THE HISTORICAL PHRASES, kept as well rather than instead.
        # The structural rule above is the real assertion; these five are the
        # reviewer's verbatim quotations, and a regression that restored the
        # exact old sentence should fail with a message naming it rather than
        # with a generic referent complaint.
        for banned, why in (
            (
                "note the contradiction",
                "the operator is told to announce the contradiction, which is "
                "the sentence that invites the stop",
            ),
            (
                "cannot both be true",
                "the caller is told their two reports are mutually "
                "impossible, which is relitigation stated as logic",
            ),
            (
                "tell them what they told you",
                "the operator is told to quote the caller's report back at "
                "them, so the caller has to defend it mid-compression",
            ),
            (
                "say it out loud",
                "the announcement is commanded explicitly",
            ),
            (
                "do not stop compressions",
                "a mitigation for a stop the sentence should not have "
                "invited; needing it is the evidence the register is wrong",
            ),
        ):
            assert banned not in lowered, (
                f"{awake} + absent breathing relitigates the caller's "
                f"report: {why}. Found {banned!r} in: {branch.rationale}"
            )

        # 2. AND THE RE-TEST SURVIVED THE DELETION OF THE RELITIGATION, which
        # is the other half and the one a careless fix loses. The contradiction
        # genuinely needs surfacing - a wrong responsiveness report is the
        # likeliest error in the first sixty seconds and the answer CHANGES THE
        # BRANCH - so a clause that merely went quiet would pass the block
        # above while silently resolving the contradiction in favour of the
        # reassuring half, which is the re-review's lead defect returning.
        assert "tell me if he tries to speak" in lowered, (
            f"{awake} + absent breathing no longer asks for the observation "
            f"that settles the contradiction; deleting the relitigation must "
            f"not delete the re-test. Got: {branch.rationale}"
        )
        # The observation is one a responder mid-compression can actually make:
        # it needs their eyes, not their hands, and it is folded into the
        # action already under way rather than standing beside it.
        assert "keep compressing" in lowered, (
            "the re-test must be folded INTO the compressions; an observation "
            "request that stands beside them is a request to pause"
        )
        assert "watch the patient's face" in lowered, (
            "and must name a hands-free place to look, since both of this "
            "responder's hands are committed"
        )
        # And the answer is stated as route-changing in both directions, which
        # is what makes it a re-test rather than a reassurance.
        assert "is not in arrest and the instruction changes" in lowered, (
            "a positive answer must be stated as changing the instruction"
        )
        assert "needs exactly the compressions that are already going" in (
            lowered
        ), "and a negative answer must confirm the compressions, not pause them"

        # 3. IT IS IN `ASK_AIRWAY_OBSTRUCTION`'S REGISTER, which is the shape
        # the reviewer named and which this module already contains. That
        # question re-tests the same wrong "alert" report and states in its own
        # words that the caller corrects themselves WITHOUT BEING TOLD they
        # were wrong. Both surfaces must share that property, or the module
        # holds two answers to one clinical question and the next author
        # copies whichever they read first.
        asked = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(awake),
                breathing=established(Breathing.NONE),
                severe_bleeding=established(SevereBleeding.NONE),
            )
        )
        assert asked.step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION, (
            "the register being copied must still be reachable, or this row "
            "compares against a step nothing speaks"
        )
        assert "without ever being told they were wrong" in (
            asked.rationale.lower()
        ), (
            "the model this clause copies must still state the invisible-"
            "re-test property it is being copied for"
        )


def test_the_contradiction_wording_gap_is_recorded_as_closed_and_not_as_open(
) -> None:
    # falsifier: the same failure the hazard-class mirror test exists for, on
    # Blocker 4. The recorded-gaps block keeps describing the relitigating
    # wording as an OPEN gap after it has been rewritten, so the next author
    # reads a warning about words that are no longer in the file, goes looking
    # for them, and either "fixes" it a second time or concludes the gaps block
    # is stale and stops trusting the rest of it - which is the block that
    # currently carries the pulmonary-oedema harm. A gap list that lies in the
    # safe direction still destroys its own credibility.
    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    lowered = " ".join(source.lower().split())
    for owed, why in (
        (
            "`_contradiction_clause`'s wording is fixed",
            "the gap must be marked closed where it was recorded, not "
            "silently deleted - a deleted gap looks like one nobody found",
        ),
        (
            "its routing was never the defect",
            "and must say which half moved, since the routing is the part a "
            "reader is most likely to 'fix' by mistake",
        ),
        (
            "the re-test itself was not deleted",
            "and must record that the contradiction is still surfaced, or the "
            "next author reads the fix as permission to drop the re-test",
        ),
    ):
        assert owed in lowered, f"the module must record: {why}"

    # And the claim is not merely prose: the words it says it now speaks are
    # the words a reachable branch actually speaks. A recorded fix whose
    # mechanism is absent is this repo's signature failure mode - a mechanism
    # whose only implementation is its own description - which the third review
    # counted five instances of.
    spoken = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.NONE),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.NONE),
        )
    ).rationale.lower()
    assert "tell me if he tries to speak" in spoken, (
        "the module records the wording gap as closed by an observation "
        "request that no reachable branch speaks"
    )


def test_the_apnoea_contradiction_is_not_re_asked_as_a_question() -> None:
    # falsifier: the contradiction is surfaced by returning to
    # ASK_RESPONSIVENESS, which reads as the careful option and is an ABSORBING
    # LOOP. L2 is pure and holds no turn history (ASM-01), so a caller who
    # re-reports the same two values gets the same question back forever - and
    # this is the one patient with no time at all. It is Blocker 3(a)'s
    # absorbing scene gate rebuilt on the apnoeic patient. The re-test therefore
    # lives INSIDE the instruction, where it costs nothing.
    inputs = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.ALERT),
        breathing=established(Breathing.NONE),
        airway_obstruction=NO_OBSTRUCTION,
        severe_bleeding=established(SevereBleeding.NONE),
    )
    first = decide(inputs)
    assert first.step is not AssessmentStep.ASK_RESPONSIVENESS, (
        "re-asking an input the caller has already answered cannot advance in "
        "a pure function; it is an absorbing loop"
    )
    # And purity means the loop would be real: the same inputs give the same
    # branch, so there is no turn on which the question would stop being asked.
    assert decide(inputs).step is first.step


def test_a_responsive_patient_is_asked_the_choking_question_before_compressions(
) -> None:
    # falsifier: FOUND BY INDEPENDENT REVIEW IN DEFECT 1'S FIRST FIX, and it was
    # reachable on the FIRST TURN of the most likely presentation. Defect 1's
    # routing gated BOTH the choking question and the choking instruction on
    # `not _routes_to_arrest_path(inputs)`. Since that predicate only stood
    # aside for an ESTABLISHED witnessed obstruction, a patient reported ALERT
    # and not breathing with the obstruction question NOT YET ASKED skipped the
    # whole block and went straight to INSTRUCT_CPR - so a conscious
    # complete-obstruction choker was told to compress and was never asked the
    # discriminator. Blocker 5's death, reintroduced through Defect 1's fix.
    #
    # The question must outrank the arrest route because it DECIDES the arrest
    # route, and its pinned phrasing does double duty: it finds the choker and
    # it re-tests the awake report ("can he answer you?"). One turn, safe under
    # both hypotheses.
    for awake in (
        Responsiveness.ALERT,
        Responsiveness.CONFUSED,
        Responsiveness.VOCAL,
    ):
        branch = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(awake),
                breathing=established(Breathing.NONE),
                # The whole point: NOT established.
                airway_obstruction=Finding.unknown(),
            )
        )
        assert branch.step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION, (
            f"a responsive patient reported not breathing, with the choking "
            f"question unasked, reached {branch.step} - so the one question "
            f"that finds a conscious choker was skipped"
        )
        # AND THE GATE MUST NOT LOWER THE REPORTED ARREST'S CATEGORY. Blocker
        # 3(b): the step is the question's, the category is not the question's
        # to cap. Absent breathing is CRITICAL whatever was said about
        # responsiveness, which is the same asymmetry `routes_to_cpr` applies.
        assert branch.criticality is Criticality.CRITICAL, (
            "a gate in front of a reported arrest must not cap it at SEVERE"
        )

    # AND THE QUESTION ADVANCES - it is a precursor, not an absorbing loop.
    # Both answers move the patient on, which is what makes spending the turn
    # legitimate at all.
    for answer, expected in (
        (AirwayObstruction.WITNESSED_FOREIGN_BODY,
         AssessmentStep.INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION),
        (AirwayObstruction.NONE, AssessmentStep.INSTRUCT_CPR),
    ):
        advanced = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(Responsiveness.ALERT),
                breathing=established(Breathing.NONE),
                airway_obstruction=established(answer),
                severe_bleeding=established(SevereBleeding.NONE),
            )
        )
        assert advanced.step is expected, (
            f"answering {answer} must advance; got {advanced.step}"
        )

    # AND A BLEEDING APNOEIC PATIENT REPORTED AWAKE STILL REACHES THE COMBINED
    # LEAF, not bare bleeding control. Found by mutation testing: over-claiming
    # the choking block for an established-NONE obstruction made
    # `_routes_to_arrest_path` under-report, the bleed hoist fired, and the
    # patient got INSTRUCT_CONTROL_BLEEDING with no compressions at all - an
    # arrest treated as a bleed, which is Blocker 2's defect in reverse. The
    # established-bleed walk permits bare bleeding control by design, so
    # nothing else in the suite covers this combination.
    bleeding_apnoeic = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.NONE),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.PRESENT),
        )
    )
    assert (
        bleeding_apnoeic.step
        is AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR
    ), (
        "an apnoeic patient with a catastrophic bleed must get the COMBINED "
        f"instruction, not bleeding control alone; got {bleeding_apnoeic.step}"
    )
    assert bleeding_apnoeic.criticality is Criticality.CRITICAL

    # The conscious asthmatic waiting behind the same question is NOT lifted to
    # CRITICAL, so the category still tracks the finding rather than the gate.
    asthmatic = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            airway_obstruction=Finding.unknown(),
        )
    )
    assert asthmatic.step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION
    assert asthmatic.criticality is Criticality.SEVERE, (
        "an awake patient breathing abnormally is not an arrest; only absent "
        "breathing lifts this gate to CRITICAL"
    )


def test_a_witnessed_obstruction_still_reconciles_awake_with_no_breathing(
) -> None:
    # falsifier: Defect 1's fix OVER-REACHES and swallows the one case where an
    # awake report and an absent-breathing report are BOTH accurate. A complete
    # foreign-body obstruction in a conscious patient moves no air at all, and
    # the caller saying "he's not breathing" is telling the truth. Compressions
    # cannot shift a lodged bolus, so routing this patient to CPR would spend
    # the window that back blows and thrusts needed - Blocker 5's death, in the
    # other direction. Verified during the fix: this case did regress to
    # INSTRUCT_CPR before the exemption was added, and it also deferred an
    # established bleed behind the choking instruction.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.NONE),
            airway_obstruction=established(
                AirwayObstruction.WITNESSED_FOREIGN_BODY
            ),
            severe_bleeding=established(SevereBleeding.NONE),
        )
    )
    assert branch.step is AssessmentStep.INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION
    assert branch.letter is Letter.A


# ------------------ BLOCKER 1: an established bleed is never deferred


def test_no_reachable_branch_defers_an_established_catastrophic_bleed() -> None:
    # falsifier: THE SIBLING OF THE ARREST WALK, and the review asked for it by
    # name. A bystander kneeling on a femoral bleed is asked "is he breathing
    # normally, or are there occasional noisy gasping breaths?" - so they take
    # their hands off the wound to look at the chest, or they answer and wait.
    # Exsanguination is measured in two to three minutes, and the airway of a
    # talking patient is self-evidently patent. This is the C-ABC / MARCH
    # reordering, and it exists for exactly this. Verified reachable before the
    # fix: scene_safe=True + ALERT + bleeding PRESENT + breathing UNKNOWN gave
    # ASK_BREATHING_QUALITY, and bleeding PRESENT alone gave ASK_RESPONSIVENESS.
    # Walks the entire input space, so this is a statement about every path.
    treats_the_bleed = {
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR,
        # BLOCKER 1 OF THE THIRD CLINICAL REVIEW. The awake choker who is moving
        # no air AND bleeding: hands-free pressure, then thrusts. It treats the
        # bleed, so it belongs here rather than among the permitted exceptions.
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION,
    }
    # The ONLY things that may come between an established bleed and its
    # treatment, listed explicitly rather than skipped by a `continue`:
    #
    # - the THREE scene-gate steps, because a responder who becomes the second
    #   casualty cannot hold pressure on anything. The third is
    #   INSTRUCT_BREAK_ELECTRICAL_CONTACT, added by Blocker 3 of the third
    #   clinical review, and it is the clearest case of that principle in the
    #   module rather than an exception to it: holding pressure on the wound of
    #   a patient still completing a circuit is a bare-handed grip on an
    #   energised casualty, so this gate is not deferring the bleed for
    #   something less urgent - it is removing the thing that makes treating the
    #   bleed fatal to the person treating it. Like the other two it advances:
    #   once the contact is broken the bleed is instructed on the next turn,
    #   asserted in the arrest walk above;
    # - resuscitation, because an arrest with a bleed is a TRAUMATIC arrest, and
    #   the leaf that owns it treats the bleed AND compresses (Blocker 2). A
    #   bare INSTRUCT_CPR appearing here would be exactly that blocker; and
    # - the choking discriminator, added by Blocker 1 of the third clinical
    #   review and admitted here ONLY under the extra assertion below. That
    #   blocker was a bleed instruction that never mentioned the airway on a
    #   conscious patient with a lodged bolus and no air movement; the fix stands
    #   the hoist aside so the airway can be dealt with, and the one turn the
    #   discriminator costs is the turn that decides thrusts versus compressions.
    #   The bleed is not deferred by it - it is instructed in the same breath -
    #   and that is what the assertion below actually checks, because a permitted
    #   STEP with a silent rationale would be the original defect back again.
    permitted = {
        AssessmentStep.ASK_SCENE_SAFE,
        AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE,
        AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT,
        AssessmentStep.INSTRUCT_CPR,
        AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR,
        AssessmentStep.ASK_AIRWAY_OBSTRUCTION,
    }
    checked = 0
    asked_the_discriminator = 0
    for assignment, branch in ALL_BRANCHES:
        if assignment["severe_bleeding"] is not SevereBleeding.PRESENT:
            continue
        checked += 1
        if branch.step in treats_the_bleed:
            continue
        assert branch.step in permitted, (
            f"{assignment} deferred an established catastrophic bleed behind "
            f"{branch.step}"
        )
        if branch.step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION:
            # The one question allowed in front of an established bleed, and it
            # is allowed only because it does not defer the bleed. The pressure
            # is instructed WITH the question, hands-free, so both hands are
            # already free when the answer arrives.
            asked_the_discriminator += 1
            lowered = branch.rationale.lower()
            assert "bleeding" in lowered, (
                f"{assignment} asked the choking discriminator without "
                f"mentioning the established bleed at all"
            )
            for owed in ("pressure", "without using your hands"):
                assert owed in lowered, (
                    f"{assignment} asked the choking discriminator in front of "
                    f"an established bleed without instructing {owed!r}, so "
                    f"the bleed is deferred behind a question after all"
                )
            continue
        # Any other question is the thing that must never appear: the bleed is
        # ESTABLISHED, so there is nothing left to ask that outranks it.
        assert branch.is_instruction or branch.step is AssessmentStep.ASK_SCENE_SAFE, (
            f"{assignment} asked {branch.step} while a catastrophic bleed was "
            f"already established"
        )
    assert asked_the_discriminator > 0, (
        "the choking-discriminator-in-front-of-a-bleed case must actually be "
        "reached, or the clause above is asserted about nothing"
    )
    assert checked > 1000, (
        f"the established-bleed space must actually be walked; only {checked} "
        f"matched"
    )


def test_an_established_bleed_precedes_the_responsiveness_and_breathing_gates(
) -> None:
    # falsifier: the two exact reproductions from the clinical review. Both were
    # executed against the code before the fix and both returned a QUESTION,
    # which is a bystander with their hands off an arterial bleed. Named
    # separately from the walk above so the regression is legible as the
    # reviewer wrote it, rather than as one row of a 140k-combination sweep.
    talking_and_bleeding = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            severe_bleeding=established(SevereBleeding.PRESENT),
            # breathing deliberately UNKNOWN.
        )
    )
    assert talking_and_bleeding.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING, (
        "a talking patient's airway is self-evidently patent; the bleed wins"
    )
    assert talking_and_bleeding.unresolved_letter is Letter.C

    # AND THE HOIST MUST SOLICIT BOTH REPORTS THAT WOULD RE-ROUTE THIS PATIENT,
    # because that solicitation is the only escape from it. There is no
    # "bleeding controlled" input - a recorded gap - so nothing about the wound
    # re-routes them, and L2 never asks the airway question while the hoist
    # holds. `_airway_outranks_the_bleed_hoist` rests on a CALLER REPORT
    # arriving, so the rationale has to ask for one; without this the deferral
    # of the airway really would be indefinite, which is the defect Blocker 1 of
    # the third clinical review found in its established-obstruction form.
    lowered = talking_and_bleeding.rationale.lower()
    assert "breathe or to speak" in lowered, (
        "the hoist must ask the responder to report loss of air, which is the "
        "report that reaches the airway"
    )
    assert "saw something go into" in lowered, (
        "and to report a witnessed foreign body, which is the other one"
    )

    bleeding_only = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            severe_bleeding=established(SevereBleeding.PRESENT),
            # responsiveness and breathing both UNKNOWN.
        )
    )
    assert bleeding_only.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING

    # And the hoist is NARROW: it is a pre-A gate on an ESTABLISHED finding,
    # not a wholesale ABCDE reordering. An unknown bleed must NOT overtake the
    # breathing question, or the defect has simply moved one letter over.
    unknown_bleed = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
        )
    )
    assert unknown_bleed.step is not AssessmentStep.ASK_SEVERE_BLEEDING, (
        "hoisting the bleeding QUESTION would delay breathing behind it"
    )


# ------------------ BLOCKER 2: a bleeding arrest is told to stop the blood


def test_a_bleeding_arrest_is_told_to_control_the_haemorrhage_and_compress(
) -> None:
    # falsifier: THE PATH THAT SAYS NOTHING ABOUT THE BLOOD. Verified before the
    # fix: breathing=ABNORMAL_OR_GASPING + severe_bleeding=PRESENT returned bare
    # INSTRUCT_CPR, and INSTRUCT_CONTROL_BLEEDING was unreachable from there, so
    # the bystander received compressions-only guidance beside an arterial
    # bleed. "Arrest outranks a limb bleed" is right as a priority and wrong for
    # TRAUMATIC arrest: haemorrhage control is part of the resuscitation, and
    # compressions on an uncontrolled arterial bleed pump the remaining
    # circulating volume onto the road - the one situation where CPR is futile
    # without controlling the source.
    for breathing in (Breathing.NONE, Breathing.ABNORMAL_OR_GASPING):
        branch = decide(
            _breathing_case(breathing, bleeding=SevereBleeding.PRESENT)
        )
        assert (
            branch.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR
        ), f"{breathing} with a catastrophic bleed gave {branch.step}"
        # Still an arrest: CRITICAL, and still a resuscitation.
        assert branch.criticality is Criticality.CRITICAL
        lowered = branch.rationale.lower()
        # The instruction must actually say BOTH things. A step named for both
        # while the words say only one is the same silence with a better label.
        assert "compress" in lowered, "compressions must be instructed"
        assert "bleed" in lowered or "wound" in lowered, (
            "and the blood must be mentioned at all - silence is the worst "
            "available option here"
        )
        # The single-rescuer conflict is real, so the resolution must name a
        # hands-free method rather than leaving the bystander to choose.
        assert "knee" in lowered or "weight" in lowered, (
            "one person cannot pack a wound and compress at once, so the "
            "hands-free pressure method must be named in code rather than "
            "left for the LLM to improvise"
        )
        assert "pulse check" in lowered, "and the no-pulse-gate property holds"


def test_the_bleeding_arrest_leaf_keeps_the_hypoxic_breath_count() -> None:
    # falsifier: the new combined leaf is checked before the hypoxic split and
    # silently DROPS the five rescue breaths for a drowned or paediatric arrest
    # that also bleeds - so fixing Blocker 2 quietly reintroduces the harm
    # ASM-12 exists to prevent, in the narrow case where both apply.
    child = decide(
        _breathing_case(
            Breathing.NONE,
            age=AgeBand.CHILD,
            bleeding=SevereBleeding.PRESENT,
        )
    )
    assert child.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR
    assert str(RESCUE_BREATHS_BEFORE_COMPRESSIONS) in child.rationale, (
        "a bleeding paediatric arrest still needs the five breaths named"
    )

    drowned = decide(
        _breathing_case(
            Breathing.NONE,
            submersion=True,
            bleeding=SevereBleeding.PRESENT,
        )
    )
    assert str(RESCUE_BREATHS_BEFORE_COMPRESSIONS) in drowned.rationale

    # And an adult primary cardiac arrest with a bleed does NOT get a breath
    # count, or the number has stopped meaning anything.
    adult = decide(
        _breathing_case(Breathing.NONE, bleeding=SevereBleeding.PRESENT)
    )
    assert str(RESCUE_BREATHS_BEFORE_COMPRESSIONS) not in adult.rationale


# --------------------------------- ASM-12: the pulse check is never a branch point


def test_the_pulse_report_cannot_change_any_branch() -> None:
    # falsifier: lay pulse detection is roughly coin-flip accurate, and a branch
    # on it has two symmetric harms that BOTH end in no resuscitation - a false
    # "no pulse" routes to expectant, a false "pulse present" routes to the
    # still-apneic-after-breaths leaf. ILCOR/AHA/ERC removed the pulse check
    # from lay BLS for exactly this reason. ASM-12.
    #
    # Asserted by holding every other input fixed and varying only the pulse
    # across all four of its states, over the whole rest of the space.
    #
    # THIS ROW NOW OWNS THE PULSE AXIS OUTRIGHT, and that is a deliberate
    # transfer rather than a weakening. The main walk used to cross `pulse` into
    # its product, which multiplied every OTHER safety row's work by four to
    # re-derive a fact this row establishes directly; when Blocker 3 of the
    # third clinical review took the scene to five members, that factor of four
    # was what pushed the walk past ASM-09's bound. So the walk holds `pulse` at
    # UNKNOWN and this row varies it explicitly - see
    # `_CROSSED_OUT_OF_THE_WALK`. The assertion is STRONGER than the grouping it
    # replaces, because it compares each branch against the UNKNOWN-pulse
    # baseline the rest of the suite actually walks, rather than merely checking
    # that the four pulse values agree with each other.
    # THE STRUCTURAL HALF, WHICH IS THE ONE THAT ACTUALLY PROVES IT. `_Consulted`
    # has no `pulse` accessor, so there is no expression by which any path can
    # read the field - the guarantee is a property of the reachable API rather
    # than of the branches that happen to exist today. This is asserted FIRST
    # because it is what licenses the walk holding `pulse` at UNKNOWN: an
    # empirical sweep can only say "no current branch reads it", while this says
    # "no branch can".
    assert not hasattr(_Consulted, "pulse"), (
        "`_Consulted` must expose no `pulse` accessor: that absence is what "
        "makes ASM-12 a fact about the reachable API rather than a rule the "
        "next author has to remember, and it is what lets the exhaustive walk "
        "hold the pulse at UNKNOWN without losing coverage"
    )
    # And nothing reaches around the accessor either - no `inputs.pulse` or
    # `_inputs.pulse` read anywhere in the decision path's source.
    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "pulse"
    ]
    assert reads == [], (
        f"the decision path reads `.pulse` at {len(reads)} site(s); ASM-12 "
        f"requires the finding be recorded and never branched on"
    )

    # THE EMPIRICAL HALF, over a bounded cross-section rather than the whole
    # space. Every pulse value is crossed against every combination of the two
    # inputs that decide the arrest path - which is where a pulse gate would be
    # reintroduced, because that is where the published algorithms put one and
    # where this module's own Blocker 2 found one.
    checked = 0
    for responsiveness in (None, *Responsiveness):
        for breathing in (None, *Breathing):
            for bleeding in (None, *SevereBleeding):
                for submersion in (None, True, False):
                    branches = set()
                    for value in _INPUT_DOMAINS["pulse"]:
                        branch = decide(
                            AssessmentInputs(
                                scene_safe=SAFE,
                                responsiveness=(
                                    Finding.unknown()
                                    if responsiveness is None
                                    else established(responsiveness)
                                ),
                                breathing=(
                                    Finding.unknown()
                                    if breathing is None
                                    else established(breathing)
                                ),
                                severe_bleeding=(
                                    Finding.unknown()
                                    if bleeding is None
                                    else established(bleeding)
                                ),
                                submersion=(
                                    Finding.unknown()
                                    if submersion is None
                                    else established(submersion)
                                ),
                                pulse=(
                                    Finding.unknown()
                                    if value is None
                                    else established(value)
                                ),
                            )
                        )
                        branches.add(
                            (branch.step, branch.criticality, branch.assumed_inputs)
                        )
                        # "Recorded for the bridge, never a branch point" has to
                        # hold of the PROVENANCE too, or a pulse report could
                        # make a level uncorrectable without changing a step.
                        assert "pulse" not in branch.assumed_inputs, (
                            "a path recorded `pulse` as an assumption, so "
                            "something read it"
                        )
                        checked += 1
                    assert len(branches) == 1, (
                        f"responsiveness={responsiveness}, "
                        f"breathing={breathing}, bleeding={bleeding}, "
                        f"submersion={submersion} changed its branch when only "
                        f"the pulse report changed: {branches}"
                    )
    assert checked > 500, (
        f"the pulse axis must actually be crossed against the arrest-deciding "
        f"inputs; only {checked} decisions were made"
    )


def test_no_step_asks_the_responder_to_check_a_pulse() -> None:
    # falsifier: a pulse check reappears as a question the agent puts to the
    # caller. It costs 30-60 seconds of the golden window on a measurement the
    # operator cannot make, and it invites a fabricated answer that then enters
    # L2 looking like data. ASM-12, ASM-15.
    assert "pulse_check" in UNPERFORMABLE_ASSESSMENTS
    for _, branch in ALL_BRANCHES:
        assert branch.step.question_key not in UNPERFORMABLE_ASSESSMENTS, (
            f"{branch.step} asks for {branch.step.question_key}"
        )


def test_the_rescue_breaths_are_unconditional_and_five() -> None:
    # falsifier: the five rescue breaths get gated on a palpated pulse again -
    # which is what the published JumpSTART algorithm says, and is correct for
    # mass-casualty rationing and wrong here. A gated breath sequence means a
    # salvageable hypoxic child gets nothing while the caller hunts for a
    # carotid they cannot find. ASM-12.
    assert RESCUE_BREATHS_BEFORE_COMPRESSIONS == 5
    for pulse in (None, *PulseReport):
        branch = decide(
            _breathing_case(Breathing.NONE, age=AgeBand.CHILD, pulse=pulse)
        )
        assert branch.step is AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR, (
            f"pulse={pulse} changed the paediatric arrest sequence"
        )
        assert str(RESCUE_BREATHS_BEFORE_COMPRESSIONS) in branch.rationale


# ------------------------------------------- ASM-13: breathing is three-state


def test_breathing_has_exactly_three_states_and_no_boolean_reading() -> None:
    # falsifier: breathing collapses back to a bool somewhere. Roughly 40% of
    # witnessed arrests present with agonal gasps, and an untrained caller
    # answers "is he breathing?" with YES - so a binary input routes the single
    # most common arrest presentation to a respiratory assessment instead of to
    # compressions. The clinical review called this the most probable way this
    # product kills someone. ASM-13.
    assert [member.value for member in Breathing] == [
        "normal",
        "abnormal_or_gasping",
        "none",
    ]
    # In an UNCONSCIOUS patient - the conjunction the guideline states - both
    # non-normal states route to CPR, and normal breathing never does.
    for unconscious in (Responsiveness.PAIN, Responsiveness.UNRESPONSIVE):
        assert Breathing.ABNORMAL_OR_GASPING.routes_to_cpr(unconscious) is True
        assert Breathing.NONE.routes_to_cpr(unconscious) is True
        assert Breathing.NORMAL.routes_to_cpr(unconscious) is False
    assert Breathing.NORMAL.is_normal is True
    assert Breathing.ABNORMAL_OR_GASPING.is_normal is False


def test_the_cpr_decision_cannot_be_made_without_naming_consciousness() -> None:
    # falsifier: ASM-13's row says "both non-normal states route to CPR" and an
    # implementation reads that as a property of the breathing value ALONE - a
    # conclusion imported without its precondition. The guideline is a
    # CONJUNCTION, "unresponsive AND not breathing normally -> compressions",
    # which this repo states three times. Dropped, it tells an ALERT patient
    # with abnormal breathing to receive chest compressions: the conscious
    # asthmatic, the anaphylaxis, the pulmonary oedema and the partial
    # obstruction are all awake and all made worse by being laid flat and
    # compressed. Enforced by the SIGNATURE - there is no expression reaching
    # the CPR decision without naming the patient's consciousness.
    signature = inspect.signature(Breathing.routes_to_cpr)
    assert list(signature.parameters) == ["self", "responsiveness"], (
        "the CPR decision must take responsiveness, so the precondition "
        f"cannot be forgotten; got {list(signature.parameters)}"
    )
    assert not isinstance(
        inspect.getattr_static(Breathing, "routes_to_cpr"), property
    ), "a bare property is the unscoped reading that harmed an awake patient"

    # RE-SCOPED TO ABNORMAL_OR_GASPING, AND THE RE-SCOPING IS THE POINT.
    #
    # This loop originally ran over ALL of `Breathing` and asserted False for
    # every member in an awake patient. That pinned a defect as correct: a
    # SECOND independent clinical review reproduced SAFE + ALERT + obstruction
    # NONE + breathing NONE returning the conscious-distress leaf at Criticality
    # 4 - a patient reported as NOT BREATHING AT ALL told to sit upright and use
    # a reliever inhaler. It was this assertion that made that look intended.
    #
    # The conjunction "unresponsive AND not breathing normally" was imported
    # faithfully but applied too widely. The harm it prevents - compressions on
    # a talking asthmatic - only ever existed for ABNORMAL_OR_GASPING, which is
    # how a conscious patient in respiratory distress actually presents. Nobody
    # awake presents with NONE: absent breathing beside an "alert" report is a
    # stale or wrong responsiveness report, the likeliest error in the first
    # sixty seconds of a panicking call, and resolving it toward the reassuring
    # half is what killed the patient.
    #
    # So NONE is deliberately EXCLUDED from this loop and asserted the other way
    # below. A future author who notices the apnoea case would otherwise have to
    # break two named tests to fix it and would reasonably conclude the tests
    # were right and the clinical finding wrong.
    for awake in (Responsiveness.ALERT, Responsiveness.CONFUSED, Responsiveness.VOCAL):
        for breathing in (Breathing.NORMAL, Breathing.ABNORMAL_OR_GASPING):
            assert breathing.routes_to_cpr(awake) is False, (
                f"{breathing} in an {awake} patient must never indicate CPR"
            )
        # And the apnoea case takes the opposite answer, on purpose. The
        # contradiction is re-tested inside the arrest instruction rather than
        # resolved silently - see `Breathing.routes_to_cpr` for why re-asking
        # would be an absorbing loop in a pure function.
        assert Breathing.NONE.routes_to_cpr(awake) is True, (
            f"absent breathing in an {awake} patient is a contradiction to "
            f"re-test from the arrest path, not one to resolve in favour of "
            f"the reassuring half"
        )


def test_gasping_routes_to_cpr_exactly_as_absent_breathing_does() -> None:
    # falsifier: gasping is treated as a lesser finding than no breathing - a
    # respiratory-rate question, an oxygen instruction, a "keep watching" - so
    # the agonal patient waits while the apneic one gets compressions. Both are
    # arrest. ASM-13.
    gasping = decide(_breathing_case(Breathing.ABNORMAL_OR_GASPING))
    absent = decide(_breathing_case(Breathing.NONE))
    assert gasping.step is absent.step
    assert gasping.criticality is absent.criticality is Criticality.CRITICAL


def test_the_breathing_question_is_never_a_bare_yes_no() -> None:
    # falsifier: the step that establishes breathing carries no signal that it
    # must be asked as a three-state question, so L3 phrases it "is he
    # breathing?" - and the agent's own question shapes the answer toward the
    # fatal one. ASM-13.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
        )
    )
    assert branch.step is AssessmentStep.ASK_BREATHING_QUALITY
    assert branch.step.question_key == "breathing_quality", (
        "the step name must commit L3 to asking about QUALITY, not existence"
    )
    lowered = branch.rationale.lower()
    assert "normal" in lowered and "gasping" in lowered, (
        "the rationale handed to L3 must name both the normal-breathing framing "
        f"and the gasping probe; got {branch.rationale!r}"
    )


def test_an_unknown_breathing_finding_cannot_carry_a_value() -> None:
    # falsifier: a caller builds `Finding(Breathing.NORMAL, Certainty.UNKNOWN)`
    # and the machine branches on a value nobody established, reporting an
    # assumption as a finding. Illegal states must be unrepresentable rather
    # than merely discouraged.
    with pytest.raises(ValueError):
        Finding(Breathing.NORMAL, Certainty.UNKNOWN)
    with pytest.raises(ValueError):
        Finding(None, Certainty.ESTABLISHED)


# --------------------------------------- ASM-04 / ASM-08: worse-branch is scoped


def test_unestablished_breathing_escalates_the_category_but_not_the_instruction(
) -> None:
    # falsifier: the blanket "always take the worse branch" rule is restored, so
    # unknown breathing produces a CPR INSTRUCTION - chest compressions on a
    # patient who is breathing normally, chosen on no evidence at all. The rule
    # holds for category and escalation; for a physical instruction the rule is
    # "the intervention safe under BOTH hypotheses". ASM-04 scoped.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
        )
    )
    assert branch.criticality >= Criticality.SEVERE, "the category takes the worse branch"
    assert not branch.is_instruction, (
        "an unestablished input must not produce a physical instruction"
    )
    assert branch.step is AssessmentStep.ASK_BREATHING_QUALITY


def test_every_branch_from_an_unestablished_input_is_a_question_not_an_instruction(
) -> None:
    # falsifier: somewhere in the input space an assumption drives a physical
    # instruction. That is the coupling Blocker 4 names: over-triage is
    # recoverable for a category and is not recoverable once it tells a
    # bystander to do something to a patient. ASM-04.
    #
    # FIVE deliberate exceptions, each with its own reason, each listed rather
    # than skipped by a blanket `continue` - and each one's own precondition
    # asserted separately below, so "exception" never means "unchecked".
    #
    # INSTRUCT_MAKE_SCENE_SAFE: an unsafe scene DOES produce an instruction,
    # "move to safety", which is safe under both hypotheses by construction
    # because it is addressed to the responder and touches no patient.
    #
    # INSTRUCT_BREAK_ELECTRICAL_CONTACT: the same argument as the line above,
    # and it is the strongest instance of it rather than a fourth exception with
    # a new rationale. Blocker 3 of the third clinical review. It fires on an
    # ESTABLISHED in-the-patient hazard with breathing and responsiveness still
    # unknown, and every physical action it names is addressed to the RESPONDER
    # and to the electrical supply - throw the breaker, pull the plug, push the
    # casualty clear with a dry non-conductor, or stay back. It is the one
    # instruction in the module that explicitly forbids touching the patient, so
    # "an assumption drove a physical instruction on a patient" is not merely
    # avoided here, it is the thing the leaf exists to prevent. Safe under every
    # hypothesis about the airway: isolating a supply harms no breathing state,
    # and it is the precondition for acting on any of them.
    #
    # INSTRUCT_CONTROL_BLEEDING: Blocker 1's pre-A gate fires on an ESTABLISHED
    # bleed while responsiveness and breathing are still unknown - which is the
    # whole point, since asking either one first is what the review found fatal.
    # Pressure on a wound harms no hypothesis about the airway, and it does not
    # stop the caller answering the next question.
    #
    # INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION: fires on an established witnessed
    # obstruction in an established-RESPONSIVE patient, with breathing still
    # unknown - again the point, because a choking patient is precisely the one
    # whose breathing answer is a useless yes. Safe under both breathing
    # hypotheses: back blows on a responsive choker who turns out to be moving
    # air anyway cost nothing, and the instruction itself carries the transition
    # to compressions if they go unconscious.
    #
    # INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION: the same two
    # exceptions at once, and it inherits both their arguments rather than
    # adding a new one. It is reached only on an ESTABLISHED witnessed
    # obstruction in an ESTABLISHED-responsive patient with an ESTABLISHED
    # bleed - so every input it acts on is established, and the only thing that
    # may be unknown is breathing, which is exactly the answer a choking patient
    # makes useless. Both of its physical actions are the ones already permitted
    # here: hands-free pressure on a wound, and back blows on a responsive
    # choker. Its preconditions are asserted below, like the others.
    unestablished_breathing_permitted = {
        AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE,
        AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING,
        AssessmentStep.INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION,
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION,
    }
    for assignment, branch in ALL_BRANCHES:
        if not branch.is_instruction:
            continue
        if branch.step in unestablished_breathing_permitted:
            continue
        deciding = {
            "scene_safe": assignment["scene_safe"],
            "breathing": assignment["breathing"],
            "responsiveness": assignment["responsiveness"],
        }
        assert all(value is not None for value in deciding.values()), (
            f"{branch.step} was instructed while {deciding} held an "
            f"unestablished input"
        )

    # And the exceptions are not a loophole: each one's OWN deciding input must
    # be established, or an assumption really is driving a physical instruction.
    for assignment, branch in ALL_BRANCHES:
        if branch.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING:
            assert assignment["severe_bleeding"] is SevereBleeding.PRESENT, (
                f"{assignment} instructed bleeding control on an "
                f"unestablished bleed"
            )
        if branch.step is AssessmentStep.INSTRUCT_MAKE_SCENE_SAFE:
            # Two hazard classes reach the move instruction now - the approach
            # hazards. The in-the-patient class does NOT, because "move to
            # safety" is the wrong action for a responder who may already be in
            # the right place and must not TOUCH.
            assert assignment["scene_safe"] in (
                SceneSafety.UNSAFE_ADJACENT,
                SceneSafety.UNSAFE_CONSUMING,
            ), f"{assignment} instructed a move on an unestablished scene"
        if branch.step is AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT:
            assert (
                assignment["scene_safe"] is SceneSafety.UNSAFE_IN_PATIENT
            ), (
                f"{assignment} instructed breaking an electrical contact "
                f"without an established in-the-patient hazard"
            )
        if branch.step in (
            AssessmentStep.INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION,
            AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION,
        ):
            assert (
                assignment["airway_obstruction"]
                is AirwayObstruction.WITNESSED_FOREIGN_BODY
            ), f"{assignment} instructed thrusts on an unwitnessed collapse"
            assert assignment["responsiveness"] is not None, (
                f"{assignment} instructed thrusts without establishing "
                f"responsiveness, which is what decides thrusts vs compressions"
            )
        if (
            branch.step
            is AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION
        ):
            # And the extra precondition the combined leaf adds over its two
            # parents: it names a haemorrhage, so the haemorrhage is established.
            assert assignment["severe_bleeding"] is SevereBleeding.PRESENT, (
                f"{assignment} instructed hands-free pressure on an "
                f"unestablished bleed"
            )


def test_algorithm_ambiguity_resolves_to_the_adult_algorithm() -> None:
    # falsifier: ASM-08 gets re-inverted to "ambiguous -> JumpSTART". JumpSTART's
    # pulse branch has a BLACK leaf, so defaulting an ambiguous adolescent there
    # routes them TOWARD expectant, where START gives them RED. The worse branch
    # was worse in the wrong direction. ASM-08.
    assert AgeBand.UNCERTAIN.resolved is AgeBand.ADULT
    assert AgeBand.ADULT.resolved is AgeBand.ADULT
    assert AgeBand.CHILD.resolved is AgeBand.CHILD

    uncertain = decide(_breathing_case(Breathing.NONE, age=AgeBand.UNCERTAIN))
    adult = decide(_breathing_case(Breathing.NONE, age=AgeBand.ADULT))
    assert uncertain.step is adult.step is AssessmentStep.INSTRUCT_CPR


def test_an_unestablished_age_resolves_the_same_way_as_an_uncertain_one() -> None:
    # falsifier: "not asked" and "asked, and they could not tell" take different
    # algorithm branches, so the safer default applies only when the caller
    # happens to have volunteered their uncertainty. ASM-08.
    never_asked = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        breathing=established(Breathing.NONE),
    )
    said_unsure = _breathing_case(Breathing.NONE, age=AgeBand.UNCERTAIN)
    assert decide(never_asked).step is decide(said_unsure).step


def test_submersion_takes_the_hypoxic_sequence_for_an_adult() -> None:
    # falsifier: a drowned adult gets standard compressions-first CPR. Drowning
    # is a hypoxic arrest, and `protocols.py:drowning-rescue` already holds the
    # five-breaths-first sequence; losing it here would mean the corpus has the
    # right answer and the state machine overrides it with the wrong one.
    branch = decide(
        _breathing_case(Breathing.NONE, age=AgeBand.ADULT, submersion=True)
    )
    assert branch.step is AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR
    assert branch.criticality is Criticality.CRITICAL


# ------------------------------- ASM-15: nothing a bystander cannot perform


def test_no_reachable_step_requires_an_unperformable_assessment() -> None:
    # falsifier: capillary refill, a counted respiratory rate, auscultation, JVP,
    # pupils or a glucometer reading reappears as something the agent asks a
    # bystander for. Every undoable assessment costs 30-60 seconds of the golden
    # window, erodes compliance, and - worst - produces a FABRICATED answer,
    # which is far more dangerous than UNKNOWN because it looks like data.
    # ASM-15.
    for _, branch in ALL_BRANCHES:
        assert branch.step.question_key not in UNPERFORMABLE_ASSESSMENTS, (
            f"{branch.step} requires {branch.step.question_key}"
        )


def test_the_excluded_list_covers_every_assessment_blocker_5_named() -> None:
    # falsifier: the exclusion list is quietly narrowed - most likely by someone
    # restoring capillary refill, because CLINICAL-STANDARDS.md §2.1 uses it as
    # the adult RED/YELLOW discriminator and looks authoritative. The blocker
    # wins over the published algorithm, and this pins the whole list so a
    # deletion is a failing test rather than a silent regression. ASM-15.
    for name in (
        "capillary_refill",
        "respiratory_rate_counted",
        "auscultation",
        "jvp",
        "pupils",
        "blood_glucose",
        "pulse_pressure",
    ):
        assert name in UNPERFORMABLE_ASSESSMENTS, f"{name} must stay excluded"


def test_no_input_field_is_an_unperformable_measurement() -> None:
    # falsifier: an unperformable assessment enters as an INPUT rather than as a
    # question - `capillary_refill_seconds` on AssessmentInputs - so nothing asks
    # the bystander for it directly but a branch still depends on a number
    # nobody can produce, and the only way to fill it is a guess. ASM-15.
    fields = set(AssessmentInputs.__dataclass_fields__)
    assert not (fields & UNPERFORMABLE_ASSESSMENTS), (
        f"unperformable inputs present: {fields & UNPERFORMABLE_ASSESSMENTS}"
    )
    assert "respiratory_rate" not in fields, (
        "nobody counts breaths in a crisis; breathing is the three-state quality"
    )


def test_the_adult_algorithm_floor_is_a_named_constant_with_a_citation() -> None:
    # falsifier: a threshold is written as a bare literal at a comparison, so the
    # rule has two homes and a clinician reviewing the numbers cannot find them.
    # CLINICAL-STANDARDS.md §3.1: JumpSTART is designed for ages 1-8 because
    # paediatric airway physiology approaches adult by about 8.
    # An independent review found the first version of this assertion passing
    # for a reason unrelated to the citation: it tested `"3.1" in source`, and
    # `"3.1"` is a substring of `"3.11"`, which this module's own docstring
    # contains twice while explaining the Python version. The test would have
    # gone green with every citation deleted. Fixed by locating the citation
    # against the constant it belongs to, rather than anywhere in the file.
    assert ADULT_ALGORITHM_FLOOR_YEARS == 8

    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    declaration = source.index("ADULT_ALGORITHM_FLOOR_YEARS: int")
    # The citation must sit in the comment block immediately above the
    # declaration, which is where a clinician reviewing the numbers will look.
    preamble = source[max(0, declaration - 900) : declaration]
    assert "CLINICAL-STANDARDS.md" in preamble, (
        "the threshold must cite the document it came from, next to itself"
    )
    assert "3.1" in preamble, "and the section within it"

    for constant, section in (
        ("RESCUE_BREATHS_BEFORE_COMPRESSIONS: int", "3.3"),
    ):
        at = source.index(constant)
        nearby = source[max(0, at - 900) : at]
        assert "CLINICAL-STANDARDS.md" in nearby, f"{constant} lacks a citation"
        assert section in nearby, f"{constant} lacks its section"


# ---------------------------------- ASM-06: L3 cannot override an L2 branch


def test_a_branch_cannot_be_constructed_outside_the_module() -> None:
    # falsifier: L3 - the only non-deterministic layer, and the least trusted -
    # builds its own AssessmentBranch with whatever step it preferred and hands
    # it onward as though L2 had computed it. A forged branch is more dangerous
    # than a mutated one because it arrives looking authentic. ASM-06, enforced
    # by the constructor rather than by convention.
    with pytest.raises(TypeError):
        AssessmentBranch(
            step=AssessmentStep.INSTRUCT_MONITOR,
            letter=Letter.E,
            criticality=Criticality.MINOR,
            rationale="L3 decided the patient is fine",
            unresolved_letter=Letter.E,
            assumed_inputs=(),
            _witness=object(),
        )


def test_a_branch_cannot_be_mutated_after_it_is_returned() -> None:
    # falsifier: the branch is a mutable object, so a caller downstream of L2
    # rewrites `step` or `criticality` in place - the same override as forgery,
    # reached without constructing anything. ASM-06.
    branch = decide(_breathing_case(Breathing.NONE))
    with pytest.raises(Exception):
        branch.step = AssessmentStep.INSTRUCT_MONITOR  # type: ignore[misc]
    with pytest.raises(Exception):
        branch.criticality = Criticality.MINOR  # type: ignore[misc]
    assert branch.step is AssessmentStep.INSTRUCT_CPR


def test_a_branch_cannot_be_forged_by_subclassing() -> None:
    # falsifier: `class Forged(AssessmentBranch)` with an overridden
    # `__post_init__` produces a branch that passes `isinstance`, so every
    # consumer that type-checks its input is satisfied while the witness never
    # runs at all. An independent review reached this in one line. ASM-06.
    with pytest.raises(TypeError):
        class Forged(AssessmentBranch):  # type: ignore[misc]
            pass


@pytest.mark.parametrize(
    "forge",
    ["object_new", "dataclasses_replace", "deepcopy", "pickle"],
)
def test_every_forgery_that_construction_cannot_block_is_detected(
    forge: str,
) -> None:
    # falsifier: THE OVERSTATED CLAIM. The witness in `__post_init__` blocks
    # only the construction an author would WRITE; it cannot block
    # `object.__new__` plus `object.__setattr__`, which never runs `__init__`,
    # nor `dataclasses.replace`/`deepcopy`/`pickle`, which carry the witness
    # forward. An independent review demonstrated all four. If ASM-06 rested on
    # the witness alone, L3 could hand a forged branch to a consumer and nothing
    # would notice - so the row rests on `is_authentic`, and this test is what
    # proves that mechanism actually covers the cases the witness misses.
    import copy
    import dataclasses
    import pickle

    genuine = decide(_breathing_case(Breathing.NONE))
    assert is_authentic(genuine) is True, "a real branch must verify"

    if forge == "object_new":
        forged = object.__new__(AssessmentBranch)
        for name, value in (
            ("step", AssessmentStep.INSTRUCT_MONITOR),
            ("letter", Letter.E),
            ("criticality", Criticality.MINOR),
            ("rationale", "L3 decided the patient is fine"),
            ("unresolved_letter", Letter.E),
            ("assumed_inputs", ()),
            ("_witness", object()),
        ):
            object.__setattr__(forged, name, value)
    elif forge == "dataclasses_replace":
        forged = dataclasses.replace(
            genuine, step=AssessmentStep.INSTRUCT_MONITOR
        )
    elif forge == "deepcopy":
        forged = copy.deepcopy(genuine)
    else:
        forged = pickle.loads(pickle.dumps(genuine))

    # The honest result: construction did NOT stop these. Pinned so nobody
    # believes it did.
    assert isinstance(forged, AssessmentBranch)
    # And the mechanism that does stop them.
    assert is_authentic(forged) is False, (
        f"{forge} produced a branch that verified as authentic"
    )


def test_is_authentic_refuses_anything_that_is_not_a_branch() -> None:
    # falsifier: `is_authentic` raises instead of returning False, so a consumer
    # wraps its safety check in a try/except - and a safety check inside an
    # except block is one commented out at the next incident. Or worse, it
    # returns truthy for a convincing duck-typed stand-in.
    class LooksLikeOne:
        step = AssessmentStep.INSTRUCT_MONITOR
        criticality = Criticality.MINOR

    for impostor in (None, "instruct_monitor", 5, LooksLikeOne()):
        assert is_authentic(impostor) is False


def test_decide_takes_only_inputs_and_returns_only_a_branch() -> None:
    # falsifier: `decide` grows a parameter L3 can use to influence the outcome -
    # an override, a hint, a preferred step, a "confidence" weight. The
    # asymmetric boundary is that L3 may resolve uncertainty about INPUTS and
    # never about OUTPUTS, and a second parameter is where that boundary is lost.
    # ASM-06, by the signature.
    signature = inspect.signature(decide)
    assert list(signature.parameters) == ["inputs"], (
        f"decide must take exactly one parameter; got {list(signature.parameters)}"
    )
    assert signature.return_annotation == "AssessmentBranch"


# ------------------------------------- ASM-10: deterioration forces re-entry at A


def test_a_deteriorating_patient_is_returned_to_the_airway_letter() -> None:
    # falsifier: the patient was alert and breathing, then stops breathing, and
    # the machine carries on from E because the later letters were already
    # resolved - so a deterioration mid-assessment is missed entirely. The
    # ABCDE rule is "reassess from the top after any change". ASM-10.
    settled = decide(_well_patient())
    assert settled.step is AssessmentStep.INSTRUCT_MONITOR
    assert settled.letter is Letter.E

    deteriorated = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            severe_bleeding=established(SevereBleeding.NONE),
            exposure_reviewed=SAFE,
            age_band=established(AgeBand.ADULT),
        )
    )
    assert deteriorated.unresolved_letter is Letter.B
    assert deteriorated.step is AssessmentStep.INSTRUCT_CPR
    assert deteriorated.criticality is Criticality.CRITICAL


def test_reassessment_never_lowers_an_evidenced_level() -> None:
    # falsifier: re-entering the assessment at A re-runs the machine from a
    # clean slate and feeds its fresh, lower category into the ratchet, so a
    # patient who was CRITICAL a minute ago reads as MODERATE because the
    # current turn happened to resolve calmly. ASM-10.
    state = TriageState(session_id="inc-deteriorate", clock=ManualClock())
    arrest = decide(_breathing_case(Breathing.NONE))
    state.set_level(
        int(arrest.criticality),
        arrest.rationale,
        source="l2",
        provenance=Provenance.EVIDENCE,
    )
    calmer = decide(_well_patient())
    change = state.set_level(
        int(calmer.criticality), calmer.rationale, source="l2",
        provenance=Provenance.EVIDENCE,
    )
    assert change.rejected is True
    assert state.level is Criticality.CRITICAL
    assert state.rationale == arrest.rationale


# ----------------------------------- ASM-14: the ratchet reads the certainty type


def test_an_assumed_level_is_correctable_by_later_evidence() -> None:
    # falsifier: THE ASM-14 DEFECT. A patient assumed-worst to 5 at minute one
    # cannot be corrected at minute three even by a clinician on the bridge, so
    # assumption-driven and evidence-driven CRITICAL are indistinguishable at
    # the ratchet - the exact conflation the certainty type exists to prevent,
    # in the one place that never read it.
    state = TriageState(session_id="inc-14", clock=ManualClock())
    state.set_level(
        5, "breathing could not be established", provenance=Provenance.ASSUMPTION
    )
    assert state.level is Criticality.CRITICAL
    assert state.level_provenance is Provenance.ASSUMPTION

    change = state.set_level(
        3,
        "clinician on the bridge established normal breathing",
        provenance=Provenance.EVIDENCE,
    )
    assert change.corrected is True
    assert change.rejected is False
    assert state.level is Criticality.MODERATE
    assert state.level_provenance is Provenance.EVIDENCE
    assert state.history[-1]["corrected"] is True


def test_an_evidenced_level_is_not_correctable_by_anything() -> None:
    # falsifier: the ASM-14 fix is written as a blanket "evidence may lower the
    # level", so a genuinely established CRITICAL gets stood down by a later
    # turn - a recovery narrative, a second caller, a calmer voice - and the
    # ratchet stops being a ratchet. Standing an incident down is a clinician's
    # decision and is out of scope for the field agent.
    state = TriageState(session_id="inc-14b", clock=ManualClock())
    state.set_level(5, "not breathing, reported", provenance=Provenance.EVIDENCE)

    by_evidence = state.set_level(
        2, "second caller says he is fine", provenance=Provenance.EVIDENCE
    )
    assert by_evidence.rejected is True
    assert state.level is Criticality.CRITICAL

    by_assumption = state.set_level(
        1, "could not re-establish", provenance=Provenance.ASSUMPTION
    )
    assert by_assumption.rejected is True
    assert state.level is Criticality.CRITICAL


def test_an_assumption_cannot_lower_an_assumed_level() -> None:
    # falsifier: the correction rule is read as "anything may lower an
    # assumption", so one guess overturns another and the level drifts downward
    # over a long incident with nobody ever having established anything. Only
    # evidence corrects.
    state = TriageState(session_id="inc-14c", clock=ManualClock())
    state.set_level(5, "assumed worst", provenance=Provenance.ASSUMPTION)
    change = state.set_level(2, "still guessing", provenance=Provenance.ASSUMPTION)
    assert change.rejected is True
    assert state.level is Criticality.CRITICAL
    assert state.level_provenance is Provenance.ASSUMPTION


def test_an_assumption_cannot_soften_an_evidenced_level_back_to_correctable(
) -> None:
    # falsifier: a later ASSUMPTION at or above the evidenced level overwrites
    # `level_provenance` back to ASSUMPTION, which unlocks a downgrade of a
    # finding that was genuinely established. The correction door is then opened
    # by a guess, one step removed - the subtlest way this whole mechanism
    # inverts.
    state = TriageState(session_id="inc-14d", clock=ManualClock())
    state.set_level(4, "bleeding seen", provenance=Provenance.EVIDENCE)
    state.set_level(5, "assumed worse still", provenance=Provenance.ASSUMPTION)
    assert state.level_provenance is Provenance.EVIDENCE, (
        "an assumption must not make an evidenced level correctable again"
    )
    refused = state.set_level(
        2, "clinician says minor", provenance=Provenance.EVIDENCE
    )
    assert refused.rejected is True
    assert state.level is Criticality.CRITICAL


def test_the_default_provenance_leaves_every_existing_caller_unchanged() -> None:
    # falsifier: the ASM-14 change alters the behaviour every existing call site
    # already depends on - `set_level` without a provenance argument starts
    # permitting downgrades, so the ratchet silently weakens across the whole
    # agent layer while this file's own tests still pass.
    state = TriageState(session_id="inc-14e", clock=ManualClock())
    state.set_level(4, "severe")
    change = state.set_level(2, "calmer now")
    assert change.rejected is True
    assert state.level is Criticality.SEVERE
    assert len(state.history) == 1


def test_certainty_maps_to_provenance_at_the_inferred_boundary() -> None:
    # falsifier: the line between assumption and evidence is drawn in the wrong
    # place. If ASSUMED_WORST counted as evidence, a deliberate worst-case guess
    # would weld itself in permanently - the original defect. If INFERRED counted
    # as an assumption, a responder's real report ("he's panting") would be
    # correctable by a later guess.
    assert Certainty.UNKNOWN.provenance is Provenance.ASSUMPTION
    assert Certainty.ASSUMED_WORST.provenance is Provenance.ASSUMPTION
    assert Certainty.INFERRED.provenance is Provenance.EVIDENCE
    assert Certainty.ESTABLISHED.provenance is Provenance.EVIDENCE
    assert Certainty.OBSERVED.provenance is Provenance.EVIDENCE


def test_a_branch_resting_on_any_assumption_is_correctable() -> None:
    # falsifier: a branch reports EVIDENCE provenance although one of the inputs
    # on its path was never established, so the level it sets becomes
    # uncorrectable and the clinician who later establishes that input cannot
    # fix it. ASM-14.
    assumed = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
        )
    )
    assert assumed.assumed_inputs != (), "breathing was never established"
    assert assumed.provenance is Provenance.ASSUMPTION

    # THE EVIDENCED HALF NOW HAS TO ESTABLISH EVERY INPUT ON THE ARREST PATH,
    # and it did not before - which was Blocker 2 of the third clinical review
    # seen from the other side. `_breathing_case` leaves `severe_bleeding` and
    # `airway_obstruction` UNKNOWN, both of which the arrest path genuinely
    # consults: the bleed decides between INSTRUCT_CPR and the combined leaf,
    # and the obstruction decides whether the mouth-check clause is carried. A
    # CRITICAL reached with either of them merely unasked is a CRITICAL a
    # clinician must be able to correct, so the old `assumed_inputs == ()` here
    # was asserting the defect rather than the contract.
    evidenced = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
            severe_bleeding=established(SevereBleeding.NONE),
            airway_obstruction=NO_OBSTRUCTION,
            age_band=established(AgeBand.ADULT),
            submersion=established(False),
        )
    )
    assert evidenced.step is AssessmentStep.INSTRUCT_CPR
    assert evidenced.assumed_inputs == ()
    assert evidenced.provenance is Provenance.EVIDENCE

    # And the same patient with the obstruction merely UNASKED is correctable,
    # which is the half the old assertion could not see.
    unasked_obstruction = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NONE),
            severe_bleeding=established(SevereBleeding.NONE),
            age_band=established(AgeBand.ADULT),
            submersion=established(False),
        )
    )
    assert unasked_obstruction.step is AssessmentStep.INSTRUCT_CPR
    assert unasked_obstruction.assumed_inputs == ("airway_obstruction",)
    assert unasked_obstruction.provenance is Provenance.ASSUMPTION


def test_the_payload_distinguishes_an_assumed_level_from_an_evidenced_one(
) -> None:
    # falsifier: both UIs show "Critical" with no indication of whether anything
    # was actually established, so the clinician joining the bridge cannot tell
    # which incident they are walking into - and cannot tell whether what they
    # are looking at is still correctable. ARCHITECTURE-ASSESSMENT.md §6.
    state = TriageState(session_id="inc-payload", clock=ManualClock())
    state.set_level(5, "assumed worst", provenance=Provenance.ASSUMPTION)
    payload = state.to_payload()
    assert payload["level_provenance"] == "assumption"
    assert payload["level_correctable"] is True

    state.set_level(5, "now established", provenance=Provenance.EVIDENCE)
    hardened = state.to_payload()
    assert hardened["level_provenance"] == "evidence"
    assert hardened["level_correctable"] is False


# --------------------------------------- ASM-05: L1 fires independently of L2/L3


def test_l1_outranks_l2_even_when_l2_computes_a_lower_level() -> None:
    # falsifier: ASM-14's correction door lets L2's arithmetic stand down a
    # reported life threat. A `phrases.py` marker firing on raw speech is
    # evidence of what was SAID - the strongest input the system has - and it
    # must keep outranking L2 regardless of how confident L2's own inputs are.
    # ASM-05.
    # Driven through escalation.py's REAL path rather than by reproducing its
    # `set_level` call here. Mutation testing found that mattered: a version of
    # this test that hand-rolled the L1 call passed while escalation.py itself
    # escalated as an ASSUMPTION, so the rule was asserted and the actual code
    # path that has to obey it was not exercised at all.
    state = TriageState(session_id="inc-l1", clock=ManualClock())

    async def drive() -> None:
        await apply_hard_escalation(
            "he's not breathing",
            state=state,
            on_state_change=_noop,
            on_escalate=_noop_escalate,
            source=SOURCE_TRANSCRIPT,
        )

    asyncio.run(drive())
    assert state.level is Criticality.CRITICAL, "L1 must have fired"
    assert state.level_provenance is Provenance.EVIDENCE, (
        "a marker on raw speech is evidence of what was SAID; an assumption "
        "here would let L2's arithmetic stand down a reported life threat"
    )

    # L2 now fully establishes a calm patient and asks for its own level.
    calm = decide(_well_patient())
    assert calm.criticality < Criticality.CRITICAL
    change = state.set_level(
        int(calm.criticality),
        calm.rationale,
        source="l2",
        provenance=calm.provenance,
    )
    assert change.rejected is True, "L2 must not be able to stand down L1"
    assert state.level is Criticality.CRITICAL


def test_l1_does_not_consult_l2_at_all() -> None:
    # falsifier: the deterministic marker net grows a dependency on the
    # assessment machine, so the backstop that exists to catch failure of
    # everything above it stops firing when the thing above it is broken - which
    # is precisely when it is needed. ASM-05.
    from aiscelapeus import escalation, phrases

    for module in (phrases, escalation):
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        assert "assessment" not in source, (
            f"{module.__name__} must not depend on L2"
        )

    hit = phrases.hard_escalation_triggered("he's not breathing")
    assert hit is not None
    assert hit.marker_id == "not_breathing"


# ------------------------------------------------------- ASM-09: the suite is fast


def test_the_whole_input_space_decides_well_inside_the_budget() -> None:
    # falsifier: L2 becomes slow enough to matter on the voice path, or the suite
    # stops being cheap enough to run on every change. A latency budget asserted
    # by nothing that runs is not asserted at all. ASM-09.
    #
    # Timed over EVERY terminal leaf rather than over one repeated case. An
    # independent review found the earlier version timing 2000 calls of a single
    # fixed input - the shortest path through `decide` - while claiming to
    # measure "the whole input space". That is a latency claim backed by a
    # measurement of something else, which is the failure mode CLAUDE.md names
    # by name, reproduced in miniature inside the test that exists to prevent it.
    cases = [
        _breathing_case(Breathing.NONE),
        _breathing_case(Breathing.NONE, age=AgeBand.CHILD),
        _breathing_case(Breathing.NONE, bleeding=SevereBleeding.PRESENT),
        _well_patient(),
        AssessmentInputs(),
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.NONE),
        ),
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NORMAL),
            severe_bleeding=established(SevereBleeding.NONE),
            spinal_risk=established(SpinalRisk.SUSPECTED),
        ),
    ]
    started = time.perf_counter()
    for _ in range(2000):
        for case in cases:
            decide(case)
    elapsed = time.perf_counter() - started
    decisions = 2000 * len(cases)
    assert elapsed < 2.0, f"{decisions} decisions took {elapsed:.3f}s"

    assert len(ALL_BRANCHES) > 100_000, (
        f"the exhaustive walk must actually be exhaustive; got {len(ALL_BRANCHES)}"
    )


def test_the_exhaustive_walk_itself_stays_inside_the_suite_budget() -> None:
    # falsifier: the thing CI actually pays for on every change is the 140k-
    # combination walk at module import, and NOTHING timed it - so an
    # accidentally-quadratic helper could take the L2 suite past ASM-09's
    # five-second budget and the only signal would be a wall-clock number
    # nobody asserts on. An independent review found this gap. A latency budget
    # asserted by nothing that runs is not asserted at all. ASM-09.
    # Asserts on the timing of the walk that ALREADY HAPPENED at import, rather
    # than walking a second time to measure it. Re-walking would spend the very
    # budget it is checking - about five seconds to prove five seconds - and
    # would measure a second, warmer run rather than the one CI actually pays.
    # 8 s, not 5 s, and the gap is deliberate rather than slack. ASM-09's
    # budget is for the L2 SUITE, and this walk is the bulk of it - so the bound
    # has to leave room for a CI runner perhaps twice as slow before it starts
    # reporting an infrastructure difference as a code regression. A threshold
    # set just above the observed time is a flaky test, and a flaky safety test
    # is one that gets deleted. What it still catches is what it exists for: an
    # accidentally-quadratic helper, or a `decide` that grows an expensive call,
    # either of which moves this by a multiple rather than by a fraction.
    #
    # THIS ROW FIRED FOR REAL, AND IT IS WHY THE WALK IS STRATIFIED. Blocker 3
    # of the third clinical review took `SceneSafety` from three members to
    # five, which took the product from 373k to 560k and this measurement from
    # ~4.9 s to 8.04-8.05 s - FAILING on two runs out of three, exactly the
    # marginal flakiness the bound was set wide to avoid. The message below
    # names the two acceptable responses and rules out the third, and the fix
    # took the first of them: `pulse` is crossed out of the product because no
    # path can read it (`_CROSSED_OUT_OF_THE_WALK`), which is stratification
    # rather than sampling - nothing decision-relevant was dropped, and the
    # walk is 140k combinations at ~1.9 s with every safety row still a
    # statement about every reachable branch. Raising the bound to 9 s was
    # considered and rejected: it would have hidden a real 60% regression in
    # the thing CI pays for, which is the failure mode CLAUDE.md names.
    assert ALL_BRANCHES_SECONDS < 8.0, (
        f"the exhaustive walk over {len(ALL_BRANCHES)} combinations took "
        f"{ALL_BRANCHES_SECONDS:.2f}s at import, which puts the L2 suite over "
        f"ASM-09's budget. Either make `decide` cheaper or stratify the walk - "
        f"do NOT sample it into meaninglessness, because the safety rows above "
        f"are statements about EVERY path."
    )
    # And the number is a real measurement of a real walk, not a zero left by a
    # walk that never ran.
    assert ALL_BRANCHES_SECONDS > 0.0
    assert len(ALL_BRANCHES) > 100_000


# ------------------------------------------------------- terminal-state coverage


def test_an_unconscious_breathing_patient_gets_the_recovery_position() -> None:
    # falsifier: the single most valuable thing a lone bystander does for an
    # unconscious-but-breathing patient is absent from every terminal state -
    # the clinical review's ranked omission 6 - so the agent finishes its survey
    # and leaves the patient supine with an unprotected airway.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NORMAL),
            severe_bleeding=established(SevereBleeding.NONE),
            spinal_risk=NO_SPINAL_RISK,
            age_band=established(AgeBand.ADULT),
        )
    )
    assert branch.step is AssessmentStep.INSTRUCT_RECOVERY_POSITION
    assert branch.letter is Letter.D


# ---------------- BLOCKER 4: the recovery position knows the mechanism


def test_the_unconscious_motorcyclist_and_the_unconscious_fainter_differ() -> None:
    # falsifier: both get one identical unconditional "roll them into the
    # recovery position", because L2 holds no mechanism-of-injury input. The
    # spinal caveat's WORDING lives in protocols.py, which is right, but the
    # caveat is a CONDITIONAL and its condition was an input L2 did not hold -
    # so whether it survived into speech depended on retrieval landing the right
    # document at 64% top-1, and on a one-in-three miss a bystander rolls an
    # unstable C-spine.
    def unconscious(risk: SpinalRisk) -> AssessmentInputs:
        return AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NORMAL),
            severe_bleeding=established(SevereBleeding.NONE),
            spinal_risk=established(risk),
        )

    fainter = decide(unconscious(SpinalRisk.NONE))
    motorcyclist = decide(unconscious(SpinalRisk.SUSPECTED))

    assert fainter.step is AssessmentStep.INSTRUCT_RECOVERY_POSITION
    assert (
        motorcyclist.step is AssessmentStep.INSTRUCT_AIRWAY_WITH_SPINAL_CARE
    )
    assert fainter.step is not motorcyclist.step, (
        "the technique differs and only one of the two is safe to improvise, "
        "so the steps must be DISTINGUISHABLE rather than differing in prose"
    )

    # The technique that must NOT be improvised is named, and the one that
    # replaces it is named too. A branch that only said "be careful" would
    # satisfy a step-identity assertion while telling the bystander nothing.
    spinal = motorcyclist.rationale.lower()
    assert "jaw thrust" in spinal, "the alternative technique must be named"
    assert "not roll" in spinal or "do not roll" in spinal

    # AIRWAY STILL BEATS SPINE. This is the rule that must not be lost while
    # fixing the one above it: a spine protected at the cost of an airway is a
    # dead patient with a good neck.
    assert "airway" in spinal
    assert "outranks" in spinal or "first" in spinal, (
        "the branch must state that a threatened airway overrides the spinal "
        "precaution, or the fix has inverted the priority"
    )


def test_the_recovery_position_is_never_ordered_without_a_mechanism() -> None:
    # falsifier: the spinal input exists but some reachable path still reaches a
    # full roll without it - most likely a later refactor adding a shortcut for
    # the common case. Walks the entire input space.
    for assignment, branch in ALL_BRANCHES:
        if branch.step is AssessmentStep.INSTRUCT_RECOVERY_POSITION:
            assert assignment["spinal_risk"] is SpinalRisk.NONE, (
                f"{assignment} ordered a roll without excluding a spinal injury"
            )
        if branch.step is AssessmentStep.INSTRUCT_AIRWAY_WITH_SPINAL_CARE:
            assert assignment["spinal_risk"] is SpinalRisk.SUSPECTED, (
                f"{assignment} ordered spinal care with no mechanism"
            )


# ------------------------- BLOCKER 5: choking, and no dead members


def test_a_conscious_choker_gets_thrusts_and_not_compressions() -> None:
    # falsifier: THE COMMON, FAST, REVERSIBLE DEATH THE MACHINE HAD ONE WRONG
    # ANSWER FOR. A witnessed choking collapse arrives as ABNORMAL_OR_GASPING or
    # NONE and routes straight to INSTRUCT_CPR. Compressions will not shift a
    # lodged bolus; back blows and abdominal thrusts in a still-conscious choker
    # is the intervention with the survival curve. Verified before the fix: no
    # chok/obstruct/back-blow/thrust anywhere in the module.
    for breathing in (Breathing.ABNORMAL_OR_GASPING, Breathing.NONE, None):
        inputs = AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=(
                Finding.unknown() if breathing is None else established(breathing)
            ),
            airway_obstruction=established(
                AirwayObstruction.WITNESSED_FOREIGN_BODY
            ),
        )
        branch = decide(inputs)
        assert (
            branch.step is AssessmentStep.INSTRUCT_CLEAR_AIRWAY_OBSTRUCTION
        ), f"a witnessed obstruction with breathing={breathing} gave {branch.step}"
        assert branch.letter is Letter.A, "an obstruction is an airway problem"
        lowered = branch.rationale.lower()
        assert "back blow" in lowered and "thrust" in lowered, (
            "the intervention with the survival curve must be named"
        )
        # And the transition out, which protocols.py states and L2 owns.
        assert "unconscious" in lowered and "compression" in lowered, (
            "the transition to CPR on loss of consciousness must be stated: "
            "protocols.py's choking document applies ONLY while responsive"
        )


def test_an_unwitnessed_collapse_is_not_treated_as_choking() -> None:
    # falsifier: the choking fix over-reaches and back blows are coached for a
    # primary cardiac arrest, which spends the golden window on a manoeuvre that
    # cannot help - the wrong-branch harm in the OTHER direction, which the
    # review names explicitly. An unresponsive patient must never be asked to
    # reconstruct whether they were eating.
    arrest = decide(
        _breathing_case(Breathing.NONE)
    )
    assert arrest.step is AssessmentStep.INSTRUCT_CPR

    # Even with an obstruction reported, an UNRESPONSIVE patient compresses:
    # protocols.py's choking protocol scopes itself to the responsive patient.
    reported_but_collapsed = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.UNRESPONSIVE),
        breathing=established(Breathing.NONE),
        airway_obstruction=established(AirwayObstruction.WITNESSED_FOREIGN_BODY),
    )
    assert decide(reported_but_collapsed).step is AssessmentStep.INSTRUCT_CPR, (
        "a collapsed choker needs compressions, not thrusts"
    )

    # And the obstruction question is never put to an unconscious patient.
    for assignment, branch in ALL_BRANCHES:
        if branch.step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION:
            responsiveness = assignment["responsiveness"]
            assert responsiveness is not None
            assert not responsiveness.is_unconscious, (
                f"{assignment} asked an unconscious patient's bystander to "
                f"reconstruct a choking history"
            )


def test_a_catastrophic_bleed_outranks_a_still_responsive_choker() -> None:
    # falsifier: the pre-A bleed hoist and the choking gate both fire on a
    # responsive patient, and nothing records which wins - so the priority is an
    # accident of line order that a future author reorders without knowing they
    # decided anything. Flagged by independent review as untested and
    # undiscussed.
    #
    # The bleed wins, and this is the decision: exsanguination is two to three
    # minutes, while nothing here has reported an obstruction OR that this
    # patient has stopped moving air. A responsive patient who is still moving
    # some air is what `protocols.py:airway-choking-adult`'s own scope means by
    # "only while they are still responsive". The choker who stops being
    # responsive stops being a choking presentation and becomes an arrest, which
    # the arrest path owns. Both are one turn apart either way, and only one of
    # them is losing volume that cannot be put back.
    #
    # THE PREMISE IS CORRECTED HERE, AND IT WAS LOAD-BEARING. This comment used
    # to justify the priority with "a patient who is STILL RESPONSIVE is by
    # definition still moving some air", unqualified - and the gate's own spoken
    # rationale made the same unqualified claim, "a patient who can be asked
    # about is a patient with an airway". True of a talking patient, and false
    # of a patient whose breathing is ESTABLISHED as NONE. Blocker 1 of the
    # third clinical review is what the over-general version cost.
    #
    # AND THE CASE IT IS ASSERTED ON HAS CHANGED WITH IT, which matters more
    # than the wording. This test used to put a WITNESSED_FOREIGN_BODY in the
    # winning cell and assert the bleed took it anyway. An independent review of
    # Blocker 1's first fix showed that is wrong for a different reason: with the
    # obstruction ESTABLISHED there is no question left to ask, so the bare bleed
    # instruction returns forever and a reported obstruction is discarded rather
    # than deferred. So the contest this test pins is now the one where the
    # premise genuinely holds - nothing reported about the airway at all - and
    # the established-obstruction cells belong to
    # `test_a_known_obstruction_is_never_discarded_by_the_bleed_hoist`.
    both = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.ALERT),
        severe_bleeding=established(SevereBleeding.PRESENT),
    )
    assert decide(both).step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING

    # And it is genuinely a contest rather than the bleed being the only branch
    # that could fire: remove the bleed and the choking DISCRIMINATOR appears,
    # which is what this patient's airway gets when nothing is known about it.
    without_bleed = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.ALERT),
        severe_bleeding=established(SevereBleeding.NONE),
    )
    assert (
        decide(without_bleed).step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION
    )

    # Same contest against the conscious-distress leaf, same winner, same
    # reason - recorded so neither ordering is an accident.
    bleeding_and_distressed = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.ALERT),
        breathing=established(Breathing.ABNORMAL_OR_GASPING),
        severe_bleeding=established(SevereBleeding.PRESENT),
        airway_obstruction=NO_OBSTRUCTION,
    )
    assert (
        decide(bleeding_and_distressed).step
        is AssessmentStep.INSTRUCT_CONTROL_BLEEDING
    )


def test_an_awake_choker_who_is_moving_no_air_gets_the_bleed_and_the_airway(
) -> None:
    # falsifier: BLOCKER 1 OF THE THIRD CLINICAL REVIEW, AND IT WAS A REACHABLE
    # DEATH. ALERT + breathing NONE + WITNESSED_FOREIGN_BODY + bleeding PRESENT
    # returned INSTRUCT_CONTROL_BLEEDING at Criticality 5, whose spoken rationale
    # names no airway, no obstruction, no thrusts and no compressions and says
    # "keep the hands on the wound while answering anything else" - so a
    # conscious person with a lodged bolus and zero air movement arrests in front
    # of a bystander who has been told not to touch the airway. Blocker 5's death
    # reached through the bleed gate instead of the arrest route.
    #
    # THE ROUND-2 CELL DOES NOT COVER THIS, which is why it needed its own test.
    # `test_a_catastrophic_bleed_outranks_a_still_responsive_choker` leaves
    # `breathing` UNKNOWN and justifies the priority with "a patient who is still
    # responsive is still moving some air". Sound for an unestablished value,
    # false for an ESTABLISHED NONE - the reading that says in so many words that
    # this patient is moving no air at all.
    #
    # AND THE CHOICE IS NOT FORCED, which is why "the bleed outranks it" is not
    # an honest answer here: `INSTRUCT_CONTROL_BLEEDING_THEN_CPR` already solves
    # this same single-rescuer conflict for the UNCONSCIOUS twin of this patient
    # with pressure that needs no hands. Thrusts need both hands. So both are
    # performable, and Blocker 2's rule applies unchanged - do both, do not
    # choose.
    established_apnoea = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.ALERT),
        breathing=established(Breathing.NONE),
        severe_bleeding=established(SevereBleeding.PRESENT),
        airway_obstruction=established(AirwayObstruction.WITNESSED_FOREIGN_BODY),
    )
    branch = decide(established_apnoea)
    assert (
        branch.step
        is AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION
    ), f"the awake apnoeic choker with a bleed got {branch.step}"
    lowered = branch.rationale.lower()

    # THE AIRWAY IS ACTED ON, which is the half that was missing entirely.
    assert "back blow" in lowered and "thrust" in lowered, (
        "compressions cannot shift a lodged bolus; the thrusts are the "
        "intervention with the survival curve and must be named"
    )
    # THE BLEED IS NOT DROPPED, which is the half the narrower fix would have
    # lost - the reviewer rejected letting the choking block simply stand the
    # hoist aside for exactly this reason.
    assert "haemorrhage" in lowered or "bleeding" in lowered, (
        "standing the bleed hoist aside must not drop the bleed"
    )
    # AND THE CONFLICT IS RESOLVED THE WAY THE MODULE ALREADY KNOWS HOW: pressure
    # held without hands, so both hands are free for the thrusts.
    assert "without using your hands" in lowered, (
        "a forced choice is not honest here: the combined arrest leaf already "
        "proves hands-free pressure frees the hands the thrusts need"
    )
    for owed in ("knee", "weight"):
        assert owed in lowered, f"the hands-free method must name {owed}"
    # The existing transition is carried, not lost in the recombination.
    assert "unconscious" in lowered and "compression" in lowered, (
        "the transition to CPR on loss of consciousness must survive"
    )
    # And the gate must not cap a reported arrest - Blocker 3(b).
    assert branch.criticality is Criticality.CRITICAL, (
        "breathing is an established NONE, so the category is the arrest's"
    )
    # THE BLEED IS STILL OUTSTANDING UNDERNEATH THE AIRWAY, so C is the letter
    # that remains unresolved and a later turn has to come back to it. Found by
    # mutation testing: flipping this to Letter.A broke nothing, which means
    # nothing was asserting that the haemorrhage stays flagged after the
    # instruction that treats it also treats something else.
    assert branch.unresolved_letter is Letter.C, (
        "the haemorrhage is treated but not resolved, so C stays open"
    )

    # THE SECOND REPRODUCED CELL: the obstruction merely UNASKED. The choking
    # discriminator is what decides thrusts versus compressions, so it still
    # wins the turn - that precedence is the previous round's and is untouched -
    # but the bleed rides along with it rather than waiting for the answer.
    unasked = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.ALERT),
        breathing=established(Breathing.NONE),
        severe_bleeding=established(SevereBleeding.PRESENT),
    )
    asked = decide(unasked)
    assert asked.step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION, (
        f"the choking question never got asked; got {asked.step}"
    )
    asked_lowered = asked.rationale.lower()
    assert "bleeding" in asked_lowered and "without using your hands" in (
        asked_lowered
    ), (
        "the question may stand in front of an established bleed only because "
        "it instructs the pressure in the same breath"
    )
    assert asked.criticality is Criticality.CRITICAL

    # AND THE ROUND-2 JUDGEMENT IS UNDISTURBED WHEREVER ITS PREMISE HOLDS: no
    # obstruction reported and no report that the air has stopped, so the bleed
    # wins outright and the airway question waits for a later turn. This is the
    # case the hoist was written for.
    for breathing in (
        Finding.unknown(),
        established(Breathing.ABNORMAL_OR_GASPING),
        established(Breathing.NORMAL),
    ):
        for obstruction in (Finding.unknown(), NO_OBSTRUCTION):
            still_the_bleed = decide(
                AssessmentInputs(
                    scene_safe=SAFE,
                    responsiveness=established(Responsiveness.ALERT),
                    breathing=breathing,
                    severe_bleeding=established(SevereBleeding.PRESENT),
                    airway_obstruction=obstruction,
                )
            )
            assert (
                still_the_bleed.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING
            ), (
                f"breathing={breathing.value} with obstruction="
                f"{obstruction.value} leaves the hoist's premise intact, so "
                f"the recorded judgement must stand; got {still_the_bleed.step}"
            )


def test_a_known_obstruction_is_never_discarded_by_the_bleed_hoist() -> None:
    # falsifier: FOUND BY INDEPENDENT REVIEW OF BLOCKER 1'S FIRST FIX, and it is
    # Blocker 1's own defect one step to the left. That fix stood the bleed hoist
    # aside only when breathing was an ESTABLISHED NONE, so ALERT +
    # ABNORMAL_OR_GASPING + WITNESSED_FOREIGN_BODY + bleeding PRESENT still
    # returned the bare INSTRUCT_CONTROL_BLEEDING, whose rationale never mentions
    # the obstruction the caller has ALREADY REPORTED.
    #
    # WHY THAT IS WORSE THAN AN UNASKED QUESTION, WHICH IS THE WHOLE ARGUMENT.
    # Every input in that cell is ESTABLISHED, so there is nothing left to ask
    # and nothing new can arrive - and `decide` is pure, so the same facts return
    # the same bare instruction forever. It is an ABSORBING STATE on a finding
    # the machine is already holding, which is Blocker 3(a)'s shape, and the
    # machine is not waiting to find out: it knows about the foreign body and
    # says nothing. Air movement past a partial obstruction is not evidence the
    # obstruction is benign - a witnessed foreign body with gasping is the
    # trajectory INTO complete obstruction that the choking block exists to
    # intercept early.
    #
    # So the discriminator is not the breathing value: it is whether deferring
    # the airway for a turn can still be repaired by a later turn.
    for breathing in (
        Finding.unknown(),
        established(Breathing.NONE),
        established(Breathing.ABNORMAL_OR_GASPING),
        established(Breathing.NORMAL),
    ):
        branch = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(Responsiveness.ALERT),
                breathing=breathing,
                severe_bleeding=established(SevereBleeding.PRESENT),
                airway_obstruction=established(
                    AirwayObstruction.WITNESSED_FOREIGN_BODY
                ),
            )
        )
        assert (
            branch.step
            is AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CLEAR_AIRWAY_OBSTRUCTION
        ), (
            f"a WITNESSED foreign body is a finding in hand, so breathing="
            f"{breathing.value} cannot make it disappear; got {branch.step}"
        )
        lowered = branch.rationale.lower()
        assert "thrust" in lowered and "back blow" in lowered, (
            "a reported obstruction must reach an instruction that acts on it"
        )
        assert "haemorrhage" in lowered or "bleeding" in lowered, (
            "and the bleed must not be dropped to make room for it"
        )

    # AND A REPORTED *ABSENCE* OF AN OBSTRUCTION IS NOT A FINDING TO ACT ON, so
    # it does not stand the hoist aside. This pins the coupling that lets
    # `_airway_outranks_the_bleed_hoist` test `is_established` alone: it is
    # correct only because `_reaches_choking_block` has already filtered an
    # established NONE out, and mutation testing showed the belt-and-braces
    # member check was behaviourally dead. If that filtering ever changed, the
    # simplified clause would send a patient with NO obstruction into the
    # choking block, and this is the assertion that would fail.
    for breathing in (
        Finding.unknown(),
        established(Breathing.ABNORMAL_OR_GASPING),
        established(Breathing.NORMAL),
    ):
        no_obstruction = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(Responsiveness.ALERT),
                breathing=breathing,
                severe_bleeding=established(SevereBleeding.PRESENT),
                airway_obstruction=NO_OBSTRUCTION,
            )
        )
        assert no_obstruction.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING, (
            f"a reported ABSENCE of an obstruction is not an airway finding, "
            f"so the bleed still wins; got {no_obstruction.step}"
        )


def test_the_obstruction_question_also_retests_the_alert_claim() -> None:
    # falsifier: a caller who describes a posturing or agonal casualty as
    # "alert" sends the machine down the conscious path, and nothing ever
    # re-tests that claim - so an arrest sits behind a choking question. The
    # discriminator is free and does two jobs: a genuinely alert patient talks.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
        )
    )
    assert branch.step is AssessmentStep.ASK_AIRWAY_OBSTRUCTION
    lowered = branch.rationale.lower()
    assert "talking" in lowered or "answer you" in lowered, (
        "the pinned phrasing must re-test the alert report in the same turn"
    )
    assert "witnessed" in lowered or "saw" in lowered, (
        "and still carry the choking discriminator"
    )


def test_no_assessment_step_is_declared_and_never_returned() -> None:
    # falsifier: THE DEAD-MEMBER DEFECT. `ASK_AGE_BAND` and
    # `INSTRUCT_OPEN_AIRWAY` were declared and never returned, and
    # `airway_opened` was an input nothing read. That is safe today and
    # misleading tomorrow: a future author sees a member plus a matching input
    # field and assumes a designed slot that works. Enforced against the
    # exhaustive walk, so a decorative member added later fails here rather than
    # waiting to mislead someone.
    reachable = {branch.step for _, branch in ALL_BRANCHES}
    unreachable = set(AssessmentStep) - reachable
    assert not unreachable, (
        f"declared but never returned by any of {len(ALL_BRANCHES)} input "
        f"combinations: {sorted(step.name for step in unreachable)}. Either "
        f"wire it or delete it - a member implying a slot that does not exist "
        f"is how the next author builds on a promise nobody kept."
    )


def test_no_input_field_is_declared_and_never_read() -> None:
    # falsifier: the mirror of the row above, on the input side. `airway_opened`
    # was read by nothing, which is what made `INSTRUCT_OPEN_AIRWAY` look like a
    # designed slot rather than a leftover. `pulse` is the ONE deliberate
    # exception and is asserted to be the only one: it is carried so a
    # volunteered "I can't find a pulse" reaches the clinician bridge, and
    # ASM-12 requires that nothing branch on it.
    #
    # OBSERVED AT RUNTIME, NOT SCANNED FROM THE SOURCE. This used to walk the
    # AST for `inputs.<name>`, which stopped being where the reads are once
    # Blocker 2's fix routed every read through `_Consulted`'s typed accessors -
    # and an AST scan would now also have to exclude those accessors' own
    # `self._inputs.<name>` bodies, which is a scan that reports the accessor
    # list rather than the decision path. The walk already visits every
    # reachable combination, so the union of what `_Consulted` recorded across
    # it IS the set of inputs the decision path reads. That is the fact the row
    # is about, observed rather than inferred from syntax.
    read: set[str] = set()
    for assignment, _ in ALL_BRANCHES:
        read |= _consulted_by(assignment)

    fields = set(AssessmentInputs.__dataclass_fields__)
    unread = fields - read
    assert unread == {"pulse"}, (
        f"every input must be read by the decision path except `pulse`, which "
        f"is recorded for the bridge by design. Unread: {sorted(unread)}"
    )


def test_every_leaf_records_the_assumptions_on_its_path() -> None:
    # falsifier: RE-REVIEW DEFECT 2, AND THE REASON IT IS A WALK AND NOT A CASE.
    # `INSTRUCT_SUPPORT_SEVERE_BREATHING_DIFFICULTY` was the only instruction
    # leaf calling neither `inputs.assumed(...)` nor passing `assumed=`.
    # Reproduced: reached with scene_safe at ASSUMED_WORST it reported
    # provenance=EVIDENCE and assumed_inputs=(), so its Criticality 4 was
    # UNCORRECTABLE by later evidence - the exact inversion ASM-14 exists to
    # prevent, on the leaf whose patient is most likely to change state. A
    # clinician who later establishes the scene cannot lower a level that claims
    # to rest on evidence.
    #
    # Asserted over EVERY branch rather than on that leaf, because a per-leaf
    # rule enforced by the author's memory is exactly the rule that was missed
    # once and will be missed again by the next leaf. Same shape as the
    # `_hazard_suffix` walk the previous round added.
    #
    # THIS TEST COULD NOT FAIL, AND THAT WAS BLOCKER 2 OF THE THIRD CLINICAL
    # REVIEW. Where the assertion below now stands, it read:
    #
    #     if name not in branch.assumed_inputs:
    #         continue
    #     assert branch.provenance is Provenance.ASSUMPTION
    #
    # A leaf that CORRECTLY recorded the input got asserted; a leaf that FAILED
    # to record it hit the `continue` and was skipped. So the test could only
    # ever fire on leaves that had already got it right, while its own comment
    # claimed "any leaf that reads an input without recording it fails here" -
    # which was false. Inverting that `continue` into the assertion exposed
    # SIXTEEN of the EIGHTEEN reachable steps under-recording, not the two the
    # review had reported.
    #
    # SCOPED TO THE INPUTS THE PATH ACTUALLY CONSULTED, via `_consulted_by`. The
    # naive inversion over-fires: it demands `ASK_SCENE_SAFE` record `age_band`,
    # which that path never reads, and recording an input off the path would
    # make every branch an assumption and destroy the distinction ASM-14 rests
    # on. The module's own accumulator answers which inputs were read, so the
    # rule asserted here is exactly "recorded == consulted-and-assumed".
    inputs_fields = list(AssessmentInputs.__dataclass_fields__)
    checked = 0
    for name in inputs_fields:
        if name == "pulse":
            # Never a branch point by design (ASM-12), so it is never on a
            # path's assumption set. Asserted separately below.
            continue
        for assignment, walked in ALL_BRANCHES:
            if assignment[name] is None:
                continue
            if name not in _consulted_by(assignment):
                # Not on this path at all, so it is not this leaf's to record.
                continue
            # Downgrade exactly one established input to an assumption and
            # re-decide. If the same branch is still reached, it must now say
            # so.
            findings = {
                field_name: (
                    Finding.unknown()
                    if value is None
                    else Finding(
                        value,
                        Certainty.ASSUMED_WORST
                        if field_name == name
                        else Certainty.ESTABLISHED,
                    )
                )
                for field_name, value in assignment.items()
            }
            branch = decide(AssessmentInputs(**findings))
            if branch.step is not walked.step:
                # The downgrade changed the route; that branch is asserted on
                # its own terms elsewhere in the walk.
                continue
            checked += 1
            # THE INVERTED GUARD. This was a `continue`, which is what made the
            # test incapable of finding the defect it was commissioned for.
            assert name in branch.assumed_inputs, (
                f"{branch.step} read {name} on its path but did not record it "
                f"as assumed, so the level it asks for reports provenance "
                f"{branch.provenance.name} and a clinician who later "
                f"establishes {name} cannot correct it - ASM-14 inverted. "
                f"Inputs: {assignment}"
            )
            assert branch.provenance is Provenance.ASSUMPTION, (
                f"{branch.step} recorded {name} as assumed but still reported "
                f"provenance EVIDENCE, so the level cannot be corrected"
            )
            break
    assert checked > 0

    # AND THE SPECIFIC LEAF THE DEFECT WAS FOUND ON, reproduced as reported.
    distress = decide(
        AssessmentInputs(
            scene_safe=Finding(SceneSafety.SAFE, Certainty.ASSUMED_WORST),
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.NONE),
        )
    )
    assert (
        distress.step
        is AssessmentStep.INSTRUCT_SUPPORT_SEVERE_BREATHING_DIFFICULTY
    )
    assert distress.assumed_inputs == ("scene_safe",), (
        "the distress leaf must record the assumptions on its path; it was "
        f"the only leaf recording none at all. Got {distress.assumed_inputs}"
    )
    assert distress.provenance is Provenance.ASSUMPTION, (
        "a Criticality 4 reached partly by assumption must be correctable by "
        "later evidence - ASM-14, inverted on this leaf before the fix"
    )


def test_no_instruction_leaf_omits_the_assumed_argument() -> None:
    # falsifier: the walk above can only observe leaves it can reach with a
    # downgraded input, so a leaf could still be added tomorrow with no
    # `assumed=` at all and slip through on a path where nothing was assumable.
    # This reads the source instead: every `_branch(` call that returns an
    # INSTRUCT_ step must pass `assumed=`, which is the structural form of the
    # rule. Defect 2 was a single missing keyword argument, and it survived a
    # clinical review.
    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_branch":
            continue
        step = node.args[0] if node.args else None
        if not isinstance(step, ast.Attribute):
            continue
        if not step.attr.startswith("INSTRUCT_"):
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "assumed" not in keywords:
            missing.append(step.attr)
    # INSTRUCT_MAKE_SCENE_SAFE is the one deliberate exception: it is reached
    # only on an ESTABLISHED UNSAFE scene, so there is no assumption on its
    # path by construction, and the walk above proves it.
    assert missing == ["INSTRUCT_MAKE_SCENE_SAFE"], (
        f"every instruction leaf must record its assumptions; these omit "
        f"`assumed=`: {missing}"
    )

    # AND IT MUST BE THE PATH, NOT A LIST. Blocker 2 of the third clinical
    # review noted this test checked only that `assumed=` was PRESENT, never
    # that it named the path - which is why sixteen steps could pass it while
    # under-recording. Every `assumed=` must now be `path.assumed`, the
    # accumulator's own answer; a hand-written `inputs.assumed("a", "b")` is a
    # second copy of the path and is what drifted.
    hand_written: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_branch":
            continue
        step = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg != "assumed":
                continue
            is_path = (
                isinstance(keyword.value, ast.Attribute)
                and keyword.value.attr == "assumed"
                and isinstance(keyword.value.value, ast.Name)
                and keyword.value.value.id == "path"
            )
            if not is_path:
                hand_written.append(
                    step.attr if isinstance(step, ast.Attribute) else "?"
                )
    assert hand_written == [], (
        f"`assumed=` must be `path.assumed` so the recorded set is the set the "
        f"path actually read; these hand-write a list instead: {hand_written}"
    )


# -------------- RE-REVIEW DEFECT 3: the arrest path reads the obstruction


def test_an_arrest_with_a_witnessed_foreign_body_says_to_look_in_the_mouth(
) -> None:
    # falsifier: RE-REVIEW DEFECT 3, AND IT IS BLOCKER 2'S SHAPE EXACTLY - an
    # established finding the arrest path did not read, fixed for bleeding and
    # left open for choking. Reproduced: UNRESPONSIVE + breathing NONE +
    # WITNESSED_FOREIGN_BODY returned a plain INSTRUCT_CPR whose rationale never
    # mentioned the mouth or the object. A bystander who watched food go in and
    # the patient collapse was never told to look, so a retrievable obstruction
    # stayed where it was while compressions ran.
    for breathing in (Breathing.NONE, Breathing.ABNORMAL_OR_GASPING):
        branch = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(Responsiveness.UNRESPONSIVE),
                breathing=established(breathing),
                airway_obstruction=established(
                    AirwayObstruction.WITNESSED_FOREIGN_BODY
                ),
            )
        )
        lowered = branch.rationale.lower()
        assert "look in the mouth" in lowered, (
            f"an arrest with a witnessed obstruction ({breathing}) never told "
            f"the responder to look for the object"
        )
        # EVERY ARREST LEAF STAYS AT CRITICAL AND STILL RESUSCITATES. The mouth
        # check is an addition to compressions, never a gate in front of them -
        # a manoeuvre gate before resuscitation is what the FIRST review's
        # Blocker 2 killed.
        assert branch.criticality is Criticality.CRITICAL
        assert branch.step in {
            AssessmentStep.INSTRUCT_CPR,
            AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR,
            AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR,
        }
        # AND IT MUST NOT COACH A BLIND FINGER SWEEP, which is the harm in the
        # other direction: a sweep on an unseen obstruction pushes it deeper,
        # can impact it in the larynx, and injures a patient who may have had
        # no obstruction at all.
        assert "see" in lowered, "removal must be conditional on seeing it"
        assert "blindly" in lowered or "blind" in lowered, (
            "and the blind sweep must be named as the thing NOT to do"
        )


def test_the_mouth_check_reaches_every_arrest_leaf_including_the_bleeding_one(
) -> None:
    # falsifier: the mouth check is added to the leaf the reviewer happened to
    # reproduce and not to the others, so a drowned child or a bleeding arrest
    # with a witnessed obstruction still loses it. This is the per-leaf failure
    # mode that produced Defect 3 in the first place - `_hazard_suffix` exists
    # because of it - so the clause is asserted on all three leaves.
    leaves = {
        AssessmentStep.INSTRUCT_CPR: {},
        AssessmentStep.INSTRUCT_RESCUE_BREATHS_THEN_CPR: {
            "submersion": YES,
        },
        AssessmentStep.INSTRUCT_CONTROL_BLEEDING_THEN_CPR: {
            "severe_bleeding": established(SevereBleeding.PRESENT),
        },
    }
    for expected, extra in leaves.items():
        branch = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(Responsiveness.UNRESPONSIVE),
                breathing=established(Breathing.NONE),
                airway_obstruction=established(
                    AirwayObstruction.WITNESSED_FOREIGN_BODY
                ),
                **extra,
            )
        )
        assert branch.step is expected, f"expected {expected}, got {branch.step}"
        assert "look in the mouth" in branch.rationale.lower(), (
            f"{expected} lost the mouth check"
        )


def test_an_arrest_without_a_witnessed_obstruction_carries_no_mouth_check(
) -> None:
    # falsifier: the mouth check is appended unconditionally, so every arrest
    # instruction grows a sentence about an object nobody saw. That is the
    # `_hazard_suffix` tune-out problem the module already records as a gap, and
    # it spends the one thing a CPR instruction has - the responder's attention
    # on compressions. An unwitnessed collapse is not a choking presentation.
    for obstruction in (NO_OBSTRUCTION, Finding.unknown()):
        branch = decide(
            AssessmentInputs(
                scene_safe=SAFE,
                responsiveness=established(Responsiveness.UNRESPONSIVE),
                breathing=established(Breathing.NONE),
                airway_obstruction=obstruction,
            )
        )
        assert "look in the mouth" not in branch.rationale.lower(), (
            "the mouth check must be scoped to a WITNESSED obstruction"
        )


# ---------- RE-REVIEW DEFECT 4: the distress leaf and the corpus positioning


def test_the_distress_leaf_states_no_positioning_imperative_it_cannot_justify(
) -> None:
    # falsifier: RE-REVIEW DEFECT 4. The leaf said "Sit them upright... and do
    # NOT lay them flat", unconditionally and in capitals, while
    # `protocols.py:anaphylaxis` says "Lay them flat with legs raised; sit them
    # up only if breathing is hard". L2 outranks retrieval, so the unconditional
    # imperative won and took out the one positioning rule that has killed
    # people - anaphylaxis patients who sit or stand up arrest, the
    # empty-ventricle phenomenon.
    #
    # Asthma, pulmonary oedema, anaphylaxis and a stabbed chest ALL reach this
    # leaf and L2 holds no input that distinguishes them, so the fix cannot be a
    # per-condition rule. What it can be is a position of COMFORT plus the
    # deterioration rule that is true for all four.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.NONE),
        )
    )
    lowered = branch.rationale.lower()
    assert "do not lay them flat" not in lowered, (
        "the unconditional prohibition contradicts protocols.py:anaphylaxis "
        "and outranks it, which loses the flat-with-legs-raised rule"
    )
    # The position that helps is still named - deferring positioning wholesale
    # to retrieval at 64% top-1 is the Blocker 4 failure mode.
    assert "position they can breathe in" in lowered
    assert "lean forward" in lowered or "leaning forward" in lowered
    # And the corpus rule's real content is restored in the form that holds for
    # every presentation here: the danger is being UP once they go shocky.
    for marker in ("pale", "grey", "clammy", "faint"):
        assert marker in lowered, (
            f"the deterioration-to-shock rule must name {marker}; it is what "
            f"the corpus's flat-with-legs-raised rule actually protects against"
        )
    assert "raise their legs" in lowered, (
        "and must give the corpus's action for that state"
    )
    # The per-condition detail is deferred to the document that owns it.
    assert "protocol" in lowered, (
        "positioning detail beyond the shared rule belongs to the cited "
        "protocol, which is where the per-condition wording lives"
    )


def test_the_distress_leaf_tells_them_to_search_for_the_auto_injector() -> None:
    # falsifier: the leaf says "if they have one" about the auto-injector where
    # `protocols.py:anaphylaxis` says to ASK for one and check pockets and bags.
    # A passive clause where the corpus is active loses the adrenaline for a
    # patient who has it in a bag two metres away - and adrenaline is the only
    # thing that treats anaphylaxis.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.ALERT),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            airway_obstruction=NO_OBSTRUCTION,
            severe_bleeding=established(SevereBleeding.NONE),
        )
    )
    lowered = branch.rationale.lower()
    assert "pockets" in lowered and "bags" in lowered, (
        "the corpus says to search pockets and bags; a passive 'if they have "
        "one' loses the adrenaline"
    )
    # And still their own device, never somebody else's.
    assert "not somebody else" in lowered or "not someone else" in lowered


def test_the_recorded_gaps_stay_recorded() -> None:
    # falsifier: the re-review's record-don't-fix findings are agreed in a
    # review file and nowhere a future author will read, so the next person
    # rediscovers each one as a bug and cannot tell whether it is an oversight
    # or a decision. The spinal one matters most: it is a genuine clinical
    # DISAGREEMENT with the previous round's fix, and silently keeping either
    # side would hide that a clinician argued the other way.
    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    # Docstring prose wraps, so a multi-word marker can straddle a newline plus
    # indentation. Collapse whitespace before matching rather than choosing
    # markers short enough to survive wrapping, which would make the assertions
    # weaker than the rules they stand for.
    lowered = " ".join(source.lower().split())
    for owed, why in (
        (
            "dose-over-time",
            "fire and gas are still not fully modelled: the class exists and "
            "gets the right warning, but dose-over-time wants a clock and an "
            "extraction decision that L2 has no expression for",
        ),
        (
            "tuned out",
            "`_hazard_suffix`'s REPETITION half remains a gap - per-class text "
            "fixes the wrong-warning half and L2 holds no turn history, so "
            "within a class the sentence is identical on turn twenty",
        ),
        (
            'no "bleeding controlled" input',
            "an established PRESENT bleed returns INSTRUCT_CONTROL_BLEEDING "
            "forever - the absorbing-loop shape the scene gate fix killed",
        ),
        (
            "contested",
            "the spinal leaf's jaw thrust is a clinical disagreement with the "
            "previous round, not a settled fix",
        ),
        (
            "tension pneumothorax",
            "leg-raising on deterioration is the weakest part of Defect 4's "
            "fix for a penetrating chest injury; flagged by independent "
            "review, kept deliberately, and recorded as a judgement",
        ),
        (
            "tongue into the airway",
            "the named untrained failure mode of a jaw thrust",
        ),
        # THE THIRD CLINICAL REVIEW'S RECORD-DON'T-FIX FINDINGS. Its Blocker 3
        # - the live-electricity re-weighting and the constant hazard string -
        # is no longer among them: it is FIXED, and the two rows that used to
        # assert it stays recorded are replaced by
        # `test_the_recorded_gaps_do_not_still_claim_the_fixed_ones_are_open`
        # below, which asserts the opposite and would fail if the fix were
        # reverted without restoring the record.
        (
            "escape hatch is now scoped to adjacent",
            "the committed escape hatch must be recorded as PER-CLASS, since "
            "the module's own docstring previously described it as global and "
            "a reader would otherwise apply it to an energised patient",
        ),
        # NOT `_contradiction_clause`'s wording, which is FIXED rather than
        # recorded - see
        # `test_the_contradiction_wording_gap_is_recorded_as_closed_and_not_as_open`.
        (
            "are the failure itself",
            "the deterioration rule is WRONG for pulmonary oedema: the "
            "greyness and clamminess ARE the cardiogenic failure, so the "
            "trigger fires on the patient the rule then harms",
        ),
        (
            "worsens the oedema",
            "flat-with-legs-raised increases venous return into a failing left "
            "ventricle; the existing chest-wound gap concedes the tension "
            "objection and does not address this, where the mechanism argument "
            "runs the other way",
        ),
    ):
        assert owed in lowered, f"the module must record: {why}"


def test_the_hazard_class_gap_is_recorded_as_closed_and_not_as_open() -> None:
    # falsifier: the mirror of the row above, and the reason it is a separate
    # test. The recorded-gaps block keeps describing the live-electricity defect
    # as an open gap after it has been fixed, so the next author reads a warning
    # about a reachable electrocution that no longer exists, goes looking for
    # it, and either "fixes" it a second time or - worse - concludes the gaps
    # block is stale and stops trusting the rest of it. A gap list that lies in
    # the safe direction still destroys its own credibility.
    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    lowered = " ".join(source.lower().split())
    # The fix is recorded AS a fix, with the mechanisms named so the next author
    # can find them rather than rediscovering the shape.
    for owed, why in (
        (
            "the hazard class gap is closed",
            "the gap must be marked closed where it was recorded, not silently "
            "deleted - a deleted gap looks like one nobody ever found",
        ),
        (
            "instruct_break_electrical_contact",
            "and name the mechanism that closes it",
        ),
        (
            "what remains of it, and it is not nothing",
            "and be honest that two narrower residues remain, rather than "
            "claiming a clean close",
        ),
    ):
        assert owed in lowered, f"the module must record: {why}"

    # And the claim is not merely prose: the mechanism it names exists and is
    # reachable. A recorded fix whose mechanism is absent is this repo's
    # signature failure mode - a mechanism whose only implementation is its own
    # description - which the third review counted five instances of.
    reachable = {branch.step for _, branch in ALL_BRANCHES}
    assert AssessmentStep.INSTRUCT_BREAK_ELECTRICAL_CONTACT in reachable, (
        "the module records the hazard-class gap as closed by a mechanism that "
        "no input combination reaches"
    )


def test_choking_is_recorded_as_implemented_rather_than_as_a_gap() -> None:
    # falsifier: the Blocker 5 decision is made in a commit message and nowhere
    # a future author will read, so the next person cannot tell whether choking
    # is absent by oversight or out of scope by decision - and the two demand
    # opposite responses. The review's instruction was to decide and justify,
    # and a justification that is not in the module is not available where it
    # is needed.
    source = Path(inspect.getfile(decide)).read_text(encoding="utf-8")
    for owed in (
        # The corpus document that owns the wording, so the DRY boundary is
        # findable rather than rediscovered.
        "airway-choking-adult",
        # The two deleted members, each named with its reason.
        "ASK_AGE_BAND",
        "INSTRUCT_OPEN_AIRWAY",
        "airway_opened",
    ):
        assert owed in source, f"the Blocker 5 decision must record {owed}"

    # And the two non-blocking gaps the review asked to be RECORDED, not fixed.
    # A gap nobody wrote down is a gap the next author rediscovers as a bug.
    lowered = source.lower()
    assert "compensated shock" in lowered, (
        "excluding capillary refill loses the pale, clammy, still-talking "
        "patient, who now has no discriminator; record it as a gap"
    )
    assert "genuinely improves" in lowered or "genuinely improve" in lowered, (
        "the ratchet stops tracking a patient who genuinely improves, and the "
        "clinician on the bridge has no channel to resolve it"
    )


def test_catastrophic_bleeding_is_treated_before_d() -> None:
    # falsifier: the treat-before-moving-on rule is not enforced for C, so the
    # agent notes the haemorrhage and moves on to ask about consciousness while
    # the patient exsanguinates. CLINICAL-STANDARDS.md §1.1 rule 2.
    branch = decide(
        AssessmentInputs(
            scene_safe=SAFE,
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.NORMAL),
            severe_bleeding=established(SevereBleeding.PRESENT),
            exposure_reviewed=SAFE,
        )
    )
    assert branch.step is AssessmentStep.INSTRUCT_CONTROL_BLEEDING
    assert branch.unresolved_letter is Letter.C
    assert branch.criticality >= Criticality.SEVERE


def test_a_fully_assessed_alert_patient_is_not_left_at_minor() -> None:
    # falsifier: a patient the agent was called to, and has fully surveyed,
    # settles at Criticality 1 - so the record reads as though nothing happened
    # and the incident looks closeable. The floor for a surveyed patient is
    # above MINOR.
    branch = decide(_well_patient())
    assert branch.step is AssessmentStep.INSTRUCT_MONITOR
    assert branch.criticality is Criticality.LOW
    assert branch.criticality > Criticality.MINOR


def test_new_confusion_settles_higher_than_a_fully_alert_patient() -> None:
    # falsifier: the C of ACVPU is treated as equivalent to A, so new confusion -
    # a red flag in its own right, and the reason ACVPU has a C at all - settles
    # at the same level as a patient who is fully alert.
    confused = AssessmentInputs(
        scene_safe=SAFE,
        responsiveness=established(Responsiveness.CONFUSED),
        breathing=established(Breathing.NORMAL),
        severe_bleeding=established(SevereBleeding.NONE),
        exposure_reviewed=SAFE,
        age_band=established(AgeBand.ADULT),
    )
    assert decide(confused).criticality > decide(_well_patient()).criticality


def test_responding_only_to_pain_counts_as_unconscious() -> None:
    # falsifier: P on the ACVPU scale is read as "responsive", so a patient who
    # reacts only to a pinch is left supine with an airway they cannot protect.
    # CLINICAL-STANDARDS.md §1.6 puts unprotected-airway-plus-unconscious into
    # the recovery position for exactly this reason.
    assert Responsiveness.PAIN.is_unconscious is True
    assert Responsiveness.UNRESPONSIVE.is_unconscious is True
    assert Responsiveness.VOCAL.is_unconscious is False
    assert Responsiveness.ALERT.is_unconscious is False
