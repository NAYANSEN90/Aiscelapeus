"""Criticality model and the escalation decision.

PRD 5.3 asks for Level 1-5 detection and automatic bridging of a clinician above a
threshold. The levels are defined here rather than left to the model's judgement,
so the escalation rule is auditable and testable independently of the LLM.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

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


# Phrases that force Level 5 regardless of what the model concluded. This is a
# deterministic safety net, not the primary path -- the model normally gets there
# first. It exists so that a model failure cannot silently downgrade an arrest.
HARD_ESCALATION_MARKERS: tuple[str, ...] = (
    "not breathing",
    "isn't breathing",
    "is not breathing",
    "stopped breathing",
    "no pulse",
    "cardiac arrest",
    "unresponsive",
    "not responding",
    "turning blue",
    "gone blue",
    "can't breathe",
    "cannot breathe",
    "drowning",
    "drowned",
    "choking and can't",
)


def hard_escalation_triggered(utterance: str) -> str | None:
    """Return the marker that forces Level 5, if the utterance contains one."""
    lowered = utterance.lower()
    for marker in HARD_ESCALATION_MARKERS:
        if marker in lowered:
            return marker
    return None


@dataclass
class TriageState:
    """The live triage verdict, mirrored to the responder and doctor UIs."""

    session_id: str
    level: int = 1
    label: str = "Minor"
    rationale: str = "No assessment yet."
    escalated: bool = False
    escalation_reason: str | None = None
    clinician_requested_at: str | None = None
    distress_score: float = 0.0
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )
    history: list[dict[str, Any]] = field(default_factory=list)

    def set_level(self, level: int, rationale: str, *, source: str = "model") -> bool:
        """Record a new criticality. Returns True if the level actually changed.

        Criticality ratchets upward within an incident: once a case has been
        assessed as severe, a later calmer turn does not quietly downgrade it.
        Only an explicit clinician stand-down should lower it, which is out of
        scope for the field agent.
        """
        level = max(1, min(5, int(level)))
        previous = self.level
        if level < previous:
            logger.info(
                "Ignoring downgrade %d -> %d (%s); criticality ratchets upward",
                previous,
                level,
                rationale,
            )
            return False

        self.level = level
        self.label = Criticality(level).label
        self.rationale = rationale
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.history.append(
            {
                "level": level,
                "label": self.label,
                "rationale": rationale,
                "source": source,
                "at": self.updated_at,
            }
        )
        return level != previous

    def mark_escalated(self, reason: str) -> bool:
        """Flag that a clinician has been requested. Idempotent."""
        if self.escalated:
            return False
        self.escalated = True
        self.escalation_reason = reason
        self.clinician_requested_at = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )
        return True

    def should_escalate(self, threshold: int) -> bool:
        return not self.escalated and self.level >= threshold

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["definition"] = Criticality(self.level).definition
        return data

    def to_json(self) -> bytes:
        return json.dumps(self.to_payload()).encode("utf-8")
