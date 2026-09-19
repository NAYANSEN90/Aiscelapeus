"""The voice corpus, asserted against the deterministic layers.

HERMETIC. This module imports no audio library, makes no network call, and
reads no credential. It reads two committed files:

    tests/audio/scenarios.yaml   the oracle
    tests/audio/manifest.json    what Deepgram actually heard (when built)

`librosa`, `soundfile` and the Deepgram call all live in
`scripts/build_corpus.py`, which runs once to produce those artefacts. That
split is what keeps `pytest -q` installable and runnable with no compiler
toolchain, no API key, and no network - per CLAUDE.md.

WHAT IS ASSERTED HERE
---------------------
Two of the three layers, the deterministic ones:

  markers   `phrases.find_markers` over the utterance text
  triage    `TriageState` criticality, the upward ratchet, escalation lifecycle

The third layer - which tools the agent calls, which protocols come back -
needs Gemini and Moss and is not asserted here.

SCRIPT TEXT VS REAL TRANSCRIPT
------------------------------
Every marker assertion runs TWICE where a transcript exists:

  1. against the script - the line the reader was given. Always available, so
     the oracle is enforced even before anyone has recorded.
  2. against `stt.transcript` - what Deepgram returned through the ambient mix.

A divergence between the two is a genuine finding about STT robustness, not a
test bug. It is reported with the scenario's SNR rather than silently tolerated,
because "the safety net works on clean text" is a much weaker claim than "the
safety net works on what the microphone actually delivered".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from aiscelapeus.clock import ManualClock
from aiscelapeus.phrases import find_markers
from aiscelapeus.triage import Criticality, EscalationStatus, TriageState

AUDIO_DIR = Path(__file__).resolve().parent.parent / "audio"
SCENARIOS_PATH = AUDIO_DIR / "scenarios.yaml"
MANIFEST_PATH = AUDIO_DIR / "manifest.json"


def _load_scenarios() -> dict[str, Any]:
    with SCENARIOS_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {"scenarios": {}}
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


SCENARIOS = _load_scenarios()
MANIFEST = _load_manifest()

ALL_TURNS: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (scenario["id"], scenario, turn)
    for scenario in SCENARIOS["scenarios"]
    for turn in scenario["turns"]
]


# Turns where Deepgram demonstrably fails to carry a marker through. Each one
# is a RECORDED FINDING, not a silenced test: it is xfailed rather than
# asserted, so the suite is green while the failure stays named, counted and
# attached to its evidence. Deleting the assertion instead would make the
# corpus claim a robustness it does not have.
#
# An entry earns its place only with a measured transcript behind it, and it is
# REMOVED the moment the failure stops happening. That is enforced rather than
# requested: `test_markers_survive_the_real_audio_channel` asserts that a
# registered turn still fails, and in exactly the registered way. A mitigation
# that fixes one of these turns therefore turns this file red until the entry
# goes, which is the point - a green corpus quietly certifying an unmitigated
# meaning inversion would be worse than having no corpus.
#
# Verified by simulation: patching the manifest so s08 t02 transcribes correctly
# produces "registered in KNOWN_STT_FAILURES as losing ['pallor'], but the
# markers now survive". Note `pytest.xfail()` alone could not do this - it
# aborts imperatively, so the recovered case would never be evaluated, and
# `xfail_strict` does not reach it either.
#
# Evidence: docs/evidence/2026-09-18-stt-degrades-under-urgency.md
KNOWN_STT_FAILURES: dict[tuple[str, int], dict[str, Any]] = {
    ("s08_site_arrest", 2): {
        "markers": ["pallor"],
        "why": (
            "THE WORST RESULT IN THE CORPUS, and it is not a lost marker but an "
            "INVERTED MEANING. Script: 'He's grey, he's gone grey.' "
            "nova-3-medical heard: 'He's great. He's doing great.' at "
            "confidence 0.82 - one phoneme, under shouted delivery, on clean "
            "audio. A responder reporting the colour of an arrested patient was "
            "transcribed as reporting that the patient is well. "
            "Nothing downstream can recover from this: the deterministic net is "
            "defeated UPSTREAM of itself, and the model receives a sentence "
            "that says the opposite of what was said."
        ),
    },
    ("s10_poolside_drowning", 1): {
        "markers": ["drowning", "not_breathing", "unresponsive"],
        "why": (
            "Panicked, shouted delivery of the fastest line in the corpus. "
            "nova-3-medical returned the single token '4.' at confidence 0.46 - "
            "it heard only the child's age. Three life-threat markers lost on "
            "CLEAN audio, before any ambient noise."
        ),
    },
}


def _flat(text: str) -> str:
    """The utterance as one line, exactly as `build_corpus` records it."""
    return " ".join(text.split())


def _ids(turns: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> list[str]:
    return [f"{sid}-t{turn['n']:02d}" for sid, _, turn in turns]


# --------------------------------------------------------------- corpus shape


def test_corpus_has_at_least_ten_scenarios() -> None:
    """The brief was ten; twelve exist. Guards against silent deletion."""
    assert len(SCENARIOS["scenarios"]) >= 10


def test_every_criticality_level_is_represented() -> None:
    """A corpus that never exercises Level 1 cannot catch over-escalation, and
    one that never reaches 5 cannot catch the thing that kills people."""
    peaks = {s["criticality_peak"] for s in SCENARIOS["scenarios"]}
    assert peaks == {1, 2, 3, 4, 5}, f"missing levels: {set(range(1, 6)) - peaks}"


def test_scenario_ids_are_unique() -> None:
    ids = [s["id"] for s in SCENARIOS["scenarios"]]
    assert len(ids) == len(set(ids))


def test_turns_are_numbered_from_one_without_gaps() -> None:
    """The turn number is the filename (`t03.wav`). A gap means a recording
    that can never be found, or one silently pointing at the wrong line."""
    for scenario in SCENARIOS["scenarios"]:
        numbers = [t["n"] for t in scenario["turns"]]
        assert numbers == list(range(1, len(numbers) + 1)), scenario["id"]


def test_every_scenario_declares_a_scene_and_a_bed() -> None:
    """Ambience is part of the test, not decoration: a scenario without a bed
    would quietly become a clean-audio test."""
    for scenario in SCENARIOS["scenarios"]:
        scene = scenario["scene"]
        assert scene.get("bed"), scenario["id"]
        assert scene.get("place"), scenario["id"]
        assert isinstance(scene.get("snr_db"), (int, float)), scenario["id"]


def test_voice_transform_stays_within_the_convincing_range() -> None:
    """Beyond +/-2 semitones the shift stops sounding like a different person
    and starts sounding like processing - and processing artefacts would
    contaminate the STT result, so a mis-transcription could no longer be
    attributed to the ambient noise."""
    for scenario in SCENARIOS["scenarios"]:
        semitones = scenario["voice"]["semitones"]
        assert -2.0 <= semitones <= 2.0, f"{scenario['id']}: {semitones}"


def test_every_turn_carries_a_delivery_direction() -> None:
    """A panicked line read calmly tests nothing that matters, so the direction
    is as much a part of the corpus as the words."""
    for sid, _, turn in ALL_TURNS:
        assert turn.get("direction", "").strip(), f"{sid} t{turn['n']}"


# ------------------------------------------------------------ layer 1: markers


@pytest.mark.parametrize(("sid", "scenario", "turn"), ALL_TURNS, ids=_ids(ALL_TURNS))
def test_markers_match_the_oracle_on_script_text(
    sid: str, scenario: dict[str, Any], turn: dict[str, Any]
) -> None:
    """`expect_markers` is exhaustive: exactly these fire, nothing else.

    Asserting the full set rather than "contains" is deliberate. A marker that
    fires unexpectedly is as much a defect as one that fails to - it is how a
    fainting passenger ends up bridging a clinician.
    """
    text = _flat(turn["text"])
    actual = sorted({hit.marker_id for hit in find_markers(text)})
    expected = sorted(turn.get("expect_markers") or [])

    assert actual == expected, (
        f"{sid} turn {turn['n']}\n"
        f"  text:     {text}\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}"
    )


@pytest.mark.parametrize(("sid", "scenario", "turn"), ALL_TURNS, ids=_ids(ALL_TURNS))
def test_forbidden_markers_stay_silent(
    sid: str, scenario: dict[str, Any], turn: dict[str, Any]
) -> None:
    """The strongest assertion in the corpus.

    Proving a marker fires shows the net catches something. Proving it stays
    silent on a negated or past-tense phrase is what stops the system crying
    wolf - and a net that escalates every recovered faint will be ignored when
    a real arrest arrives.
    """
    forbidden = turn.get("expect_no_markers") or []
    if not forbidden:
        pytest.skip("no forbidden markers declared for this turn")

    text = _flat(turn["text"])
    fired = {hit.marker_id for hit in find_markers(text)}
    violations = sorted(set(forbidden) & fired)

    assert not violations, (
        f"{sid} turn {turn['n']}: {violations} fired but must not\n"
        f"  text: {text}"
    )


def test_marker_ids_in_the_oracle_all_exist() -> None:
    """A typo in scenarios.yaml would otherwise make an assertion vacuous: an
    expectation naming a marker that does not exist can never be violated."""
    from aiscelapeus.phrases import MARKERS

    known = {marker.marker_id for marker in MARKERS}
    for sid, _, turn in ALL_TURNS:
        named = set(turn.get("expect_markers") or []) | set(
            turn.get("expect_no_markers") or []
        )
        unknown = named - known
        assert not unknown, f"{sid} t{turn['n']} names unknown marker(s): {unknown}"


# ------------------------------------------------------------- layer 2: triage


@pytest.mark.parametrize(
    ("sid", "scenario"),
    [(s["id"], s) for s in SCENARIOS["scenarios"]],
    ids=[s["id"] for s in SCENARIOS["scenarios"]],
)
def test_criticality_never_decreases_within_a_scenario(
    sid: str, scenario: dict[str, Any]
) -> None:
    """The oracle itself must respect the ratchet.

    `TriageState.set_level` refuses downgrades, so an oracle that expected one
    would be asserting something the domain cannot do - the test would pass
    while describing impossible behaviour.
    """
    levels = [turn["triage"]["level"] for turn in scenario["turns"]]
    for previous, current in zip(levels, levels[1:]):
        assert current >= previous, (
            f"{sid}: criticality drops {previous} -> {current}; "
            "TriageState ratchets upward and cannot express this"
        )


@pytest.mark.parametrize(
    ("sid", "scenario"),
    [(s["id"], s) for s in SCENARIOS["scenarios"]],
    ids=[s["id"] for s in SCENARIOS["scenarios"]],
)
def test_replaying_a_scenario_reproduces_the_expected_triage_state(
    sid: str, scenario: dict[str, Any]
) -> None:
    """Drive a real `TriageState` through the scenario and compare at each turn.

    This is the integration point between the two deterministic layers: the
    oracle's per-turn level and escalation status have to be reachable by the
    actual domain object, not merely plausible numbers in a YAML file.
    """
    state = TriageState(session_id=f"test-{sid}", clock=ManualClock())
    threshold = Criticality.SEVERE

    for turn in scenario["turns"]:
        expected = turn["triage"]
        level = Criticality(expected["level"])

        change = state.set_level(int(level), f"turn {turn['n']}")

        if expected.get("downgrade_refused"):
            assert change.rejected or state.level >= level, (
                f"{sid} t{turn['n']}: expected the ratchet to refuse a downgrade"
            )

        if state.should_escalate(int(threshold)):
            state.request_escalation(
                f"turn {turn['n']}", key=f"{sid}:t{turn['n']}"
            )

        assert int(state.level) == int(level), (
            f"{sid} t{turn['n']}: level {int(state.level)} != expected {int(level)}"
        )

        expected_escalation = EscalationStatus(expected["escalation"])
        assert state.escalation is expected_escalation, (
            f"{sid} t{turn['n']}: escalation {state.escalation.value} "
            f"!= expected {expected_escalation.value}"
        )


def test_escalation_is_declared_for_every_turn_at_or_above_the_threshold() -> None:
    """Level 4 is the configured threshold, so a turn at 4 or 5 that claims
    `not_needed` contradicts `TriageState.should_escalate`."""
    for sid, _, turn in ALL_TURNS:
        triage = turn["triage"]
        if triage["level"] >= int(Criticality.SEVERE):
            assert triage["escalation"] != EscalationStatus.NOT_NEEDED.value, (
                f"{sid} t{turn['n']}: level {triage['level']} "
                "must have requested a clinician"
            )


def test_escalation_status_values_are_legal() -> None:
    legal = {status.value for status in EscalationStatus}
    for sid, _, turn in ALL_TURNS:
        value = turn["triage"]["escalation"]
        assert value in legal, f"{sid} t{turn['n']}: unknown status {value!r}"


def test_escalation_once_requested_is_never_withdrawn() -> None:
    """A clinician request has no un-request. An oracle that went back to
    `not_needed` would describe a state transition the lifecycle forbids."""
    for scenario in SCENARIOS["scenarios"]:
        seen = False
        for turn in scenario["turns"]:
            escalated = turn["triage"]["escalation"] != EscalationStatus.NOT_NEEDED.value
            if seen:
                assert escalated, (
                    f"{scenario['id']} t{turn['n']}: escalation withdrawn"
                )
            seen = seen or escalated


# ------------------------------------------- layer 1 again, on the real audio


def _transcribed_turns() -> list[tuple[str, dict[str, Any], dict[str, Any], str]]:
    """Turns whose mixed audio has actually been through Deepgram."""
    out = []
    for scenario in SCENARIOS["scenarios"]:
        recorded = MANIFEST.get("scenarios", {}).get(scenario["id"], {}).get("turns", {})
        for turn in scenario["turns"]:
            entry = recorded.get(f"t{turn['n']:02d}") or {}
            stt = entry.get("stt") or {}
            transcript = (stt.get("transcript") or "").strip()
            if transcript:
                out.append((scenario["id"], scenario, turn, transcript))
    return out


TRANSCRIBED = _transcribed_turns()


@pytest.mark.skipif(
    not TRANSCRIBED,
    reason="no transcripts yet; run scripts/build_corpus.py --transcribe",
)
@pytest.mark.parametrize(
    ("sid", "scenario", "turn", "transcript"),
    TRANSCRIBED,
    ids=[f"{sid}-t{t['n']:02d}" for sid, _, t, _ in TRANSCRIBED],
)
def test_markers_survive_the_real_audio_channel(
    sid: str, scenario: dict[str, Any], turn: dict[str, Any], transcript: str
) -> None:
    """The assertion that actually matters.

    Runs the marker oracle against what Deepgram returned through the ambient
    mix, not against the script. If a safety-critical marker is lost between
    the two, the failure names the SNR that lost it.
    """
    expected = sorted(turn.get("expect_markers") or [])
    actual = sorted({hit.marker_id for hit in find_markers(transcript)})
    lost = sorted(set(expected) - set(actual))

    entry = (
        MANIFEST.get("scenarios", {})
        .get(sid, {})
        .get("turns", {})
        .get(f"t{turn['n']:02d}", {})
    )
    # Report the SNR that was actually delivered, not the one the scenario
    # table asks for. They differ whenever the ambient beds have not been
    # fetched: `mix_turn` reports inf for a silent bed, and blaming a
    # transcription failure on "12 dB of pool noise" that was never mixed would
    # be a fabricated cause attached to a real finding.
    delivered = (entry.get("mix") or {}).get("snr_db")
    if delivered is None or delivered == float("inf") or delivered == "Infinity":
        channel = "clean audio, no ambient bed mixed"
    else:
        channel = f"{delivered} dB SNR ({scenario['scene']['bed']})"

    known = KNOWN_STT_FAILURES.get((sid, turn["n"]))
    if known:
        registered = set(known["markers"])

        # A registered failure that no longer happens must FAIL, loudly. The
        # obvious spelling of this - `pytest.xfail(...)` inside `if lost` - is
        # wrong in a way that matters here: it aborts imperatively, so the
        # recovered case is never even evaluated, and a stale entry would keep
        # asserting a failure that had already been fixed. `xfail_strict` does
        # not help either, because the imperative form bypasses it.
        #
        # This is the case the corpus exists to get right. If a mitigation lands
        # for the "grey" -> "great" inversion, s08 t02 is the before/after
        # evidence, and a green suite that quietly kept certifying the
        # unmitigated behaviour would be worse than no suite.
        assert lost, (
            f"{sid} turn {turn['n']} is registered in KNOWN_STT_FAILURES as "
            f"losing {sorted(registered)}, but the markers now survive.\n"
            f"  transcript: {transcript}\n"
            f"  If something was fixed, REMOVE the entry - do not leave the\n"
            f"  corpus claiming a failure that no longer happens."
        )
        assert set(lost) == registered, (
            f"{sid} turn {turn['n']}: registered failure is "
            f"{sorted(registered)} but the actual loss is {lost}.\n"
            f"  transcript: {transcript}\n"
            f"  The registry excuses one specific failure, not whatever this\n"
            f"  turn happens to do next. Update the entry with evidence."
        )
        pytest.xfail(
            f"known STT failure: {lost} lost on {channel}. {known['why']}"
        )

    assert not lost, (
        f"{sid} turn {turn['n']}: marker(s) {lost} LOST in transmission on "
        f"{channel}\n"
        f"  script:     {_flat(turn['text'])}\n"
        f"  transcript: {transcript or '(nothing - empty transcript)'}\n"
        f"  confidence: {(entry.get('stt') or {}).get('confidence')}\n"
        f"  This is an STT robustness finding, not a test bug. If it is real,\n"
        f"  add it to KNOWN_STT_FAILURES with evidence rather than deleting\n"
        f"  the assertion."
    )


@pytest.mark.skipif(
    not TRANSCRIBED,
    reason="no transcripts yet; run scripts/build_corpus.py --transcribe",
)
@pytest.mark.parametrize(
    ("sid", "scenario", "turn", "transcript"),
    TRANSCRIBED,
    ids=[f"{sid}-t{t['n']:02d}" for sid, _, t, _ in TRANSCRIBED],
)
def test_forbidden_markers_stay_silent_on_the_real_audio(
    sid: str, scenario: dict[str, Any], turn: dict[str, Any], transcript: str
) -> None:
    """Noise must not INVENT a life threat either.

    A mis-transcription that turns an ordinary sentence into one containing
    "not breathing" would bridge a clinician to a casualty who is fine.
    """
    forbidden = set(turn.get("expect_no_markers") or [])
    if not forbidden:
        pytest.skip("no forbidden markers declared for this turn")

    fired = {hit.marker_id for hit in find_markers(transcript)}
    violations = sorted(forbidden & fired)

    assert not violations, (
        f"{sid} turn {turn['n']}: noise INVENTED marker(s) {violations} at "
        f"{scenario['scene']['snr_db']} dB SNR\n"
        f"  script:     {_flat(turn['text'])}\n"
        f"  transcript: {transcript}"
    )


# ------------------------------------------------------------------ manifest


def test_known_stt_failures_stay_a_short_reviewed_list() -> None:
    """A registry of accepted failures decays into a dumping ground unless
    something pushes back.

    Two guards: every entry must name markers the oracle actually expects for
    that turn (so a typo cannot silently excuse a different failure), and the
    list must stay small enough that each entry is still read. Past a handful,
    the honest response is that the STT layer is not fit for this job - not
    another line here.
    """
    assert len(KNOWN_STT_FAILURES) <= 5, (
        f"{len(KNOWN_STT_FAILURES)} accepted STT failures. Each one is a marker "
        "the safety net never sees; this list is not the place to absorb a "
        "systemic problem."
    )

    for (sid, n), entry in KNOWN_STT_FAILURES.items():
        scenario = next((s for s in SCENARIOS["scenarios"] if s["id"] == sid), None)
        assert scenario, f"KNOWN_STT_FAILURES names unknown scenario {sid!r}"

        turn = next((t for t in scenario["turns"] if t["n"] == n), None)
        assert turn, f"KNOWN_STT_FAILURES names unknown turn {sid} t{n}"

        expected = set(turn.get("expect_markers") or [])
        named = set(entry["markers"])
        assert named <= expected, (
            f"{sid} t{n}: excuses {sorted(named - expected)}, which the oracle "
            "does not expect for this turn"
        )
        assert entry.get("why", "").strip(), f"{sid} t{n} has no explanation"


@pytest.mark.skipif(not MANIFEST.get("scenarios"), reason="corpus not built yet")
def test_manifest_scenarios_all_exist_in_the_oracle() -> None:
    """A manifest entry with no scenario is an orphan: audio that nothing
    asserts against, which would look like coverage and provide none."""
    known = {s["id"] for s in SCENARIOS["scenarios"]}
    unknown = set(MANIFEST["scenarios"]) - known
    assert not unknown, f"manifest has orphaned scenarios: {sorted(unknown)}"


@pytest.mark.skipif(not MANIFEST.get("scenarios"), reason="corpus not built yet")
def test_manifest_scripts_match_the_oracle() -> None:
    """The recorded line must still be the line the oracle expects.

    Editing `scenarios.yaml` after recording silently invalidates the audio:
    the WAV says one thing and the assertion expects another. This catches that
    drift instead of letting a stale recording quietly pass.
    """
    drift: list[str] = []
    for scenario in SCENARIOS["scenarios"]:
        recorded = MANIFEST["scenarios"].get(scenario["id"], {}).get("turns", {})
        for turn in scenario["turns"]:
            entry = recorded.get(f"t{turn['n']:02d}")
            if entry and entry.get("script") != _flat(turn["text"]):
                drift.append(
                    f"{scenario['id']} t{turn['n']}:\n"
                    f"    manifest: {entry.get('script')}\n"
                    f"    oracle:   {_flat(turn['text'])}"
                )
    assert not drift, (
        "scenarios.yaml changed after recording; re-record or revert:\n"
        + "\n".join(drift)
    )
