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
    "Provenance",
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


class Provenance(str, Enum):
    """Whether a criticality level rests on evidence or on an assumption.

    ASM-14, and the reason it needed real design work rather than a flag. The
    ratchet below latches, which is correct for its original purpose: a calmer
    later turn must not quietly downgrade a severe incident. But it latched
    assumptions too, so a patient assumed-worst to 5 at minute one could not be
    corrected at minute three *even by a clinician on the bridge*. Assumption-
    driven and evidence-driven CRITICAL then became indistinguishable at the
    ratchet - the exact conflation the certainty type in `assessment.py` exists
    to prevent, in the one place that never read it.

    Two members, not five. `assessment.Certainty` has five and is the richer
    type; the ratchet needs only the line between "somebody reported this" and
    "nobody did, and we took the worse branch on purpose". A third member here
    would imply an ordering among assumptions that nothing has defined.

    ASSUMPTION is the DEFAULT, deliberately. The safe failure for a caller that
    has not thought about provenance is a level that a later genuine finding can
    correct, not one welded in place by an omission. The exception is stated
    where it matters: `set_level`'s callers that represent real reported speech
    pass EVIDENCE explicitly, and L1 is one of them.
    """

    ASSUMPTION = "assumption"
    EVIDENCE = "evidence"

    @property
    def is_correctable(self) -> bool:
        """Whether a later evidence-grade finding may lower a level from this."""
        return self is Provenance.ASSUMPTION


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
    #: True when this call LOWERED the level because the previous one rested on
    #: an assumption and this one carries evidence. A distinct outcome from both
    #: "raised" and "refused": it is the only way the level ever falls, and a
    #: clinician reading the record needs to see that a correction happened
    #: rather than infer it from two history entries. ASM-14.
    corrected: bool = False
    #: What the level now rests on. An ASSUMPTION level stays correctable; an
    #: EVIDENCE level is final for the incident.
    provenance: Provenance = Provenance.ASSUMPTION

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
    #: What the current level rests on. ASM-14. A fresh incident's MINOR is an
    #: assumption - nobody has assessed anything - which is why the default is
    #: ASSUMPTION rather than EVIDENCE. It makes no practical difference at
    #: MINOR, since there is nothing below it to correct down to, but stating it
    #: the other way round would make the floor read as a finding.
    level_provenance: Provenance = Provenance.ASSUMPTION
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

    def set_level(
        self,
        level: int,
        rationale: str,
        *,
        source: str = "model",
        provenance: Provenance = Provenance.ASSUMPTION,
    ) -> LevelChange:
        """Record a criticality assessment.

        Criticality ratchets upward within an incident: once a case has been
        assessed as severe, a later calmer turn does not quietly downgrade it.

        A refused downgrade changes nothing at all. The previous implementation
        returned False for it but had already overwritten the rationale and
        appended a second history entry, so a calmer turn rewrote the reasoning
        behind a severe assessment while reporting that nothing happened.

        THE ONE EXCEPTION, AND WHY IT IS NARROW (ASM-14)
        ------------------------------------------------
        A level reached by ASSUMPTION is correctable by later EVIDENCE. A level
        reached by evidence is not correctable by anything.

        The defect this closes: the ratchet latched assumptions, so a patient
        assumed-worst to 5 at minute one could not be corrected at minute three
        even by a clinician who had since established the finding. That made
        assumption-driven and evidence-driven CRITICAL indistinguishable at the
        one place the distinction has consequences.

        Note the asymmetry, which is the whole safety argument. This does NOT
        make the ratchet negotiable:

        - evidence lowering an assumption is a CORRECTION - somebody finally
          established the thing we had been guessing at, and refusing it means
          the record permanently overstates a patient nobody ever assessed;
        - assumption lowering evidence is REFUSED - a guess does not overturn a
          report, so the most dangerous direction is closed;
        - evidence lowering evidence is REFUSED - two reports disagreeing is not
          a correction, it is a deterioration or a recovery narrative, and
          `phrases.py` owns the second of those. Standing an incident down on
          conflicting reports is a clinician's call and stays out of scope;
        - assumption lowering assumption is REFUSED - unchanged behaviour, and
          the case every existing caller is in.

        So the default is ASSUMPTION for the requesting side and the guard reads
        the STORED provenance. A caller that passes nothing can neither perform
        a correction nor suffer one, which is why every existing call site
        behaves exactly as before.

        L1 KEEPS OUTRANKING L2. A `phrases.py` marker fires on raw speech, and
        what it establishes is evidence of what was SAID - the strongest input
        this system has, and not something L2's arithmetic may revise.
        `escalation.py` passes `Provenance.EVIDENCE`, so a marker-driven
        CRITICAL is uncorrectable by anything L2 later computes. That ordering is
        asserted by a test rather than left to this paragraph.
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
        previous_provenance = self.level_provenance

        correcting = (
            requested < previous
            and previous_provenance.is_correctable
            and provenance is Provenance.EVIDENCE
        )

        if requested < previous and not correcting:
            logger.info(
                "Ignoring downgrade %d -> %d (%s); criticality ratchets upward "
                "(stored provenance=%s, requested provenance=%s)",
                int(previous),
                int(requested),
                rationale,
                previous_provenance.value,
                provenance.value,
            )
            return LevelChange(
                changed=False,
                previous=previous,
                current=previous,
                rejected=True,
                rationale_updated=False,
                reason="criticality ratchets upward; downgrade refused",
                corrected=False,
                provenance=previous_provenance,
            )

        changed = requested != previous
        rationale_updated = rationale != self.rationale
        # A re-assertion at the same level that HARDENS an assumption into
        # evidence is a real event even when nothing else moved: it is the point
        # after which the level can no longer be corrected downward. Recording
        # it is what keeps the record's account of correctability accurate.
        provenance_hardened = (
            not changed
            and previous_provenance.is_correctable
            and provenance is Provenance.EVIDENCE
        )

        if not changed and not rationale_updated and not provenance_hardened:
            # Nothing new was said. Recording it would pad the timeline with
            # repetitions of an assessment that never moved.
            return LevelChange(
                changed=False,
                previous=previous,
                current=previous,
                rejected=False,
                rationale_updated=False,
                reason="no change",
                corrected=False,
                provenance=previous_provenance,
            )

        self.level = requested
        self.rationale = rationale
        # Evidence hardens the level permanently. An assumption arriving on top
        # of an already-evidenced level must NOT soften it back into something
        # correctable, or a later guess could unlock a downgrade of a finding.
        # `max` over the two is wrong here - the rule is one-way, so it is
        # written as the one-way rule it is.
        if provenance is Provenance.EVIDENCE or previous_provenance.is_correctable:
            self.level_provenance = provenance
        self.updated_at = isoformat(self.clock.now())
        self.history.append(
            {
                "level": int(requested),
                "label": requested.label,
                "rationale": rationale,
                "source": source,
                "provenance": self.level_provenance.value,
                "corrected": correcting,
                "at": self.updated_at,
            }
        )
        if correcting:
            reason = "assumption corrected by evidence"
        elif changed:
            reason = "level raised"
        elif provenance_hardened:
            reason = "reassessed at the same level; assumption hardened to evidence"
        else:
            reason = "reassessed at the same level"
        return LevelChange(
            changed=changed,
            previous=previous,
            current=requested,
            rejected=False,
            rationale_updated=rationale_updated,
            reason=reason,
            corrected=correcting,
            provenance=self.level_provenance,
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
            # ASM-14 / ARCHITECTURE-ASSESSMENT.md §6: a CRITICAL reached because
            # a finding was genuinely established and a CRITICAL reached because
            # nothing could be established are clinically different events that
            # would otherwise look identical on both UIs. The clinician joining
            # the bridge needs to know which one they are walking into, and
            # whether what they are looking at is still correctable.
            "level_provenance": self.level_provenance.value,
            "level_correctable": self.level_provenance.is_correctable,
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
