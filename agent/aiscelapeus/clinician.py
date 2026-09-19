"""Clinician participant presence and late-join snapshots.

LiveKit emits participant events; triage owns escalation state. This module is
the small domain seam between them. It deliberately imports no LiveKit type, so
malformed vendor objects are rejected at one boundary and the lifecycle can be
proved without a room or network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from .ports import FactRecord
from .triage import TriageState

SNAPSHOT_TOPIC = "triage.snapshot"
ParticipantRole = Literal["responder", "clinician"]


def participant_role(participant: object) -> ParticipantRole | None:
    """Parse the trusted role shape from untrusted participant metadata."""

    metadata = getattr(participant, "metadata", None)
    if not isinstance(metadata, str):
        return None
    try:
        decoded = json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    role = decoded.get("role")
    return role if role in ("responder", "clinician") else None


def clinician_identity(participant: object) -> str | None:
    """Return a clinician identity from checked LiveKit metadata.

    Metadata is untrusted JSON supplied when the participant's token is minted.
    A missing field, wrong shape, wrong role, or blank/non-string identity is a
    non-clinician rather than an exception inside the room event emitter.
    """

    identity = getattr(participant, "identity", None)
    if participant_role(participant) != "clinician":
        return None
    if not isinstance(identity, str) or not identity:
        return None
    return identity


def clinician_connection(participant: object) -> tuple[str, str] | None:
    """Return the authenticated identity plus this participant generation.

    LiveKit can deliver a stale disconnect after the same identity reconnects.
    Tracking only identity lets that old event evict the new connection. The
    participant SID is the generation token that makes the stale event a no-op.
    """

    identity = clinician_identity(participant)
    sid = getattr(participant, "sid", None)
    if identity is None or not isinstance(sid, str) or not sid:
        return None
    return identity, sid


@dataclass
class ClinicianRoster:
    """The identities currently active in one incident room.

    A set is load-bearing: LiveKit may repeat an active event, and two clinicians
    can overlap. A boolean cannot distinguish either case and marks help lost as
    soon as the first participant disconnects.
    """

    _connections: dict[str, str] = field(default_factory=dict)

    @property
    def identities(self) -> frozenset[str]:
        return frozenset(self._connections)

    @property
    def present(self) -> bool:
        return bool(self._connections)

    def activate(self, identity: str, connection_id: str | None = None) -> bool:
        if not identity:
            raise ValueError("clinician identity must not be blank")
        generation = connection_id or identity
        if self._connections.get(identity) == generation:
            return False
        self._connections[identity] = generation
        return True

    def deactivate(self, identity: str, connection_id: str | None = None) -> bool:
        active_generation = self._connections.get(identity)
        if active_generation is None:
            return False
        if connection_id is not None and active_generation != connection_id:
            return False
        del self._connections[identity]
        return True

    def is_active(self, identity: str, connection_id: str) -> bool:
        return self._connections.get(identity) == connection_id

    def reconcile(self, state: TriageState) -> bool:
        """Make escalation presence agree with the active identity set.

        Presence alone never invents an escalation. Once a request exists, any
        active clinician means joined; when the last one leaves, joined becomes
        lost. Returns whether the state changed so callers know whether a
        broadcast is warranted.
        """

        if self.present:
            if state.escalated and not state.clinician_present:
                state.mark_clinician_joined()
                return True
            return False

        if state.clinician_present:
            state.mark_clinician_lost()
            return True
        return False


def snapshot_payload(
    state: TriageState,
    timeline: Iterable[FactRecord],
    *,
    timeline_status: Literal["complete", "unavailable"] = "complete",
) -> dict[str, Any]:
    """Build the complete event a late clinician receives.

    `to_payload` is the one wire-shape owner for both state and facts. Eager list
    construction prevents a live timeline iterator from changing underneath an
    in-flight publish.
    """

    return {
        "state": state.to_payload(),
        "findings": [fact.to_payload() for fact in timeline],
        "timeline_status": timeline_status,
    }
