"""Putting `output_gate` on the production path: the three things it cannot know.

`output_gate.validate_turn` is a pure function of a `ClinicalTurn` and this
turn's retrieval hits. Wiring it means supplying those two arguments from a
pipeline that offers neither, and this module is exactly that adaptation - no
new rule, no second copy of a rule. Everything about *what is unsafe* stays in
`output_gate`; everything here is about where the values come from.

WHY THIS IS NOT IN `output_gate.py`. That module is a leaf over `retrieval` and
`phrases` with no vendor import, which is what makes the gate assertable with no
model, no network and no session. Classifying prose and buffering a token stream
are both jobs of the *caller*, and folding them in would put a heuristic inside
the module whose docstring carefully distinguishes declarations from heuristics.

THE THREE GAPS, and each is a gap because the adapter closes a door.

`google.LLM.__init__` has no `response_format`, schema or structured-output
parameter; neither does `AgentSession.__init__` or `Agent.__init__` (verified
against the installed `livekit-plugins-google`). So DESIGN 7.4's implied
approach - the model returns a `ClinicalTurn` as JSON and the gate validates the
parse - is not available through this adapter at all. What IS available is
`Agent.tts_node`, whose own docstring supports overriding it for "specialized
processing", and which is the last point before audio synthesis. That seam
receives `AsyncIterable[str]`: a stream of text segments, not a structure. Hence:

1. **`protocol_ids` cannot be model-declared.** They come from what the agent
   already knows - which protocol ids `lookup_protocol` actually returned on
   THIS turn. `RetrievedThisTurn` below holds them, and the lifetime is the
   whole safety property: see its docstring.

2. **`numerics` cannot be model-declared either.** `output_gate.extract_numerics`
   exists for a caller that "has plain text rather than a model's structured
   output", and that is precisely this caller. Stated plainly rather than
   dressed up: THE MODEL DECLARES NOTHING HERE. Extraction is not a completeness
   check on a declaration, because there is no declaration - it IS the list. The
   consequence is recorded rather than glossed: `NumericProvenance` is therefore
   always `CLINICAL`, so a caller's address or callback number read back inside
   a turn classified as a clinical instruction has no way to be exempted and
   will be refused. That is the strict direction, it costs a safe line, and it
   is the correct side to fail on until a structured-output path exists.

3. **`instruction_kind` cannot be model-labelled.** Classified from text by
   `classify`, whose rule and its bias are stated on that function.

No vendor import here either, for `escalation.py`'s reason: the gate decision
must be assertable without the LiveKit SDK installed. `agent.py` owns the two
node overrides and injects its telemetry and publishing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterable, Iterable
from dataclasses import dataclass, field

from .output_gate import (
    CLINICAL_UNITS,
    CLINICAL_VERBS,
    ClinicalTurn,
    GateDecision,
    InstructionKind,
    extract_numerics,
    numeric_values,
    validate_turn,
)
from .retrieval import Retrieved

logger = logging.getLogger(__name__)

#: How long to wait for the LLM stream to finish before gating what arrived.
#:
#: Not a latency budget - it is a LIVENESS bound. NFR-002 targets 500ms
#: end-to-end while WORKLOG Session 2 measured a Gemini turn at 2.3-2.6s, so a
#: value near the budget would fire on ordinary turns and replace correct
#: guidance with the safe line. 12s is deliberately far above the measured turn
#: and far below what a responder will tolerate as silence: it exists to catch a
#: stream that has STOPPED, not one that is merely slow.
BUFFER_TIMEOUT_S = 12.0

__all__ = [
    "ASSESSMENT_ONLY",
    "GATE_REJECTION_TOPIC",
    "RetrievedThisTurn",
    "buffer_stream",
    "classify",
    "gate_turn",
    "once",
    "rejection_payload",
    "rejection_span_attributes",
]


#: The published event a rejected turn produces. The dashboard reads triage.*
#: topics; this is the one that says the gate refused something, which is the
#: difference between a gate and a silent filter.
GATE_REJECTION_TOPIC = "triage.gate_rejection"


# ------------------------------------------------------- what retrieval returned


@dataclass
class RetrievedThisTurn:
    """This turn's protocol hits, with a lifetime that is the safety property.

    A citation is only a citation if the model was actually shown the document
    (`output_gate` guarantee 2: "the model cannot have read text it was never
    shown"). `validate_turn` says so itself and says it cannot detect staleness:
    "Passing an earlier turn's hits would let a stale document ground a fresh
    number, so the caller must pass what came back now." This object is that
    caller's answer, so getting its lifetime wrong is a real hole rather than an
    untidiness.

    THE LIFETIME, stated exactly.

    SET in `AiscelapeusAgent.lookup_protocol`, by `record`, on a lookup that
    returned hits - the same place and the same moment the hits are handed to
    the model. Nothing else adds to it. A lookup that returned nothing, was
    refused for a blank query, or raised `TierViolation` records nothing, so a
    failed retrieval cannot leave a citable id behind.

    CLEARED in `AiscelapeusAgent.on_user_turn_completed`, which the SDK awaits
    *before* generating the reply (`agent_activity._user_turn_completed_impl`
    awaits the hook, then schedules the speech). So the order within one
    responder turn is: clear, then the model runs, then any `lookup_protocol`
    records, then `tts_node` gates against exactly what that turn retrieved.

    WHY NOT CLEARED AFTER GATING. Because one responder turn can produce more
    than one agent speech - a tool-step reply and a follow-up - and clearing on
    first use would make the second one uncitable although the same retrieval
    genuinely backs it. Clearing at the start of the next responder turn is the
    boundary that matches "what was retrieved for this exchange".

    ACCUMULATES WITHIN A TURN, deliberately. `max_tool_steps=4` lets the model
    look up twice in one turn (say airway then cardiac), and both documents were
    genuinely shown to it, so both are citable. Union, not replacement.

    Mutable by design: it is per-session scratch state on the agent, in the same
    role `TriageState` fills for criticality. Frozen would mean rebinding an
    attribute on every lookup, which is how a clear gets skipped.
    """

    #: Hit id -> the hit. A mapping rather than a list because `validate_turn`
    #: keys by id and a document retrieved twice in one turn is one citable
    #: document, not two.
    hits: dict[str, Retrieved] = field(default_factory=dict)

    def record(self, retrieved: Iterable[Retrieved]) -> None:
        """Remember hits the model is being shown on this turn."""
        for hit in retrieved:
            self.hits[hit.id] = hit

    def clear(self) -> None:
        """Forget everything. Called at the start of each responder turn."""
        self.hits.clear()

    @property
    def ids(self) -> tuple[str, ...]:
        """The citable ids, in the order they were retrieved."""
        return tuple(self.hits)

    def as_hits(self) -> tuple[Retrieved, ...]:
        """What `validate_turn` takes as its second argument."""
        return tuple(self.hits.values())


# ------------------------------------------------------------- classification


#: Speech that is asking rather than instructing, so `output_gate` does not
#: require a citation for it. Anchored at the start of the (stripped) text and
#: paired with the text ending in a question mark - see `classify` for why both
#: halves are required. "Is he breathing?" is the most important thing the agent
#: says first, and requiring it to cite a document would reject it.
#:
#: DELIBERATELY NARROW, and the words that are MISSING are the point. An earlier
#: version of this list also held "can", "do", "does", "are" and "have" - and
#: independent review found that those introduce a POLITE IMPERATIVE just as
#: naturally as a question:
#:
#:     "Can you push at least five centimetres deep now?"
#:     "Do you give three hundred milligrams of aspirin now?"
#:     "Are you able to give a second dose of adrenaline?"
#:
#: Each is one sentence, each ends in `?`, each opened with a listed
#: interrogative - so each classified as ASSESSMENT_QUESTION, which sets
#: `requires_citation` False and skipped output_gate's citation, grounding and
#: ratio rules ENTIRELY. An invented dose phrased that way reached the responder
#: with nothing checked and no `lookup_protocol` call needed. That is precisely
#: the bypass `classify`'s docstring claimed to prevent, present in the
#: mechanism that was supposed to prevent it - the "mechanism whose only
#: implementation is its docstring" defect this repo keeps finding.
#:
#: THE FIX IS NOT THE WORD LIST. Deleting those words closed those four
#: phrasings and broke two genuine ones - "Can you feel a pulse?" and "Are they
#: responsive?" are exactly the questions the agent must ask before it can look
#: anything up, and both became uncitable clinical instructions, i.e. the safe
#: line at the start of every call. Narrowing a word list is not a rule: the
#: next plausible phrasing is always one word outside it, in whichever
#: direction.
#:
#: So the list stays wide enough for the auxiliaries a question really uses, and
#: what actually closes the bypass is `_names_a_clinical_quantity` - a check on
#: the CONTENT rather than the first word. A question that names a dose or a
#: depth is an instruction whatever it opens with; a question that names neither
#: is a question whatever it opens with. That rule holds for phrasings nobody
#: enumerated, which a word list cannot do.
ASSESSMENT_ONLY: tuple[str, ...] = (
    r"is\b",
    r"are\b",
    r"was\b",
    r"were\b",
    r"does\b",
    r"do\b",
    r"did\b",
    r"can\b",
    r"could\b",
    r"has\b",
    r"have\b",
    r"how\b",
    r"what\b",
    r"where\b",
    r"who\b",
    r"which\b",
    r"when\b",
    r"why\b",
    r"tell me\b",
)

_ASSESSMENT_RE = tuple(
    re.compile(rf"^\s*{pattern}", re.IGNORECASE) for pattern in ASSESSMENT_ONLY
)


def classify(speech: str) -> InstructionKind:
    """Which `InstructionKind` this prose is, erring towards the gated one.

    THE RULE, in one sentence: a turn is an `ASSESSMENT_QUESTION` only when it
    is a single question - every sentence in it ends in `?` and the text opens
    with an interrogative from `ASSESSMENT_ONLY`. Everything else, including
    anything that would read as reassurance, is a `CLINICAL_INSTRUCTION`.

    THE BIAS AND WHY. Misclassification is not symmetric. Calling a clinical
    instruction `REASSURANCE` (or `ASSESSMENT_QUESTION`) sets
    `InstructionKind.requires_citation` to False and so DISABLES the citation
    and grounding checks for that turn - an invented dose would then walk
    straight through the wired gate. Calling reassurance a clinical instruction
    merely demands a citation it cannot supply, which costs the safe line. The
    first error is the one the gate exists to prevent, so the classifier never
    returns a kind that switches the gate off unless the text cannot be an
    instruction at all.

    `REASSURANCE` is therefore NEVER returned, and that is a deliberate
    narrowing rather than an omission: it is the kind that disables citation
    while permitting declarative prose, which is exactly the shape an invented
    instruction has. The cost is that `output_gate`'s DESIGN 7.4 rule 4
    (`DIAGNOSIS_PATTERNS` on reassurance) is unreachable through this caller -
    recorded here, and reported rather than worked around, because reaching it
    would mean classifying declarative prose as non-clinical.

    `ESCALATION_NOTICE` is likewise never returned. It would exempt the turn
    from citation on the strength of a text match, and "I'm bringing a doctor
    in" carries no clinical quantity anyway - so an escalation notice classified
    as a clinical instruction passes the gate on its own merits, with no numbers
    to ground. The exemption would buy nothing and could be imitated.

    BOTH HALVES of the question test are required, and the asymmetry is the
    point. "Is he breathing?" is a question. "Is he breathing? Then push five
    centimetres." opens with an interrogative and is an instruction, so the
    every-sentence-ends-in-? half refuses it. "Push five centimetres, okay?"
    ends in a question mark and is an instruction, so the opening-interrogative
    half refuses it.
    """
    stripped = speech.strip()
    if not stripped:
        # An empty turn has nothing to gate, but it must not be classified into
        # the kind that skips the checks - a caller could otherwise reach the
        # exempt branch with whitespace. `ClinicalTurn` rejects blank speech
        # anyway (min_length=1), so this is the conservative default, not a path.
        return InstructionKind.CLINICAL_INSTRUCTION

    if not _is_all_questions(stripped):
        return InstructionKind.CLINICAL_INSTRUCTION

    if _names_a_clinical_quantity(stripped):
        # THE CHECK THAT ACTUALLY CLOSES THE POLITE-IMPERATIVE BYPASS, found by
        # independent review. "Can you push at least five centimetres deep now?"
        # is one sentence, ends in `?`, and opens with an interrogative - so the
        # two structural halves above both pass it, and without this it
        # classified as ASSESSMENT_QUESTION and skipped output_gate's citation,
        # grounding and ratio rules entirely. An invented dose phrased that way
        # reached the responder with nothing checked.
        #
        # A genuine assessment question asks about the casualty ("Is he
        # breathing?", "Can you feel a pulse?") and names no clinical quantity.
        # An instruction names one. So the test is on the CONTENT, which holds
        # for phrasings nobody enumerated - see `ASSESSMENT_ONLY` for why
        # deleting words from that list was the wrong fix.
        #
        # The cost is stated: a genuine question about a quantity - "How deep
        # should the compressions be?" - is classified as a clinical instruction
        # and must cite. That is the strict direction, it costs a safe line on a
        # question the agent should be answering from a retrieved protocol
        # anyway, and it is the correct side of this asymmetry.
        return InstructionKind.CLINICAL_INSTRUCTION

    if any(pattern.match(stripped) for pattern in _ASSESSMENT_RE):
        return InstructionKind.ASSESSMENT_QUESTION

    return InstructionKind.CLINICAL_INSTRUCTION


def _names_a_clinical_quantity(text: str) -> bool:
    """Whether `text` speaks a number alongside a clinical verb or unit.

    Reuses `output_gate`'s `CLINICAL_VERBS` and `CLINICAL_UNITS` rather than
    listing the words again, per CLAUDE.md's DRY rule: "one source of truth for
    a rule". Those two constants already encode "this construction names a
    clinical quantity" for `output_gate._in_clinical_construction`, and a second
    copy here would be a second answer to the same question that could drift.

    Requires BOTH a number and one of those words. "What happened?" has neither;
    "Is he breathing?" has neither. "Can you give three hundred milligrams?" has
    both, and is an instruction however it is punctuated.
    """
    if not numeric_values(text):
        return False
    words = {word.lower() for word in _WORDS.findall(text)}
    return bool(words & CLINICAL_VERBS) or bool(words & CLINICAL_UNITS)


#: Word-level, so "grams" does not match inside "programs" - the same reason
#: `output_gate._carries_clinical_unit` tokenises rather than substring-matching.
_WORDS = re.compile(r"[a-z]+", re.IGNORECASE)


#: Sentence-ish split on terminal punctuation. Crude, and it does not need to be
#: better: it is only ever asked "does every piece end in a question mark", and
#: a decimal or an abbreviation splitting wrongly produces a fragment that does
#: NOT end in `?`, which pushes the turn towards CLINICAL_INSTRUCTION - the safe
#: direction. `output_gate`'s own `SPEECH_CEILING_CHARS` comment makes the same
#: argument for preferring an unambiguous crude proxy over a prose parser.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _is_all_questions(text: str) -> bool:
    """Whether every sentence in `text` is a question."""
    pieces = [piece.strip() for piece in _SENTENCE_SPLIT.split(text) if piece.strip()]
    if not pieces:
        return False
    return all(piece.endswith("?") for piece in pieces)


# ----------------------------------------------------------------- the decision


def gate_turn(speech: str, retrieved: RetrievedThisTurn) -> tuple[ClinicalTurn, GateDecision]:
    """Build the turn this pipeline can actually describe, and validate it.

    Returns both, because the caller needs the turn's classified kind for its
    span even when the decision is a rejection.

    `numerics` is `extract_numerics(speech)` and nothing else. There is no model
    declaration to check for completeness against (see the module docstring), so
    every number in the speech is treated as a clinical quantity requiring
    grounding - except the ones `output_gate` exempts by value, which is the
    emergency-service set. This is the strict side of the asymmetry that module
    documents, chosen knowingly.
    """
    kind = classify(speech)
    turn = ClinicalTurn(
        speech=speech,
        instruction_kind=kind,
        # Everything retrieved this turn is cited, because everything retrieved
        # this turn is what the model was shown. A narrower claim is not
        # available: with no structured output the model cannot tell us which
        # of the documents in front of it it actually used.
        protocol_ids=retrieved.ids,
        numerics=extract_numerics(speech),
    )
    return turn, validate_turn(turn, retrieved.as_hits())


# ------------------------------------------------------------------- observing


def rejection_span_attributes(
    *, session_id: str, turn: ClinicalTurn, decision: GateDecision
) -> dict[str, object]:
    """Span attributes for a refusal, carrying NO clinical text.

    `moss_context.py` and `agent.py` both treat spans as a non-PHI surface -
    `_do_escalate` puts the escalation *category* on the span and keeps the
    free-text reason off it. The same rule here, and it bites harder: the
    rejected text is model output about a patient, and `decision.details`
    quotes the spoken numerals (`Numeric.spoken`) verbatim. So the span carries
    the reason enum, the counts and the cited ids - enough to say which rule
    fired and against which documents - and never the speech, the details or the
    ungrounded numerics' words.
    """
    return {
        "session.id": session_id,
        "gate.outcome": decision.outcome.value,
        "gate.instruction_kind": turn.instruction_kind.value,
        # Sorted so the attribute is stable for grouping; joined because span
        # attributes take scalars and sequences of one type.
        "gate.reasons": ",".join(sorted(reason.value for reason in decision.reasons)),
        "gate.protocol_ids": ",".join(turn.protocol_ids),
        "gate.ungrounded_count": len(decision.ungrounded),
        "gate.speech_chars": len(turn.speech),
    }


def rejection_payload(
    *, session_id: str, turn: ClinicalTurn, decision: GateDecision
) -> dict[str, object]:
    """The published event for a refusal.

    Richer than the span on purpose. The data channel already carries the
    transcript and every recorded finding, so it is a PHI surface by
    construction and the dashboard is where a clinician needs to see *what* was
    refused - the `details` are the audit record `RejectionReason`'s docstring
    calls for ("a rejected turn all looks the same to the responder, but it must
    not look the same to an audit"). The span, which is not that surface, gets
    the enums only.
    """
    return {
        "session_id": session_id,
        "outcome": decision.outcome.value,
        "instruction_kind": turn.instruction_kind.value,
        "reasons": [reason.value for reason in decision.reasons],
        "details": list(decision.details),
        "protocol_ids": list(turn.protocol_ids),
        "ungrounded": [
            {"spoken": numeric.spoken, "value": numeric.value}
            for numeric in decision.ungrounded
        ],
        "refused_speech": turn.speech,
        "spoken_instead": decision.speech,
    }


# ------------------------------------------------------------------- streaming


async def buffer_stream(stream: AsyncIterable[str]) -> str:
    """Drain a segment stream into the whole turn's text.

    THE DECISION: buffer the WHOLE TURN, not per sentence. Both were considered
    and the choice is not free, so the cost is stated in the units that matter.

    WHY NOT PER-SENTENCE, which is the lower-latency option. `output_gate`
    checks three things that are properties of the turn, not of a sentence:

    - A citation covers the turn. "Push hard in the centre of the chest." has no
      numbers and cites fine; the depth arrives in the next sentence. Gated
      per-sentence, the first sentence is spoken and the second is replaced, so
      the responder hears half an instruction followed by the safe line - which
      is a worse artefact than the safe line alone, and it does it *after*
      committing them to an action.
    - `SPEECH_CEILING_CHARS` is a ceiling on the turn. Per-sentence, a
      four-minute monologue of short sentences never trips it.
    - The ratio rule (`_ratio_forms`) spans a clause that STT-shaped segments
      split: "Thirty compressions," / "then two rescue breaths." Split, neither
      piece carries the `30:2` form, so an INVERTED ratio passes - a
      permissive-direction failure, which is the direction that defeats the gate.

    So per-sentence gating does not enforce the rules `output_gate` implements.
    That is a correctness argument, and CLAUDE.md orders correctness before
    latency.

    THE LATENCY COST, reasoned rather than asserted, because this module has no
    measurement and a number with no evidence under it is the failure mode
    CLAUDE.md names. Buffering defers the FIRST audio byte by the model's
    remaining generation time for the turn, since nothing is synthesised until
    the LLM stream closes. It does NOT add compute - `validate_turn` is pure
    string work over a few hundred characters with no model call, no network and
    no clock. The prompt caps a turn at "two short sentences is the target; four
    is the ceiling" and `SPEECH_CEILING_CHARS` caps it at 400 characters, so the
    deferral is bounded by the generation time of at most 400 characters rather
    than being unbounded. THE HONEST STATEMENT: that deferral is real, it is
    per-turn, and it is not measured here. What is measured is asserted by
    `test_buffering_is_bounded_by_the_speech_ceiling`, which pins the bound
    rather than the wall time.

    WHY THE TRADE IS ACCEPTABLE ANYWAY: `allow_interruptions=True` means the
    responder can talk over the agent at any point, and the gate's alternative
    to a deferred first byte is a spoken invented dose. DESIGN CONFLICT-1
    already resolved this asymmetry for the rejection path - which is why
    `SAFE_LINE` is a pre-rendered constant and costs zero model calls. This is
    the same asymmetry one step earlier.
    """
    segments: list[str] = []
    try:
        async with asyncio.timeout(BUFFER_TIMEOUT_S):
            async for segment in stream:
                segments.append(segment)
    except TimeoutError:
        # A stalled or non-terminating LLM stream. Found by independent review
        # and reproduced: a stream that yields once and then blocks makes this
        # function never return, so the responder hears NOTHING - indefinitely,
        # mid-arrest, with no safe line and no escalation, because the
        # deterministic net runs off STT transcripts and not off agent
        # generation state.
        #
        # The SDK supplies no bound of its own: `generation.py` contains no
        # timeout at all (its two `wait_for` calls are playout waits), and
        # `tts_node` runs in a task nothing wraps. The only thing that could
        # unblock it is the responder interrupting - which is exactly what they
        # will not do while their hands are on a chest waiting to be told the
        # next thing, or when the casualty is the only other person present.
        # This docstring's own "allow_interruptions makes the trade acceptable"
        # argument assumed someone would speak.
        #
        # Returning what arrived rather than raising: the partial text still
        # goes through the gate, so a half-formed clinical instruction is
        # refused and becomes SAFE_LINE. Raising here would abort synthesis and
        # reproduce the silence this exists to prevent. Fail closed, but SAY SO.
        logger.warning(
            "LLM stream did not complete within %.1fs; gating the %d segment(s) "
            "that arrived rather than waiting",
            BUFFER_TIMEOUT_S,
            len(segments),
        )
    return "".join(segments)


async def once(text: str) -> AsyncGenerator[str, None]:
    """One text as a single-segment stream, for handing back to a default node.

    Needed because both node overrides consume their input stream to gate it and
    then have to give the pipeline a stream again. A generator rather than a
    list, because the SDK's nodes take an `AsyncIterable[str]`.
    """
    yield text
