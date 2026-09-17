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
substring matching pinned the incident at Critical irreversibly. A negator
that actually governs the match suppresses it. "Actually governs" is the whole
difficulty, and a flat word-count window got it wrong in both directions - see
`_is_negated`.
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

# Words that flip the meaning of a finding reported after them.
_NEGATORS = frozenset(
    {
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
    }
)

# Punctuation stripped off a token before it is classified, so a negator or a
# function word fused to a comma or a dash is still recognised as one.
_TRAILING_PUNCTUATION = ",.;:!?-'\""

# Words a negator may reach ACROSS and still govern the finding. Strictly
# function words that cannot begin a new predicate on their own: determiners,
# degree adverbs, and the auxiliaries a negator takes as its own verb group
# ("never HAD a cardiac arrest", "has not BEEN breathing").
#
# What is deliberately absent is the combination that matters: a subject
# pronoun, and a copula carrying one. That absence is the whole mechanism. It is
# what makes "the casualty is not | unresponsive" suppressed and
# "no, | he's | not breathing" not - in the second the negator has to cross a
# fresh subject and verb to reach the finding, which means it governs a
# different predicate and is a discourse marker, not a logical negation.
#
# Possessive determiners - "his", "her", "their", "its" - are deliberately NOT
# here, though they look like the determiners that are. Unlike "a" and "the"
# they routinely head the SUBJECT of a fresh clause: "her lips are blue", "his
# heart has stopped". Treating them as transparent let a discourse "no" reach
# across one into an unrelated finding, so "no, her lips are blue" reported
# nothing at all. Found by independent review and confirmed by execution.
_NEGATION_TRANSPARENT = frozenset(
    {
        "a",
        "an",
        "the",
        "any",
        "really",
        "even",
        "quite",
        "at",
        "all",
        "yet",
        "still",
        "currently",
        "properly",
        # Auxiliary verb group. A negator immediately governs these.
        "had",
        "has",
        "have",
        "been",
        "being",
        "be",
    }
)


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


#: Where a person gets pulled out of, for the `drowning` marker. Named, because
#: hard-coding the literal word "water" was a defect with a clinical cost:
#: nobody says "water" about a swimming pool, and a missed `drowning` means the
#: hypoxic-arrest sequence - five rescue breaths BEFORE compressions, which
#: inverts adult BLS - is never signalled, so a paediatric drowning is handled
#: as a standard adult arrest.
#:
#: One source of truth per CLAUDE.md: the marker pattern derives from this, and
#: the corpus exercises it rather than restating it.
_SUBMERSION_PLACES: tuple[str, ...] = (
    "water",
    "pool",
    "swimming\\s+pool",
    "paddling\\s+pool",
    "hot\\s+tub",
    "jacuzzi",
    "lake",
    "river",
    "canal",
    "pond",
    "reservoir",
    "sea",
    "bath",
    "bathtub",
    "tub",
    "harbour",
    "harbor",
    "dock",
)


#: Things that get pulled out of water and are not the casualty. The drowning
#: marker claims the extracted object is a person; without this it fired on
#: "pulled the plug out of the bath" and "got the dog out of the pool". Named
#: rather than inlined for the same reason as the places above: it is a rule,
#: and a rule gets one home.
_NON_CASUALTY_OBJECTS: tuple[str, ...] = (
    "plug",
    "dog",
    "cat",
    "bags?",
    "keys",
    "phone",
    "towels?",
    "toys?",
    "leaves",
    "water",
    "filter",
    "hose",
    "cover",
    "ball",
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
        r"|not\s+breathing\s+at\s+all"
        # Subject-first phrasing: "her breathing has stopped" rather than "she
        # has stopped breathing". The marker had the verb-first form only, so
        # the same finding stated the other way round reported nothing. Found
        # while testing the possessive-determiner fix; a pre-existing gap
        # unrelated to the five negation defects.
        r"|breathing\s+(?:has\s+)?stopped",
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
        # First-person "I can't wake him" is at least as common as the
        # third-person "he won't wake up" - it is what the person beside them
        # says at 3am - and "up" is frequently dropped. Both the object and the
        # trailing "up" are optional so all four combinations match.
        r"|can(?:'t|not)\s+wake\s+(?:him|her|them|\w+)(?:\s+up)?"
        r"|can(?:'t|not)\s+wake\s+up"
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
        r"drowning|drowned"
        # The casualty must be the OBJECT of the extraction: at least one word
        # between the verb and "out of". "got the baby out of the bath" is a
        # submersion; "got out of the pool" is someone leaving under their own
        # power, and matching that would route an ordinary collapse to the
        # rescue-breath sequence.
        #
        # The object must also not be a thing. Independent review found that a
        # bare word gap fires on "pulled the plug out of the bath" and "got the
        # dog out of the pool" - over-firing, so the safe direction, but the
        # comment above claims the object is the casualty and the pattern should
        # make that claim true rather than merely assert it.
        rf"|(?:pulled|dragged|got|lifted|carried)\s+"
        # The excluded object must be the noun ACTUALLY extracted - the last word
        # before "out of the" - not merely present somewhere in the phrase.
        #
        # This is load-bearing, and the looser form was a missed escalation. The
        # original `(?:...)\s` let the trailing `\s` be satisfied by a space
        # *inside* a compound noun, so "dog" matched within "dog walker" and
        # "pulled my dog walker out of the lake" reported NOTHING. A `\b` on each
        # side does not fix it either, for the same reason. An exclusion list
        # added to stop an over-fire had opened a miss underneath it - the unsafe
        # direction, and precisely what this module exists to prevent. Anchoring
        # the lookahead on the full tail makes the exclusion mean what its
        # comment claims: the thing pulled out was not a person.
        rf"(?!(?:\w+\s+){{0,2}}?"
        rf"\b(?:{'|'.join(_NON_CASUALTY_OBJECTS)})\b\s+out\s+of\s+the\b)"
        rf"(?:\w+\s+){{1,3}}?"
        rf"out\s+of\s+the\s+(?:{'|'.join(_SUBMERSION_PLACES)})",
        "Drowning - treat as arrest until proven otherwise.",
    ),
)


def _is_negated(normalised: str, start: int) -> bool:
    """Whether a negator actually governs the finding at `start`.

    Walk left from the match. A negator suppresses it; any word that is not a
    function word stops the walk. So a negator governs the finding only when it
    reaches it across nothing but determiners, degree adverbs and auxiliaries.

    This replaced a flat `_NEGATION_WINDOW = 3` word count, which caused the
    most dangerous defect this module has had - it suppressed in both the wrong
    directions at once:

    Discourse "no". "no, he's not breathing" - the single most natural answer
    to "is he breathing?" - returned nothing, because the "no" that means "no,
    in answer to your question" sat within three words of the finding. It is
    now separated from it by a subject and a verb ("he's"), neither of which is
    a function word, so the walk stops before reaching it. Note what this does
    NOT rely on: punctuation. The unpunctuated "no he's not breathing" is the
    same utterance and fires identically.

    Adjacent-clause leakage. "he's not moving, he's not breathing" - two life
    threats in one breath - lost the second, because the "not" of "not moving"
    was three words behind it. The walk from "not breathing" now stops at
    "moving", so the earlier negator is never reached.

    Clause boundaries are deliberately NOT consulted, and that is a finding
    rather than an omission. An earlier version of this fix split the lookback
    at punctuation and coordinating conjunctions as well. Mutation testing
    showed that mechanism to be entirely redundant: the walk already halts at
    the first content word, which in English always sits at or before the
    clause boundary, so removing the split changed no outcome anywhere in the
    corpus. It was deleted rather than kept, because a safety mechanism no test
    can distinguish from its own absence is a source of false confidence.

    Both directions of error were weighed, and the asymmetry decided it: a
    missed escalation can kill a patient, a spurious one is stood down by a
    clinician. Where a phrasing is ambiguous this errs toward firing.
    """
    for word in reversed(normalised[:start].split()):
        # Punctuation is stripped so a negator fused to a comma ("no,") is still
        # recognised as one. It is stripped rather than treated as a boundary:
        # "the casualty is not, unresponsive" must stay suppressed, and a
        # transcript's comma placement is not evidence about meaning.
        token = word.strip(_TRAILING_PUNCTUATION)
        if token in _NEGATORS:
            return True
        if token not in _NEGATION_TRANSPARENT:
            return False
    return False


def _contradicted_later(normalised: str, end: int) -> bool:
    """Whether the responder corrects the finding immediately after reporting it.

    "he was not breathing but now he is breathing again" and "he was
    unresponsive but he's awake now" are narratives of recovery. The finding is
    in the past and the sentence says so.
    """
    remainder = normalised[end:]
    if not remainder.strip():
        return False

    # A QUESTION IS NOT A RETRACTION, and this is a confirmed missed escalation
    # rather than a precaution. Found by an independent reviewer and reproduced:
    #
    #   "he was not breathing, but now, is he talking?"        -> SUPPRESSED
    #   "he was unresponsive, but now is he alert? I cannot say" -> SUPPRESSED
    #
    # The responder is ASKING what to check, not reporting that the casualty
    # recovered - and the net went silent on a reported life threat. The window
    # below is a flat character count with no clause awareness, the same class of
    # mechanism as the `_NEGATION_WINDOW` this module deleted, so it reads a
    # recovery word inside an interrogative as a recovery narrative.
    #
    # Detected by STRUCTURE, not by trailing punctuation. Truncating at "?" does
    # not work - the question mark sits *after* the recovery word ("is he
    # talking?"), so the word is still in scope by the time it is seen. What
    # distinguishes the two is the inversion: a retraction asserts a subject then
    # a verb ("he IS breathing now"), while a question inverts them ("IS HE
    # talking"). Requiring the recovery word to be preceded by subject-then-verb,
    # and rejecting the inverted order, separates them without a wider window.
    #
    # The real-time phrasing was never at risk - "he's not breathing, but now
    # what, is he talking?" has no past-tense auxiliary adjacent to the match, so
    # the caller's gate never opens. That is why this hid: it needs retrospective
    # framing AND an interrogative together.
    inverted = re.search(
        r"\b(?:is|are|was|were|has|have|can|does|do|should|will)\s+"
        r"(?:he|she|they|it|him|her|the\s+\w+)\b"
        r".{0,20}?"
        r"\b(?:breathing|awake|responding|conscious|talking|alert|fine|ok|okay)\b",
        remainder,
    )
    if inverted:
        return False

    return bool(
        re.search(
            r"\b(?:but|now|then)\b.{0,40}?"
            r"\b(?:breathing|awake|responding|conscious|talking|alert|fine|ok|okay)\b",
            remainder,
        )
    )


#: The past-tense auxiliaries that put a finding in the past, contracted forms
#: included. "wasn't" is one word to the tokenizer and `\bwas\b` does not match
#: inside it, which is why a contracted recovery narrative - "he wasn't
#: breathing properly but he is breathing now" - escaped the guard below and
#: escalated a recovered faint to Critical.
_PAST_TENSE_AUXILIARIES = frozenset(
    {"was", "were", "had", "wasn't", "weren't", "hadn't"}
)

def _past_tense_before(normalised: str, start: int) -> bool:
    """Whether the finding is reported in the past tense ("he was unresponsive").

    Contracted forms count. `wasn't` is a past tense that happens to carry its
    own negation, and reading only the bare `was` made the tense invisible
    exactly where the responder was most clearly narrating the past.

    The tense can sit either immediately before the match or inside it: several
    markers spell a contraction into the pattern itself, so "he wasn't
    breathing" matches from the `wasn't`, leaving nothing past-tense in front of
    it. Looking only before the match is what let a contracted recovery
    narrative escalate a recovered faint to Critical.
    """
    preceding = normalised[:start].split()
    if preceding and preceding[-1] in _PAST_TENSE_AUXILIARIES:
        return True
    matched_first = normalised[start:].split(maxsplit=1)
    return bool(matched_first) and matched_first[0] in _PAST_TENSE_AUXILIARIES


def _is_suppressed(normalised: str, start: int, end: int) -> bool:
    """Whether this one occurrence of a marker is not a live finding.

    One predicate over one span, so `find_markers` can ask the same question of
    every occurrence. Exactly two reasons to suppress: the finding is negated,
    or it is reported in the past tense AND then contradicted - a narrative of
    recovery.

    There used to be a third, weaker guard: a contradiction later plus a
    past-tense auxiliary ANYWHERE in the preceding text, at unbounded distance
    and with no adjacency check. It was the flat-window failure mode F-001
    removed, reintroduced in the one direction that can kill someone -
    suppression - and widening the contracted auxiliaries into it made the hole
    bigger. It dropped this, verified by execution:

        "He was talking to me fine a minute ago, now he's not breathing,
         but earlier he seemed totally ok"                        -> NOTHING

    The "was" narrates the timeline and the trailing "ok" describes how the
    patient seemed BEFORE the arrest; neither retracts "not breathing". Found by
    independent review. Removing the branch restores that utterance to
    `not_breathing` and suppresses every recovery narrative in the corpus
    regardless, because the surviving guard reads the tense adjacent to - or
    inside - the match, which is where a real recovery narrative puts it.
    """
    if _is_negated(normalised, start):
        return True
    return _past_tense_before(normalised, start) and _contradicted_later(
        normalised, end
    )


def find_markers(text: str) -> tuple[MarkerHit, ...]:
    """Every life-threat marker present in the utterance, in pattern order.

    Negated and self-contradicted matches are dropped rather than returned with
    a flag: the caller's question is "is this a life threat", and a suppressed
    match is not one.

    A marker is reported if ANY of its occurrences survives suppression, not if
    its first one does. That distinction is a safety property rather than a
    nicety. `search` decided the whole utterance on the first span, so a
    responder who denies a finding and then corrects toward danger -
    "he's not unresponsive. he's unresponsive now." - was silently dropped. That
    is a deterioration report, the most urgent thing a caller can say, and it is
    the recovery narrative in reverse: the module handled "was bad, now fine"
    and had no path at all for "was fine, now bad" inside one utterance.

    The surviving occurrence is the one reported, so `span` and `matched_text`
    point at the live finding rather than the denied one.
    """
    if not text or not text.strip():
        return ()

    normalised = normalize_for_match(text)
    hits: list[MarkerHit] = []

    for marker in MARKERS:
        for match in marker.pattern.finditer(normalised):
            start, end = match.span()
            if _is_suppressed(normalised, start, end):
                continue
            hits.append(
                MarkerHit(
                    marker_id=marker.marker_id,
                    matched_text=match.group(0),
                    span=(start, end),
                )
            )
            break

    return tuple(hits)


def hard_escalation_triggered(text: str) -> MarkerHit | None:
    """The first life-threat marker in the utterance, or None."""
    hits = find_markers(text)
    return hits[0] if hits else None
