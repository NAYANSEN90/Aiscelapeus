"""LiveKit room-event wiring for clinician presence and late join.

Falsifier: the pure roster is correct but production never subscribes to the
events, sends a snapshot before the participant can receive data, or lets a
timeline/publish failure escape the room emitter and take down the call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable

import pytest

pytest.importorskip(
    "livekit.agents",
    reason="aiscelapeus.main imports the LiveKit SDK; installed via .[dev] in CI",
)

from aiscelapeus.clinician import ClinicianRoster  # noqa: E402
from aiscelapeus.main import (  # noqa: E402
    make_clinician_active_handler,
    make_clinician_disconnected_handler,
    seed_active_participants,
    wait_for_role,
)
from aiscelapeus.ports import FactRecord  # noqa: E402
from aiscelapeus.triage import (  # noqa: E402
    Criticality,
    EscalationStatus,
    FactKind,
    TriageState,
)


@dataclass
class _Participant:
    identity: str
    metadata: str = '{"role":"clinician"}'
    sid: str = "participant-sid"
    state: int = 2


@dataclass
class _RoomCollaborators:
    state: TriageState
    roster: ClinicianRoster = field(default_factory=ClinicianRoster)
    facts: list[FactRecord] = field(default_factory=list)
    spawned: list[Awaitable[None]] = field(default_factory=list)
    snapshots: list[tuple[str, dict]] = field(default_factory=list)
    broadcasts: int = 0
    timeline_error: Exception | None = None
    snapshot_error: Exception | None = None
    snapshot_failures_remaining: int = 0
    snapshot_attempts: int = 0

    def spawn(self, work: Awaitable[None]) -> None:
        self.spawned.append(work)

    async def timeline(self) -> list[FactRecord]:
        if self.timeline_error is not None:
            raise self.timeline_error
        return list(self.facts)

    async def broadcast(self) -> None:
        self.broadcasts += 1

    async def publish_snapshot(self, identity: str, payload: dict) -> None:
        self.snapshot_attempts += 1
        if self.snapshot_failures_remaining:
            self.snapshot_failures_remaining -= 1
            raise RuntimeError("transient data channel failure")
        if self.snapshot_error is not None:
            raise self.snapshot_error
        self.snapshots.append((identity, payload))

    async def no_retry_wait(self, _delay: float) -> None:
        return None

    async def drain(self) -> None:
        pending, self.spawned = self.spawned, []
        for work in pending:
            await work

    def active_handler(self):
        return make_clinician_active_handler(
            roster=self.roster,
            state=self.state,
            timeline=self.timeline,
            on_state_change=self.broadcast,
            publish_snapshot=self.publish_snapshot,
            spawn=self.spawn,
            retry_delay=self.no_retry_wait,
        )

    def disconnected_handler(self):
        return make_clinician_disconnected_handler(
            roster=self.roster,
            state=self.state,
            on_state_change=self.broadcast,
            spawn=self.spawn,
        )


def _requested() -> TriageState:
    state = TriageState(session_id="inc-live")
    state.request_escalation(
        "not breathing",
        key="inc-live:not_breathing",
        minimum_level=Criticality.CRITICAL,
    )
    return state


async def test_active_clinician_marks_joined_and_receives_a_targeted_snapshot() -> None:
    # falsifier: participant is present but state/timeline never reaches them.
    fact = FactRecord(
        id="fact-1",
        text="CPR started",
        kind=FactKind.INTERVENTION,
        seq=1,
        recorded_at="now",
        elapsed_s=9.0,
        metadata={},
    )
    collaborators = _RoomCollaborators(state=_requested(), facts=[fact])

    collaborators.active_handler()(_Participant("clinician-1"))
    assert collaborators.state.escalation is EscalationStatus.CLINICIAN_JOINED
    await collaborators.drain()

    assert collaborators.broadcasts == 1
    assert len(collaborators.snapshots) == 1
    identity, payload = collaborators.snapshots[0]
    assert identity == "clinician-1", "catch-up must not rebroadcast PHI to every participant"
    assert payload["state"]["clinician_present"] is True
    assert payload["findings"] == [fact.to_payload()]
    assert payload["timeline_status"] == "complete"


async def test_active_event_is_idempotent_and_does_not_duplicate_the_snapshot() -> None:
    # falsifier: repeated SDK readiness emits duplicate timeline cards.
    collaborators = _RoomCollaborators(state=_requested())
    handler = collaborators.active_handler()
    participant = _Participant("clinician-1")
    handler(participant)
    handler(participant)
    await collaborators.drain()
    assert len(collaborators.snapshots) == 1


async def test_proactive_clinician_gets_context_without_inventing_escalation() -> None:
    # falsifier: a clinician opening early changes a Minor incident to Severe.
    collaborators = _RoomCollaborators(state=TriageState(session_id="inc-live"))
    collaborators.active_handler()(_Participant("clinician-1"))
    await collaborators.drain()
    assert collaborators.state.escalation is EscalationStatus.NOT_NEEDED
    assert collaborators.broadcasts == 0
    assert collaborators.snapshots[0][1]["state"]["escalated"] is False


async def test_non_clinician_or_malformed_metadata_schedules_nothing() -> None:
    # falsifier: a responder receives a clinician-only incident snapshot.
    collaborators = _RoomCollaborators(state=_requested())
    handler = collaborators.active_handler()
    handler(_Participant("responder-1", '{"role":"responder"}'))
    handler(_Participant("unknown", "bad json"))
    assert collaborators.spawned == []
    assert collaborators.roster.identities == frozenset()


async def test_timeline_failure_still_sends_current_state_with_no_fabricated_facts() -> None:
    # falsifier: a Moss read failure makes a present clinician invisible.
    collaborators = _RoomCollaborators(
        state=_requested(), timeline_error=RuntimeError("Moss unavailable")
    )
    collaborators.active_handler()(_Participant("clinician-1"))
    await collaborators.drain()
    assert collaborators.snapshots[0][1]["state"]["clinician_present"] is True
    assert collaborators.snapshots[0][1]["findings"] == []
    assert collaborators.snapshots[0][1]["timeline_status"] == "unavailable"


async def test_existing_room_participants_are_seeded_without_duplicate_snapshot() -> None:
    # falsifier: a clinician who joined before worker startup never receives catch-up.
    collaborators = _RoomCollaborators(state=_requested())
    participant = _Participant("clinician-1", sid="existing-sid")
    handler = collaborators.active_handler()

    seed_active_participants([participant], handler)
    handler(participant)  # event/enumeration race
    await collaborators.drain()

    assert collaborators.state.clinician_present is True
    assert len(collaborators.snapshots) == 1


async def test_joined_participant_waits_for_real_active_event_before_snapshot() -> None:
    # falsifier: startup enumeration suppresses the later event that makes data receivable.
    collaborators = _RoomCollaborators(state=_requested())
    participant = _Participant("clinician-1", sid="warming-up", state=1)
    handler = collaborators.active_handler()

    seed_active_participants([participant], handler)
    assert collaborators.spawned == []
    assert collaborators.roster.identities == frozenset()

    participant.state = 2
    handler(participant)
    await collaborators.drain()
    assert len(collaborators.snapshots) == 1


async def test_stale_disconnect_after_reconnect_keeps_new_generation_active() -> None:
    # falsifier: an identity ABA race marks the newly reconnected clinician lost.
    collaborators = _RoomCollaborators(state=_requested())
    old = _Participant("clinician-1", sid="old-sid")
    new = _Participant("clinician-1", sid="new-sid")
    active = collaborators.active_handler()
    active(old)
    active(new)

    collaborators.disconnected_handler()(old)
    await collaborators.drain()

    assert collaborators.roster.is_active("clinician-1", "new-sid") is True
    assert collaborators.state.escalation is EscalationStatus.CLINICIAN_JOINED
    assert len(collaborators.snapshots) == 1


async def test_snapshot_publish_failure_is_contained_inside_the_handler() -> None:
    # falsifier: a dead data channel raises through LiveKit's room emitter.
    collaborators = _RoomCollaborators(
        state=_requested(), snapshot_error=RuntimeError("data channel down")
    )
    collaborators.active_handler()(_Participant("clinician-1"))
    await collaborators.drain()
    assert collaborators.state.clinician_present is True
    assert collaborators.snapshot_attempts == 3


async def test_transient_snapshot_publish_failure_is_retried_to_delivery() -> None:
    # falsifier: one lossy data-channel moment leaves the clinician waiting forever.
    collaborators = _RoomCollaborators(
        state=_requested(), snapshot_failures_remaining=1
    )
    collaborators.active_handler()(_Participant("clinician-1"))
    await collaborators.drain()
    assert collaborators.snapshot_attempts == 2
    assert len(collaborators.snapshots) == 1


async def test_last_clinician_disconnect_marks_lost_and_broadcasts() -> None:
    # falsifier: responder UI keeps claiming a disconnected clinician is present.
    collaborators = _RoomCollaborators(state=_requested())
    participant = _Participant("clinician-1")
    collaborators.active_handler()(participant)
    await collaborators.drain()

    collaborators.disconnected_handler()(participant)
    assert collaborators.state.escalation is EscalationStatus.CLINICIAN_LOST
    await collaborators.drain()
    assert collaborators.broadcasts == 2


async def test_one_of_two_clinician_disconnects_does_not_broadcast_lost() -> None:
    # falsifier: the first departure hides a second active clinician.
    collaborators = _RoomCollaborators(state=_requested())
    active = collaborators.active_handler()
    active(_Participant("clinician-1"))
    active(_Participant("clinician-2"))
    await collaborators.drain()
    before = collaborators.broadcasts

    collaborators.disconnected_handler()(_Participant("clinician-1"))
    await collaborators.drain()
    assert collaborators.state.clinician_present is True
    assert collaborators.broadcasts == before


async def test_disconnect_before_active_work_runs_sends_no_stale_snapshot() -> None:
    # falsifier: a departed participant is sent PHI after a fast connect/drop race.
    collaborators = _RoomCollaborators(state=_requested())
    participant = _Participant("clinician-1")
    collaborators.active_handler()(participant)
    collaborators.disconnected_handler()(participant)
    await collaborators.drain()
    assert collaborators.snapshots == []
    assert collaborators.state.escalation is EscalationStatus.CLINICIAN_LOST


class _EventRoom:
    def __init__(self, participants: list[_Participant]) -> None:
        self.remote_participants = {p.identity: p for p in participants}
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler):
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler) -> None:
        self.handlers[event].remove(handler)

    def emit(self, event: str, participant: _Participant) -> None:
        self.remote_participants[participant.identity] = participant
        for handler in list(self.handlers.get(event, [])):
            handler(participant)


async def test_clinician_first_worker_waits_for_the_responder_identity() -> None:
    # falsifier: clinician-first dispatch binds STT to the doctor and never hears the responder.
    room = _EventRoom([_Participant("clinician-1")])
    pending = asyncio.create_task(wait_for_role(room, "responder"))
    await asyncio.sleep(0)
    assert pending.done() is False

    responder = _Participant(
        "responder-1", metadata='{"role":"responder"}', sid="responder-sid"
    )
    room.emit("participant_active", responder)

    assert await pending == "responder-1"


async def test_existing_active_responder_is_selected_without_waiting() -> None:
    # falsifier: responder-first dispatch waits for an event that already happened and hangs.
    responder = _Participant(
        "responder-1", metadata='{"role":"responder"}', sid="responder-sid"
    )
    room = _EventRoom([responder])
    assert await wait_for_role(room, "responder") == "responder-1"
