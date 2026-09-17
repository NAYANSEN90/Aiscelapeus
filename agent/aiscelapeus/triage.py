"""Criticality model and the escalation decision.

PRD 5.3 asks for Level 1-5 detection and automatic bridging of a clinician above a
threshold. The levels are defined here rather than left to the model's judgement,
so the escalation rule is auditable and testable independently of the LLM.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any

from .clock import SYSTEM_CLOCK, Clock, isoformat

# The deterministic rule lives in phrases.py, where it can be exercised against
# the utterance corpus without constructing triage state. Re-exported because
# it is part of this module's published surface.
from .phrases import MARKERS, MarkerHit, find_markers, hard_escalation_triggered

__all__ = [
    "MARKERS",
    "Criticality",
    "EscalationStatus",
    "FactKind",
    "LevelChange",
    "MarkerHit",
    "TriageState",
    "find_markers",
    "hard_escalation_triggered",
]

logger = logging.getLogger(__name__)


class Criticality(IntEnum):
    MINOR = 1
    LOW = 2
    MODERATE = 3
    SEVERE = 4
    CRITICAL = 5

    @property
    def label(self) -> str:
        return {
            1: "Minor",
            2: "Low",
            3: "Moderate",
            4: "Severe",
            5: "Critical",
        }[int(self)]

    @property
    def definition(self) -> str:
        return {
            1: "Self-limiting. No transport needed. Example: small clean cut, graze.",
            2: "Needs care but not urgent. Example: simple fracture, minor burn.",
            3: "Needs professional assessment soon. Example: seizure that has stopped, "
            "moderate burn, suspected concussion.",
            4: "Time-critical. Clinician bridged. Example: uncontrolled bleeding, "
            "suspected stroke, anaphylaxis responding to adrenaline.",
            5: "Immediately life-threatening. Clinician bridged without delay. "
            "Example: cardiac arrest, drowning, airway obstruction with no air movement.",
        }[int(self)]


class FactKind(str, Enum):
    """What kind of thing was recorded into the incident timeline.

    An unrecognised kind used to be silently rewritten to "observation", so a
    typo became a plausible record filed under the wrong classification with no
    log line. These are the only legal values.

    Lives here rather than with the storage port because it is a domain
    classification: what the record says happened, not how it is stored.
    """

    VITAL = "vital"
    INTERVENTION = "intervention"
    OBSERVATION = "observation"
    SYMPTOM = "symptom"
    ESCALATION = "escalation"


class EscalationStatus(str, Enum):
    """Where the request for a human clinician has got to.

    This was a single boolean, which could express "asked" and "not asked" and
    nothing else. The three outcomes it could not express are the failure ones:
    a request nobody answered, and a clinician who joined and then dropped, both
    read as success. A dispatch mechanism has nowhere to report failure into
    unless those states exist.
    """

    NOT_NEEDED = "not_needed"
    REQUESTED = "requested"
    CLINICIAN_JOINED = "clinician_joined"
    CLINICIAN_LOST = "clinician_lost"
    FAILED_NO_RESPONSE = "failed_no_response"


@dataclass(frozen=True)
class LevelChange:
    """What a criticality assessment actually did.

    Returned instead of a bool because a bool conflated three outcomes: the
    level rose, the level was re-asserted with new reasoning, and a downgrade
    was refused. Callers branching on the bool skipped the durable write for the
    middle case, so the UI showed reasoning the record never received.
    """

    changed: bool
    previous: Criticality
    current: Criticality
    rejected: bool
    rationale_updated: bool
    reason: str

    @property
    def should_persist(self) -> bool:
        """Whether there is anything here worth writing to the record."""
        return self.changed or self.rationale_updated


@dataclass
class TriageState:
    """The live triage verdict, mirrored to the responder and doctor UIs."""

    session_id: str
    clock: Clock = SYSTEM_CLOCK
    level: Criticality = Criticality.MINOR
    rationale: str = "No assessment yet."
    escalation: EscalationStatus = EscalationStatus.NOT_NEEDED
    escalation_reason: str | None = None
    escalation_key: str | None = None
    clinician_requested_at: str | None = None
    updated_at: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    # Keys already dispatched. Survives reconstruction via `restore_escalation`
    # so a restart mid-incident does not page the on-call clinician twice.
    escalation_log: list[str] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = isoformat(self.clock.now())

    @property
    def label(self) -> str:
        return self.level.label

    @property
    def escalated(self) -> bool:
        """Whether a clinician has been asked for at all.

        Kept as a convenience for callers that only need the coarse answer;
        anything that distinguishes outcomes must read `escalation`.
        """
        return self.escalation is not EscalationStatus.NOT_NEEDED

    @property
    def clinician_present(self) -> bool:
        return self.escalation is EscalationStatus.CLINICIAN_JOINED

    def set_level(self, level: int, rationale: str, *, source: str = "model") -> LevelChange:
        """Record a criticality assessment.

        Criticality ratchets upward within an incident: once a case has been
        assessed as severe, a later calmer turn does not quietly downgrade it.
        Only an explicit clinician stand-down should lower it, which is out of
        scope for the field agent.

        A refused downgrade changes nothing at all. The previous implementation
        returned False for it but had already overwritten the rationale and
        appended a second history entry, so a calmer turn rewrote the reasoning
        behind a severe assessment while reporting that nothing happened.
        """
        try:
            requested = Criticality(int(level))
        except ValueError as exc:
            # A model emitting 7 or 0 is malfunctioning. Clamping hid that:
            # 7 became 5, 0 became 1, and nothing was ever investigated.
            raise ValueError(
                f"criticality must be 1-5, got {level!r}; "
                "a value outside the scale means the model is malfunctioning"
            ) from exc

        previous = self.level

        if requested < previous:
            logger.info(
                "Ignoring downgrade %d -> %d (%s); criticality ratchets upward",
                int(previous),
                int(requested),
                rationale,
            )
            return LevelChange(
                changed=False,
                previous=previous,
                current=previous,
                rejected=True,
                rationale_updated=False,
                reason="criticality ratchets upward; downgrade refused",
            )

        changed = requested != previous
        rationale_updated = rationale != self.rationale

        if not changed and not rationale_updated:
            # Nothing new was said. Recording it would pad the timeline with
            # repetitions of an assessment that never moved.
            return LevelChange(
                changed=False,
                previous=previous,
                current=previous,
                rejected=False,
                rationale_updated=False,
                reason="no change",
            )

        self.level = requested
        self.rationale = rationale
        self.updated_at = isoformat(self.clock.now())
        self.history.append(
            {
                "level": int(requested),
                "label": requested.label,
                "rationale": rationale,
                "source": source,
                "at": self.updated_at,
            }
        )
        return LevelChange(
            changed=changed,
            previous=previous,
            current=requested,
            rejected=False,
            rationale_updated=rationale_updated,
            reason="level raised" if changed else "reassessed at the same level",
        )

    # ------------------------------------------------------- escalation lifecycle

    def request_escalation(
        self,
        reason: str,
        *,
        key: str,
        minimum_level: Criticality = Criticality.SEVERE,
    ) -> EscalationStatus:
        """Ask for a human clinician. Idempotent on `key`.

        Raises the criticality to at least `minimum_level`: an incident that
        needs a clinician is not Minor. The explicit escalation tool previously
        left the level untouched, so the dashboard could show a green "Minor"
        badge directly above a red "Clinician bridged" banner.

        `key` identifies the request, not the object holding it. A boolean on
        an instance rebuilt at process start comes back False, so a restart
        mid-incident re-dispatched. A key that has already been seen does not
        dispatch again.
        """
        if key in self._seen_keys:
            logger.info("Escalation %s already dispatched; not repeating", key)
            return self.escalation

        self._seen_keys.add(key)

        if self.escalated:
            # Already escalated under a different key: record the key so a
            # replay is recognised, but do not dispatch or overwrite the reason.
            return self.escalation

        if self.level < minimum_level:
            self.set_level(
                int(minimum_level),
                f"escalation requested: {reason}",
                source="escalation",
            )

        self.escalation = EscalationStatus.REQUESTED
        self.escalation_reason = reason
        self.escalation_key = key
        self.clinician_requested_at = isoformat(self.clock.now())
        self.escalation_log.append(key)
        return self.escalation

    def restore_escalation(self, key: str | None, status: EscalationStatus) -> None:
        """Rehydrate escalation state after a restart, without re-dispatching."""
        self.escalation = status
        self.escalation_key = key
        if key is not None:
            self._seen_keys.add(key)

    def mark_clinician_joined(self) -> None:
        """A clinician is on the call."""
        if not self.escalated:
            raise ValueError("a clinician cannot join an incident that never requested one")
        self.escalation = EscalationStatus.CLINICIAN_JOINED
        self.updated_at = isoformat(self.clock.now())

    def mark_clinician_lost(self) -> None:
        """The clinician dropped. The responder is alone again and must be told."""
        self.escalation = EscalationStatus.CLINICIAN_LOST
        self.updated_at = isoformat(self.clock.now())

    def mark_escalation_failed(self) -> None:
        """Nobody answered. Distinct from nobody having been asked."""
        self.escalation = EscalationStatus.FAILED_NO_RESPONSE
        self.updated_at = isoformat(self.clock.now())

    def should_escalate(self, threshold: int) -> bool:
        return not self.escalated and self.level >= threshold

    # ------------------------------------------------------------------ payload

    def to_payload(self) -> dict[str, Any]:
        """The wire shape sent to both UIs.

        Built explicitly rather than by `asdict`, so a field added for internal
        bookkeeping does not silently become part of the contract, and so a
        field the UI requires cannot quietly stop being written.
        """
        return {
            "session_id": self.session_id,
            "level": int(self.level),
            "label": self.label,
            "definition": self.level.definition,
            "rationale": self.rationale,
            "escalation": self.escalation.value,
            "escalated": self.escalated,
            "clinician_present": self.clinician_present,
            "escalation_reason": self.escalation_reason,
            "clinician_requested_at": self.clinician_requested_at,
            "updated_at": self.updated_at,
            "history": list(self.history),
        }

    def to_json(self) -> bytes:
        return json.dumps(self.to_payload()).encode("utf-8")
