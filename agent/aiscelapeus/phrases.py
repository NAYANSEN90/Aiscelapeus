"""The deterministic escalation rule.

A phrase indicating a life threat forces Level 5 regardless of what the model
concluded. This is the system's most defensible safety property, and it is
deliberately not a model call: putting a model in front of the net that exists
to catch model failure reintroduces the failure mode.

Three things this has to get right that naive substring matching did not:

Normalisation. Deepgram runs with smart_format enabled, which emits typographic
punctuation, so "isn't breathing" arrives with U+2019 and never matched a
pattern written with a straight apostrophe. Text is normalised before matching.

Coverage. Matching literal strings meant "unconscious", "no heartbeat", "barely
breathing", "out cold" and "went into arrest" - all ordinary things to say -
scored as nothing at all. Markers are patterns over the ways a finding is
actually reported, not the one way it was first written down.

Negation. "The casualty is NOT unresponsive" contains "unresponsive", and
substring matching pinned the incident at Critical irreversibly. A negation
window before the match suppresses it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Curly quotes and dashes that STT emits and pattern authors do not type.
_PUNCTUATION_FOLD = str.maketrans(
    {
        "’": "'",  # right single quotation mark
        "‘": "'",  # left single quotation mark
        "ʼ": "'",  # modifier letter apostrophe
        "′": "'",  # prime
        "“": '"',
        "”": '"',
        "–": "-",  # en dash
        "—": "-",  # em dash
    }
)

_WHITESPACE = re.compile(r"\s+")

# Words that flip the meaning of a finding reported after them. Scoped tightly:
# this window is why "he was not breathing but now he is breathing again" does
# not escalate, and it is also why the window must be short enough that "not
# breathing" itself still fires.
_NEGATORS = (
    "not",
    "no",
    "never",
    "isn't",
    "wasn't",
    "aren't",
    "doesn't",
    "didn't",
    "hasn't",
    "without",
)

# How far back from a match to look for a negator, in words.
_NEGATION_WINDOW = 3


def normalize_for_match(text: str) -> str:
    """Fold text into the one form patterns are written against.

    NFKC unifies compatibility forms, punctuation is folded to ASCII, case is
    dropped, and runs of whitespace collapse. Idempotent: normalising an
    already-normalised string returns it unchanged.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION_FOLD)
    return _WHITESPACE.sub(" ", folded).strip().casefold()


@dataclass(frozen=True)
class Marker:
    """One life-threatening finding and the ways it gets reported."""

    marker_id: str
    pattern: re.Pattern[str]
    description: str


@dataclass(frozen=True)
class MarkerHit:
    """Where a marker matched, so an escalation can be explained afterwards."""

    marker_id: str
    matched_text: str
    span: tuple[int, int]
    negated: bool = False


def _marker(marker_id: str, alternatives: str, description: str) -> Marker:
    # \b on both ends keeps "arrest" from matching inside "arrested" and stops
    # a marker firing on a word fragment.
    return Marker(
        marker_id=marker_id,
        pattern=re.compile(rf"\b(?:{alternatives})\b"),
        description=description,
    )


MARKERS: tuple[Marker, ...] = (
    _marker(
        "not_breathing",
        r"(?:is\s+)?not\s+breathing"
        r"|isn't\s+breathing"
        r"|ain't\s+breathing"
        r"|stopped\s+breathing"
        r"|has\s+stopped\s+breathing"
        r"|no\s+longer\s+breathing"
        r"|wasn't\s+breathing"
        r"|not\s+breathing\s+at\s+all",
        "No respiratory effort.",
    ),
    _marker(
        "inadequate_breathing",
        r"barely\s+breathing"
        r"|hardly\s+breathing"
        r"|agonal(?:\s+breathing|\s+respirations?)?"
        r"|gasping(?:\s+for\s+(?:air|breath))?"
        r"|making\s+gasping\s+(?:noises|sounds)"
        r"|gulping\s+for\s+air",
        "Agonal or inadequate breathing - an arrest presentation.",
    ),
    _marker(
        "no_pulse",
        r"no\s+pulse"
        r"|can(?:'t|not)\s+find\s+a\s+pulse"
        r"|can(?:'t|not)\s+feel\s+a\s+pulse"
        r"|cannot\s+find\s+a\s+pulse"
        r"|no\s+heart\s*beat"
        r"|can(?:'t|not)\s+feel\s+a\s+heart\s*beat"
        r"|can(?:'t|not)\s+find\s+a\s+heart\s*beat"
        r"|pulseless",
        "No palpable circulation.",
    ),
    _marker(
        "cardiac_arrest",
        r"cardiac\s+arrest"
        r"|went\s+into\s+arrest"
        r"|gone\s+into\s+arrest"
        r"|in\s+arrest"
        r"|heart\s+(?:has\s+)?stopped",
        "Cardiac arrest, reported as such.",
    ),
    _marker(
        "unresponsive",
        r"unresponsive"
        r"|unconscious"
        r"|not\s+responding(?!\s+to\s+the\b)"
        r"|isn't\s+responding(?!\s+to\s+the\b)"
        r"|won't\s+respond"
        r"|out\s+cold"
        r"|passed\s+out"
        r"|won't\s+wake\s+up"
        r"|isn't\s+moving"
        r"|not\s+moving",
        "No response to voice or pain.",
    ),
    _marker(
        "cyanosis",
        r"turning\s+blue"
        r"|gone\s+blue"
        r"|going\s+blue"
        r"|blue\s+around\s+the\s+(?:lips|mouth)"
        r"|lips\s+are\s+blue"
        r"|blue\s+lips",
        "Cyanosis - failing oxygenation.",
    ),
    _marker(
        "cannot_breathe",
        r"can(?:'t|not)\s+breathe"
        r"|can(?:'t|not)\s+get\s+(?:any\s+)?air"
        r"|choking\s+and\s+can(?:'t|not)"
        r"|airway\s+(?:is\s+)?blocked",
        "Obstructed airway.",
    ),
    _marker(
        "drowning",
        r"drowning|drowned|pulled\s+(?:him|her|them|the\s+\w+)\s+out\s+of\s+the\s+water",
        "Drowning - treat as arrest until proven otherwise.",
    ),
)


def _is_negated(normalised: str, start: int) -> bool:
    """Whether a negator appears in the few words before `start`.

    Only the words immediately preceding the finding count. A negator further
    back usually belongs to a different clause.
    """
    preceding = normalised[:start].split()
    if not preceding:
        return False
    window = preceding[-_NEGATION_WINDOW:]
    return any(word.strip(",.;:") in _NEGATORS for word in window)


def _contradicted_later(normalised: str, end: int) -> bool:
    """Whether the responder corrects the finding immediately after reporting it.

    "he was not breathing but now he is breathing again" and "he was
    unresponsive but he's awake now" are narratives of recovery. The finding is
    in the past and the sentence says so.
    """
    remainder = normalised[end:]
    if not remainder.strip():
        return False
    return bool(
        re.search(
            r"\b(?:but|now|then)\b.{0,40}?"
            r"\b(?:breathing|awake|responding|conscious|talking|alert|fine|ok|okay)\b",
            remainder,
        )
    )


def _past_tense_before(normalised: str, start: int) -> bool:
    """Whether the finding is reported in the past tense ("he was unresponsive")."""
    preceding = normalised[:start].split()
    if not preceding:
        return False
    return preceding[-1] in ("was", "were", "had")


def find_markers(text: str) -> tuple[MarkerHit, ...]:
    """Every life-threat marker present in the utterance, in pattern order.

    Negated and self-contradicted matches are dropped rather than returned with
    a flag: the caller's question is "is this a life threat", and a suppressed
    match is not one.
    """
    if not text or not text.strip():
        return ()

    normalised = normalize_for_match(text)
    hits: list[MarkerHit] = []

    for marker in MARKERS:
        match = marker.pattern.search(normalised)
        if match is None:
            continue

        start, end = match.span()
        if _is_negated(normalised, start):
            continue
        if _past_tense_before(normalised, start) and _contradicted_later(normalised, end):
            continue
        if _contradicted_later(normalised, end) and re.search(
            r"\b(?:was|were|had)\b", normalised[:start]
        ):
            continue

        hits.append(
            MarkerHit(
                marker_id=marker.marker_id,
                matched_text=match.group(0),
                span=(start, end),
            )
        )

    return tuple(hits)


def hard_escalation_triggered(text: str) -> MarkerHit | None:
    """The first life-threat marker in the utterance, or None."""
    hits = find_markers(text)
    return hits[0] if hits else None
