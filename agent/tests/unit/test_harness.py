"""B5 - the headless scenario harness, and the guarantees it makes about itself.

Two kinds of test live here and the distinction matters.

The first kind asserts the RUNNER's properties: that it refuses an empty
directory, that it fails rather than skips an uninterpretable turn, that it
enforces FR-013's budget, that it calls `is_authentic` on every branch. These
are the tests that stop the harness becoming this project's signature defect - a
mechanism whose only implementation is its own description. It has been found
five ways here (a vacuous CI job, a fixture that only `yield`ed, a rule
unreachable through its caller, a test that DEFENDED a defect, a guard that
`continue`d past the failure case), and a scenario runner is unusually exposed
because the two easiest ways to write one are to pass over zero scenarios and to
skip what it cannot parse.

The second kind asserts that the COMMITTED scenarios actually run and pass. That
is not a duplicate of the first: a runner with perfect guarantees over zero real
scenarios is worth nothing, which is the whole reason the removed CI job was
removed rather than stubbed.

Everything here goes through the real entry points - `main`, `run_suite`,
`load_scenarios` - never through a helper. `test_assessment.py`'s
`_hazard_suffix` lesson is on record: a test that called the helper directly hid
that the step a fire scene actually reached never used it, and 98 tests passed
over dead clinical guidance.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pytest

from aiscelapeus.assessment import (
    AssessmentBranch,
    AssessmentInputs,
    AssessmentStep,
    Breathing,
    Certainty,
    Criticality,
    Finding,
    Letter,
    Responsiveness,
    SceneSafety,
    SevereBleeding,
    decide,
    is_authentic,
)
from aiscelapeus.harness import (
    BUDGET_SECONDS,
    HarnessError,
    NoScenariosFound,
    ScenarioError,
    load_scenario,
    load_scenarios,
    main,
    run_scenario,
    run_suite,
    unreached_requirements,
)
from aiscelapeus.triage import EscalationStatus, Provenance

#: The committed suite. A path rather than a fixture, because the point of
#: several tests below is that these files exist on disk and are found.
SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"

#: The smallest scenario that loads and passes. Written inline rather than read
#: from `scenarios/` so a test about the LOADER cannot fail because a clinical
#: oracle changed, and so each malformed variant below differs from a working
#: baseline by exactly the one thing it is testing.
MINIMAL = """
id: SCN-T01-minimal
title: A minimal well patient
traces_to: tests/unit/test_harness.py
turns:
  - responder: "He's up and talking, says he's fine"
    findings:
      scene_safe: safe
      responsiveness: alert
      breathing: normal
      severe_bleeding: none
      airway_obstruction: none
      spinal_risk: none
      age_band: adult
      submersion: false
      exposure_reviewed: true
    expect:
      step: instruct_monitor
      criticality: 2
      marker: null
"""


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------- the committed suite runs


def test_the_committed_scenario_suite_exists_and_is_not_empty() -> None:
    # falsifier: the scenario directory is emptied, renamed or never committed,
    # and every other test in this file passes over a suite that asserts nothing
    # about the clinical stack - which is precisely the vacuous-CI defect the
    # removed `harness` job would have had, reproduced in the test suite.
    scenarios = load_scenarios(SCENARIO_DIR)
    assert len(scenarios) >= 6, (
        f"expected the six review-traceable scenario families, "
        f"found {[scenario.id for scenario in scenarios]}"
    )
    assert sum(len(scenario.turns) for scenario in scenarios) >= 20


def test_the_committed_suite_passes_within_the_fr013_budget() -> None:
    # falsifier: a clinical regression in L1, L2, the ratchet or the output gate
    # lands and nothing fails, because the only thing running the scenarios is a
    # CLI nobody invokes. This is the assertion that makes `harness` a legitimate
    # entry in DESIGN.md section 8's evidence column rather than a filename.
    result = run_suite(SCENARIO_DIR)
    assert result.failures == (), "\n".join(
        str(failure) for failure in result.failures
    )
    assert result.within_budget, (
        f"FR-013: headless suite took {result.wall_seconds:.3f}s, "
        f"budget {result.budget_seconds}s"
    )
    assert result.passed


def test_every_committed_scenario_traces_to_a_review() -> None:
    # falsifier: a scenario is added that nobody can trace to a finding, so when
    # it fails there is no way to tell whether the code regressed or the oracle
    # was always wrong - and an untraceable oracle tends to be "fixed" by
    # editing the expectation.
    for scenario in load_scenarios(SCENARIO_DIR):
        assert scenario.traces_to.strip(), f"{scenario.id} traces to nothing"
        assert len(scenario.traces_to) >= 30, (
            f"{scenario.id}: traces_to {scenario.traces_to!r} is a label, "
            f"not a citation"
        )


def test_every_committed_turn_explains_itself() -> None:
    # falsifier: a turn is added with an expectation and no reasoning, so a
    # later author who breaks it cannot tell whether the expectation encodes a
    # clinical rule or an incidental fact about today's implementation. This is
    # the `# falsifier:` convention applied to the scenario layer, where the
    # oracle is data rather than code.
    for scenario in load_scenarios(SCENARIO_DIR):
        for turn in scenario.turns:
            assert turn.why is not None and len(turn.why) >= 40, (
                f"{scenario.id} {turn.label} has no `why`. State what breaks in "
                f"the real world if this expectation stops holding."
            )


# --------------------------------------- the runner refuses to pass over nothing


def test_an_empty_directory_is_refused_rather_than_reported_green(
    tmp_path: Path,
) -> None:
    # falsifier: THE DEFECT THE REMOVED CI JOB WOULD HAVE HAD. A harness that
    # exits 0 over an empty directory launders FR-013's "<= 120 s" claim - green
    # in 0.2 s, having asserted nothing about any clinical path - and the job was
    # deleted rather than stubbed for exactly this reason. If this stops raising,
    # a mis-set path in CI reports a passing suite forever.
    with pytest.raises(NoScenariosFound):
        load_scenarios(tmp_path)


def test_a_missing_directory_is_refused_rather_than_reported_green(
    tmp_path: Path,
) -> None:
    # falsifier: a renamed or mistyped scenario directory reads as "no failures"
    # instead of as an error, which is the same laundering as the empty
    # directory and the more likely typo in a CI file.
    with pytest.raises(NoScenariosFound):
        load_scenarios(tmp_path / "does-not-exist")


def test_the_cli_exits_non_zero_on_an_empty_directory(tmp_path: Path) -> None:
    # falsifier: `load_scenarios` raises but the CLI swallows it and returns 0,
    # so the guard above is real and the thing CI actually runs is not. Asserted
    # through `main` rather than through the loader for that reason - the exit
    # code is the only part of this a CI job can see.
    stream = io.StringIO()
    code = main(["--headless", str(tmp_path)], stream=stream)
    assert code != 0, f"expected a non-zero exit, got {code}"
    assert "refused to run" in stream.getvalue()


def test_a_scenario_with_no_turns_is_refused(tmp_path: Path) -> None:
    # falsifier: the empty-directory guard is satisfied by a directory of empty
    # scenario FILES - the same vacuity one level down, and harder to spot
    # because the run reports "3 scenarios" over zero assertions.
    _write(tmp_path, "empty.yaml", "id: X\ntitle: T\ntraces_to: nowhere\nturns: []\n")
    with pytest.raises(ScenarioError, match="asserts nothing"):
        load_scenarios(tmp_path)


def test_a_turn_with_no_expectation_is_refused(tmp_path: Path) -> None:
    # falsifier: a turn that sets up state and asserts nothing counts towards
    # the turn total, so a suite's reported size stops being a measure of what
    # it checks. A setup-only turn belongs merged into the turn that asserts.
    _write(
        tmp_path,
        "no-expect.yaml",
        "id: X\ntitle: T\ntraces_to: nowhere\n"
        "turns:\n  - responder: \"something happened\"\n",
    )
    with pytest.raises(ScenarioError, match="asserts nothing"):
        load_scenarios(tmp_path)


# ------------------------------------- the runner fails rather than skips a turn


@pytest.mark.parametrize(
    ("mutation", "body", "expected_message"),
    [
        (
            "an unknown expect key",
            MINIMAL.replace("criticality: 2", "criticallity: 2"),
            "unknown key",
        ),
        (
            "an unknown finding name",
            MINIMAL.replace("breathing: normal", "breathin: normal"),
            "unknown finding",
        ),
        (
            "a value that is not a member of its enum",
            MINIMAL.replace("breathing: normal", "breathing: not_breathing"),
            "is not a Breathing",
        ),
        (
            "a step that does not exist",
            MINIMAL.replace("step: instruct_monitor", "step: instruct_monitor_now"),
            "is not a AssessmentStep",
        ),
        (
            "a criticality off the 1-5 scale",
            MINIMAL.replace("criticality: 2", "criticality: 7"),
            "off the 1-5 scale",
        ),
        (
            "an unknown top-level scenario key",
            MINIMAL.replace("traces_to:", "traces:"),
            "unknown key",
        ),
        (
            "assumed naming a finding the turn does not establish",
            MINIMAL.replace("    expect:", "    assumed: [pulse]\n    expect:"),
            "does not establish",
        ),
        (
            "a gate expectation with nothing to gate",
            MINIMAL.replace("      marker: null", "      gate: approved"),
            "needs `speech`",
        ),
    ],
)
def test_an_uninterpretable_turn_fails_the_run_rather_than_being_skipped(
    tmp_path: Path, mutation: str, body: str, expected_message: str
) -> None:
    # falsifier: THE SIGNATURE DEFECT, IN THE FORM A SCENARIO RUNNER INVITES. A
    # runner that interprets what it understands and passes over the rest
    # reports on what it managed to check rather than on what it was asked to,
    # so an author who mistypes `criticallity: 5` believes they pinned a
    # criticality and pinned nothing. Every mutation here is a plausible typo,
    # and every one must stop the run by name.
    _write(tmp_path, "mutant.yaml", body)
    with pytest.raises(ScenarioError, match=expected_message):
        load_scenarios(tmp_path)


def test_the_uninterpretable_turn_guard_is_not_vacuous(tmp_path: Path) -> None:
    # falsifier: every mutation above raises for some reason unrelated to the
    # mutation - a stray indent, a missing key - so the parametrised test would
    # pass even if the loader rejected the UNMUTATED baseline too. This is the
    # control: MINIMAL itself must load and pass, or the eight mutations prove
    # nothing about what the loader discriminates.
    _write(tmp_path, "baseline.yaml", MINIMAL)
    result = run_suite(tmp_path)
    assert result.failures == (), "\n".join(str(f) for f in result.failures)
    assert result.turn_count == 1


def test_a_malformed_turn_is_refused_before_any_turn_executes(tmp_path: Path) -> None:
    # falsifier: a bad scenario is detected lazily, mid-run, so turns from
    # earlier scenarios have already been reported PASS when the run dies. A
    # partial report is worse than no report: it looks like evidence.
    _write(tmp_path, "a-good.yaml", MINIMAL)
    _write(
        tmp_path,
        "b-bad.yaml",
        MINIMAL.replace("id: SCN-T01-minimal", "id: SCN-T02-bad").replace(
            "step: instruct_monitor", "step: nonsense"
        ),
    )
    stream = io.StringIO()
    code = main(["--headless", str(tmp_path)], stream=stream)
    assert code != 0
    # The good scenario's turn must NOT have been reported as passing.
    assert "[PASS]" not in stream.getvalue(), (
        f"a turn was reported before the suite was known to be loadable:\n"
        f"{stream.getvalue()}"
    )


def test_a_duplicate_scenario_id_is_refused(tmp_path: Path) -> None:
    # falsifier: two files share an id, so a failure message naming that id
    # points at two places and the reader investigates the wrong one.
    _write(tmp_path, "one.yaml", MINIMAL)
    _write(tmp_path, "two.yaml", MINIMAL)
    with pytest.raises(ScenarioError, match="duplicate scenario id"):
        load_scenarios(tmp_path)


def test_a_string_where_a_list_belongs_is_refused(tmp_path: Path) -> None:
    # falsifier: `assumed: breathing` is silently read as the list
    # ['b','r','e','a','t','h','i','n','g'], so the author's intended assumption
    # is not recorded and nine non-existent input names are. Python's string
    # iteration makes this the single most likely YAML-to-domain mistake.
    _write(
        tmp_path,
        "stringy.yaml",
        MINIMAL.replace("    expect:", "    assumed: breathing\n    expect:"),
    )
    with pytest.raises(ScenarioError, match="expected a list"):
        load_scenarios(tmp_path)


# ------------------------------------------------ ASM-06: the forged branch


class _ForgedBranch:
    """An object convincing in every field that `decide` never returned.

    Constructed without `AssessmentBranch` at all, because that class refuses
    subclassing and refuses construction without the witness - both of which are
    the mechanisms `is_authentic` exists to back up rather than replace. ASM-06
    is DETECTION, not prevention: Python affords no unforgeable value, which the
    module says in as many words and which this class demonstrates.
    """

    step = AssessmentStep.INSTRUCT_MONITOR
    letter = Letter.E
    criticality = Criticality.LOW
    rationale = "Nothing to worry about here."
    unresolved_letter = Letter.E
    assumed_inputs: tuple[str, ...] = ()
    provenance = Provenance.EVIDENCE
    is_instruction = True


def test_the_harness_rejects_a_forged_branch_loudly(tmp_path: Path) -> None:
    # falsifier: ASM-06 IS HALF-ENFORCED UNTIL A CALLER CHECKS, and the harness
    # is the first caller. Without this, a compromised or buggy L3 could hand a
    # hand-built "everything is fine, criticality 2" object to the clinical
    # pipeline and every consumer checking the type would be satisfied while the
    # witness never ran. The forgery below is exactly that object.
    _write(tmp_path, "forged.yaml", MINIMAL)
    scenario = load_scenarios(tmp_path)[0]

    def _forge(inputs: AssessmentInputs) -> Any:
        return _ForgedBranch()

    with pytest.raises(HarnessError, match="is_authentic"):
        run_scenario(scenario, decide_fn=_forge)


def test_a_forged_branch_is_not_reported_as_an_ordinary_scenario_failure(
    tmp_path: Path,
) -> None:
    # falsifier: the forgery is caught but tallied alongside a wrong criticality
    # as one more failed expectation, so a reader of the report concludes the
    # clinical oracle needs updating. A branch that L2 did not mint means the
    # thing under test was not the thing under test, which is a different
    # category of event and must not be counted as evidence about medicine.
    _write(tmp_path, "forged.yaml", MINIMAL)
    scenario = load_scenarios(tmp_path)[0]

    with pytest.raises(HarnessError) as caught:
        run_scenario(scenario, decide_fn=lambda inputs: _ForgedBranch())  # type: ignore[arg-type,return-value]
    message = str(caught.value)
    assert "ASM-06" in message
    # The turn's own text, per the B5 brief: a reader of a CI log does not have
    # the YAML open beside them.
    assert "up and talking" in message


def test_a_stale_but_genuine_branch_is_also_rejected() -> None:
    # falsifier: `is_authentic` is satisfied by anything that has EVER been
    # minted, so a branch cached from an earlier incident passes forever and the
    # registry's bound becomes decoration. The module documents the stale
    # direction as the safe one ("a consumer that cannot verify must re-ask
    # rather than trust"), and this asserts the bound actually evicts.
    established = lambda value: Finding(value, Certainty.ESTABLISHED)
    first = decide(
        AssessmentInputs(
            scene_safe=established(SceneSafety.SAFE),
            responsiveness=established(Responsiveness.UNRESPONSIVE),
            breathing=established(Breathing.ABNORMAL_OR_GASPING),
            severe_bleeding=established(SevereBleeding.NONE),
        )
    )
    assert is_authentic(first)
    # Push it out of the bounded registry through the real entry point.
    for _ in range(200):
        decide(AssessmentInputs())
    assert not is_authentic(first), (
        "a branch older than the registry must not verify: a consumer that "
        "cannot verify has to re-ask, and trusting the stale one is the unsafe "
        "direction"
    )
    assert isinstance(first, AssessmentBranch)


def test_every_branch_the_committed_suite_produces_is_authentic() -> None:
    # falsifier: the authenticity check runs only on the injected-forgery path
    # and is somehow skipped on the real one - so ASM-06 is enforced only in the
    # test that tests it. Every branch the committed scenarios produce passed
    # through `_check_branch_authentic` to get here, and this re-asserts it on
    # the results.
    checked = 0
    turns_seen = 0
    for scenario in load_scenarios(SCENARIO_DIR):
        for turn in run_scenario(scenario).turns:
            turns_seen += 1
            assert is_authentic(turn.branch), (
                f"{scenario.id} {turn.turn.label} produced an inauthentic branch"
            )
            checked += 1
    # EVERY turn, not most. An earlier version of `run_scenario` consulted L2
    # only when the turn supplied findings or named an L2 expectation, which
    # meant ASM-06 was enforced on most turns rather than on every branch the
    # harness receives - the skip shape this project keeps finding, one level up
    # from the turn loop. Asserting equality rather than a floor is what stops
    # that condition being reintroduced as an optimisation.
    assert checked == turns_seen, (
        f"{turns_seen - checked} turn(s) produced no branch to verify; L2 must "
        f"be consulted on every turn or ASM-06 is enforced only sometimes"
    )
    assert turns_seen >= 20


# ------------------------------------------------------- FR-013 and reporting


def test_the_budget_is_enforced_by_the_runner_not_only_by_ci() -> None:
    # falsifier: FR-013's "<= 120 s or it is not green" lives only in a printed
    # line and in a CI timeout, so nobody can reproduce the bound locally and a
    # suite that slows to three minutes still reports success. This repo has a
    # sub-10 ms retrieval claim on record that had never been executed; a latency
    # budget is asserted by something that runs or it is not asserted.
    result = run_suite(SCENARIO_DIR, budget_seconds=0.0)
    assert not result.within_budget
    assert not result.passed, (
        "a suite over its wall-clock budget must not pass, even with every "
        "clinical expectation met"
    )
    assert result.failures == (), (
        "a budget breach is not a failed expectation: conflating them would "
        "report a clinical regression that did not happen"
    )


def test_a_budget_breach_exits_non_zero_through_the_cli() -> None:
    # falsifier: the budget is a property of `SuiteResult` that the CLI never
    # reads, so the bound is enforced in a dataclass nobody runs. The exit code
    # is the only part of FR-013 a CI job can act on.
    stream = io.StringIO()
    code = main(
        ["--headless", str(SCENARIO_DIR), "--budget-seconds", "0"], stream=stream
    )
    assert code == 1, f"expected a failing exit on a budget breach, got {code}"
    assert "BREACHED" in stream.getvalue()


def test_the_default_budget_is_fr013s_number() -> None:
    # falsifier: the constant drifts from the requirement it encodes, so the
    # runner enforces a bound the register does not state and DESIGN.md section
    # 8's FR-013 row stops describing what runs.
    assert BUDGET_SECONDS == 120.0


def test_the_cli_exits_zero_on_the_committed_suite_and_reports_its_scope() -> None:
    # falsifier: the report omits which section 8 rows it does NOT cover, so a
    # reader of a green log concludes the harness evidences FR-001, FR-002 and
    # NFR-001 - the exact over-claim that put five unproven BUILT rows in the
    # register, whose evidence column named this file while it did not exist.
    stream = io.StringIO()
    code = main(["--headless", str(SCENARIO_DIR)], stream=stream)
    output = stream.getvalue()
    assert code == 0, output
    assert "wall time" in output
    assert "WITHIN" in output
    for row in ("FR-001", "FR-002", "NFR-001", "OBS-002"):
        assert row in output, f"{row} is unreached but not disclosed in the report"


def test_the_unreached_rows_are_real_register_rows_and_not_claimed_as_covered() -> None:
    # falsifier: `unreached_requirements` drifts into naming rows the harness
    # DOES assert, or into a vague apology with no row ids, and stops being a
    # scope statement anyone can check against DESIGN.md.
    unreached = unreached_requirements()
    assert len(unreached) >= 8
    for row, reason in unreached.items():
        assert row[:3] in {"FR-", "NFR", "OBS", "SEC"}, f"{row} is not a register id"
        assert len(reason) >= 20, f"{row}: {reason!r} does not say why"
    assert "FR-013" not in unreached, (
        "FR-013 is the row this harness exists to evidence; listing it as "
        "unreached would make the whole subsystem self-refuting"
    )


# --------------------------------------------------- the stack is really driven


def test_the_ratchet_is_observed_across_turns_not_within_one(tmp_path: Path) -> None:
    # falsifier: the harness reads criticality off the BRANCH rather than off
    # `TriageState`, so every ratchet property is invisible to it - a turn whose
    # own branch asks for SEVERE while the incident is already CRITICAL would
    # report 4, and a regression that let a calm turn downgrade a severe
    # incident would pass every scenario in the suite.
    _write(
        tmp_path,
        "ratchet.yaml",
        """
id: SCN-T03-ratchet
title: A critical incident that a later calm turn must not lower
traces_to: tests/unit/test_harness.py, the upward ratchet
turns:
  - responder: "He's not breathing"
    expect:
      criticality: 5
      escalated: true
      marker: not_breathing
  - findings:
      scene_safe: safe
      responsiveness: alert
      breathing: normal
      severe_bleeding: none
      airway_obstruction: none
      spinal_risk: none
      age_band: adult
      submersion: false
      exposure_reviewed: true
    expect:
      step: instruct_monitor
      criticality: 5
""",
    )
    result = run_suite(tmp_path)
    assert result.failures == (), "\n".join(str(f) for f in result.failures)
    turns = result.scenarios[0].turns
    # The branch and the incident disagree, and that disagreement IS the ratchet.
    assert turns[1].branch.criticality == Criticality.LOW
    assert turns[1].level is Criticality.CRITICAL


def test_the_gate_is_driven_through_gate_turn_and_can_refuse(tmp_path: Path) -> None:
    # falsifier: the `gate` expectation is wired to something that always
    # approves - a stub, or `classify` alone without `validate_turn` - so B4's
    # refusal path is never exercised and SCN-106's central claim (an escalation
    # survives a rejection) is asserted against a gate that cannot reject.
    _write(
        tmp_path,
        "gate.yaml",
        """
id: SCN-T04-gate
title: An uncited dose is refused
traces_to: tests/unit/test_harness.py, B4's output gate
turns:
  - speech: "Give him 300 milligrams of aspirin."
    expect:
      gate: rejected
      gate_reasons: [missing_citation, ungrounded_numeric]
  - speech: "Give him 300 milligrams of aspirin."
    retrieved:
      - id: chest-pain-cardiac
        text: "Give 300 milligrams of aspirin, chewed."
    expect:
      gate: approved
      gate_reasons: []
""",
    )
    result = run_suite(tmp_path)
    assert result.failures == (), "\n".join(str(f) for f in result.failures)
    outcomes = [turn.gate_outcome for turn in result.scenarios[0].turns]
    assert outcomes[0] is not outcomes[1], (
        "the same speech was judged identically with and without the document "
        "that grounds it, so the citation check is not running"
    )


def test_a_wrong_oracle_is_reported_with_the_turns_own_text(tmp_path: Path) -> None:
    # falsifier: a failure message says "turn 3" and nothing else, so a reader
    # of a CI log has to open the YAML to learn what turn 3 was - and a clinical
    # reviewer judging whether a regression matters does not have the YAML. This
    # is also the control proving the oracle can FAIL: without it, every green
    # scenario above is consistent with a runner that never compares anything.
    _write(
        tmp_path, "wrong.yaml", MINIMAL.replace("criticality: 2", "criticality: 5")
    )
    result = run_suite(tmp_path)
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.expectation == "criticality"
    assert (failure.expected, failure.actual) == (5, 2)
    assert "up and talking" in str(failure)
    assert not result.passed


def test_findings_accumulate_across_turns(tmp_path: Path) -> None:
    # falsifier: each turn rebuilds `AssessmentInputs` from only its own
    # findings, so the incident forgets what it was told and a deteriorating
    # patient's re-entry at an earlier letter is unrepresentable. SCN-105 would
    # then be asserting three unrelated one-turn decisions.
    _write(
        tmp_path,
        "accumulate.yaml",
        """
id: SCN-T05-accumulate
title: A finding established early is still held late
traces_to: tests/unit/test_harness.py, cumulative incident state
turns:
  - responder: "The scene is clear"
    findings:
      scene_safe: safe
    expect:
      step: ask_responsiveness
  - responder: "He's not responding"
    findings:
      responsiveness: unresponsive
    expect:
      step: ask_breathing_quality
  - responder: "He's gasping"
    findings:
      breathing: abnormal_or_gasping
    expect:
      step: instruct_cpr
      letter: breathing
""",
    )
    result = run_suite(tmp_path)
    assert result.failures == (), "\n".join(str(f) for f in result.failures)
    # Turn 3 supplied only `breathing`. Reaching INSTRUCT_CPR requires the
    # scene from turn 1 and the responsiveness from turn 2 to still be held.
    assert result.scenarios[0].turns[2].branch.step is AssessmentStep.INSTRUCT_CPR


def test_an_assumed_finding_keeps_the_level_correctable(tmp_path: Path) -> None:
    # falsifier: `assumed:` is parsed and then dropped, so every scenario
    # finding arrives ESTABLISHED and ASM-14's distinction is unreachable from
    # the scenario layer - meaning a level reached on guesses would report
    # `provenance=evidence` and the clinician on the bridge who finally
    # established the input could not correct it. That inversion is the exact
    # consequence round 3's Blocker 2 closed.
    _write(
        tmp_path,
        "assumed.yaml",
        """
id: SCN-T06-assumed
title: A level reached on an assumed scene stays correctable
traces_to: tests/unit/test_harness.py, ASM-14
turns:
  - responder: "I can't tell if it's safe, he's just lying there"
    findings:
      scene_safe: safe
      responsiveness: unresponsive
      breathing: abnormal_or_gasping
    assumed: [scene_safe]
    expect:
      step: instruct_cpr
      provenance: assumption
      assumed_inputs:
        - age_band
        - airway_obstruction
        - scene_safe
        - severe_bleeding
        - submersion
""",
    )
    result = run_suite(tmp_path)
    assert result.failures == (), "\n".join(str(f) for f in result.failures)
    branch = result.scenarios[0].turns[0].branch
    assert branch.provenance is Provenance.ASSUMPTION
    # TWO ROUTES INTO THAT SET, AND THE ORACLE NAMES BOTH. `scene_safe` is there
    # because the scenario declared it ASSUMED_WORST while still giving it a
    # value; the other four are there because they were never established at
    # all and this path read them. Both are assumptions in ASM-14's sense. The
    # oracle enumerates all five rather than only the interesting one, because a
    # subset assertion would pass against a branch that had silently stopped
    # recording the four - which is exactly the under-recording round 3's
    # Blocker 2 found in sixteen of eighteen steps.
    assert "scene_safe" in branch.assumed_inputs
    assert len(branch.assumed_inputs) == 5


def test_the_escalation_lifecycle_is_the_real_one(tmp_path: Path) -> None:
    # falsifier: the harness sets a boolean of its own instead of driving
    # `TriageState.request_escalation`, so the five-state lifecycle - and the
    # dedup that stops an on-call clinician being paged twice for one patient -
    # is never exercised by any scenario.
    _write(
        tmp_path,
        "escalate.yaml",
        """
id: SCN-T07-escalate
title: Two life-threat reports, one page
traces_to: tests/unit/test_harness.py, EscalationStatus and the dedup key
turns:
  - responder: "He's not breathing"
    expect:
      escalated: true
      escalation_status: requested
      marker: not_breathing
  - responder: "He's got no pulse either"
    expect:
      escalated: true
      escalation_status: requested
      marker: no_pulse
""",
    )
    result = run_suite(tmp_path)
    assert result.failures == (), "\n".join(str(f) for f in result.failures)
    for turn in result.scenarios[0].turns:
        assert turn.escalation is EscalationStatus.REQUESTED


def test_the_escalation_key_format_matches_productions() -> None:
    # falsifier: THE HARNESS'S DEDUP IS ITS OWN, NOT PRODUCTION'S. The claim
    # that the headless runner exercises the real deduplication rests entirely
    # on the two key formats being identical - `agent.py:_do_escalate` builds
    # `f"{session_id}:{category}"`, and a paraphrase of the same finding
    # collides only because the harness builds the same string. If either side
    # drifts, `test_the_escalation_lifecycle_is_the_real_one` still passes,
    # because it never compares against production's key: the harness would
    # simply have a differently-keyed escalation of its own that could never
    # collide, and the on-call clinician paged twice for one patient would be
    # invisible to this suite.
    #
    # Flagged by independent review as a claim living in a docstring rather than
    # in anything that runs. Asserted by reading BOTH sources rather than by
    # restating the format here, because a third copy of the string is a third
    # thing to drift.
    agent_source = (
        Path(__file__).resolve().parents[2] / "aiscelapeus" / "agent.py"
    ).read_text(encoding="utf-8")
    harness_source = (
        Path(__file__).resolve().parents[2] / "aiscelapeus" / "harness.py"
    ).read_text(encoding="utf-8")

    pattern = re.compile(r'f"\{(?:self\.)?state\.session_id\}:\{category\}"')
    assert pattern.search(agent_source), (
        "agent.py no longer builds the escalation key as "
        "f'{session_id}:{category}'; the harness's key must follow it"
    )
    assert pattern.search(harness_source), (
        "harness.py's escalation key has drifted from agent.py's, so the "
        "headless runner is exercising its own dedup rather than production's"
    )


def test_the_harness_can_express_every_l2_input() -> None:
    # falsifier: an input is added to `AssessmentInputs` and no scenario can
    # ever set it, so a clinical rule branching on it is unreachable from this
    # suite while the suite still reports green - the "designed slot that is not
    # a slot" trap Blocker 5 recorded for `airway_opened`, reproduced in the
    # scenario format. The import-time guard in harness.py enforces this; this
    # test is what makes the guard's failure visible as a test failure rather
    # than as an import error in an unrelated module.
    from aiscelapeus.harness import _FINDING_NAMES

    assert set(AssessmentInputs.__dataclass_fields__) == set(_FINDING_NAMES)


def test_a_scenario_file_that_is_not_yaml_is_refused(tmp_path: Path) -> None:
    # falsifier: a truncated or half-edited scenario file raises something other
    # than ScenarioError - a bare yaml.YAMLError - so the CLI's `except
    # HarnessError` misses it and the runner dies with a traceback instead of a
    # message naming the file.
    path = _write(tmp_path, "broken.yaml", "id: X\n  title: bad indent\n\tturns: []\n")
    with pytest.raises(ScenarioError, match="not valid YAML"):
        load_scenario(path)
