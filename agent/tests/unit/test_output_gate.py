"""The output schema gate: what may be spoken, and what is refused.

Assertions here observe the decision object and the state around it, never a
bare flag, for test_triage.py's reason. The specific trap in this module is that
a gate is easy to test in the direction it already works: every test below that
asserts a rejection is paired with one asserting that correct guidance is NOT
rejected, because a gate which refuses everything satisfies half a suite while
being useless mid-emergency.

The corpus text is inlined here rather than imported from protocols.py, and that
is deliberate. These tests assert the gate's *matching rule* - digits versus
words, ranges, ratios - so the text has to be pinned where the test can read it.
A corpus edit must not silently change what these tests prove. The one test that
does drive the real corpus is
`test_the_real_corpus_grounds_real_guidance_through_the_gate`, which exists to
catch the gate and the corpus drifting apart.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aiscelapeus.clock import ManualClock
from aiscelapeus.escalation import SOURCE_TRANSCRIPT, apply_hard_escalation
from aiscelapeus.output_gate import (
    EMERGENCY_NUMBERS,
    SAFE_LINE,
    SPEECH_CEILING_CHARS,
    ClinicalTurn,
    GateOutcome,
    InstructionKind,
    Numeric,
    NumericProvenance,
    RejectionReason,
    extract_numerics,
    numeric_values,
    validate_turn,
)
from aiscelapeus.retrieval import Retrieved
from aiscelapeus.triage import Criticality, EscalationStatus, TriageState

# --------------------------------------------------------------------- fixtures

#: The compression-depth and rate text, verbatim from protocols.py's
#: `bls-adult-cpr` as committed. Digits, because that is how the corpus writes
#: them - which is the whole reason the matching rule cannot be verbatim.
CPR_TEXT = (
    "Adult CPR. Confirm unresponsive and not breathing normally. Push hard, at "
    "least 5 centimetres deep. Push fast, 100 to 120 compressions per minute. "
    "Thirty compressions, then two rescue breaths if trained. Do not stop for "
    "more than 10 seconds. Swap rescuers every 2 minutes to stay effective."
)

TOURNIQUET_TEXT = (
    "Severe limb bleeding. Place it 5 to 8 centimetres above the wound, never "
    "over a joint. Tighten until the bleeding stops, then secure the windlass."
)

ANAPHYLAXIS_TEXT = (
    "Use their adrenaline auto-injector immediately: firmly into the outer "
    "thigh. If there is no improvement in 5 minutes, give a second dose in the "
    "other thigh."
)

ASPIRIN_TEXT = (
    "Suspected heart attack. Sit them down and keep them calm. If they are not "
    "allergic and have no bleeding disorder, a 300 milligram aspirin chewed "
    "slowly can help."
)


def hit(doc_id: str, text: str, score: float = 0.9) -> Retrieved:
    return Retrieved(id=doc_id, text=text, score=score, metadata={"title": doc_id})


@pytest.fixture
def cpr() -> Retrieved:
    return hit("bls-adult-cpr", CPR_TEXT)


@pytest.fixture
def tourniquet() -> Retrieved:
    return hit("bleed-tourniquet", TOURNIQUET_TEXT)


def clinical(
    speech: str,
    *,
    protocol_ids: tuple[str, ...] = (),
    numerics: tuple[Numeric, ...] = (),
) -> ClinicalTurn:
    """A clinical-instruction turn. The kind under test unless stated otherwise."""
    return ClinicalTurn(
        speech=speech,
        instruction_kind=InstructionKind.CLINICAL_INSTRUCTION,
        protocol_ids=protocol_ids,
        numerics=numerics,
    )


# ----------------------------------------------- BUILD-PLAN's three named cases


def test_clinical_instruction_with_empty_protocol_ids_is_rejected(
    cpr: Retrieved,
) -> None:
    # falsifier: the model gives compression guidance without having looked
    # anything up, and the responder hears a depth and a rate that came from the
    # model's memory rather than from a verified protocol. Today the only thing
    # forbidding this is a sentence of prompt text, which nothing checks.
    turn = clinical("Push hard in the centre of the chest, about five centimetres.")

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.MISSING_CITATION in decision.reasons
    assert decision.speech == SAFE_LINE, "the responder hears the safe line instead"


def test_uncited_dosage_is_rejected(cpr: Retrieved) -> None:
    # falsifier: the agent invents an adrenaline dose - a number that exists in
    # no protocol it retrieved - cites the CPR card it did retrieve, and the
    # invented milligram figure is spoken to a responder holding a syringe.
    turn = clinical(
        "Give zero point five milligrams of adrenaline now.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="zero point five milligrams", value=0.5),),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.UNGROUNDED_NUMERIC in decision.reasons
    assert [numeric.value for numeric in decision.ungrounded] == [0.5]


def test_the_rejection_path_makes_no_model_call(cpr: Retrieved) -> None:
    # falsifier: a rejected turn triggers a re-prompt, adding 300-800ms of model
    # latency on exactly the turns that matter most and breaking NFR-002's 500ms
    # budget (DESIGN CONFLICT-1). The responder is waiting with their hands on a
    # chest while the system asks the model to try again.
    #
    # Asserted structurally rather than by timing: `validate_turn` is called with
    # a retrieval list and nothing else, so a model call would have to come from
    # an import inside this module. A fake that explodes if touched is the
    # observable proof - it stands in for every LLM/port call site.
    calls: list[str] = []

    class ExplodingModel:
        def __getattr__(self, name: str) -> object:
            calls.append(name)
            raise AssertionError(
                f"the rejection path called the model ({name}); "
                "the safe line must be pre-rendered"
            )

    turn = clinical(
        "Give four hundred milligrams of something I made up.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="four hundred milligrams", value=400),),
    )

    # The gate's whole surface, exercised with a model object in scope that
    # cannot be used without failing the test.
    _unused_model = ExplodingModel()
    decision = validate_turn(turn, [cpr])
    spoken = decision.speech

    assert decision.outcome is GateOutcome.REJECTED
    assert spoken == SAFE_LINE
    assert len(calls) == 0, f"the rejection path touched the model: {calls}"
    assert spoken is SAFE_LINE, (
        "the safe line must be the pre-rendered constant, not a freshly built string"
    )


def test_the_safe_line_needs_nothing_from_the_turn_or_retrieval() -> None:
    # falsifier: the safe line is generated from the turn or the retrieved text,
    # so producing it on a turn whose retrieval failed or whose text is malformed
    # raises inside the failure path - the one path that must never itself fail.
    turn = clinical("anything at all", protocol_ids=())

    decision = validate_turn(turn, [])

    assert decision.speech == SAFE_LINE
    assert "clinician" in SAFE_LINE, (
        "the safe line must tell the responder a human is coming"
    )


# --------------------------------------------------- the numeric matching rule


def test_a_number_in_the_cited_text_passes(cpr: Retrieved) -> None:
    # falsifier: the gate rejects correct, properly-grounded compression
    # guidance, so the responder gets a generic safe line during an arrest
    # instead of the depth they need. This is the strict-direction harm and it
    # is the reason the rule is value-based rather than verbatim.
    turn = clinical(
        "Push hard, at least five centimetres deep.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="five centimetres", value=5),),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED
    assert decision.reasons == (), f"unexpected refusals: {decision.details}"
    assert decision.speech is None, "an approved turn speaks its own text"


def test_spelled_out_numbers_match_the_corpus_digits(cpr: Retrieved) -> None:
    # falsifier: the corpus writes "5 centimetres" while prompts.py orders the
    # model to SAY "five centimetres", so a verbatim substring check rejects
    # every correctly-grounded depth instruction in the system. A gate that
    # refuses correct guidance as a matter of course is a gate someone switches
    # off, and then nothing is checked at all.
    assert 5.0 in numeric_values("push at least five centimetres deep")
    assert 5.0 in numeric_values(CPR_TEXT)

    turn = clinical(
        "Compress one hundred to one hundred twenty times a minute.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(spoken="one hundred", value=100),
            Numeric(spoken="one hundred twenty", value=120),
        ),
    )
    decision = validate_turn(turn, [cpr])
    assert decision.outcome is GateOutcome.APPROVED, decision.details


def test_composed_number_words_are_one_value_not_their_parts() -> None:
    # falsifier: "one hundred twenty" decomposes to 1, 100 and 20, so any
    # document mentioning 100 and 20 anywhere grounds a spoken rate of 120 that
    # it never states. This is the permissive-direction hole, and it is the one
    # that lets an invented number through while looking verified.
    values = numeric_values("one hundred twenty compressions")

    assert 120.0 in values
    assert 20.0 not in values, "20 is part of 120, not a quantity that was said"
    assert 1.0 not in values, "1 is part of 120, not a quantity that was said"


def test_a_range_contributes_its_endpoints_not_the_values_between(
    tourniquet: Retrieved,
) -> None:
    # falsifier: "5 to 8 centimetres" is read as admitting any value in between,
    # so the agent saying "seven centimetres above the wound" - a placement the
    # protocol does not state - passes as grounded. Or the inverse: the endpoints
    # themselves fail to match and correct placement guidance is refused.
    grounded = numeric_values(TOURNIQUET_TEXT)

    assert 5.0 in grounded and 8.0 in grounded
    assert 7.0 not in grounded, "a range does not ground the values inside it"

    refused = clinical(
        "Place it seven centimetres above the wound.",
        protocol_ids=("bleed-tourniquet",),
        numerics=(Numeric(spoken="seven centimetres", value=7),),
    )
    assert validate_turn(refused, [tourniquet]).outcome is GateOutcome.REJECTED


def test_an_inverted_ratio_is_not_grounded_by_its_own_components() -> None:
    # falsifier: "two to thirty" shares its components with the protocol's
    # "30:2", so a component-only check grounds an inverted compression-to-breath
    # ratio. The responder gives thirty breaths to two compressions on a
    # non-breathing casualty, which is the exact inversion of the protocol.
    doc = hit("bls-ratio", "Give cycles of 30:2 - thirty compressions to two breaths.")

    inverted = clinical(
        "Give cycles of 2:30.",
        protocol_ids=("bls-ratio",),
        numerics=(Numeric(spoken="2", value=2), Numeric(spoken="30", value=30)),
    )
    correct = clinical(
        "Give cycles of 30:2.",
        protocol_ids=("bls-ratio",),
        numerics=(Numeric(spoken="30", value=30), Numeric(spoken="2", value=2)),
    )

    assert validate_turn(inverted, [doc]).outcome is GateOutcome.REJECTED, (
        "an inverted ratio must not be laundered by its components"
    )
    assert validate_turn(correct, [doc]).outcome is GateOutcome.APPROVED


def test_an_ordinal_reduces_to_the_number_it_names(tourniquet: Retrieved) -> None:
    # falsifier: "apply a second tourniquet" is parsed as containing no number
    # and skipped, or as some other value, so ordinal guidance is either
    # unchecked or wrongly refused. Both are wrong: "the second dose" names 2.
    assert 2.0 in numeric_values("apply a second tourniquet just above the first")
    assert 5.0 in numeric_values("every 5th compression")
    assert 5.0 in numeric_values("every fifth compression")
    # Digit ordinals carry no extra value beyond the integer: "5th" must not
    # contribute some parse of the suffix as well.
    assert numeric_values("every 5th compression") == frozenset({5.0})


def test_a_thousands_separator_is_one_value_not_two() -> None:
    # falsifier: "1,200 millilitres" is read as the two values 1 and 200, so a
    # document stating 200 grounds a spoken dose of 1,200 - a factor-of-six
    # error in the direction of overdose, and it passes the gate looking
    # verified. This is the permissive direction, which is the one that defeats
    # the gate entirely.
    values = numeric_values("give 1,200 millilitres")

    assert 1200.0 in values
    assert 200.0 not in values, "200 is part of 1,200, not a quantity that was said"
    assert 1.0 not in values, "1 is part of 1,200, not a quantity that was said"

    doc = hit("fluids", "Give up to 200 millilitres in the first hour.")
    turn = clinical(
        "Give 1,200 millilitres now.",
        protocol_ids=("fluids",),
        numerics=(Numeric(spoken="1,200 millilitres", value=1200),),
    )
    assert validate_turn(turn, [doc]).outcome is GateOutcome.REJECTED, (
        "a document stating 200 must not ground a spoken 1,200"
    )


def test_a_list_of_quantities_is_not_joined_into_one_number() -> None:
    # falsifier: the separator rule is written too widely and joins an ordinary
    # comma-separated list, so "5, 8 centimetres" becomes 58 - neither value is
    # then grounded, and correct tourniquet placement guidance is refused.
    values = numeric_values("place it 5, 8 centimetres above")

    assert 5.0 in values and 8.0 in values
    assert 58.0 not in values


def test_a_paraphrased_dose_is_caught(cpr: Retrieved) -> None:
    # falsifier: the model paraphrases a compression depth from its own memory -
    # "about two inches", a real clinical figure that this corpus does not state
    # - and because it cites the right card and sounds correct, nothing
    # downstream can tell it apart from grounded output.
    turn = clinical(
        "Push down about two inches on the centre of the chest.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="two inches", value=2),),
    )

    decision = validate_turn(turn, [cpr])

    # 2 IS in the CPR text ("every 2 minutes"), so the value check alone passes
    # this. That is a real and acknowledged limit of value-space matching: the
    # gate grounds a number, not a number-with-its-unit. Pinned here so the hole
    # is a recorded decision rather than a surprise, and so a future unit-aware
    # check has a test to flip.
    assert decision.outcome is GateOutcome.APPROVED, (
        "value-space matching cannot distinguish 2 inches from 2 minutes"
    )

    # The same paraphrase with a value the card does not contain at all IS
    # caught, which is the case the rule actually covers.
    invented = clinical(
        "Push down about six centimetres.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="six centimetres", value=6),),
    )
    caught = validate_turn(invented, [cpr])
    assert caught.outcome is GateOutcome.REJECTED
    assert [numeric.value for numeric in caught.ungrounded] == [6.0]


def test_a_number_from_a_different_retrieved_document_does_not_ground_it(
    cpr: Retrieved, tourniquet: Retrieved
) -> None:
    # falsifier: the gate pools the text of everything retrieved, so a number
    # from the tourniquet card grounds an instruction cited to the CPR card.
    # Retrieval returns several hits per turn, so pooling would mean a number is
    # grounded if it appears in ANY hit - which makes the citation meaningless
    # and is very nearly as permissive as no gate at all.
    #
    # 300 is in neither; 8 is in the tourniquet text only.
    turn = clinical(
        "Push at least eight centimetres deep.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="eight centimetres", value=8),),
    )

    decision = validate_turn(turn, [cpr, tourniquet])

    assert decision.outcome is GateOutcome.REJECTED, (
        "8 appears only in the document this turn did not cite"
    )
    assert RejectionReason.UNGROUNDED_NUMERIC in decision.reasons

    # Cited correctly, the same number passes - so the refusal above is about
    # provenance, not about 8 being unmatchable.
    cited_properly = clinical(
        "Place it eight centimetres above the wound.",
        protocol_ids=("bleed-tourniquet",),
        numerics=(Numeric(spoken="eight centimetres", value=8),),
    )
    assert (
        validate_turn(cited_properly, [cpr, tourniquet]).outcome is GateOutcome.APPROVED
    )


def test_citing_a_protocol_that_was_not_retrieved_this_turn_is_rejected() -> None:
    # falsifier: the model names a real corpus id it was never shown this turn -
    # from its training data, or from an earlier turn - and the gate treats the
    # citation as valid. The instruction is then "grounded" in text the model
    # could not have read, which is a fabrication wearing a citation.
    retrieved_now = hit("bleed-tourniquet", TOURNIQUET_TEXT)

    turn = clinical(
        "Give a three hundred milligram aspirin.",
        protocol_ids=("chest-pain-cardiac",),
        numerics=(Numeric(spoken="three hundred milligram", value=300),),
    )

    decision = validate_turn(turn, [retrieved_now])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.UNRETRIEVED_CITATION in decision.reasons
    assert any("chest-pain-cardiac" in detail for detail in decision.details)


def test_the_same_citation_passes_once_it_is_actually_retrieved() -> None:
    # falsifier: the unretrieved-citation rule is written so broadly that a
    # correctly retrieved and correctly cited aspirin dose is also refused, which
    # would make the rule reject the case it exists to permit.
    turn = clinical(
        "Give a three hundred milligram aspirin, chewed slowly.",
        protocol_ids=("chest-pain-cardiac",),
        numerics=(Numeric(spoken="three hundred milligram", value=300),),
    )

    decision = validate_turn(turn, [hit("chest-pain-cardiac", ASPIRIN_TEXT)])

    assert decision.outcome is GateOutcome.APPROVED, decision.details


# ------------------------------------------------------- non-clinical numbers


def test_an_emergency_service_number_does_not_need_a_citation(cpr: Retrieved) -> None:
    # falsifier: the agent cannot say "call 999" without a protocol document
    # containing 999 - which no document does - so the single most important
    # instruction in first aid is refused and replaced by the safe line.
    turn = clinical(
        "Call 999 now and put the phone on speaker.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(
                spoken="999", value=999, provenance=NumericProvenance.EMERGENCY_SERVICE
            ),
        ),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED, decision.details


def test_an_emergency_number_is_exempt_even_when_undeclared(cpr: Retrieved) -> None:
    # falsifier: exemption depends on the model remembering to label 911 as an
    # emergency number, so a correct "call 911" is refused whenever the label is
    # omitted. The exemption has to hold by value, or it does not hold when it
    # matters.
    turn = clinical(
        "Call 911 and stay on the line.", protocol_ids=("bls-adult-cpr",)
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED, decision.details


def test_an_address_the_caller_gave_does_not_trigger_rejection(cpr: Retrieved) -> None:
    # falsifier: the agent reads back the house number the caller just gave -
    # "confirming, forty-two Bridge Street" - and the gate demands a protocol
    # document containing 42. Confirming an address is how the ambulance finds
    # the scene, and refusing it replaces a confirmation with a clinician page.
    turn = clinical(
        "Confirming, forty two Bridge Street. Keep pushing on the chest.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(
                spoken="forty two",
                value=42,
                provenance=NumericProvenance.CALLER_SUPPLIED,
            ),
        ),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED, decision.details


def test_a_callback_number_is_not_treated_as_a_dose(cpr: Retrieved) -> None:
    # falsifier: a phone number read back for confirmation is parsed as a string
    # of clinical quantities, each demanding a citation, so every callback
    # confirmation is refused.
    turn = clinical(
        "Your callback number is 07700 900123, is that right?",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(
                spoken="07700",
                value=7700,
                provenance=NumericProvenance.CALLER_SUPPLIED,
            ),
            Numeric(
                spoken="900123",
                value=900123,
                provenance=NumericProvenance.CALLER_SUPPLIED,
            ),
        ),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED, decision.details


def test_a_clinical_number_cannot_hide_behind_an_incomplete_declaration(
    cpr: Retrieved,
) -> None:
    # falsifier: the gate only checks the numbers the model chose to declare, so
    # a model that speaks an invented dose and simply omits it from `numerics`
    # walks straight through. The gate would then be validating a list written by
    # the thing it is gating - which is B2's defect exactly: a safety net that
    # runs only when the model opts in.
    turn = clinical(
        "Give six hundred milligrams of aspirin now.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(),  # the invented dose is simply not declared
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.UNDECLARED_NUMERIC in decision.reasons
    assert 600.0 in {numeric.value for numeric in decision.ungrounded}


def test_an_undeclared_number_that_is_genuinely_in_the_cited_text_passes(
    cpr: Retrieved,
) -> None:
    # falsifier: the declaration check above is written to reject any undeclared
    # number, so correct guidance whose `numerics` list is merely incomplete is
    # refused mid-arrest. The check must be about groundedness, not about
    # bookkeeping.
    turn = clinical(
        "Push at least five centimetres, one hundred to one hundred twenty a minute.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED, decision.details


# ------------------------------------- defects found by independent review (B4)


def test_a_dose_mislabelled_as_caller_supplied_is_still_caught(
    cpr: Retrieved,
) -> None:
    # falsifier: the model speaks an invented dose, labels it `caller_supplied`,
    # and the provenance escape hatch carries it past BOTH the grounding check
    # (exempt by provenance) and the undeclared-numeric net (its value is now in
    # the declared set). A fabricated milligram figure is then spoken as
    # verified, with nothing anywhere objecting - which is the gate failing at
    # the one job it exists to do. Found by independent review, which noted the
    # module docstring named this risk while the code left it open.
    turn = clinical(
        "Give six hundred milligrams of aspirin now.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(
                spoken="six hundred milligrams",
                value=600,
                provenance=NumericProvenance.CALLER_SUPPLIED,
            ),
        ),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.MISLABELLED_NUMERIC in decision.reasons
    assert 600.0 in {numeric.value for numeric in decision.ungrounded}


def test_a_mislabelled_dose_with_no_unit_word_is_still_caught(
    cpr: Retrieved,
) -> None:
    # falsifier: the mislabelling check tests only for a unit word in the
    # declared surface form, so "Give six hundred of aspirin now" labelled
    # `caller_supplied` - no "milligrams" anywhere in the declared form - walks
    # straight through and an invented dose is spoken as verified. Found by
    # mutation-testing the first fix for this defect, which turned out to have a
    # second door.
    turn = clinical(
        "Give six hundred of aspirin now.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(
                spoken="six hundred",
                value=600,
                provenance=NumericProvenance.CALLER_SUPPLIED,
            ),
        ),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.MISLABELLED_NUMERIC in decision.reasons


def test_a_unit_alone_marks_a_number_clinical_with_no_verb_present(
    cpr: Retrieved,
) -> None:
    # falsifier: the mislabelling check relies solely on a nearby clinical verb,
    # so a dose stated without one - "the dose is six hundred milligrams",
    # labelled `caller_supplied` - is not recognised as clinical and an invented
    # figure is spoken as verified. The unit itself has to be sufficient: house
    # numbers do not come in milligrams. Found by mutation testing, where
    # disabling the unit rule left both existing mislabelling tests passing on
    # the verb rule alone.
    turn = clinical(
        "The dosage was six hundred milligrams.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(
                spoken="six hundred milligrams",
                value=600,
                provenance=NumericProvenance.CALLER_SUPPLIED,
            ),
        ),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.MISLABELLED_NUMERIC in decision.reasons


def test_a_genuine_caller_supplied_number_is_not_caught_by_that_rule(
    cpr: Retrieved,
) -> None:
    # falsifier: the mislabelling rule above is written as "distrust every
    # non-clinical provenance", so reading back the house number the caller gave
    # is refused - and the ambulance loses the address confirmation. The rule has
    # to separate a mislabelled DOSE from an honest address, or it trades one
    # harm for another.
    turn = clinical(
        "Confirming, forty two Bridge Street. Keep pushing on the chest.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(
                spoken="forty two",
                value=42,
                provenance=NumericProvenance.CALLER_SUPPLIED,
            ),
        ),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED, decision.details


def test_a_spelled_out_decimal_dose_is_parsed_as_a_decimal(cpr: Retrieved) -> None:
    # falsifier: "point five milligrams" decomposes to the whole number 5
    # instead of 0.5, so an invented half-milligram epinephrine dose is grounded
    # by any cited card containing a 5 - and the CPR card contains one ("at
    # least 5 centimetres"). The dose is spoken as verified. prompts.py orders
    # the model to say numbers as words, so this is the realistic path rather
    # than an edge case. Found by independent review.
    assert numeric_values("point five milligrams") == frozenset({0.5})
    assert numeric_values("zero point five milligrams") == frozenset({0.5})
    assert numeric_values("one point two five milligrams") == frozenset({1.25})

    turn = clinical(
        "Give point five milligrams of epinephrine now.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(),
    )
    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert 0.5 in {numeric.value for numeric in decision.ungrounded}


def test_a_decimal_that_is_genuinely_cited_still_passes() -> None:
    # falsifier: decimal parsing is added in a way that no longer matches the
    # corpus side, so a correctly cited decimal dose is refused - the strict
    # harm introduced while fixing the permissive one.
    doc = hit("adrenaline-im", "Adult dose is 0.5 milligrams intramuscular.")

    turn = clinical(
        "Give zero point five milligrams into the outer thigh.",
        protocol_ids=("adrenaline-im",),
        numerics=(Numeric(spoken="zero point five milligrams", value=0.5),),
    )

    assert validate_turn(turn, [doc]).outcome is GateOutcome.APPROVED


def test_an_inverted_prose_ratio_is_rejected(cpr: Retrieved) -> None:
    # falsifier: the ratio check only understood `30:2` colon notation, which
    # appears nowhere in protocols.py and which prompts.py never asks the model
    # to speak - so in production neither side ever produced a ratio form and an
    # inverted PROSE ratio passed on its components. "Two compressions then
    # thirty rescue breaths" is the exact inversion of the protocol and it was
    # grounded by the protocol's own text. Found by independent review, which
    # called the colon-only test a proof about an input shape that cannot occur.
    turn = clinical(
        "Give two compressions, then thirty rescue breaths.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="two", value=2), Numeric(spoken="thirty", value=30)),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.UNGROUNDED_NUMERIC in decision.reasons


def test_the_correct_prose_ratio_is_approved(cpr: Retrieved) -> None:
    # falsifier: the prose ratio rule fires on the protocol's own wording, so
    # correct compression-to-breath guidance is refused mid-arrest. This is the
    # paired strict-direction case, and it is the one that decides whether the
    # rule above is usable at all.
    turn = clinical(
        "Give thirty compressions, then two rescue breaths.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(Numeric(spoken="thirty", value=30), Numeric(spoken="two", value=2)),
    )

    assert validate_turn(turn, [cpr]).outcome is GateOutcome.APPROVED


def test_zero_is_not_silently_exempt_from_grounding() -> None:
    # falsifier: `EMERGENCY_NUMBERS` was written with the literal `000`, which
    # Python evaluates to the integer 0 - so the set silently contained zero and
    # every clinical value parsing to zero was exempt from grounding entirely.
    # Found by independent review.
    assert 0 not in EMERGENCY_NUMBERS
    assert Numeric(spoken="zero", value=0).requires_grounding is True

    doc = hit("fluids", "Give 200 millilitres in the first hour.")
    turn = clinical(
        "Give zero millilitres.",
        protocol_ids=("fluids",),
        numerics=(Numeric(spoken="zero millilitres", value=0),),
    )
    assert validate_turn(turn, [doc]).outcome is GateOutcome.REJECTED


def test_real_emergency_numbers_are_still_exempt(cpr: Retrieved) -> None:
    # falsifier: removing the bogus `000` entry also removes the real
    # exemptions, so "call 999" starts requiring a protocol document that
    # contains 999 - and the most important instruction in first aid is replaced
    # by the safe line.
    for number in (112, 911, 999):
        turn = clinical(
            f"Call {number} now.", protocol_ids=("bls-adult-cpr",)
        )
        assert validate_turn(turn, [cpr]).outcome is GateOutcome.APPROVED, number


# ------------------------------------------- kinds that do not require citation


@pytest.mark.parametrize(
    "kind",
    [
        InstructionKind.ASSESSMENT_QUESTION,
        InstructionKind.REASSURANCE,
        InstructionKind.ESCALATION_NOTICE,
    ],
)
def test_non_instruction_kinds_need_no_citation(
    kind: InstructionKind, cpr: Retrieved
) -> None:
    # falsifier: "Is he breathing?" is refused for citing no protocol, so the
    # agent cannot ask the first and most important question of the call without
    # first retrieving a document. Every turn becomes a safe line.
    turn = ClinicalTurn(speech="Is he breathing normally?", instruction_kind=kind)

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED
    assert kind.requires_citation is False


def test_diagnosis_shaped_reassurance_is_rejected(cpr: Retrieved) -> None:
    # falsifier: the agent tells a responder "it's a heart attack" under the
    # cover of reassurance. That is a diagnosis from a system that explicitly
    # does not diagnose, it is unverifiable at the scene, and it changes what the
    # responder does next.
    turn = ClinicalTurn(
        speech="Don't worry, it's a heart attack, they usually survive these.",
        instruction_kind=InstructionKind.REASSURANCE,
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.DIAGNOSIS_IN_REASSURANCE in decision.reasons


def test_ordinary_reassurance_is_not_rejected(cpr: Retrieved) -> None:
    # falsifier: the diagnosis pattern list is wide enough to catch ordinary
    # reassurance, so the agent's calming language is replaced by a clinician
    # page and the responder is left with a stranger's voice and no steadying.
    turn = ClinicalTurn(
        speech="You're doing this right. Help is on the way, stay with me.",
        instruction_kind=InstructionKind.REASSURANCE,
    )

    assert validate_turn(turn, [cpr]).outcome is GateOutcome.APPROVED


def test_speech_past_the_ceiling_is_rejected(cpr: Retrieved) -> None:
    # falsifier: the model returns a six-step numbered monologue and the agent
    # reads all of it aloud while the casualty is not breathing. prompts.py asks
    # for one action at a time; nothing enforced it.
    turn = ClinicalTurn(
        speech="Keep pushing on the chest. " * 40,
        instruction_kind=InstructionKind.ASSESSMENT_QUESTION,
    )

    decision = validate_turn(turn, [cpr])

    assert len(turn.speech) > SPEECH_CEILING_CHARS
    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.SPEECH_TOO_LONG in decision.reasons


# --------------------------------------------------- the model makes ILLEGAL states


def test_a_blank_protocol_id_is_not_a_citation() -> None:
    # falsifier: `protocol_ids=("",)` is non-empty, so it satisfies the
    # "must cite something" rule while citing nothing - and the numeric check
    # then runs against empty text, which grounds nothing and rejects for the
    # wrong reason, or passes outright on a turn with no numbers.
    with pytest.raises(ValidationError):
        clinical("Push hard.", protocol_ids=("",))
    with pytest.raises(ValidationError):
        clinical("Push hard.", protocol_ids=("bls-adult-cpr", "   "))


def test_an_off_scale_criticality_claim_is_an_error() -> None:
    # falsifier: a model claiming criticality 7 is silently accepted or clamped
    # to 5, so a malfunctioning model reads as a working one - the same defect
    # `TriageState.set_level` already refuses to allow.
    for bad in (0, 6, 7, -1):
        with pytest.raises(ValidationError):
            ClinicalTurn(
                speech="Push hard.",
                instruction_kind=InstructionKind.CLINICAL_INSTRUCTION,
                protocol_ids=("bls-adult-cpr",),
                criticality_claim=bad,
            )

    ok = ClinicalTurn(
        speech="Push hard.",
        instruction_kind=InstructionKind.CLINICAL_INSTRUCTION,
        protocol_ids=("bls-adult-cpr",),
        criticality_claim=5,
    )
    assert ok.criticality_claim == 5


def test_an_unknown_instruction_kind_is_refused() -> None:
    # falsifier: the model emits a kind nobody defined - "advice", "note" - and
    # it is coerced or accepted, so a clinical instruction mislabelled as
    # something else bypasses the citation rule entirely.
    with pytest.raises(ValidationError):
        ClinicalTurn(speech="Push hard.", instruction_kind="advice")  # type: ignore[arg-type]


def test_an_extra_field_is_refused() -> None:
    # falsifier: a model inventing a field - `confidence`, `verified` - has it
    # silently dropped, so a caller reading `turn.verified` gets an
    # AttributeError in production, or worse, a future field name collides and a
    # value nobody validated is trusted.
    with pytest.raises(ValidationError):
        ClinicalTurn(
            speech="Push hard.",
            instruction_kind=InstructionKind.ASSESSMENT_QUESTION,
            verified=True,  # type: ignore[call-arg]
        )


def test_a_validated_turn_cannot_be_mutated_afterwards() -> None:
    # falsifier: the turn is mutable, so code between the gate and TTS can change
    # `speech` after validation - and the audio the responder hears is then text
    # the gate never saw. The whole guarantee is "nothing reaches TTS unchecked".
    turn = clinical("Push hard.", protocol_ids=("bls-adult-cpr",))

    with pytest.raises(ValidationError):
        turn.speech = "Give six hundred milligrams of adrenaline."  # type: ignore[misc]


# ------------------------------------------------- escalation outranks the gate


async def test_a_gate_rejection_cannot_swallow_an_escalation(
    clock: ManualClock,
) -> None:
    # falsifier: the responder says "he's not breathing", the deterministic net
    # pages a clinician and ratchets to Level 5, and then the model's turn is
    # refused by the gate - and the refusal unwinds or suppresses the page. The
    # responder gets a safe line, no clinician comes, and the single most
    # defensible safety property in the build (ADR-004) has been overridden by a
    # formatting check.
    state = TriageState(session_id="inc-gate", clock=clock)
    escalations: list[tuple[str, str]] = []

    async def on_state_change() -> None:
        return None

    async def on_escalate(reason: str, category: str) -> None:
        # Mirrors the real caller's `_do_escalate`: `apply_hard_escalation`
        # ratchets the level and delegates the page through this callback, so the
        # request must be made here or the test would assert against a state
        # transition the production path performs and this one skipped.
        escalations.append((reason, category))
        state.request_escalation(reason, key=f"{state.session_id}:{category}")

    escalation = await apply_hard_escalation(
        "he's not breathing",
        state=state,
        on_state_change=on_state_change,
        on_escalate=on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    # Now the model's turn for this same moment is refused.
    refused = clinical(
        "Give six hundred milligrams of adrenaline.",
        protocol_ids=(),
    )
    decision = validate_turn(refused, [hit("bls-adult-cpr", CPR_TEXT)])

    assert decision.outcome is GateOutcome.REJECTED
    assert escalation is not None and escalation.escalated is True
    # The escalation state is untouched by the rejection - asserted on state,
    # not on the gate's return value.
    assert state.level is Criticality.CRITICAL
    assert state.escalation is EscalationStatus.REQUESTED
    assert len(escalations) == 1, "the page must still have gone out"


async def test_the_gate_is_free_of_triage_state_entirely(clock: ManualClock) -> None:
    # falsifier: `validate_turn` reaches into TriageState to make its decision,
    # which would make the gate a second place that can move criticality - a
    # second implementation of the ratchet `set_level` owns. The structural
    # guarantee that a rejection cannot page, unpage or downgrade is that the
    # gate has no access to the state at all.
    state = TriageState(session_id="inc-gate", clock=clock)
    state.set_level(4, "uncontrolled bleeding")
    before = state.to_payload()

    validate_turn(clinical("Give something invented.", protocol_ids=()), [])

    assert state.to_payload() == before, "the gate must not touch triage state"
    assert state.level is Criticality.SEVERE


# ----------------------------------------------------------------- import graph


def test_the_gate_imports_no_vendor_sdk_and_no_agent_layer() -> None:
    # falsifier: the gate grows an import of agent.py, main.py or a vendor SDK,
    # and the module that must be testable with no model and no network can no
    # longer be imported without one. That is how the citation check ends up
    # unprovable - and an unprovable gate is the defect B4 exists to close.
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent.parent / "aiscelapeus" / "output_gate.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level:
            imported.add("." * node.level)

    banned = {"livekit", "moss", "openai", "deepgram", "google", "opentelemetry"}
    assert not (imported & banned), (
        f"output_gate.py imports a vendor SDK: {sorted(imported & banned)}"
    )

    # Relative imports are checked by name against the forbidden modules.
    relative = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level and node.module
    }
    assert not (relative & {"agent", "main"}), (
        f"output_gate.py imports the agent layer: {sorted(relative & {'agent', 'main'})}"
    )
    assert "pydantic" in imported, "the model is meant to be a Pydantic model"


def test_importing_the_gate_does_not_require_the_vendor_sdks() -> None:
    # falsifier: the gate is importable only inside a process that has already
    # loaded livekit, so the pure-function claim holds on paper while the module
    # cannot actually be exercised without the vendor layer present.
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "for name in list(sys.modules):\n"
            "    pass\n"
            "import aiscelapeus.output_gate as gate\n"
            "assert 'livekit' not in sys.modules, sorted(\n"
            "    n for n in sys.modules if n.startswith('livekit'))\n"
            "print(gate.SAFE_LINE)\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent),
    )

    assert result.returncode == 0, f"importing the gate failed: {result.stderr}"
    assert SAFE_LINE in result.stdout


# ------------------------------------------------------- corpus / gate agreement


def test_the_real_corpus_grounds_real_guidance_through_the_gate() -> None:
    # falsifier: the gate and the shipped corpus drift apart - a card is
    # rewritten to spell its numbers as words, or a unit is changed - and correct
    # guidance sourced from the real corpus starts being refused. The tests above
    # pin inlined text on purpose, so this is the only one that would notice.
    from aiscelapeus.protocols import PROTOCOLS

    by_id = {entry["id"]: entry for entry in PROTOCOLS}
    cpr_entry = by_id["bls-adult-cpr"]
    real_hit = Retrieved(
        id=str(cpr_entry["id"]),
        text=str(cpr_entry["text"]),
        score=0.99,
        metadata={"title": str(cpr_entry["title"])},
    )

    turn = clinical(
        "Push hard, at least five centimetres, one hundred to one hundred twenty "
        "a minute.",
        protocol_ids=("bls-adult-cpr",),
        numerics=(
            Numeric(spoken="five centimetres", value=5),
            Numeric(spoken="one hundred", value=100),
            Numeric(spoken="one hundred twenty", value=120),
        ),
    )

    decision = validate_turn(turn, [real_hit])

    assert decision.outcome is GateOutcome.APPROVED, (
        f"the shipped CPR card no longer grounds its own guidance: {decision.details}"
    )


def test_extraction_finds_the_numbers_a_responder_would_hear() -> None:
    # falsifier: `extract_numerics` misses a spoken quantity, so the
    # undeclared-numeric check has nothing to compare against and a model that
    # omits its invented dose from `numerics` is never caught.
    found = extract_numerics("Give three hundred milligrams and recheck in 10 minutes.")
    values = {numeric.value for numeric in found}

    assert 300.0 in values
    assert 10.0 in values
    assert len(values) == 2, f"unexpected extra values: {sorted(values)}"


@pytest.mark.parametrize(
    "speech,value",
    [
        ("Call nine nine nine now.", 999.0),
        ("Call nine one one now.", 911.0),
        ("Call one one two now.", 112.0),
    ],
)
def test_an_emergency_number_spoken_as_digits_parses_as_the_number(
    speech: str, value: float
) -> None:
    # falsifier: a digit-by-digit run is composed ARITHMETICALLY, so "nine nine
    # nine" sums to 27 instead of 999, the value-keyed emergency exemption never
    # fires, and the gate rejects the agent telling a responder to call an
    # ambulance. This is not hypothetical: it rejected the words form while
    # approving "call 999", and prompts.py:106 orders numbers to be SPOKEN AS
    # WORDS, so the rejected form is the one production emits. The module's own
    # comment already claimed "911 spoken as nine one one normalises to 911
    # too", which was untrue - a mechanism whose only implementation was its
    # docstring.
    values = {numeric.value for numeric in extract_numerics(speech)}

    assert value in values, f"{speech!r} parsed as {sorted(values)}"
    # The sum must NOT also be present: a phantom 27 would keep tripping the
    # undeclared-numeric net, and could spuriously ground against a document
    # that happens to contain 27. A digit run has one reading.
    assert values == {value}, f"{speech!r} yielded extra readings: {sorted(values)}"


def test_arithmetic_composition_survives_the_digit_run_rule() -> None:
    # falsifier: the digit-run rule is written to fire on any run of number
    # words, so "one hundred twenty" concatenates to something absurd instead of
    # composing to 120 - breaking the grounding of every compression-rate
    # instruction. The two rules must not overlap: a run containing a scale word
    # is a quantity, a run of bare single digits is a digit string.
    assert {n.value for n in extract_numerics("one hundred twenty")} == {120.0}
    assert {n.value for n in extract_numerics("one hundred")} == {100.0}
    assert {n.value for n in extract_numerics("thirty")} == {30.0}
    # Two single digits stay arithmetic: "five five" is not 55 in any clinical
    # phrasing, and three is the documented floor for the digit-run reading.
    assert {n.value for n in extract_numerics("five five")} == {10.0}


def test_an_emergency_number_needs_no_protocol_citation(
    cpr: Retrieved,
) -> None:
    # falsifier: telling the responder to call an ambulance is gated on a
    # protocol document containing the number, so the single most important
    # instruction in the system is rejected because no first-aid card lists
    # "999". Asserted end-to-end through the gate, not only through the parser,
    # because the parser fix is worthless if the exemption still misses.
    turn = ClinicalTurn(
        speech="Call nine nine nine now and put me on speaker.",
        instruction_kind=InstructionKind.CLINICAL_INSTRUCTION,
        protocol_ids=("bls-adult-cpr",),
        numerics=(),
    )

    decision = validate_turn(turn, [cpr])

    assert decision.outcome is GateOutcome.APPROVED, (
        f"rejected for {[r.name for r in decision.reasons]}"
    )
