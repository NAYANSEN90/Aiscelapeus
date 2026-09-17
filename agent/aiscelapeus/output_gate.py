"""The output schema gate: nothing reaches TTS uncited (PE-005, ADR-005, DESIGN 7.4).

WHAT THIS GUARANTEES, and the list is deliberately short:

1. A turn declared a `clinical_instruction` carries at least one protocol id.
2. Every cited id was actually retrieved *on this turn*. Citing a real corpus
   document that this turn's retrieval did not return is a fabrication, not a
   citation - the model cannot have read text it was never shown.
3. Every clinical quantity spoken in the instruction appears in the text of a
   cited document. Doses, depths, rates, counts, durations.

WHAT THIS DOES NOT GUARANTEE, stated here because the gap is the dangerous part
and BUILD-PLAN 8.5 names it in these words: "An instruction sourced from the
wrong document is correctly formed, correctly cited, and wrong."

- **Not clinical correctness.** The gate checks that an instruction cites a
  document containing its numbers. It has no opinion on whether that document is
  the right one for this casualty. "Push at least 5 centimetres" cited to
  `bls-adult-cpr` passes; so does the same sentence cited to `bls-adult-cpr`
  while the casualty is an infant, because the adult card genuinely contains
  that number. Retrieval quality (RET-C02) is what makes the cited document the
  right one, and no downstream component can substitute for it.
- **Not paraphrase safety.** Numbers are checked; words are not. "Do not give
  aspirin" and "give aspirin" both carry the number 300 if 300 is spoken, and
  both pass. A negation flip is invisible here.
- **Not units.** Matching is on values, so a number is grounded by the cited
  document stating that number, not that number *of that unit*. "Push two
  inches" is grounded by a card saying "every 2 minutes". A declared numeric
  whose words carry a clinical unit while claiming non-clinical provenance IS
  caught (`CLINICAL_UNITS`), but that is a mislabelling check, not a unit check.
  Pinned by `test_a_paraphrased_dose_is_caught`.
- **Not every ratio.** Order is preserved for explicit `d:d` notation and for
  the compressions-to-breaths prose form. Any other prose ratio - a drug-to-
  diluent mix, inspiratory-to-expiratory timing - is checked only on its
  component values, so an inversion of one of those would pass.
- **Not completeness.** An instruction that omits a critical step is
  well-formed. The gate cannot know what was left out.
- **Not the whole DESIGN 7.4 list.** Rules 3 and 4 there (a speech-length
  ceiling, a diagnosis-shaped-language pattern list on `reassurance`) are
  implemented; see `SPEECH_CEILING_CHARS` and `DIAGNOSIS_PATTERNS` for what each
  covers and what it misses.

THE NUMERIC RULE, because getting it wrong either way causes harm.

The corpus writes digits - "at least 5 centimetres deep", "100 to 120
compressions per minute" (protocols.py). The prompt orders the model to *speak*
words - "five centimetres", "one hundred to one hundred twenty" (prompts.py
specifics). So DESIGN 7.4's literal wording, that every numeric "must appear
verbatim in the text of a cited protocol document", cannot be implemented as
written: a verbatim substring check would reject every correctly-grounded
compression-depth instruction in the system, mid-arrest. That is the strict-side
harm, and it is not hypothetical - it is what the two source files as committed
would produce.

So both sides are normalised to *numeric values* before comparison, and the
comparison is over that value set:

    speech  "push at least five centimetres"  ->  {5}
    cited   "at least 5 centimetres deep"     ->  {5, 100, 120, 30, 2, 10}
    5 in the cited set  ->  grounded

Spelled-out numerals, digits, ranges ("100 to 120" contributes 100 and 120, not
the 21 values between), ratios ("30:2" contributes 30 and 2, and also the
composite 30:2), decimals, and ordinals ("the fifth", "the 5th") all reduce to
the integers they name. Word-number composition handles "one hundred twenty" as
120 rather than as 1, 100, 20.

Deliberately NOT clinical, and excluded before matching:

- Emergency service numbers (999, 911, 112, ...). The agent says these and the
  corpus does not contain them.
- Anything the turn marks as non-clinical provenance: a street number or a phone
  number the caller gave. That is `Numeric.provenance`, and it is an explicit
  declaration on the turn rather than a heuristic over the text, because a
  heuristic that guesses "42" is an address is a heuristic that can be made to
  guess a dose is one.

The asymmetry is chosen. A false rejection costs the responder tailored guidance
and buys a safe line plus a clinician - recoverable. A false acceptance puts an
invented dose in a responder's ear - not recoverable. Where the two conflict the
gate rejects, EXCEPT where rejecting would fire on correct guidance as a matter
of course (the digits-vs-words case above), because a gate that rejects correct
guidance routinely is a gate that gets switched off.

No vendor import beyond pydantic, and no import from `agent.py` or `main.py`:
this module is a leaf over `retrieval` and `phrases`, so the gate is testable
with no model, no network and no session.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .retrieval import Retrieved

__all__ = [
    "DIAGNOSIS_PATTERNS",
    "EMERGENCY_NUMBERS",
    "SAFE_LINE",
    "SPEECH_CEILING_CHARS",
    "ClinicalTurn",
    "GateDecision",
    "GateOutcome",
    "InstructionKind",
    "Numeric",
    "NumericProvenance",
    "RejectionReason",
    "extract_numerics",
    "numeric_values",
    "validate_turn",
]


#: Spoken on rejection. Pre-rendered, module-level, and a constant rather than a
#: function of the turn, which is the whole latency argument (DESIGN CONFLICT-1):
#: the failure path must cost zero additional model calls, so there is nothing
#: here to generate. Wording from DESIGN 7.4.
SAFE_LINE = "Stay with me. I need a clinician for this one, bringing them in now."

#: DESIGN 7.4 rule 3. The prompt's own ceiling is "two short sentences is the
#: target; four is the ceiling" (prompts.py specifics), which is a sentence count
#: over prose the gate cannot parse reliably - abbreviations and decimals both
#: contain periods. A character ceiling is the crude but unambiguous proxy: at
#: conversational TTS rates this is roughly twelve seconds of speech, which is
#: already far past "one action at a time, then wait". Stated as a proxy rather
#: than as the prompt's rule, because it is one.
SPEECH_CEILING_CHARS = 400

#: Numbers that are never clinical quantities, so never require a citation. An
#: agent saying "call 999" must not need a protocol document containing 999.
#: Values, not text: 911 spoken as "nine one one" normalises to 911 too - which
#: this comment asserted before it was true. The word parser composed digit runs
#: arithmetically, so "nine one one" summed to 11 and "nine nine nine" to 27,
#: neither in this set, and the gate rejected the spoken form while approving
#: "call 999". Since `prompts.py:106` requires numbers to be spoken as words,
#: the rejected form was the only one production emits. Now pinned by
#: `test_an_emergency_number_spoken_as_digits_parses_as_the_number`.
#:
#: `000` is NOT in this set, and its absence is deliberate. It was here, written
#: as the literal `000`, which Python evaluates to the integer 0 - so the set
#: silently contained 0 and every clinical numeric that parses to zero was exempt
#: from grounding entirely. Found by independent review. Australia's number is
#: spoken "triple zero" and reaches the gate as three separate zeros or not at
#: all, so it was never being matched as 000 anyway; the entry bought nothing and
#: cost the exemption of a real value.
EMERGENCY_NUMBERS: frozenset[int] = frozenset({112, 911, 999, 1122})

#: Units that make a number a clinical quantity regardless of how the model
#: labelled its provenance. Used by `_carries_clinical_unit` to stop a
#: `CALLER_SUPPLIED` label being used to speak an invented dose: a street number
#: is not measured in milligrams.
#:
#: Deliberately a unit list rather than a general classifier. The question asked
#: is narrow and answerable - "do these words name a clinical measure" - whereas
#: guessing whether an unqualified 42 is an address is not, which is why the
#: provenance declaration still exists for that case.
#: Verbs that introduce a clinical quantity. A number appearing as the object of
#: one of these is a dose, a depth or a count whatever provenance the model
#: declared - nobody "gives six hundred" of a house number.
#:
#: This is the second half of the mislabelling check and it exists because
#: `CLINICAL_UNITS` alone was not enough: "Give six hundred of aspirin now",
#: declared `caller_supplied`, carries no unit word in its `spoken` form and so
#: slipped through a unit-only test. Found by mutation-testing the fix for the
#: review's own finding - the narrowing that was supposed to catch this was
#: unreachable, which is precisely the "mechanism whose only implementation is
#: its docstring" defect this repo keeps finding.
CLINICAL_VERBS: frozenset[str] = frozenset(
    {
        "give",
        "giving",
        "given",
        "administer",
        "administered",
        "inject",
        "push",
        "pushing",
        "compress",
        "repeat",
        "dose",
        "dosed",
        "apply",
        "deliver",
        "shock",
    }
)

CLINICAL_UNITS: frozenset[str] = frozenset(
    {
        "milligram",
        "milligrams",
        "mg",
        "microgram",
        "micrograms",
        "mcg",
        "gram",
        "grams",
        "g",
        "millilitre",
        "millilitres",
        "millilitres",
        "ml",
        "litre",
        "litres",
        "centimetre",
        "centimetres",
        "cm",
        "millimetre",
        "millimetres",
        "mm",
        "inch",
        "inches",
        "unit",
        "units",
        "joule",
        "joules",
        "j",
        "dose",
        "doses",
        "compression",
        "compressions",
        "breath",
        "breaths",
        "puff",
        "puffs",
        "tablet",
        "tablets",
    }
)

#: DESIGN 7.4 rule 4: diagnosis-shaped language in `reassurance`. A pattern list,
#: and its limits are real - it catches the confident forms ("it's a heart
#: attack", "he is having a stroke") and not an arbitrary paraphrase ("that's the
#: classic presentation of the big one"). Kept narrow on purpose: this rule fires
#: on `reassurance` only, where the legitimate vocabulary is small, so a wide
#: pattern would reject ordinary reassurance rather than catch a diagnosis.
DIAGNOSIS_PATTERNS: tuple[str, ...] = (
    r"\b(?:it|this|that|he|she|they)\s*(?:'s|s|is|are)\s+(?:probably\s+|likely\s+|definitely\s+)?"
    r"(?:a|an|the)?\s*(?:heart attack|stroke|seizure|arrest|overdose|concussion|"
    r"anaphylaxis|hypoglyca?emi[ac]|internal bleeding|broken|fractur)",
    r"\b(?:you|he|she|they)\s+(?:are|is|'re|'s)\s+having\s+(?:a|an)\s+",
    r"\b(?:i|we)\s+(?:can\s+)?(?:diagnos|confirm)",
    r"\bdiagnos(?:is|ed|ing)\b",
)

_DIAGNOSIS_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in DIAGNOSIS_PATTERNS)


# ------------------------------------------------------------------ enumerations


class InstructionKind(str, Enum):
    """What the turn is doing, from DESIGN 7.4's `Literal`.

    An enum rather than that `Literal[...]` so the four values have one
    definition site the tests and the gate both read, per CLAUDE.md's DRY rule.
    Which rules apply is a property of the kind, not a chain of string
    comparisons scattered through `validate_turn`: see `requires_citation`.
    """

    ASSESSMENT_QUESTION = "assessment_question"
    CLINICAL_INSTRUCTION = "clinical_instruction"
    REASSURANCE = "reassurance"
    ESCALATION_NOTICE = "escalation_notice"

    @property
    def requires_citation(self) -> bool:
        """Whether a turn of this kind must cite a protocol and ground its numbers.

        Only `clinical_instruction` does. "Is he breathing?" is a question, not
        guidance, and requiring it to cite a document would make the gate reject
        the single most important thing the agent says first.
        """
        return self is InstructionKind.CLINICAL_INSTRUCTION


class NumericProvenance(str, Enum):
    """Where a number in the speech came from, as declared by the turn.

    This is the one place the gate takes the model's word for something, so it is
    a named enum with a documented risk rather than an implicit bool.

    `CLINICAL` is the default and the only value that requires grounding. The
    others exist because a responder's own address or callback number gets read
    back to confirm it, and no protocol document contains a house number - so
    without this distinction the gate would reject every confirmation of a
    detail the caller themselves supplied.

    THE RISK, plainly: a malfunctioning model could label an invented dose
    `CALLER_SUPPLIED` and route it around the citation check. That is why the
    field is an explicit declaration on a typed model rather than a heuristic
    over the text - a heuristic cannot be audited, whereas every non-CLINICAL
    numeric is visible in the turn and on the span. Narrowing this further (for
    instance, requiring a caller-supplied number to match something in the
    session transcript) needs the session index and belongs with B3's wiring, not
    here. Recorded rather than quietly accepted.
    """

    CLINICAL = "clinical"
    CALLER_SUPPLIED = "caller_supplied"
    EMERGENCY_SERVICE = "emergency_service"
    TIME_OF_DAY = "time_of_day"

    @property
    def requires_grounding(self) -> bool:
        return self is NumericProvenance.CLINICAL


class RejectionReason(str, Enum):
    """Why a turn was refused. One value per rule, so a span says which rule fired.

    A rejected turn all looks the same to the responder - one safe line - but it
    must not look the same to an audit. "The model cited nothing" and "the model
    spoke a number that is not in the document it cited" are different
    malfunctions with different fixes, and a single `rejected: bool` could not
    tell them apart.
    """

    MISSING_CITATION = "missing_citation"
    UNRETRIEVED_CITATION = "unretrieved_citation"
    UNGROUNDED_NUMERIC = "ungrounded_numeric"
    SPEECH_TOO_LONG = "speech_too_long"
    DIAGNOSIS_IN_REASSURANCE = "diagnosis_in_reassurance"
    UNDECLARED_NUMERIC = "undeclared_numeric"
    #: A number declared non-clinical while carrying a clinical unit. Its own
    #: reason rather than folded into UNGROUNDED_NUMERIC, because the two call
    #: for different investigations: an ungrounded numeric means the corpus and
    #: the speech disagree, while this means the model mislabelled a dose - which
    #: is either a malfunction or an attempt to route around the gate.
    MISLABELLED_NUMERIC = "mislabelled_numeric"


class GateOutcome(str, Enum):
    """Whether the turn may be spoken.

    An enum rather than `approved: bool` per CLAUDE.md: the two states are read
    by a caller that must choose between two different pieces of audio, and a
    bool at that branch invites `if not decision.approved` to be written as a
    truthiness test on the decision object itself - which is always truthy.
    """

    APPROVED = "approved"
    REJECTED = "rejected"


# ----------------------------------------------------------------- numeric model


class Numeric(BaseModel):
    """One number appearing in the spoken text, with what it is.

    `spoken` is the surface form as it will be said ("five centimetres", "300
    milligram"); `value` is what it means. Both are carried because the gate
    matches on the value while an audit needs the words - a span reading
    "ungrounded numeric: 5" is far less useful than one naming the phrase the
    responder would have heard.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spoken: Annotated[str, Field(min_length=1)]
    value: float
    provenance: NumericProvenance = NumericProvenance.CLINICAL

    @property
    def requires_grounding(self) -> bool:
        """Whether this number must appear in a cited document.

        Emergency service numbers are exempt by *value* as well as by
        provenance, so a model that says "call 999" without labelling it is not
        rejected for a number no corpus document will ever contain.
        """
        if not self.provenance.requires_grounding:
            return False
        return not _is_emergency_number(self.value)


class ClinicalTurn(BaseModel):
    """One turn of agent speech, validated before a byte reaches TTS.

    Shape from DESIGN 7.4. Pydantic rather than a frozen dataclass because this
    is the boundary type the model's structured output is parsed into - the
    schema is what constrains generation - and `extra="forbid"` means a model
    inventing a field is an error rather than a silently dropped value.

    `frozen=True` for the reason `RetrievalRequest` is frozen: a turn that has
    passed the gate must not be mutable afterwards, or the audio spoken can
    differ from the text validated.

    CRITICALITY. `criticality_claim` is the model's *claim* and the field name
    says so. It is carried for the record and is deliberately NOT used by this
    module to change any level: `TriageState.set_level` owns the ratchet and
    `escalation.py` owns the deterministic net. A gate that could move
    criticality would be a second implementation of that rule.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    speech: Annotated[str, Field(min_length=1)]
    instruction_kind: InstructionKind
    protocol_ids: tuple[str, ...] = ()
    numerics: tuple[Numeric, ...] = ()
    criticality_claim: int | None = None

    @field_validator("protocol_ids")
    @classmethod
    def _reject_blank_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """A blank or whitespace id is not a citation.

        Raised rather than dropped, for `RetrievalRequest.__post_init__`'s
        reason: dropping turns a model bug into what looks like an uncited turn,
        and the two want different investigations. Crucially, dropping would
        also let `protocol_ids=("",)` satisfy the "not empty" check in
        `validate_turn` while citing nothing at all.
        """
        blanks = [entry for entry in value if not entry.strip()]
        if blanks:
            raise ValueError(
                f"protocol_ids must not contain blank entries (got {value!r}); "
                "a blank id would satisfy the non-empty citation check while citing nothing"
            )
        return tuple(dict.fromkeys(value))

    @field_validator("criticality_claim")
    @classmethod
    def _reject_off_scale_criticality(cls, value: int | None) -> int | None:
        """1-5 or nothing. A 7 means the model is malfunctioning (CLAUDE.md).

        Mirrors `TriageState.set_level`'s refusal to clamp, and for the same
        reason: 7 coerced to 5 is a malfunction that reads as a working system.
        """
        if value is not None and not 1 <= value <= 5:
            raise ValueError(
                f"criticality_claim must be 1-5 or None, got {value!r}; "
                "a value outside the scale means the model is malfunctioning"
            )
        return value


class GateDecision(BaseModel):
    """What the gate decided, and everything an audit needs about it.

    A result object rather than a bool, per CLAUDE.md. Three things had to travel
    together: whether to speak the turn, which rules refused it, and *what to
    speak instead* - and the third is the one a bool loses. A caller holding only
    `False` has to source the safe line itself, which puts a second copy of the
    rejection behaviour at every call site.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: GateOutcome
    reasons: tuple[RejectionReason, ...] = ()
    #: Human-readable, one per refused rule, for the span and the audit record.
    details: tuple[str, ...] = ()
    #: Numerics that required grounding and did not have it. Carried separately
    #: from `details` because this is the set a corpus-authoring review needs.
    ungrounded: tuple[Numeric, ...] = ()

    @property
    def approved(self) -> bool:
        return self.outcome is GateOutcome.APPROVED

    @property
    def speech(self) -> str | None:
        """What to actually say. None when the caller should speak the turn's own text.

        The rejection path resolves to `SAFE_LINE`, a module constant. No model
        call, no formatting of the turn, no second retrieval: reading this
        property is the entire cost of the failure path (DESIGN CONFLICT-1).
        """
        return None if self.approved else SAFE_LINE


# --------------------------------------------------------------- numeric parsing

#: Word forms that name an integer. Composition ("one hundred twenty") is handled
#: by `_parse_number_words`; this is only the vocabulary.
_UNITS: Mapping[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    # Ordinals, which appear in guidance as "the second dose", "every fifth
    # compression". They name the same integer, so they reduce to it.
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "twelfth": 12,
    "fifteenth": 15,
    "twentieth": 20,
    "thirtieth": 30,
}

_TENS: Mapping[str, int] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "twentieth": 20,
    "thirtieth": 30,
}

_SCALES: Mapping[str, int] = {"hundred": 100, "thousand": 1000}

#: Words naming the decimal point. "decimal" is included because a model told to
#: speak numbers plainly may use it; both reach `_parse_number_words` the same way.
_DECIMAL_POINT_WORDS: frozenset[str] = frozenset({"point", "decimal"})

_NUMBER_WORDS: frozenset[str] = frozenset(
    {*_UNITS, *_TENS, *_SCALES, *_DECIMAL_POINT_WORDS}
)

#: A digit run, optionally decimal, optionally a `:`-joined ratio ("30:2").
#: Thousands separators are handled by `_strip_separators` before this runs.
_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?(?::\d+(?:\.\d+)?)*")

#: `1,200` -> `1200`, but only between digits and only before a 3-digit group, so
#: a list of quantities ("5, 8 centimetres") is untouched. This one is
#: load-bearing rather than cosmetic: without it `_DIGIT_RE` reads "1,200 ml" as
#: the two values 1 and 200, so a document stating 200 would ground a spoken
#: dose of 1,200 - a permissive-direction failure, and a factor-of-six dosing
#: error in the direction of overdose.
_SEPARATOR_RE = re.compile(r"(?<=\d),(?=\d\d\d\b)")

#: NO ordinal-suffix rule here, deliberately. An earlier version stripped "st",
#: "nd", "rd" and "th" from digits so "5th" would read as 5 - and mutation
#: testing showed removing that rule changed no behaviour at all, because
#: `_DIGIT_RE` already matches the bare `5` in "5th" and the suffix is simply not
#: part of the match. It was a mechanism whose only effect was its own comment,
#: which is the defect this repo keeps finding, so it is gone rather than kept as
#: reassurance. Digit ordinals are covered by `_DIGIT_RE`; word ordinals
#: ("fifth") are covered by `_UNITS`, and both are asserted in
#: `test_an_ordinal_reduces_to_the_number_it_names`.

_WORD_RE = re.compile(r"[a-z]+", re.IGNORECASE)


def _is_emergency_number(value: float) -> bool:
    return float(value).is_integer() and int(value) in EMERGENCY_NUMBERS


def _carries_clinical_unit(spoken: str) -> bool:
    """Whether `spoken` names a clinical unit, so it cannot be a street number.

    Word-level rather than substring, so "grams" does not match inside
    "programs" and "g" does not match inside every word containing one.
    """
    return any(word.lower() in CLINICAL_UNITS for word in _WORD_RE.findall(spoken))


def _in_clinical_construction(speech: str, numeric: Numeric) -> bool:
    """Whether `numeric` is spoken as a clinical quantity in `speech`.

    True when its surface form carries a clinical unit, or when it appears as the
    object of a clinical verb ("give six hundred", "push five"). Either is enough
    to overrule a non-clinical provenance label, because neither construction
    describes a street number or a callback digit.

    The verb test looks at the words immediately before the number's surface
    form, not anywhere in the sentence: "Give the aspirin. Confirming, forty two
    Bridge Street" must not read 42 as the object of "give".
    """
    if _carries_clinical_unit(numeric.spoken):
        return True

    position = speech.lower().find(numeric.spoken.lower())
    if position < 0:
        return False
    preceding = _WORD_RE.findall(speech[:position])
    # Three words of context: "give six hundred", "push down five",
    # "give a second". Wider than that starts catching unrelated clauses.
    return any(word.lower() in CLINICAL_VERBS for word in preceding[-3:])


def _strip_separators(text: str) -> str:
    """Join thousands-separated digit groups so `1,200` is one value, not two."""
    return _SEPARATOR_RE.sub("", text)


def _parse_number_words(tokens: Sequence[str]) -> set[float]:
    """Compose a run of number words into the values it names.

    "one hundred twenty" is 120, not {1, 100, 20}. "one hundred to one hundred
    twenty" is two runs (the connector breaks them) and yields {100, 120}.

    This matters in the permissive direction, which is the one that defeats the
    gate: if "one hundred twenty" decomposed to {1, 100, 20}, then a document
    containing 100 and 20 anywhere would ground a spoken rate of 120 that it
    never states.

    DECIMALS. "point" joins the run and everything after it is read as digits
    after the decimal point: "zero point five" is 0.5, "point five" is 0.5,
    "one point two five" is 1.25. Without this the words decomposed into their
    unrelated whole parts - "point five milligrams" yielded {5} and nothing at
    all yielded 0.5 - so an invented "point five milligrams of epinephrine"
    was grounded by any cited document that happened to contain a 5, which the
    CPR card does ("at least 5 centimetres"). That is a false acceptance of a
    fabricated dose, found by independent review.
    """
    values: set[float] = set()
    total = 0
    current = 0
    seen = False
    fraction_digits: list[int] | None = None

    for token in tokens:
        word = token.lower()
        if word in _DECIMAL_POINT_WORDS:
            # Everything from here on is a digit after the point.
            fraction_digits = []
            seen = True
            continue

        if fraction_digits is not None:
            # After the point only single digits are meaningful: "point twenty"
            # is not idiomatic and "point two zero" is 0.20. A word that is not
            # a plain 0-9 digit ends the fractional part rather than being
            # folded into it, so nothing invents a value from unparsed words.
            if word in _UNITS and _UNITS[word] <= 9:
                fraction_digits.append(_UNITS[word])
            continue

        if word in _UNITS:
            current += _UNITS[word]
            seen = True
        elif word in _TENS:
            current += _TENS[word]
            seen = True
        elif word in _SCALES:
            scale = _SCALES[word]
            # "one hundred" -> 100; a bare "hundred" -> 100.
            current = (current or 1) * scale
            if scale >= 1000:
                total += current
                current = 0
            seen = True

    if not seen:
        return values

    # DIGIT STRINGS, spoken digit-by-digit rather than as a quantity. An
    # emergency number is said "nine nine nine" or "nine one one", never "nine
    # hundred ninety-nine", and the arithmetic composition above SUMS those to
    # 27 and 11. So the exemption keyed on the value 999 never fired for the
    # spoken form, and the gate rejected "call nine nine nine" while approving
    # "call 999" - with `prompts.py:106` mandating that numbers be spoken as
    # words, the rejected form is the one the model actually emits.
    #
    # Found by probing the word form of a case that was only ever tested in
    # digits; line 136's comment already claimed "911 spoken as nine one one
    # normalises to 911 too", which was untrue.
    #
    # A digit-by-digit run has ONE reading, so the concatenation REPLACES the
    # sum rather than joining it. Emitting both would leave 27 in the value set
    # for "nine nine nine": the undeclared-numeric net would still fire on it,
    # and worse, a phantom 27 could spuriously ground against a document that
    # happens to contain 27. Nobody means the quantity twenty-seven by
    # "nine nine nine".
    #
    # Three digits is the floor. Shorter runs are ordinary quantities - "five
    # five" is not 55 in any clinical phrasing - and every emergency number is
    # at least three digits. Arithmetic composition is untouched: "one hundred
    # twenty" contains scale words, so it never reaches this branch.
    digit_run: float | None = None
    if fraction_digits is None and len(tokens) >= 3:
        lowered = [token.lower() for token in tokens]
        if all(word in _UNITS and _UNITS[word] <= 9 for word in lowered):
            digit_run = float("".join(str(_UNITS[word]) for word in lowered))

    if digit_run is not None:
        values.add(digit_run)
        return values

    whole = float(total + current)
    if fraction_digits:
        values.add(float(f"{int(whole)}.{''.join(str(d) for d in fraction_digits)}"))
    else:
        values.add(whole)
    return values


def _split_into_number_runs(text: str) -> list[list[str]]:
    """Contiguous runs of number words, split on anything that is not one.

    "and" deliberately does NOT join a run: "thirty and two" is two quantities,
    and treating it as 32 would invent a value neither side said.
    """
    runs: list[list[str]] = []
    current: list[str] = []
    for word in _WORD_RE.findall(text):
        if word.lower() in _NUMBER_WORDS:
            current.append(word)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def numeric_values(text: str) -> frozenset[float]:
    """Every number `text` names, as values, however it was written.

    The one definition of "what numbers are in this text", used for both the
    spoken side and the retrieved side, so the two cannot drift into disagreeing
    - which is the DRY rule that matters most here: a gate whose two sides parse
    numbers differently is a gate that passes ungrounded doses.

    A ratio contributes its parts *and* its composite: "30:2" yields 30, 2 and
    the composite 30.2 is not used - instead `_ratio_tokens` carries the literal
    form. See `_ratio_forms`.
    """
    cleaned = _strip_separators(text)
    values: set[float] = set()

    for match in _DIGIT_RE.findall(cleaned):
        for part in match.split(":"):
            values.add(float(part))

    for run in _split_into_number_runs(cleaned):
        values.update(_parse_number_words(run))

    return frozenset(values)


#: The compressions-to-breaths ratio as the corpus and the responder actually
#: say it: "Thirty compressions, then two rescue breaths", "30 compressions to 2
#: breaths". Captured as (compressions, breaths) so the ORDER is preserved, which
#: is the entire point - the components alone cannot tell "thirty compressions to
#: two breaths" from its inversion.
#:
#: This pattern exists because the `d:d` check below was dead in production: no
#: entry in protocols.py writes a colon ratio, and prompts.py orders the model to
#: speak numbers plainly, so neither side of the comparison ever produced a form
#: and an inverted PROSE ratio passed on its components. Found by independent
#: review, which correctly called the colon-only test a proof about an input
#: shape that cannot occur.
_PROSE_RATIO_RE = re.compile(
    r"(?P<compressions>\d+|[a-z]+(?:[\s-]+[a-z]+)*?)\s*"
    r"compressions?\b[^.;]{0,40}?"
    r"(?P<breaths>\d+|[a-z]+(?:[\s-]+[a-z]+)*?)\s*"
    r"(?:rescue\s+)?breaths?\b",
    re.IGNORECASE,
)


def _normalise_ratio_part(raw: str) -> str | None:
    """One side of a ratio as a canonical number string, or None if it is not one."""
    values = numeric_values(raw)
    if len(values) != 1:
        return None
    value = next(iter(values))
    return f"{value:g}"


def _ratio_forms(text: str) -> frozenset[str]:
    """Ratios as canonical normalised forms, so order survives comparison.

    "30:2" and "thirty compressions, then two rescue breaths" both become the
    single form "30:2".

    Carried in addition to the component values because a ratio is a claim the
    components do not make. A document stating "thirty compressions, then two
    rescue breaths" contains 30 and 2, so a component-only check would ground a
    spoken "two compressions then thirty breaths" - an inversion that means the
    opposite of the protocol - from that very document.

    Two notations are recognised: explicit `d:d`, and the compressions/breaths
    prose form. LIMIT, stated rather than implied: any OTHER prose ratio (a
    drug-to-diluent mix, an inspiratory-to-expiratory time) is not parsed, so for
    those this check contributes nothing and only the component values apply.
    """
    cleaned = _strip_separators(text)
    forms: set[str] = set()

    for match in _DIGIT_RE.findall(cleaned):
        if ":" in match:
            parts = [f"{float(part):g}" for part in match.split(":")]
            forms.add(":".join(parts))

    for prose in _PROSE_RATIO_RE.finditer(cleaned):
        compressions = _normalise_ratio_part(prose.group("compressions"))
        breaths = _normalise_ratio_part(prose.group("breaths"))
        if compressions is not None and breaths is not None:
            forms.add(f"{compressions}:{breaths}")

    return frozenset(forms)


def extract_numerics(
    speech: str, *, provenance: NumericProvenance = NumericProvenance.CLINICAL
) -> tuple[Numeric, ...]:
    """Pull the numbers out of `speech` as `Numeric`s.

    A convenience for tests and for a caller that has plain text rather than a
    model's structured output. The production path expects the model to declare
    its own `numerics`, because only the model knows which of them is a street
    number - but `validate_turn` never trusts that declaration to be *complete*
    (see `RejectionReason.UNDECLARED_NUMERIC`), and this function is how it
    checks.
    """
    cleaned = _strip_separators(speech)
    found: list[Numeric] = []
    seen: set[float] = set()

    for match in _DIGIT_RE.findall(cleaned):
        for part in match.split(":"):
            value = float(part)
            if value not in seen:
                seen.add(value)
                found.append(Numeric(spoken=match, value=value, provenance=provenance))

    for run in _split_into_number_runs(cleaned):
        for value in _parse_number_words(run):
            if value not in seen:
                seen.add(value)
                found.append(
                    Numeric(spoken=" ".join(run), value=value, provenance=provenance)
                )

    return tuple(found)


# -------------------------------------------------------------------- the gate


def _cited_text(
    turn: ClinicalTurn, retrieved: Mapping[str, Retrieved]
) -> str:
    """The concatenated text of exactly the documents this turn cites.

    Exactly the cited ones, not everything retrieved. This is the check that
    stops a number being grounded by a *different* document that happened to
    come back in the same result set: if the model cites the tourniquet card and
    speaks a compression depth, the CPR card being at rank 2 must not launder it.
    """
    return "\n".join(
        retrieved[protocol_id].text
        for protocol_id in turn.protocol_ids
        if protocol_id in retrieved
    )


def validate_turn(
    turn: ClinicalTurn, retrieved: Iterable[Retrieved]
) -> GateDecision:
    """Decide whether `turn` may be spoken, given what retrieval returned.

    A pure function of its two arguments: no model, no network, no session, no
    clock. That is what makes the gate assertable - and it is also what makes the
    rejection path free, because there is nothing here that could issue a second
    LLM call even by accident.

    `retrieved` is this turn's retrieval hits. Passing an earlier turn's hits
    would let a stale document ground a fresh number, so the caller must pass
    what came back now; the gate cannot detect staleness itself and this is
    noted rather than implied to be covered.

    Every rule is evaluated and every failure is collected, rather than returning
    on the first. A turn that both cites nothing and speaks an invented dose has
    two things wrong with it, and an audit that sees only the first would
    conclude the corpus needs a citation added.
    """
    reasons: list[RejectionReason] = []
    details: list[str] = []
    ungrounded: list[Numeric] = []

    by_id = {hit.id: hit for hit in retrieved}

    # DESIGN 7.4 rule 3, and it applies to every kind. A four-minute monologue
    # is a failure whether or not it is clinical.
    if len(turn.speech) > SPEECH_CEILING_CHARS:
        reasons.append(RejectionReason.SPEECH_TOO_LONG)
        details.append(
            f"speech is {len(turn.speech)} characters, ceiling is {SPEECH_CEILING_CHARS}"
        )

    # DESIGN 7.4 rule 4.
    if turn.instruction_kind is InstructionKind.REASSURANCE:
        for pattern in _DIAGNOSIS_RE:
            match = pattern.search(turn.speech)
            if match is not None:
                reasons.append(RejectionReason.DIAGNOSIS_IN_REASSURANCE)
                details.append(
                    f"reassurance contains diagnosis-shaped language: {match.group(0)!r}"
                )
                break

    if turn.instruction_kind.requires_citation:
        # DESIGN 7.4 rule 1.
        if not turn.protocol_ids:
            reasons.append(RejectionReason.MISSING_CITATION)
            details.append(
                "a clinical instruction with no protocol_ids is ungrounded by construction"
            )

        # Citing a document this turn did not retrieve. Not in DESIGN's numbered
        # list, and it has to be here: without it, a model that names a real
        # corpus id it was never shown produces a turn whose numerics are checked
        # against text it could not have read - and if that id is absent from
        # `retrieved`, `_cited_text` is empty, so the numeric check would reject
        # for the wrong reason or, on a turn with no numbers, pass outright.
        unretrieved = [pid for pid in turn.protocol_ids if pid not in by_id]
        if unretrieved:
            reasons.append(RejectionReason.UNRETRIEVED_CITATION)
            details.append(
                f"cited protocol(s) {unretrieved!r} were not retrieved this turn; "
                f"retrieved were {sorted(by_id)!r}"
            )

        cited_text = _cited_text(turn, by_id)
        grounded_values = numeric_values(cited_text)
        grounded_ratios = _ratio_forms(cited_text)

        # DESIGN 7.4 rule 2, in value space rather than verbatim - see the module
        # docstring for why verbatim is unimplementable against this corpus.
        for numeric in turn.numerics:
            if not numeric.requires_grounding:
                continue
            if numeric.value not in grounded_values:
                ungrounded.append(numeric)
                details.append(
                    f"numeric {numeric.value:g} ({numeric.spoken!r}) does not appear in "
                    f"cited protocol(s) {list(turn.protocol_ids)!r}"
                )

        # An explicit ratio must survive as a ratio. "two to thirty" shares its
        # components with "thirty to two" and means the opposite.
        spoken_ratios = _ratio_forms(turn.speech)
        for ratio in sorted(spoken_ratios - grounded_ratios):
            reasons.append(RejectionReason.UNGROUNDED_NUMERIC)
            details.append(
                f"ratio {ratio!r} does not appear in cited protocol(s) "
                f"{list(turn.protocol_ids)!r}"
            )

        if ungrounded:
            reasons.append(RejectionReason.UNGROUNDED_NUMERIC)

        # The declaration check. `turn.numerics` is the model's own account of
        # what it said, and a model that simply omits the invented dose from that
        # list would otherwise walk straight through the rule above - the gate
        # would be checking a list the thing it is gating gets to write. So the
        # speech is re-parsed independently and any clinical value present in the
        # text but absent from both the declaration and the cited documents is a
        # rejection in its own right.
        #
        # A non-clinical provenance exempts a number from needing a CITATION. It
        # must not exempt it from being NOTICED, and an independent review found
        # that it did: a model could speak an invented dose, label it
        # `CALLER_SUPPLIED`, and be skipped by the grounding loop (provenance
        # exempt) *and* by this net (its value present in `declared`) - approving
        # a fabricated milligram figure with nothing anywhere objecting.
        #
        # The exemption is therefore narrowed by what the words say rather than
        # dropped, because dropping it would refuse every read-back of an address
        # or a callback number - the strict-direction harm the provenance enum
        # exists to prevent. A number spoken with a CLINICAL UNIT attached
        # ("milligrams", "centimetres") is a clinical quantity whatever the model
        # labelled it: house numbers do not come in milligrams. So a mislabelled
        # dose is pulled back into the check, while "forty two Bridge Street"
        # stays exempt.
        mislabelled = tuple(
            numeric
            for numeric in turn.numerics
            if not numeric.provenance.requires_grounding
            and _in_clinical_construction(turn.speech, numeric)
        )
        for numeric in mislabelled:
            reasons.append(RejectionReason.MISLABELLED_NUMERIC)
            details.append(
                f"numeric {numeric.value:g} ({numeric.spoken!r}) is declared "
                f"{numeric.provenance.value!r} but carries a clinical unit; "
                "a non-clinical number does not come in clinical units"
            )
            if numeric.value not in grounded_values:
                ungrounded.append(numeric)

        # Every declared value, whatever its provenance. An earlier attempt at
        # the mislabelling fix narrowed this set instead, on the theory that a
        # non-clinical label should not buy a value its way past this net -
        # and mutation testing showed the narrowing was unreachable, because the
        # `mislabelled` rule above already rejects the turn in every case it
        # would have covered. It was dead code dressed as a safety check, so it
        # is gone; `_in_clinical_construction` is where that concern actually
        # lives.
        declared = {numeric.value for numeric in turn.numerics}
        for numeric in extract_numerics(turn.speech):
            if numeric.value in declared or not numeric.requires_grounding:
                continue
            if numeric.value in grounded_values:
                # Undeclared but genuinely present in the cited text. Not a
                # safety failure, so not a rejection: rejecting here would fire
                # on correct guidance whose numerics list was merely incomplete.
                continue
            reasons.append(RejectionReason.UNDECLARED_NUMERIC)
            ungrounded.append(numeric)
            details.append(
                f"numeric {numeric.value:g} ({numeric.spoken!r}) is spoken but was "
                "declared in neither numerics nor the cited protocol text"
            )

    if reasons:
        return GateDecision(
            outcome=GateOutcome.REJECTED,
            reasons=tuple(dict.fromkeys(reasons)),
            details=tuple(details),
            ungrounded=tuple(ungrounded),
        )
    return GateDecision(outcome=GateOutcome.APPROVED)
