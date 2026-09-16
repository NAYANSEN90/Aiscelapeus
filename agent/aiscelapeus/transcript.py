"""The speech-to-text edge, as a pure function.

Two problems live here. The first is testability: the STT event is an opaque
SDK object read with `getattr`, so nothing downstream of it could be tested
without standing up a LiveKit session.

The second is safety, and it is the more serious one. The deterministic
escalation rule - the net that is supposed to outrank the model - had exactly
one call site, inside a tool, fed by text the *model* chose to pass. Meanwhile
every raw final transcript arrived here and was published to the UI and
otherwise discarded. A responder saying "he's not breathing" only escalated if
the model decided to call `record_finding` with that phrasing. Normalising the
event here is what lets the rule run on what was actually said.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .clock import SYSTEM_CLOCK, Clock

# Diarization labels speakers by index, not by role. The responder is whoever
# is holding the phone, which is the first speaker the session hears; anyone
# else on scene is a bystander whose words must not be attributed to them.
RESPONDER = "responder"
BYSTANDER = "bystander"
UNKNOWN_SPEAKER = "unknown"


@dataclass(frozen=True)
class Utterance:
    """One final transcript, normalised away from the SDK's event shape."""

    text: str
    speaker: str
    is_final: bool
    at: datetime
    speaker_index: int | None = None

    @property
    def is_responder(self) -> bool:
        return self.speaker == RESPONDER


def speaker_for(index: int | None) -> str:
    """Map a diarization speaker index onto a role.

    Speaker 0 is the responder - they initiated the call. Any other index is a
    bystander or the patient. Previously every utterance was labelled
    "responder" regardless, so a bystander's panic could be recorded as a
    responder's clinical report.
    """
    if index is None:
        return UNKNOWN_SPEAKER
    return RESPONDER if index == 0 else BYSTANDER


def normalize_transcript(event: Any, *, clock: Clock = SYSTEM_CLOCK) -> Utterance | None:  # noqa: ANN401 - SDK event type
    """Turn an STT event into an `Utterance`, or None if there is nothing to record.

    Returns None for interim results and for empty or whitespace-only text.
    Reads defensively because the event is a vendor type that has changed shape
    between SDK versions.
    """
    if event is None:
        return None

    if not bool(getattr(event, "is_final", False)):
        return None

    raw_text = getattr(event, "transcript", None) or ""
    if not isinstance(raw_text, str):
        return None

    text = raw_text.strip()
    if not text:
        return None

    index = _speaker_index(event)
    return Utterance(
        text=text,
        speaker=speaker_for(index),
        is_final=True,
        at=clock.now(),
        speaker_index=index,
    )


def _speaker_index(event: Any) -> int | None:  # noqa: ANN401 - SDK event type
    """Best-effort read of the diarization speaker index."""
    for attribute in ("speaker_id", "speaker", "speaker_index"):
        value = getattr(event, attribute, None)
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            digits = value.strip().removeprefix("speaker_").removeprefix("SPEAKER_")
            if digits.isdigit():
                return int(digits)
    return None
