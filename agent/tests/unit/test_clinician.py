"""Clinician presence and late-join snapshot contracts.

Falsifier: a request is displayed as joined without a clinician participant, a
duplicate room event corrupts presence, one of two clinicians leaving marks all
help lost, or a late join receives anything other than the current durable facts.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from aiscelapeus.clinician import (
    ClinicianRoster,
    clinician_connection,
    clinician_identity,
    snapshot_payload,
)
from aiscelapeus.ports import FactRecord
from aiscelapeus.triage import (
    Criticality,
    EscalationStatus,
    FactKind,
    TriageState,
)


@dataclass
class _Participant:
    identity: object = "clinician-1"
    metadata: object = '{"role":"clinician"}'
    sid: object = "participant-1"


@pytest.mark.parametrize(
    "participant",
    [
        object(),
        _Participant(metadata=""),
        _Participant(metadata="not json"),
        _Participant(metadata="[]"),
        _Participant(metadata='{"role":"responder"}'),
        _Participant(metadata='{"role":7}'),
        _Participant(identity=""),
        _Participant(identity=7),
    ],
)
def test_only_a_well_formed_clinician_participant_is_admitted(participant: object) -> None:
    # falsifier: malformed or responder metadata mutates clinician presence.
    assert clinician_identity(participant) is None


def test_clinician_identity_is_extracted_once_at_the_vendor_boundary() -> None:
    # falsifier: valid LiveKit metadata cannot enter the domain roster.
    assert clinician_identity(_Participant()) == "clinician-1"


def test_clinician_connection_requires_a_livekit_participant_generation() -> None:
    # falsifier: a stale disconnect can evict a newer connection with the same identity.
    assert clinician_connection(_Participant()) == ("clinician-1", "participant-1")
    assert clinician_connection(_Participant(sid="")) is None


def _requested_state() -> TriageState:
    state = TriageState(session_id="inc-test")
    state.request_escalation(
        "time-critical",
        key="inc-test:threshold",
        minimum_level=Criticality.SEVERE,
    )
    return state


def test_duplicate_active_events_are_idempotent() -> None:
    # falsifier: one LiveKit participant is counted twice and never fully leaves.
    roster = ClinicianRoster()
    assert roster.activate("clinician-1") is True
    assert roster.activate("clinician-1") is False
    assert roster.identities == frozenset({"clinician-1"})


def test_proactive_clinician_presence_does_not_invent_an_escalation() -> None:
    # falsifier: opening the doctor dashboard makes a Minor case look escalated.
    state = TriageState(session_id="inc-test")
    roster = ClinicianRoster()
    roster.activate("clinician-1")

    assert roster.reconcile(state) is False
    assert state.escalation is EscalationStatus.NOT_NEEDED


def test_a_requested_case_becomes_joined_when_a_clinician_is_active() -> None:
    # falsifier: a real participant is present while UI stays stuck on waiting.
    state = _requested_state()
    roster = ClinicianRoster()
    roster.activate("clinician-1")

    assert roster.reconcile(state) is True
    assert state.escalation is EscalationStatus.CLINICIAN_JOINED


def test_a_proactive_join_reconciles_when_escalation_is_requested_later() -> None:
    # falsifier: join-before-request and request-before-join behave differently.
    state = TriageState(session_id="inc-test")
    roster = ClinicianRoster()
    roster.activate("clinician-1")
    assert roster.reconcile(state) is False

    state.request_escalation("deterioration", key="inc-test:deterioration")

    assert roster.reconcile(state) is True
    assert state.clinician_present is True


def test_one_of_two_clinicians_leaving_does_not_mark_all_help_lost() -> None:
    # falsifier: roster is represented as a boolean rather than identities.
    state = _requested_state()
    roster = ClinicianRoster()
    roster.activate("clinician-1")
    roster.activate("clinician-2")
    roster.reconcile(state)

    assert roster.deactivate("clinician-1") is True
    assert roster.reconcile(state) is False
    assert state.escalation is EscalationStatus.CLINICIAN_JOINED


def test_the_last_clinician_leaving_marks_the_request_lost() -> None:
    # falsifier: a dropped clinician remains displayed as present.
    state = _requested_state()
    roster = ClinicianRoster()
    roster.activate("clinician-1")
    roster.reconcile(state)

    assert roster.deactivate("clinician-1") is True
    assert roster.reconcile(state) is True
    assert state.escalation is EscalationStatus.CLINICIAN_LOST


def test_an_unknown_disconnect_is_a_no_op() -> None:
    # falsifier: a responder disconnect changes the clinician lifecycle.
    state = _requested_state()
    roster = ClinicianRoster()
    assert roster.deactivate("responder-1") is False
    assert roster.reconcile(state) is False
    assert state.escalation is EscalationStatus.REQUESTED


def test_a_reconnecting_clinician_recovers_the_lost_state() -> None:
    # falsifier: CLINICIAN_LOST is absorbing even after a real rejoin.
    state = _requested_state()
    roster = ClinicianRoster()
    roster.activate("clinician-1")
    roster.reconcile(state)
    roster.deactivate("clinician-1")
    roster.reconcile(state)
    assert state.escalation is EscalationStatus.CLINICIAN_LOST

    roster.activate("clinician-1")
    assert roster.reconcile(state) is True
    assert state.escalation is EscalationStatus.CLINICIAN_JOINED


def test_stale_disconnect_does_not_remove_a_newer_participant_generation() -> None:
    # falsifier: an old participant disconnect hides the same clinician's new session.
    roster = ClinicianRoster()
    roster.activate("clinician-1", "old-sid")
    roster.activate("clinician-1", "new-sid")

    assert roster.deactivate("clinician-1", "old-sid") is False
    assert roster.is_active("clinician-1", "new-sid") is True


def test_snapshot_is_the_current_wire_state_and_numeric_fact_payload() -> None:
    # falsifier: late join receives stale/stringified fields or storage objects.
    state = _requested_state()
    fact = FactRecord(
        id="fact-1",
        text="tourniquet applied",
        kind=FactKind.INTERVENTION,
        seq=2,
        recorded_at="2026-09-19T10:00:00+00:00",
        elapsed_s=12.5,
        metadata={"speaker": "responder"},
    )

    payload = snapshot_payload(state, [fact])

    assert payload["state"] == state.to_payload()
    assert payload["findings"] == [fact.to_payload()]
    assert payload["timeline_status"] == "complete"
    assert payload["findings"][0]["seq"] == 2
    assert payload["findings"][0]["elapsed_s"] == 12.5


def test_snapshot_copies_inputs_instead_of_sharing_a_mutable_timeline() -> None:
    # falsifier: later context mutation rewrites a payload already being sent.
    state = _requested_state()
    facts: list[FactRecord] = []
    payload = snapshot_payload(state, facts)
    facts.append(
        FactRecord(
            id="late",
            text="late",
            kind=FactKind.OBSERVATION,
            seq=1,
            recorded_at="now",
            elapsed_s=1,
            metadata={},
        )
    )
    assert payload["findings"] == []
