"""B5 - the headless scenario harness.

WHAT THIS IS FOR. `docs/DESIGN.md` §8 carries a column named `Ev`, and for a
row of that register the word `harness` in that column is the *whole* argument
that the row is BUILT. `docs/reviews/2026-09-15-readiness-3.md` found five rows
whose evidence named a mechanism FR-013 itself marked NEW, which is to say: the
evidence for those rows was the existence of this file. This file did not exist.
B5 exists to make that column true rather than to make it look true.

WHAT IT DRIVES, AND WHY THAT IS THE HONEST SCOPE. A scenario turn is pushed
through the *real* entry points of the deterministic stack:

  - `phrases.hard_escalation_triggered` / `escalation.apply_hard_escalation` -
    L1, the deterministic escalation net, over the responder's literal words;
  - `assessment.decide` - L2, the ABCDE state machine, over typed findings;
  - `triage.TriageState.set_level` - the upward ratchet with ASM-14 provenance;
  - `turn_gate.gate_turn` -> `output_gate.validate_turn` - B4's output gate.

It does NOT drive a model, a network, STT, TTS or LiveKit. That is the point:
every assertion below is reproducible to the character, on a laptop with every
credential stripped, in a fraction of FR-013's 120 s. What it costs is stated in
`unreached_requirements()` rather than hidden - a harness that quietly claimed
the rows it cannot reach would be exactly the laundering B5 was built to stop.

THE TRANSLATION LAYER IS L3'S JOB AND IT DOES NOT EXIST YET. L2's inputs are
ten typed clinical findings; the agent layer's `record_finding(kind, detail)` is
free text over a four-value vocabulary that does not overlap them. So a scenario
turn supplies `findings:` as typed values directly. That is not the harness
stubbing L3 - there is nothing to stub. It is the harness driving L2 through its
real signature while the layer that would populate that signature from speech is
unbuilt, which is why the harness is worth having *first*: L2's safety
properties become evidenced now rather than after L3 lands.

THE SIGNATURE FAILURE MODE, NAMED SO IT CANNOT RECUR HERE. This repo has found
the same defect five separate ways - a CI job that passed over an empty
directory, a fixture that only `yield`ed, a rule unreachable through its caller,
a test that *defended* a defect, and a guard that `continue`d past the case it
was commissioned to catch. All one shape: a mechanism whose only implementation
is its own description. A scenario runner is unusually exposed to it, because
the two easiest ways to write one are to pass over zero scenarios and to skip a
turn it cannot interpret. Both are refused here, structurally:

  - `load_scenarios` raises `NoScenariosFound` on an empty or absent directory.
    The runner cannot report success over nothing.
  - An uninterpretable turn - an unknown key, an unknown enum member, an
    expectation naming a field that does not exist - raises `ScenarioError` at
    LOAD time, before any turn runs. There is no `continue`, no `skip`, and no
    `try/except` around a turn's assertions.

ASM-06 IS ENFORCED HERE, AND THIS IS ITS FIRST CALLER. `is_authentic` detects a
forged branch; detection with no caller is a claim rather than a mechanism.
Every branch this harness receives is checked before any expectation is read,
and a forged one fails the scenario loudly with the turn's own text. See
`_check_branch_authentic`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from aiscelapeus.assessment import (
    AgeBand,
    AirwayObstruction,
    AssessmentBranch,
    AssessmentInputs,
    AssessmentStep,
    Breathing,
    Certainty,
    Finding,
    PulseReport,
    Responsiveness,
    SceneSafety,
    SevereBleeding,
    SpinalRisk,
    decide,
    is_authentic,
)
from aiscelapeus.escalation import SOURCE_TRANSCRIPT, apply_hard_escalation
from aiscelapeus.output_gate import GateOutcome
from aiscelapeus.phrases import hard_escalation_triggered
from aiscelapeus.retrieval import Retrieved
from aiscelapeus.triage import Criticality, EscalationStatus, Provenance, TriageState
from aiscelapeus.turn_gate import RetrievedThisTurn, gate_turn

__all__ = [
    "BUDGET_SECONDS",
    "HarnessError",
    "NoScenariosFound",
    "Scenario",
    "ScenarioError",
    "ScenarioResult",
    "SuiteResult",
    "Turn",
    "TurnFailure",
    "TurnResult",
    "load_scenario",
    "load_scenarios",
    "main",
    "run_scenario",
    "run_suite",
    "unreached_requirements",
]


#: FR-013: "Headless suite green in CI in <= 120 s". Asserted by `run_suite`
#: itself rather than by a CI wall-clock, because a budget enforced only by the
#: job that runs the suite is a budget nobody can reproduce locally - and this
#: repo has a sub-10 ms retrieval claim on record that was never executed.
BUDGET_SECONDS = 120.0


class HarnessError(Exception):
    """Base for every way the harness refuses to run.

    Distinct from a scenario *failing*: a failure is evidence the harness
    produced, and a `HarnessError` means it could not produce any. Conflating
    the two is how a runner ends up reporting success over nothing.
    """


class NoScenariosFound(HarnessError):
    """The scenario directory yielded nothing to run.

    THE VACUOUS-CI DEFECT, CLOSED BY CONSTRUCTION. `.github/workflows/ci.yml`
    once declared a `harness` job running this module over `scenarios/`; the job
    was removed rather than stubbed precisely because a harness that exits 0
    over an empty directory launders FR-013's latency claim - green in 0.2 s,
    having asserted nothing. There is no flag to downgrade this to a warning,
    because the only reason to want one is to make an empty run pass.
    """


class ScenarioError(HarnessError):
    """A scenario file the harness cannot interpret.

    Raised at LOAD time, so a malformed scenario stops the suite before any turn
    executes rather than being skipped with a note. The alternative - interpret
    what is understood and pass over the rest - is the shape that let a
    100-test banned-phrase suite go green against a rephrased mutant: the
    mechanism reports on what it managed to check, not on what it was asked to.
    """


# ------------------------------------------------------------------ the format
#
# Everything below is derived from the enums in `assessment.py` and `triage.py`
# rather than listed. DRY here is not about duplicated text: a hand-written
# vocabulary is a second source of truth for what a legal finding is, and it
# would go stale the first time an enum grows a member - silently accepting a
# scenario the domain no longer understands, or rejecting one it does.

#: Input name -> the enum its value is drawn from. Keys are checked against
#: `AssessmentInputs`' own fields at import, below, so a field added to L2
#: without a line here is a startup error rather than a scenario key the loader
#: silently ignores.
_FINDING_ENUMS: Mapping[str, type[Enum]] = {
    "scene_safe": SceneSafety,
    "responsiveness": Responsiveness,
    "breathing": Breathing,
    "severe_bleeding": SevereBleeding,
    "airway_obstruction": AirwayObstruction,
    "spinal_risk": SpinalRisk,
    "age_band": AgeBand,
    "pulse": PulseReport,
}

#: The two boolean inputs. Separate because `Finding[bool]` has no enum to
#: resolve a name against, not because booleans are a special case clinically.
_BOOLEAN_FINDINGS: frozenset[str] = frozenset({"submersion", "exposure_reviewed"})

_FINDING_NAMES: frozenset[str] = frozenset(_FINDING_ENUMS) | _BOOLEAN_FINDINGS

# The check that keeps the mapping honest. An L2 input with no entry above would
# otherwise be un-settable from a scenario *and* silently so, which is the
# "designed slot that is not a slot" trap Blocker 5 of the clinical review
# recorded for `airway_opened`.
_MISSING = set(AssessmentInputs.__dataclass_fields__) - set(_FINDING_NAMES)
if _MISSING:  # pragma: no cover - a startup guard, asserted by a unit test
    raise RuntimeError(
        f"harness cannot express these L2 inputs: {sorted(_MISSING)}. "
        f"Add them to _FINDING_ENUMS or _BOOLEAN_FINDINGS."
    )

#: Certainty name -> member, for a scenario that wants to state a finding is an
#: assumption rather than a report. Defaults to ESTABLISHED: a scenario turn
#: saying "the responder told us he is not breathing" is a report, and making
#: the common case explicit in every entry would bury the ones that are not.
_DEFAULT_CERTAINTY = Certainty.ESTABLISHED

#: Expectation key -> what it asserts. Defined as a table so
#: `_validate_expectation_keys` can reject an unknown key by name, which is what
#: stops a typo ("criticallity: 5") from being an expectation that never runs.
_EXPECTATION_KEYS: frozenset[str] = frozenset(
    {
        "step",
        "criticality",
        "letter",
        "provenance",
        "assumed_inputs",
        "escalated",
        "escalation_status",
        "marker",
        "gate",
        "gate_reasons",
    }
)

_TURN_KEYS: frozenset[str] = frozenset(
    {"responder", "findings", "assumed", "speech", "retrieved", "expect", "why"}
)

_SCENARIO_KEYS: frozenset[str] = frozenset({"id", "title", "traces_to", "turns"})


def _enum_by_value(enum: type[Enum], raw: object, where: str) -> Any:
    """A member of `enum` named by its value, or a `ScenarioError` naming both.

    Raising rather than returning None is the load-time half of "fail rather
    than skip": a scenario that says `breathing: not_breathing` (which is a
    marker id, not a `Breathing` value) must stop the run, not contribute a
    turn whose breathing input silently stayed UNKNOWN.
    """
    for member in enum:
        if member.value == raw:
            return member
    legal = ", ".join(sorted(str(member.value) for member in enum))
    raise ScenarioError(
        f"{where}: {raw!r} is not a {enum.__name__}. Legal values: {legal}"
    )


def _finding(name: str, raw: object, certainty: Certainty, where: str) -> Finding[Any]:
    """One typed clinical finding, from the scenario's name for its value."""
    if name in _BOOLEAN_FINDINGS:
        if not isinstance(raw, bool):
            raise ScenarioError(
                f"{where}: {name} takes true or false, got {raw!r}"
            )
        return Finding(raw, certainty)
    enum = _FINDING_ENUMS[name]
    return Finding(_enum_by_value(enum, raw, f"{where}: {name}"), certainty)


@dataclass(frozen=True)
class Turn:
    """One turn of a scenario: what happened, and what must be true after it.

    Frozen, and parsed once at the boundary into domain types (CLAUDE.md §1):
    by the time `run_scenario` sees a `Turn`, `findings` holds real `Finding`
    objects over real enum members, and an illegal value has already stopped the
    run. Nothing downstream re-reads a string.
    """

    index: int
    #: What the responder said. Fed to L1's deterministic net verbatim. Optional:
    #: a turn may establish findings without new speech (a clinician reporting in
    #: over the bridge, a responder who answered a question non-verbally).
    responder: str | None
    #: Findings that became established this turn. Cumulative across the
    #: scenario - the incident does not forget what it was told - which is why
    #: `run_scenario` carries them forward rather than rebuilding from one turn.
    findings: Mapping[str, Finding[Any]]
    #: What the agent would say this turn, if the scenario exercises the gate.
    speech: str | None
    #: Protocol documents the model was shown this turn. The gate's citation
    #: check is only meaningful against what was actually retrieved, so the
    #: scenario states it rather than the harness inventing a hit.
    retrieved: tuple[Retrieved, ...]
    expect: Mapping[str, Any]
    why: str | None

    @property
    def label(self) -> str:
        """How this turn identifies itself in a failure message.

        Carries the responder's own words, per the B5 brief: a failure that says
        "turn 3" sends the reader back to the YAML to find out what turn 3 was,
        and a clinical reviewer reading a CI log does not have the YAML.
        """
        if self.responder is not None:
            return f"turn {self.index} ({self.responder!r})"
        if self.speech is not None:
            return f"turn {self.index} (agent: {self.speech!r})"
        return f"turn {self.index} (findings only: {sorted(self.findings)})"


@dataclass(frozen=True)
class Scenario:
    """A committed scenario file, parsed.

    THE FILE IS THE SPECIFICATION AND THE HARNESS IS AN IMPLEMENTATION OF IT -
    the framing `tests/data/utterances.yaml` already uses, and the reason
    `expect` is written in clinical vocabulary (`step`, `criticality`,
    `escalated`, `gate`) rather than in the harness's own. A clinician must be
    able to write the oracle without reading this module.
    """

    id: str
    title: str
    #: The review or document this scenario is traceable to. Required, because a
    #: scenario nobody can trace is a scenario nobody can judge wrong: this
    #: repo's scenarios exist to pin defects that cost real review findings.
    traces_to: str
    turns: tuple[Turn, ...]
    path: Path


@dataclass(frozen=True)
class TurnFailure:
    """One expectation that did not hold, with everything needed to act on it."""

    scenario_id: str
    turn_label: str
    expectation: str
    expected: object
    actual: object

    def __str__(self) -> str:
        return (
            f"{self.scenario_id} {self.turn_label}: "
            f"{self.expectation} expected {self.expected!r}, got {self.actual!r}"
        )


@dataclass(frozen=True)
class TurnResult:
    """What one turn actually produced, and which expectations it failed."""

    turn: Turn
    #: Never None: L2 is consulted on every turn, so every turn has a branch
    #: that passed `is_authentic`. See `run_scenario`.
    branch: AssessmentBranch
    level: Criticality
    escalation: EscalationStatus
    marker_id: str | None
    gate_outcome: GateOutcome | None
    failures: tuple[TurnFailure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's turns, in order."""

    scenario: Scenario
    turns: tuple[TurnResult, ...]

    @property
    def failures(self) -> tuple[TurnFailure, ...]:
        return tuple(
            failure for turn in self.turns for failure in turn.failures
        )

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class SuiteResult:
    """The whole run, including the wall time FR-013 is asserted against."""

    scenarios: tuple[ScenarioResult, ...]
    wall_seconds: float
    budget_seconds: float = BUDGET_SECONDS

    @property
    def failures(self) -> tuple[TurnFailure, ...]:
        return tuple(
            failure for scenario in self.scenarios for failure in scenario.failures
        )

    @property
    def turn_count(self) -> int:
        return sum(len(scenario.turns) for scenario in self.scenarios)

    @property
    def within_budget(self) -> bool:
        """FR-013's bound, as a property of the result rather than of the log.

        A budget that lives only in a printed line is a budget a reader has to
        evaluate. `passed` reads this, so a suite that is clinically correct and
        slower than 120 s exits non-zero - which is what "green in <= 120 s or
        it is not green" actually means.
        """
        return self.wall_seconds <= self.budget_seconds

    @property
    def passed(self) -> bool:
        return not self.failures and self.within_budget


# ------------------------------------------------------------------- the loader


def _require_mapping(raw: object, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ScenarioError(f"{where}: expected a mapping, got {type(raw).__name__}")
    return raw


def _reject_unknown_keys(
    raw: Mapping[str, Any], legal: frozenset[str], where: str
) -> None:
    """Refuse a key the harness does not act on.

    THE LESSON FROM THE `_hazard_suffix` DEFECT, APPLIED TO DATA. A test that
    called a helper directly hid that the step a fire scene actually reached
    never used it - 98 tests passing over dead clinical guidance. An unknown
    `expect` key is the same defect in the scenario layer: the author believes
    they asserted something, the harness never reads it, and the scenario is
    green over an expectation that does not exist.
    """
    unknown = sorted(set(raw) - legal)
    if unknown:
        raise ScenarioError(
            f"{where}: unknown key(s) {unknown}. Legal keys: {sorted(legal)}"
        )


def _parse_findings(
    raw: object, assumed: object, where: str
) -> Mapping[str, Finding[Any]]:
    """The turn's typed findings, with certainty applied.

    `assumed:` names findings this turn took as ASSUMED_WORST rather than being
    told. It is a separate list rather than a per-finding certainty because that
    is how the clinical vocabulary reads - "we assumed the scene was unsafe"
    describes the finding, not its value - and because ASM-14's correctability
    turns on exactly this distinction, so a scenario must be able to state it.
    """
    findings = _require_mapping(raw if raw is not None else {}, f"{where}: findings")
    assumed_names = _parse_str_sequence(assumed, f"{where}: assumed")

    unknown = sorted(set(findings) - _FINDING_NAMES)
    if unknown:
        raise ScenarioError(
            f"{where}: unknown finding(s) {unknown}. "
            f"Legal findings: {sorted(_FINDING_NAMES)}"
        )
    stray = sorted(set(assumed_names) - set(findings))
    if stray:
        raise ScenarioError(
            f"{where}: assumed names {stray} which this turn does not establish. "
            f"An assumption is still a value - state it under `findings`."
        )

    return {
        name: _finding(
            name,
            value,
            Certainty.ASSUMED_WORST if name in assumed_names else _DEFAULT_CERTAINTY,
            where,
        )
        for name, value in findings.items()
    }


def _parse_str_sequence(raw: object, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise ScenarioError(
            f"{where}: expected a list, got the string {raw!r}. "
            f"A bare string would silently become a list of its characters."
        )
    if not isinstance(raw, Sequence):
        raise ScenarioError(f"{where}: expected a list, got {type(raw).__name__}")
    for item in raw:
        if not isinstance(item, str):
            raise ScenarioError(f"{where}: expected strings, got {item!r}")
    return tuple(raw)


def _parse_retrieved(raw: object, where: str) -> tuple[Retrieved, ...]:
    """Protocol hits the model was shown, as the gate's own `Retrieved` type."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise ScenarioError(f"{where}: retrieved must be a list of documents")
    hits: list[Retrieved] = []
    for position, item in enumerate(raw):
        entry = _require_mapping(item, f"{where}: retrieved[{position}]")
        _reject_unknown_keys(
            entry, frozenset({"id", "text"}), f"{where}: retrieved[{position}]"
        )
        for required in ("id", "text"):
            if required not in entry:
                raise ScenarioError(
                    f"{where}: retrieved[{position}] needs {required!r}"
                )
        hits.append(
            Retrieved(
                id=str(entry["id"]),
                text=str(entry["text"]),
                # Not scenario-settable: the gate's citation check reads the
                # text, never the score, so a score in the file would be a knob
                # with no effect - the "designed slot that is not a slot" trap.
                score=1.0,
                metadata={},
            )
        )
    return tuple(hits)


def _validate_expectation(raw: object, where: str) -> Mapping[str, Any]:
    """The turn's oracle, with every key checked against what the harness reads."""
    expect = _require_mapping(raw if raw is not None else {}, f"{where}: expect")
    _reject_unknown_keys(expect, _EXPECTATION_KEYS, f"{where}: expect")

    # Every value that names a domain member is resolved HERE rather than at
    # assertion time. A scenario expecting `step: instruct_cpr_now` must fail to
    # load, not compare unequal against a correct branch and read as a caught
    # defect - a false failure is as corrosive to a suite's authority as a false
    # pass, and harder to notice because somebody investigates it.
    if "step" in expect:
        _enum_by_value(AssessmentStep, expect["step"], f"{where}: expect.step")
    if "criticality" in expect:
        level = expect["criticality"]
        if not isinstance(level, int) or isinstance(level, bool):
            raise ScenarioError(
                f"{where}: expect.criticality must be an integer 1-5, got {level!r}"
            )
        if level not in {int(member) for member in Criticality}:
            raise ScenarioError(
                f"{where}: expect.criticality {level} is off the 1-5 scale. "
                f"A level outside the scale is a malfunction to surface, not a "
                f"value to clamp (CLAUDE.md)."
            )
    if "provenance" in expect:
        _enum_by_value(Provenance, expect["provenance"], f"{where}: expect.provenance")
    if "escalation_status" in expect:
        _enum_by_value(
            EscalationStatus,
            expect["escalation_status"],
            f"{where}: expect.escalation_status",
        )
    if "gate" in expect:
        _enum_by_value(GateOutcome, expect["gate"], f"{where}: expect.gate")
    if "escalated" in expect and not isinstance(expect["escalated"], bool):
        raise ScenarioError(
            f"{where}: expect.escalated must be true or false, "
            f"got {expect['escalated']!r}"
        )
    if "assumed_inputs" in expect:
        names = _parse_str_sequence(
            expect["assumed_inputs"], f"{where}: expect.assumed_inputs"
        )
        unknown = sorted(set(names) - _FINDING_NAMES)
        if unknown:
            raise ScenarioError(
                f"{where}: expect.assumed_inputs names non-inputs {unknown}"
            )
    if "gate_reasons" in expect:
        _parse_str_sequence(expect["gate_reasons"], f"{where}: expect.gate_reasons")
    if "marker" in expect and expect["marker"] is not None:
        if not isinstance(expect["marker"], str):
            raise ScenarioError(
                f"{where}: expect.marker must be a marker id or null, "
                f"got {expect['marker']!r}"
            )
    return expect


def _parse_turn(raw: object, index: int, where: str) -> Turn:
    entry = _require_mapping(raw, f"{where}: turns[{index}]")
    location = f"{where}: turns[{index}]"
    _reject_unknown_keys(entry, _TURN_KEYS, location)

    responder = entry.get("responder")
    if responder is not None and not isinstance(responder, str):
        raise ScenarioError(f"{location}: responder must be a string")
    speech = entry.get("speech")
    if speech is not None and not isinstance(speech, str):
        raise ScenarioError(f"{location}: speech must be a string")
    why = entry.get("why")
    if why is not None and not isinstance(why, str):
        raise ScenarioError(f"{location}: why must be a string")

    findings = _parse_findings(entry.get("findings"), entry.get("assumed"), location)
    expect = _validate_expectation(entry.get("expect"), location)

    if not expect:
        raise ScenarioError(
            f"{location}: a turn with no `expect` asserts nothing. "
            f"A turn that only sets up state belongs merged into the turn that "
            f"asserts against it."
        )
    if responder is None and speech is None and not findings:
        raise ScenarioError(
            f"{location}: a turn must supply `responder`, `speech` or `findings`. "
            f"There is nothing for this turn to have done."
        )
    if "gate" in expect and speech is None:
        raise ScenarioError(
            f"{location}: expect.gate needs `speech` - the gate judges what the "
            f"agent would say, and there is nothing here for it to judge."
        )

    return Turn(
        index=index + 1,
        responder=responder,
        findings=findings,
        speech=speech,
        retrieved=_parse_retrieved(entry.get("retrieved"), location),
        expect=expect,
        why=why,
    )


def load_scenario(path: Path) -> Scenario:
    """One scenario file, fully validated.

    Every structural problem raises `ScenarioError` naming the file and the
    turn. Nothing is tolerated and nothing is defaulted into silence.
    """
    try:
        import yaml
    except ModuleNotFoundError as error:  # pragma: no cover - environment guard
        raise HarnessError(
            "the harness needs PyYAML to read scenario files "
            '(pip install -e ".[dev]")'
        ) from error

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ScenarioError(f"{path.name}: not valid YAML: {error}") from error

    document = _require_mapping(raw, path.name)
    _reject_unknown_keys(document, _SCENARIO_KEYS, path.name)
    for required in ("id", "title", "traces_to", "turns"):
        if required not in document:
            raise ScenarioError(f"{path.name}: missing required key {required!r}")

    turns_raw = document["turns"]
    if not isinstance(turns_raw, Sequence) or isinstance(turns_raw, str):
        raise ScenarioError(f"{path.name}: turns must be a list")
    if not turns_raw:
        raise ScenarioError(
            f"{path.name}: a scenario with no turns asserts nothing. "
            f"This is the empty-directory defect one level down."
        )

    return Scenario(
        id=str(document["id"]),
        title=str(document["title"]),
        traces_to=str(document["traces_to"]),
        turns=tuple(
            _parse_turn(entry, index, path.name)
            for index, entry in enumerate(turns_raw)
        ),
        path=path,
    )


def load_scenarios(directory: Path) -> tuple[Scenario, ...]:
    """Every scenario in `directory`, sorted by filename.

    Raises `NoScenariosFound` when there are none. See that class: this is the
    guard against the defect the removed CI job would have had.
    """
    if not directory.is_dir():
        raise NoScenariosFound(
            f"{directory} is not a directory, so there are no scenarios to run"
        )
    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    if not paths:
        raise NoScenariosFound(
            f"no *.yaml scenarios under {directory}. A harness that reports "
            f"success over zero scenarios is not evidence of anything."
        )
    scenarios = tuple(load_scenario(path) for path in sorted(set(paths)))

    duplicates = sorted(
        {
            scenario.id
            for scenario in scenarios
            if sum(other.id == scenario.id for other in scenarios) > 1
        }
    )
    if duplicates:
        raise ScenarioError(
            f"duplicate scenario id(s) {duplicates} under {directory}: a failure "
            f"message naming an id must identify one file."
        )
    return scenarios


# ------------------------------------------------------------------- the runner


def _check_branch_authentic(branch: object, scenario: Scenario, turn: Turn) -> AssessmentBranch:
    """ASM-06, enforced. The harness is `is_authentic`'s first caller.

    `is_authentic` returns False rather than raising, so that a production
    consumer's correct response - re-ask L2 - does not require a try/except
    around its own safety check. A harness has no "re-ask" to fall back on: its
    job is to say loudly that the value it was handed is not one L2 computed. So
    it raises, and it raises with the turn's own text, because a forged branch
    reaching a clinical decision is not a scenario failure to be tallied
    alongside a wrong criticality - it means the thing under test was not the
    thing under test.
    """
    if not is_authentic(branch):
        raise HarnessError(
            f"{scenario.id} {turn.label}: L2 returned a branch that "
            f"assessment.is_authentic rejects ({branch!r}). ASM-06: a branch "
            f"not minted by decide() must never reach a clinical decision."
        )
    assert isinstance(branch, AssessmentBranch)  # narrowed by is_authentic
    return branch


def _record(
    failures: list[TurnFailure],
    scenario: Scenario,
    turn: Turn,
    expectation: str,
    expected: object,
    actual: object,
) -> None:
    if expected != actual:
        failures.append(
            TurnFailure(
                scenario_id=scenario.id,
                turn_label=turn.label,
                expectation=expectation,
                expected=expected,
                actual=actual,
            )
        )


async def _noop() -> None:
    """The broadcast callback `escalation.apply_hard_escalation` injects.

    Nothing to observe: the state change the harness asserts against is read
    from `TriageState` directly, and a UI broadcast has no headless meaning.
    """
    return None


def _dispatch_to(state: TriageState) -> Callable[[str, str], Any]:
    """The `on_escalate` callback, keyed the way production keys it.

    `agent.py:_do_escalate` builds `f"{session_id}:{category}"` and `category`
    is the marker id, which is what makes the dedup real: the transcript edge
    and a later `record_finding` paraphrase of the same utterance compute the
    same key, so `request_escalation` recognises it and the on-call clinician is
    not paged twice. Reproducing the key format here rather than inventing one
    is the difference between the harness exercising that dedup and the harness
    having its own, differently-keyed escalation that would never collide.

    This is the closest the headless runner gets to a dispatch transport, and
    the gap is named in `unreached_requirements`: FR-008's bridge is not
    asserted here, only that the incident recorded a request for one.
    """

    async def _on_escalate(reason: str, category: str) -> None:
        state.request_escalation(reason, key=f"{state.session_id}:{category}")

    return _on_escalate


def run_scenario(
    scenario: Scenario,
    *,
    decide_fn: Callable[[AssessmentInputs], AssessmentBranch] = decide,
) -> ScenarioResult:
    """Execute one scenario, turn by turn, against the real stack.

    `decide_fn` is injectable for exactly one purpose: a unit test proving that
    a FORGED branch fails the scenario loudly. ASM-06 is detection, not
    prevention - Python affords no unforgeable value - so the only way to prove
    the detection runs is to hand the harness a forgery, which requires a seam.
    Production and the CLI both use the default.

    STATE IS CUMULATIVE ACROSS TURNS, and that is the property that makes a
    scenario a scenario rather than a list of independent assertions. One
    `TriageState` carries the ratchet and the escalation lifecycle from turn to
    turn; one `AssessmentInputs` accretes findings. A deteriorating patient's
    re-entry at A is only expressible because turn 3 still knows what turn 1
    established.
    """
    state = TriageState(session_id=f"harness:{scenario.id}")
    inputs = AssessmentInputs()
    results: list[TurnResult] = []

    for turn in scenario.turns:
        failures: list[TurnFailure] = []

        # ---- L1: the deterministic escalation net, over the literal words.
        marker_id: str | None = None
        if turn.responder is not None:
            hit = hard_escalation_triggered(turn.responder)
            marker_id = None if hit is None else hit.marker_id
            if hit is not None:
                # Through the real entry point, not by calling `set_level`
                # ourselves. `apply_hard_escalation` is what production runs,
                # and it is the only thing that gets the provenance argument
                # right (EVIDENCE, so L2 arithmetic cannot stand down a
                # reported arrest) - a harness that reimplemented the two lines
                # would be asserting its own copy of the rule.
                asyncio.run(
                    apply_hard_escalation(
                        turn.responder,
                        state=state,
                        on_state_change=_noop,
                        on_escalate=_dispatch_to(state),
                        source=SOURCE_TRANSCRIPT,
                    )
                )

        # ---- L2: the ABCDE state machine, over the accreted typed findings.
        #
        # UNCONDITIONALLY, ON EVERY TURN, and that is deliberate rather than
        # incidental. An earlier version called `decide` only when the turn
        # supplied findings or named an L2 expectation - which is the SKIP shape
        # this project keeps finding, one level up from the turn loop. Three
        # things went wrong with it and all three are closed by removing the
        # condition:
        #
        #   - `is_authentic` did not run on such a turn, so ASM-06 was enforced
        #     on most turns rather than on every branch the harness receives;
        #   - a turn expecting only `criticality` read `TriageState` without L2
        #     having been consulted at all, so the number came from history
        #     rather than from the stack under test;
        #   - `branch` was None, so `step`/`letter`/`provenance` silently
        #     compared against None instead of failing to load - an expectation
        #     that cannot be satisfied is a load error, not a quiet mismatch.
        #
        # `decide` is pure and costs microseconds, so there is no reason to
        # guess at when it is needed. Re-deciding over unchanged inputs returns
        # an equal branch by ASM-01, which is what makes this safe as well as
        # simpler.
        inputs = _with_findings(inputs, turn.findings)
        branch = _check_branch_authentic(decide_fn(inputs), scenario, turn)
        # The ratchet, through its real entry point. L2's branch carries its own
        # provenance, which is what lets a clinician's later evidence correct an
        # assumption-driven level and nothing else lower it.
        state.set_level(
            int(branch.criticality),
            branch.rationale,
            source="assessment",
            provenance=branch.provenance,
        )

        # ---- B4: the output gate, over what the agent would say.
        gate_outcome: GateOutcome | None = None
        gate_reasons: tuple[str, ...] = ()
        if turn.speech is not None:
            retrieved = RetrievedThisTurn()
            retrieved.record(turn.retrieved)
            _, decision = gate_turn(turn.speech, retrieved)
            gate_outcome = decision.outcome
            gate_reasons = tuple(sorted(reason.value for reason in decision.reasons))

        # ---- the oracle.
        # Every expectation below reads a value that definitely exists: `branch`
        # is never None (L2 runs on every turn) and `state` always holds a
        # level. There is no `if ... is None` fallback anywhere in this block,
        # because a comparison against None is how an expectation ends up
        # neither passing nor failing.
        expect = turn.expect
        if "step" in expect:
            _record(
                failures, scenario, turn, "step", expect["step"], branch.step.value
            )
        if "letter" in expect:
            _record(
                failures, scenario, turn, "letter", expect["letter"], branch.letter.value
            )
        if "provenance" in expect:
            _record(
                failures, scenario, turn, "provenance",
                expect["provenance"], branch.provenance.value,
            )
        if "assumed_inputs" in expect:
            _record(
                failures, scenario, turn, "assumed_inputs",
                tuple(sorted(_parse_str_sequence(expect["assumed_inputs"], "expect"))),
                tuple(sorted(branch.assumed_inputs)),
            )
        if "criticality" in expect:
            # Read from TriageState, not from the branch. The scenario's claim is
            # about the incident's level after this turn, and that is the
            # ratchet's answer - which can be higher than this branch's, because
            # L1 or an earlier turn put it there. Asserting the branch's own
            # number would make every ratchet property invisible to the harness.
            _record(
                failures, scenario, turn, "criticality",
                expect["criticality"], int(state.level),
            )
        if "escalated" in expect:
            _record(
                failures, scenario, turn, "escalated",
                expect["escalated"], state.escalated,
            )
        if "escalation_status" in expect:
            _record(
                failures, scenario, turn, "escalation_status",
                expect["escalation_status"], state.escalation.value,
            )
        if "marker" in expect:
            _record(failures, scenario, turn, "marker", expect["marker"], marker_id)
        if "gate" in expect:
            _record(
                failures, scenario, turn, "gate",
                expect["gate"], None if gate_outcome is None else gate_outcome.value,
            )
        if "gate_reasons" in expect:
            _record(
                failures, scenario, turn, "gate_reasons",
                tuple(sorted(_parse_str_sequence(expect["gate_reasons"], "expect"))),
                gate_reasons,
            )

        results.append(
            TurnResult(
                turn=turn,
                branch=branch,
                level=state.level,
                escalation=state.escalation,
                marker_id=marker_id,
                gate_outcome=gate_outcome,
                failures=tuple(failures),
            )
        )

    return ScenarioResult(scenario=scenario, turns=tuple(results))


def _with_findings(
    inputs: AssessmentInputs, findings: Mapping[str, Finding[Any]]
) -> AssessmentInputs:
    """A NEW inputs object carrying `findings` on top of what was already known.

    Replaces rather than mutates, because `AssessmentInputs` is frozen for a
    stated reason (ASM-01): a branch computed from an earlier set must not be
    retrospectively invalidated by a later write. The scenario's accretion is
    therefore a sequence of distinct immutable snapshots, and turn 3's branch
    stays the branch turn 3's inputs produce forever.
    """
    if not findings:
        return inputs
    return replace(inputs, **dict(findings))


def run_suite(
    directory: Path,
    *,
    budget_seconds: float = BUDGET_SECONDS,
) -> SuiteResult:
    """Load and run every scenario under `directory`, timing the whole thing.

    The wall time is measured with `perf_counter` around load AND run, because
    FR-013's claim is about the suite a CI job executes, and a loader slow
    enough to matter is as much a budget breach as a slow runner.
    """
    started = time.perf_counter()
    scenarios = load_scenarios(directory)
    results = tuple(run_scenario(scenario) for scenario in scenarios)
    elapsed = time.perf_counter() - started
    return SuiteResult(
        scenarios=results, wall_seconds=elapsed, budget_seconds=budget_seconds
    )


# ------------------------------------------------------------------- reporting


def unreached_requirements() -> Mapping[str, str]:
    """DESIGN.md §8 rows whose `Ev` column says `harness` and that this cannot reach.

    STATED IN CODE RATHER THAN IN A REPORT, and that is the evidence-discipline
    rule from CLAUDE.md turned on the harness itself. §8 marks five rows BUILT on
    evidence that named this file while this file did not exist. The failure mode
    is not fixed by the file existing - it is fixed by the file being explicit
    about which rows it does and does not carry. Printed by `--headless` on every
    run, so a reader of a green log cannot mistake its scope.
    """
    return {
        "FR-001": "full-duplex interruption needs real media; no audio path here",
        "FR-002": "STT WER needs an audio corpus and a live STT engine",
        "FR-003": "TTS first-byte latency needs a live TTS connector",
        "FR-004": "recall_state round-trip needs the agent layer and its tools",
        "FR-008": "clinician bridge needs LiveKit rooms and a dispatch transport",
        "FR-011": "SOAP generation needs an LLM call",
        "NFR-001": "Moss p100 needs real Moss; behind the `infra` marker",
        "NFR-002": "end-to-end voice round trip needs the full media path",
        "NFR-003": "exporter overhead needs a turn pipeline to measure against",
        "OBS-002": "span-tree completeness needs the agent's per-turn spans",
    }


def _write_report(result: SuiteResult, stream: TextIO) -> None:
    """Per-turn pass/fail, then the budget, then the scope."""
    for scenario in result.scenarios:
        stream.write(f"\n{scenario.scenario.id} - {scenario.scenario.title}\n")
        stream.write(f"  traces to: {scenario.scenario.traces_to}\n")
        for turn in scenario.turns:
            mark = "PASS" if turn.passed else "FAIL"
            stream.write(f"  [{mark}] {turn.turn.label}\n")
            for failure in turn.failures:
                stream.write(
                    f"         {failure.expectation}: "
                    f"expected {failure.expected!r}, got {failure.actual!r}\n"
                )

    failures = result.failures
    stream.write(
        f"\n{len(result.scenarios)} scenario(s), {result.turn_count} turn(s), "
        f"{len(failures)} failed expectation(s)\n"
    )
    stream.write(
        f"wall time {result.wall_seconds:.3f}s against FR-013's "
        f"{result.budget_seconds:.0f}s budget: "
        f"{'WITHIN' if result.within_budget else 'BREACHED'}\n"
    )
    # ASCII only. A Windows console defaults to a code page that cannot encode
    # the section sign, and a report that raises UnicodeEncodeError while
    # printing its own scope is a runner that fails for a reason with no
    # clinical content.
    stream.write(
        "\nNOT asserted by this runner (DESIGN.md section 8 rows whose evidence\n"
        "is the harness and which need infrastructure this run does not touch):\n"
    )
    for row, why in sorted(unreached_requirements().items()):
        stream.write(f"  {row}: {why}\n")


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    """`python -m aiscelapeus.harness --headless <dir>`.

    Exits non-zero on any failed expectation, on a budget breach, and on any
    `HarnessError` - including the empty directory. There is no argument that
    makes an empty run succeed.
    """
    out = stream if stream is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="python -m aiscelapeus.harness",
        description="Run the headless scenario suite (B5). No model, no network.",
    )
    parser.add_argument(
        "--headless",
        metavar="DIR",
        required=True,
        type=Path,
        help="directory of *.yaml scenario files",
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=BUDGET_SECONDS,
        help=f"FR-013 wall-clock bound (default {BUDGET_SECONDS:.0f})",
    )
    args = parser.parse_args(argv)

    try:
        result = run_suite(args.headless, budget_seconds=args.budget_seconds)
    except HarnessError as error:
        out.write(f"harness refused to run: {error}\n")
        return 2

    _write_report(result, out)
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
