"""The criticality verdict: the ratchet and the escalation lifecycle.

Assertions here observe resulting state, never return flags. That rule exists
because of a specific defect in this module: `set_level` rejected a same-level
re-assertion by returning False, but only *after* overwriting the rationale and
appending a second history entry. The obvious test -

    assert state.set_level(2, "calmer") is False

- passes, because the flag really is False. It certifies the bug as correct
behaviour. What is wrong is the state behind the flag.
"""

from __future__ import annotations

import pytest

from aiscelapeus.clock import ManualClock
from aiscelapeus.triage import Criticality, EscalationStatus, TriageState


@pytest.fixture
def state(clock: ManualClock) -> TriageState:
    return TriageState(session_id="inc-test", clock=clock)


# ------------------------------------------------------------------ the ratchet


def test_level_rises(state: TriageState) -> None:
    # falsifier: an assessed increase in severity is not recorded, so the UI and
    # the record disagree with what the agent concluded.
    change = state.set_level(4, "uncontrolled bleeding")
    assert change.changed is True
    assert state.level is Criticality.SEVERE
    assert state.rationale == "uncontrolled bleeding"
    assert len(state.history) == 1


def test_rejected_downgrade_leaves_state_untouched(state: TriageState) -> None:
    # falsifier: a calmer later turn rewrites the rationale of a severe
    # incident, so the record reads as if the severe finding was never made.
    state.set_level(4, "massive haemorrhage")
    change = state.set_level(2, "responder sounds calmer now")

    assert change.rejected is True
    assert change.changed is False
    # State, not the flag. Every one of these is the actual assertion.
    assert state.level is Criticality.SEVERE
    assert state.rationale == "massive haemorrhage"
    assert len(state.history) == 1, "a rejected downgrade must not append history"


def test_same_level_reassertion_updates_rationale_without_duplicating_history(
    state: TriageState,
) -> None:
    # falsifier: the agent re-states Level 4 with new reasoning; the UI shows
    # the new rationale while the durable record never hears about it, because
    # the caller branches on a False flag and skips the write.
    state.set_level(4, "uncontrolled bleeding")
    change = state.set_level(4, "bleeding controlled but patient shocky")

    assert change.changed is False, "the level itself did not move"
    assert change.rejected is False, "this is not a rejected downgrade"
    assert change.rationale_updated is True, (
        "the caller needs to know there is something new to persist"
    )
    assert state.rationale == "bleeding controlled but patient shocky"
    assert len(state.history) == 2, "a genuine re-assessment is a history entry"


def test_out_of_range_level_is_an_error(state: TriageState) -> None:
    # falsifier: a model emitting level 7 or 0 is silently clamped to 5 or 1, so
    # a malfunctioning model reads as a working one and nothing is investigated.
    with pytest.raises(ValueError):
        state.set_level(7, "model malfunction")
    with pytest.raises(ValueError):
        state.set_level(0, "model malfunction")
    assert state.level is Criticality.MINOR, "a rejected input must not move state"
    assert state.history == []


def test_level_is_an_enum_not_a_bare_int(state: TriageState) -> None:
    # falsifier: level is a plain mutable int, so `state.level = 1` bypasses the
    # ratchet entirely and type-checks clean.
    state.set_level(5, "cardiac arrest")
    assert isinstance(state.level, Criticality)


def test_history_records_what_moved_it(state: TriageState) -> None:
    # falsifier: history does not distinguish a deterministic rule from a model
    # judgement, so an audit cannot tell which one escalated the incident.
    state.set_level(5, "not breathing", source="rule")
    entry = state.history[-1]
    assert entry["source"] == "rule"
    assert entry["level"] == 5
    assert entry["rationale"] == "not breathing"


def test_timestamps_come_from_the_injected_clock(
    state: TriageState, clock: ManualClock
) -> None:
    # falsifier: timestamps come from the wall clock, so the ordering of events
    # in the record cannot be asserted and replay cannot be verified.
    state.set_level(3, "first")
    clock.advance(12)
    state.set_level(4, "second")
    assert state.history[0]["at"] != state.history[1]["at"]


# --------------------------------------------------------- escalation lifecycle


def test_escalation_starts_not_needed(state: TriageState) -> None:
    # falsifier: a fresh incident reads as already escalated.
    assert state.escalation is EscalationStatus.NOT_NEEDED
    assert state.escalated is False


def test_requesting_escalation_raises_the_level(state: TriageState) -> None:
    # falsifier: the agent bridges a clinician while the dashboard still shows a
    # green "Minor" badge - the explicit escalation tool never touched level.
    state.request_escalation("responder overwhelmed", key="inc-test:1")
    assert state.escalation is EscalationStatus.REQUESTED
    assert state.level >= Criticality.SEVERE, (
        "an incident needing a clinician is not Minor"
    )


def test_escalation_is_idempotent_within_a_session(state: TriageState) -> None:
    # falsifier: a second request pages the on-call clinician twice for one
    # incident.
    first = state.request_escalation("arrest", key="inc-test:1")
    second = state.request_escalation("arrest again", key="inc-test:1")
    assert first is EscalationStatus.REQUESTED
    assert second is EscalationStatus.REQUESTED
    assert state.escalation_reason == "arrest", "the first reason stands"
    assert len(state.escalation_log) == 1


def test_escalation_is_idempotent_across_reconstruction(clock: ManualClock) -> None:
    # falsifier: the process restarts mid-incident, the flag comes back False,
    # and the clinician is paged a second time for the same emergency. Today the
    # guard is a boolean on an object rebuilt fresh on every start.
    key = "inc-test:1"
    before = TriageState(session_id="inc-test", clock=clock)
    before.request_escalation("arrest", key=key)

    after = TriageState(session_id="inc-test", clock=clock)
    after.restore_escalation(before.escalation_key, before.escalation)
    outcome = after.request_escalation("arrest", key=key)

    assert outcome is EscalationStatus.REQUESTED
    assert len(after.escalation_log) == 0, "a replayed key must not dispatch again"


@pytest.mark.parametrize(
    ("transition", "expected"),
    [
        ("mark_clinician_joined", EscalationStatus.CLINICIAN_JOINED),
        ("mark_clinician_lost", EscalationStatus.CLINICIAN_LOST),
        ("mark_escalation_failed", EscalationStatus.FAILED_NO_RESPONSE),
    ],
)
def test_escalation_reaches_every_outcome(
    state: TriageState, transition: str, expected: EscalationStatus
) -> None:
    # falsifier: escalation is one terminal boolean, so the three failure
    # outcomes cannot be expressed and a dispatch that nobody answered is
    # indistinguishable from one a clinician accepted.
    state.request_escalation("arrest", key="inc-test:1")
    getattr(state, transition)()
    assert state.escalation is expected


def test_clinician_cannot_join_without_a_request(state: TriageState) -> None:
    # falsifier: state reports a clinician on the call when none was ever
    # requested.
    with pytest.raises(ValueError):
        state.mark_clinician_joined()


def test_a_lost_clinician_is_not_silently_still_joined(state: TriageState) -> None:
    # falsifier: the clinician drops and the dashboard still shows them present,
    # so nobody knows the responder is alone again.
    state.request_escalation("arrest", key="inc-test:1")
    state.mark_clinician_joined()
    state.mark_clinician_lost()
    assert state.escalation is EscalationStatus.CLINICIAN_LOST
    assert state.clinician_present is False


# -------------------------------------------------------------------- payload


def test_payload_carries_no_unwritten_fields(state: TriageState) -> None:
    # falsifier: the wire payload declares a field no code path ever writes, so
    # the UI renders a constant as though it were a measurement. `distress_score`
    # was required by the TypeScript type and permanently 0.0.
    payload = state.to_payload()
    assert "distress_score" not in payload


def test_payload_reports_escalation_status_not_just_a_flag(state: TriageState) -> None:
    # falsifier: the UI receives a boolean and cannot distinguish "requested" from
    # "a clinician is actually on the call".
    state.request_escalation("arrest", key="inc-test:1")
    payload = state.to_payload()
    assert payload["escalation"] == EscalationStatus.REQUESTED.value
