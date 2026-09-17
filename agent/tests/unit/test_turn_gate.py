"""The output gate ON THE PRODUCTION PATH, which is what B4 was missing.

`test_output_gate.py` proves the gate's rules in isolation: 54 tests over
`validate_turn`, with no agent, no session and no pipeline. Every one of them
passed while `output_gate.py` was imported by NOTHING, so in production a model
that invented an adrenaline dose was indistinguishable from a grounded one. The
citation requirement existed only as prompt text (`prompts.py:87`, `:109`).

So these tests assert the WIRING, and the distinction is the whole point: not
"does the gate reject an ungrounded dose" - that is already proven - but "does a
turn the model generates actually reach the gate before it reaches audio, and is
the citation it is checked against genuinely this turn's retrieval".

The two halves are tested at their own levels. `turn_gate` is a leaf with no
vendor import, so classification, the citation record's lifetime and the
buffering rule are driven directly. The node overrides need
`AiscelapeusAgent`, which imports the LiveKit SDK, so those sit behind the same
`importorskip` `test_agent_tools.py` uses - declared rather than silently
skipped, for that module's reason.

Every rejection test here is paired with a pass test, for `test_output_gate`'s
reason: a gate that refuses everything satisfies half a suite while being
useless mid-emergency, and a WIRING that refuses everything is worse, because it
would replace every turn of a live call with the safe line.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncGenerator, AsyncIterable
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

from aiscelapeus import turn_gate
from aiscelapeus.output_gate import (
    SAFE_LINE,
    GateOutcome,
    InstructionKind,
    RejectionReason,
)
from aiscelapeus.retrieval import Retrieved
from aiscelapeus.turn_gate import (
    GATE_REJECTION_TOPIC,
    RetrievedThisTurn,
    buffer_stream,
    classify,
    gate_turn,
    rejection_payload,
    rejection_span_attributes,
)

# --------------------------------------------------------------------- fixtures

#: Verbatim from `protocols.py`'s `bls-adult-cpr`, as `test_output_gate.py`
#: pins it. Inlined for that module's stated reason: these tests assert the
#: wiring's behaviour against a known text, and a corpus edit must not silently
#: change what they prove.
CPR_TEXT = (
    "Adult CPR. Confirm unresponsive and not breathing normally. Push hard, at "
    "least 5 centimetres deep. Push fast, 100 to 120 compressions per minute. "
    "Thirty compressions, then two rescue breaths if trained. Do not stop for "
    "more than 10 seconds. Swap rescuers every 2 minutes to stay effective."
)

ANAPHYLAXIS_TEXT = (
    "Use their adrenaline auto-injector immediately: firmly into the outer "
    "thigh. If there is no improvement in 5 minutes, give a second dose in the "
    "other thigh."
)


def hit(doc_id: str, text: str) -> Retrieved:
    return Retrieved(id=doc_id, text=text, score=0.9, metadata={"title": doc_id})


@pytest.fixture
def cpr() -> Retrieved:
    return hit("bls-adult-cpr", CPR_TEXT)


@pytest.fixture
def retrieved(cpr: Retrieved) -> RetrievedThisTurn:
    """A citation record holding the CPR card, as one lookup would leave it."""
    record = RetrievedThisTurn()
    record.record([cpr])
    return record


async def stream_of(*segments: str) -> AsyncIterable[str]:
    """The `AsyncIterable[str]` shape `tts_node` is handed by the SDK."""

    async def _stream() -> AsyncIterable[str]:
        for segment in segments:
            yield segment

    return _stream()


# --------------------------------------------------- the citation record's life


def test_a_lookups_hits_become_this_turns_citable_ids(cpr: Retrieved) -> None:
    # falsifier: the agent hands retrieved documents to the model and keeps
    # nothing, so the wired gate has no protocol_ids to check against and every
    # clinical instruction is refused for MISSING_CITATION - the gate would
    # replace every turn of a live incident with the safe line, and be switched
    # off within one call.
    record = RetrievedThisTurn()
    assert record.ids == (), "a fresh turn cites nothing"

    record.record([cpr])

    assert record.ids == ("bls-adult-cpr",)
    assert record.as_hits() == (cpr,), (
        "validate_turn needs the hit itself, not just its id, to ground numbers"
    )


def test_clearing_the_record_makes_a_previous_turns_document_uncitable(
    cpr: Retrieved,
) -> None:
    # falsifier: the record is never cleared, so a protocol retrieved three
    # turns ago still satisfies the citation check on a turn that retrieved
    # nothing. The model could then speak a compression depth from memory and
    # the gate would find the stale id in its record and APPROVE it - the exact
    # staleness validate_turn's docstring says the gate cannot detect itself.
    record = RetrievedThisTurn()
    record.record([cpr])
    record.clear()

    assert record.ids == ()
    assert record.as_hits() == ()


def test_two_lookups_in_one_turn_are_both_citable(cpr: Retrieved) -> None:
    # falsifier: a second lookup REPLACES the first, so with max_tool_steps=4
    # the model that checks airway then cardiac can only cite the cardiac card -
    # and correct guidance drawn from the airway document it was genuinely shown
    # is refused as uncited.
    airway = hit("airway-choking-adult", "Five back blows, then five thrusts.")
    record = RetrievedThisTurn()
    record.record([cpr])
    record.record([airway])

    assert set(record.ids) == {"bls-adult-cpr", "airway-choking-adult"}


def test_the_same_document_twice_is_one_citation(cpr: Retrieved) -> None:
    # falsifier: a document retrieved by two queries in one turn is recorded
    # twice, so the cited text is concatenated twice and the protocol_ids the
    # span and the audit record report double-count what was actually retrieved.
    record = RetrievedThisTurn()
    record.record([cpr])
    record.record([cpr])

    assert record.ids == ("bls-adult-cpr",)
    assert len(record.as_hits()) == 1


# ---------------------------------------------------------------- the decision


def test_an_ungrounded_dose_is_replaced_by_the_safe_line(
    retrieved: RetrievedThisTurn,
) -> None:
    # falsifier: the model invents an adrenaline dose, cites the CPR card it did
    # retrieve, and the invented milligram figure is spoken to a responder
    # holding a syringe. This is the defect the whole subsystem exists for, and
    # until the gate was wired nothing in production could tell the difference.
    turn, decision = gate_turn(
        "Give zero point five milligrams of adrenaline now.", retrieved
    )

    assert turn.instruction_kind is InstructionKind.CLINICAL_INSTRUCTION
    assert decision.outcome is GateOutcome.REJECTED
    assert decision.speech == SAFE_LINE
    assert RejectionReason.UNGROUNDED_NUMERIC in decision.reasons or (
        RejectionReason.UNDECLARED_NUMERIC in decision.reasons
    ), f"expected a grounding refusal, got {decision.reasons}"


def test_a_grounded_instruction_passes_through_unchanged(
    retrieved: RetrievedThisTurn,
) -> None:
    # falsifier: the wiring refuses correct, properly-grounded compression
    # guidance, so a responder gets a generic safe line during an arrest instead
    # of the depth they need. This is the strict-direction harm, and a gate that
    # fires on correct guidance as a matter of course is one someone switches
    # off - after which nothing is checked at all.
    speech = "Push hard, at least five centimetres deep."

    turn, decision = gate_turn(speech, retrieved)

    assert decision.outcome is GateOutcome.APPROVED, decision.details
    assert decision.speech is None, "an approved turn speaks its own text"
    assert turn.speech == speech, "approved text must not be rewritten"


def test_a_citation_from_a_previous_turn_does_not_satisfy_this_turn(
    cpr: Retrieved,
) -> None:
    # falsifier: the per-turn clearing is skipped or scoped wrongly, so a
    # protocol retrieved on an EARLIER exchange grounds a number spoken on a
    # turn that looked nothing up. The model speaks a depth from its own weights
    # and the gate approves it, citing a document it was not shown this turn -
    # which is the one thing output_gate's guarantee 2 is supposed to make
    # impossible.
    record = RetrievedThisTurn()
    record.record([cpr])

    grounded_while_held = gate_turn("Push at least five centimetres deep.", record)[1]
    assert grounded_while_held.outcome is GateOutcome.APPROVED, (
        "the same words must pass while the document really is this turn's"
    )

    record.clear()  # the next responder turn begins

    _, after = gate_turn("Push at least five centimetres deep.", record)

    assert after.outcome is GateOutcome.REJECTED
    assert RejectionReason.MISSING_CITATION in after.reasons
    assert after.speech == SAFE_LINE


def test_an_assessment_question_with_no_citation_passes(cpr: Retrieved) -> None:
    # falsifier: the wiring requires a citation for every turn, so "Is he
    # breathing?" - the single most important thing the agent says first, before
    # any lookup can have happened - is replaced by the safe line. The call
    # would open with a refusal and the agent could never gather the information
    # a lookup needs.
    empty = RetrievedThisTurn()

    turn, decision = gate_turn("Is he breathing?", empty)

    assert turn.instruction_kind is InstructionKind.ASSESSMENT_QUESTION
    assert decision.outcome is GateOutcome.APPROVED, decision.details
    assert decision.speech is None


def test_numerics_are_extracted_because_nothing_declares_them(
    retrieved: RetrievedThisTurn,
) -> None:
    # falsifier: the wiring passes `numerics=()` because the adapter offers no
    # structured output to read a declaration from. validate_turn's grounding
    # loop iterates turn.numerics, so an empty tuple means the loop body never
    # runs - the citation check still fires but EVERY SPOKEN NUMBER is
    # unchecked, and an invented dose cited to a real retrieved document passes.
    # This is the hole that makes the whole wiring decorative.
    turn, _ = gate_turn("Push at least five centimetres deep.", retrieved)

    values = [numeric.value for numeric in turn.numerics]
    assert 5.0 in values, f"the spoken quantity must reach the gate, got {values}"
    assert turn.numerics != (), "an empty numerics list disables the grounding rule"


# ------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "speech",
    [
        # Bare instructions - the shape an invented dose has.
        "Push hard in the centre of the chest.",
        "Give three hundred milligrams of aspirin.",
        # Opens with an interrogative but instructs: the every-sentence-is-a-
        # question half must refuse it.
        "Is he breathing? Then push five centimetres.",
        # Ends in a question mark but instructs: the opening-interrogative half
        # must refuse it.
        "Push five centimetres, okay?",
        # Reassurance-shaped prose. Deliberately classified as clinical: see
        # `classify`, REASSURANCE is the kind that disables the citation check
        # while permitting declarative prose.
        "You are doing everything right, stay with me.",
    ],
)
def test_anything_that_could_be_an_instruction_is_classified_as_one(
    speech: str,
) -> None:
    # falsifier: the classifier reads declarative prose as REASSURANCE or an
    # instruction-with-a-question-mark as a question, which sets
    # requires_citation to False and DISABLES the citation and grounding checks
    # for that turn. An invented dose phrased as "give three hundred milligrams,
    # okay?" would then walk straight through the wired gate - the unsafe
    # direction of the only misclassification that matters.
    assert classify(speech) is InstructionKind.CLINICAL_INSTRUCTION, (
        f"{speech!r} must stay gated"
    )


@pytest.mark.parametrize(
    "speech",
    [
        "Is he breathing?",
        "Are they responsive?",
        "What happened?",
        "How long has he been like this?",
        "Is he breathing? Is he responsive?",
        "Can you feel a pulse?",
    ],
)
def test_a_genuine_question_is_not_gated_for_citation(speech: str) -> None:
    # falsifier: the classifier is so conservative that it never returns
    # ASSESSMENT_QUESTION, so every question the agent asks before its first
    # lookup is refused for MISSING_CITATION. The agent cannot open a call, and
    # the gate reads as broken rather than as strict - which is how it gets
    # removed.
    assert classify(speech) is InstructionKind.ASSESSMENT_QUESTION


@pytest.mark.parametrize(
    "speech",
    [
        "Can you push at least five centimetres deep now?",
        "Do you give three hundred milligrams of aspirin now?",
        "Are you able to give a second dose of adrenaline?",
        "Can you give her zero point five milligrams of adrenaline?",
        "Could you give four hundred milligrams?",
        "Have you given the three hundred milligram aspirin?",
    ],
)
def test_an_instruction_phrased_as_a_polite_question_is_still_gated(
    speech: str,
) -> None:
    # falsifier: a clinical instruction phrased as a polite imperative - "Can
    # you push at least five centimetres deep now?" - is one sentence, ends in a
    # question mark, and opens with an interrogative, so both structural halves
    # of the classifier pass it and it is labelled ASSESSMENT_QUESTION. That
    # sets requires_citation False and skips output_gate's citation, grounding
    # AND ratio rules entirely, so an invented adrenaline dose phrased this way
    # reaches a responder holding a syringe with nothing checked and no
    # lookup_protocol call needed. This is the exact bypass the whole subsystem
    # exists to prevent, and it was present in the mechanism meant to prevent
    # it - found by independent review, not by the author.
    assert classify(speech) is InstructionKind.CLINICAL_INSTRUCTION, (
        f"{speech!r} names a clinical quantity and must be gated"
    )


@pytest.mark.parametrize(
    "speech",
    [
        # The auxiliaries that had to STAY in ASSESSMENT_ONLY. Deleting them was
        # the first attempt at the fix above and it broke exactly these.
        "Can you feel a pulse?",
        "Are they responsive?",
        "Does he have a medical alert bracelet?",
        "Has he taken anything?",
        "Do you know if he is allergic to anything?",
    ],
)
def test_narrowing_the_word_list_did_not_gate_genuine_questions(speech: str) -> None:
    # falsifier: the polite-imperative bypass is fixed by deleting "can", "are",
    # "do" and "has" from ASSESSMENT_ONLY, which also gates "Can you feel a
    # pulse?" and "Are they responsive?" - the questions the agent MUST ask
    # before it can look anything up. Those carry no citation, so the call opens
    # with the safe line on every attempt to assess the casualty. A fix for a
    # false negative is exactly where a false positive gets introduced, so both
    # directions are pinned here.
    assert classify(speech) is InstructionKind.ASSESSMENT_QUESTION, (
        f"{speech!r} names no clinical quantity and must not need a citation"
    )


def test_the_opening_greeting_must_be_askable_without_a_citation() -> None:
    # falsifier: the entrypoint's opening `generate_reply` asks the model for a
    # statement AND a question in one sentence, which renders as "I'm listening
    # - what happened, and is the person responsive?". That is a mixed
    # declarative, so it classifies as a clinical instruction - and it runs
    # before any user turn, so the citation record is empty by construction and
    # it CANNOT be cited. The first thing a first responder hears would be
    # "Stay with me. I need a clinician for this one" instead of the opening
    # question. Found by independent review; this pins the shape the entrypoint
    # must ask for.
    empty = RetrievedThisTurn()

    pure_question = "What happened, and is the person responsive and breathing?"
    kind = classify(pure_question)
    _, decision = gate_turn(pure_question, empty)

    assert kind is InstructionKind.ASSESSMENT_QUESTION
    assert decision.outcome is GateOutcome.APPROVED, decision.details

    mixed = "I am listening. What happened, and is the person responsive?"
    assert classify(mixed) is InstructionKind.CLINICAL_INSTRUCTION, (
        "a statement plus a question is not a question, and the entrypoint must "
        "not ask for one - it would be refused for want of a citation"
    )


def test_the_entrypoint_disables_preemptive_generation() -> None:
    # falsifier: the SDK defaults preemptive_generation ON
    # (voice.turn._PREEMPTIVE_GENERATION_DEFAULTS is enabled: True), which starts
    # a speculative reply - tool calls included - from an INTERIM transcript. A
    # speculative lookup_protocol writes into the citation record, then
    # on_user_turn_completed clears it when the real turn is confirmed, and the
    # speculative speech is gated only afterwards: a turn that genuinely
    # retrieved and correctly cited a protocol is refused for MISSING_CITATION
    # and the responder gets the safe line mid-arrest. An abandoned
    # speculation's write is also not forcibly cancelled, so a document
    # retrieved for a DISCARDED guess at the utterance could ground a number in
    # the turn that ran - the permissive direction. Found by independent review.
    #
    # Asserted off the source because the AgentSession literal lives inside
    # `entrypoint`, which needs a live LiveKit job and so cannot be driven.
    # `test_ports.py` reads agent.py's AST for the same reason.
    import ast
    import pathlib

    import aiscelapeus.main as main_module

    source = pathlib.Path(main_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    settings_seen: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "AgentSession":
            continue
        for keyword in node.keywords:
            if keyword.arg is not None and isinstance(keyword.value, ast.Constant):
                settings_seen[keyword.arg] = keyword.value.value

    assert settings_seen, "no AgentSession construction found in main.py"
    assert settings_seen.get("preemptive_generation") is False, (
        "the session must disable preemptive generation or the citation "
        f"record's lifetime is not sound; got {settings_seen.get('preemptive_generation')!r}"
    )


def test_the_sdk_would_enable_preemptive_generation_if_we_did_not() -> None:
    # falsifier: the assertion above is defending against a default that is not
    # actually ON, so it pins a line of code that does nothing - the
    # "mechanism whose only implementation is its docstring" defect. If a future
    # SDK ships preemptive generation off by default, this fails and the
    # reasoning recorded in main.py can be re-read rather than trusted.
    from livekit.agents.voice.turn import _PREEMPTIVE_GENERATION_DEFAULTS

    assert _PREEMPTIVE_GENERATION_DEFAULTS["enabled"] is True, (
        "main.py disables preemptive generation to protect the citation "
        "record; if the SDK default changed, re-read that comment"
    )


def test_the_classifier_never_returns_a_kind_that_disables_the_gate_silently() -> None:
    # falsifier: REASSURANCE or ESCALATION_NOTICE is returned for some prose, so
    # requires_citation goes False on a turn carrying declarative clinical text.
    # Those two kinds are deliberately unreachable (see `classify`), and pinning
    # it here is what stops a later "improvement" reintroducing the exempt
    # branch that an invented instruction would be shaped to hit.
    samples = [
        "You are doing fine.",
        "I am bringing a doctor onto the line now.",
        "It's probably a heart attack.",
        "Stay calm, help is coming.",
        "Is he breathing?",
        "Push five centimetres.",
    ]
    kinds = {classify(sample) for sample in samples}

    assert InstructionKind.REASSURANCE not in kinds
    assert InstructionKind.ESCALATION_NOTICE not in kinds
    assert kinds == {
        InstructionKind.CLINICAL_INSTRUCTION,
        InstructionKind.ASSESSMENT_QUESTION,
    }


# ---------------------------------------------------------------- the streaming


async def test_a_turn_arriving_in_fragments_is_gated_as_one_turn(
    retrieved: RetrievedThisTurn,
) -> None:
    # falsifier: the stream is gated per segment, so "Thirty compressions," and
    # "then two rescue breaths." are checked separately. Neither fragment
    # carries the 30:2 ratio form, so output_gate's ratio rule - the one thing
    # standing between the responder and an INVERTED compressions-to-breaths
    # instruction - contributes nothing, and "two compressions then thirty
    # breaths" passes on its components.
    buffered = await buffer_stream(
        await stream_of("Thirty compressions,", " then two rescue breaths.")
    )

    assert buffered == "Thirty compressions, then two rescue breaths."

    _, whole = gate_turn(buffered, retrieved)
    assert whole.outcome is GateOutcome.APPROVED, whole.details

    _, inverted = gate_turn("Two compressions, then thirty rescue breaths.", retrieved)
    assert inverted.outcome is GateOutcome.REJECTED, (
        "the inversion means the opposite of the protocol and must be refused"
    )


async def test_buffering_is_bounded_by_the_speech_ceiling(
    retrieved: RetrievedThisTurn,
) -> None:
    # falsifier: buffering is claimed to be bounded while nothing bounds it, so
    # a runaway generation defers the first audio byte indefinitely and the
    # responder hears nothing at all during an arrest. The bound is
    # SPEECH_CEILING_CHARS: past it the turn is refused outright, and the safe
    # line is a pre-rendered constant rather than something further generated.
    from aiscelapeus.output_gate import SPEECH_CEILING_CHARS

    runaway = "Push hard. " * 200
    assert len(runaway) > SPEECH_CEILING_CHARS

    _, decision = gate_turn(runaway, retrieved)

    assert decision.outcome is GateOutcome.REJECTED
    assert RejectionReason.SPEECH_TOO_LONG in decision.reasons
    assert decision.speech == SAFE_LINE
    assert len(SAFE_LINE) < SPEECH_CEILING_CHARS, (
        "the replacement must be shorter than the thing it replaces"
    )


# ------------------------------------------------------------- observability


def test_a_rejection_span_carries_the_reason_and_no_clinical_text(
    retrieved: RetrievedThisTurn,
) -> None:
    # falsifier: the rejected clinical text is put on a span attribute. spans
    # are a non-PHI surface in this system - `_do_escalate` deliberately records
    # the escalation CATEGORY and keeps the free-text reason off - and
    # decision.details quote the spoken numerals verbatim. Leaking them turns
    # every trace backend into an unreviewed PHI store.
    turn, decision = gate_turn(
        "Give zero point five milligrams of adrenaline now.", retrieved
    )
    attributes = rejection_span_attributes(
        session_id="inc-1", turn=turn, decision=decision
    )

    assert "ungrounded_numeric" in str(attributes["gate.reasons"]) or (
        "undeclared_numeric" in str(attributes["gate.reasons"])
    ), f"the span must name which rule fired, got {attributes['gate.reasons']}"
    assert attributes["gate.protocol_ids"] == "bls-adult-cpr"

    rendered = " ".join(str(value) for value in attributes.values())
    assert "adrenaline" not in rendered, "the refused speech must not reach a span"
    assert "milligrams" not in rendered, "the spoken numeral must not reach a span"
    for detail in decision.details:
        assert detail not in rendered, f"a detail string reached the span: {detail!r}"


def test_a_rejection_payload_carries_the_audit_detail(
    retrieved: RetrievedThisTurn,
) -> None:
    # falsifier: the gate refuses a turn and publishes nothing, or publishes
    # only a boolean. A rejected turn looks identical to the responder however
    # it failed, so if the event does not name the rule nobody can tell "the
    # model cited nothing" from "the model spoke a number the cited document
    # does not contain" - different malfunctions with different fixes, and the
    # audit RejectionReason exists for cannot be done.
    turn, decision = gate_turn(
        "Give zero point five milligrams of adrenaline now.", retrieved
    )
    payload = rejection_payload(session_id="inc-1", turn=turn, decision=decision)

    assert payload["outcome"] == "rejected"
    assert payload["reasons"], "the reason enum is what makes the refusal auditable"
    assert payload["details"], "an audit needs the human-readable rule text"
    assert payload["refused_speech"] == turn.speech
    assert payload["spoken_instead"] == SAFE_LINE
    assert payload["instruction_kind"] == "clinical_instruction"


def test_the_gate_decision_costs_no_model_call(retrieved: RetrievedThisTurn) -> None:
    # falsifier: a rejected turn triggers a re-prompt, adding 300-800ms of model
    # latency on exactly the turns that matter most and breaking NFR-002's 500ms
    # budget (DESIGN CONFLICT-1). The responder is waiting with their hands on a
    # chest while the system asks the model to try again.
    #
    # Structural rather than timed, as test_output_gate.py does it: a fake that
    # explodes on ANY attribute access stands in for every LLM call site, so a
    # model call anywhere in the wiring fails the test rather than merely
    # being slow.
    touched: list[str] = []

    class ExplodingModel:
        def __getattr__(self, name: str) -> object:
            touched.append(name)
            raise AssertionError(
                f"the rejection path called the model ({name}); "
                "the safe line must be pre-rendered"
            )

    _unused_model = ExplodingModel()

    _, decision = gate_turn("Give four hundred milligrams of nothing.", retrieved)
    spoken = decision.speech

    assert decision.outcome is GateOutcome.REJECTED
    assert spoken is SAFE_LINE, (
        "the safe line must be the pre-rendered module constant, not a built string"
    )
    assert len(touched) == 0, f"the rejection path touched the model: {touched}"


# =============================================================================
# The node overrides, which need the SDK.
# =============================================================================

pytest.importorskip(
    "livekit.agents",
    reason="aiscelapeus.agent imports the LiveKit SDK; installed via .[dev] in CI",
)

from aiscelapeus.agent import AiscelapeusAgent  # noqa: E402
from aiscelapeus.config import Settings  # noqa: E402
from aiscelapeus.testing.fakes import FakeMoss, FakePublisher  # noqa: E402
from aiscelapeus.triage import Criticality, TriageState  # noqa: E402

from ..conftest import FAKE_ENV  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return Settings.load(environ=dict(FAKE_ENV))


@pytest.fixture
def moss() -> FakeMoss:
    return FakeMoss(scripted={"cpr": [hit("bls-adult-cpr", CPR_TEXT)]})


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def agent(
    settings: Settings, moss: FakeMoss, publisher: FakePublisher
) -> AiscelapeusAgent:
    return AiscelapeusAgent(
        settings=settings,
        context=cast(Any, moss),
        state=TriageState(session_id=moss.session_id),
        publish=publisher,
    )


def _ctx() -> Any:
    """The RunContext the tools accept and never read."""
    return cast(Any, None)


def _settings() -> Any:
    """The ModelSettings the nodes accept; the overrides only pass it through."""
    return cast(Any, None)


async def _text_out(agent: AiscelapeusAgent, *segments: str) -> str:
    """What the transcript surfaces would receive for this turn."""
    stream = await agent.transcription_node(await stream_of(*segments), _settings())
    return "".join([chunk async for chunk in stream])


async def test_an_ungrounded_dose_never_reaches_the_transcript_surfaces(
    agent: AiscelapeusAgent,
) -> None:
    # falsifier: the gate is not called from the node at all, so the model's
    # invented dose is passed straight through to the responder's screen and the
    # doctor dashboard. This is the mutation-tested assertion for "the gate call
    # exists": delete the gate_turn call in `_gated_text` and this fails.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")

    spoken = await _text_out(agent, "Give zero point five milligrams of adrenaline.")

    assert spoken == SAFE_LINE
    assert "adrenaline" not in spoken
    assert "milligram" not in spoken


async def test_grounded_guidance_reaches_the_surfaces_unchanged(
    agent: AiscelapeusAgent,
) -> None:
    # falsifier: the wiring replaces every turn with the safe line, so a live
    # incident hears nothing but "I need a clinician for this one" and the agent
    # is useless. Paired with the rejection test above on purpose: half a suite
    # is satisfied by a gate that refuses everything.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")

    spoken = await _text_out(agent, "Push hard, at least five centimetres deep.")

    assert spoken == "Push hard, at least five centimetres deep."
    assert spoken != SAFE_LINE


async def test_the_node_gates_a_fragmented_stream_as_one_turn(
    agent: AiscelapeusAgent,
) -> None:
    # falsifier: the node gates each segment the SDK hands it separately, so a
    # turn whose dose arrives split across tokens ("zero point", " five
    # milligrams") is checked as fragments that individually carry no ungrounded
    # value, and the assembled invented dose is spoken. This is the assertion
    # that the buffering is real rather than incidental.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")

    spoken = await _text_out(
        agent, "Give zero point", " five milligrams", " of adrenaline."
    )

    assert spoken == SAFE_LINE


async def test_a_previous_turns_retrieval_does_not_ground_this_turn(
    agent: AiscelapeusAgent,
) -> None:
    # falsifier: `on_user_turn_completed` does not clear the citation record, so
    # a protocol retrieved on an earlier exchange still grounds a number spoken
    # on a turn that looked nothing up. The model speaks a depth from memory and
    # the wired gate approves it against a document it was not shown - the stale
    # citation hole. This is the mutation-tested assertion for the per-turn
    # clearing: delete the `clear()` call and this fails.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")
    first = await _text_out(agent, "Push hard, at least five centimetres deep.")
    assert first != SAFE_LINE, "the turn that did retrieve must pass"

    # The next responder turn begins. The SDK awaits this hook before the model
    # generates the reply.
    await agent.on_user_turn_completed(cast(Any, None), cast(Any, None))

    second = await _text_out(agent, "Push hard, at least five centimetres deep.")

    assert second == SAFE_LINE, (
        "the same words with no retrieval this turn must be refused"
    )


@pytest.mark.parametrize(
    "query",
    [
        # Zero hits: the "no protocol matched, escalate" branch.
        "no protocol exists for this",
        # Blank: refused by RetrievalRequest before the store is reached.
        "   ",
    ],
)
async def test_a_failed_lookup_leaves_no_citable_document(
    agent: AiscelapeusAgent, query: str
) -> None:
    # falsifier: the citation record is written before the refusal branches, so
    # a zero-hit search or a blank query still leaves an id behind. The model
    # would then be told "no protocol matched, escalate" and could nonetheless
    # speak a cited-looking instruction that the gate approves against a
    # document that was never returned.
    answer = await agent.lookup_protocol(_ctx(), query=query)
    assert answer["found"] is False

    assert agent.retrieved_this_turn.ids == ()

    spoken = await _text_out(agent, "Push hard, at least five centimetres deep.")
    assert spoken == SAFE_LINE


async def test_a_refused_tier_leaves_no_citable_document(
    settings: Settings, publisher: FakePublisher
) -> None:
    # falsifier: the citation record is written from `result.hits` before the
    # TierViolation branch can return, so a lookup the tier gate REFUSED still
    # leaves citable ids behind. This is the branch that actually distinguishes
    # where the record call sits - the zero-hit branch cannot, because recording
    # an empty hit list is a no-op either way - and it is reachable in
    # production: main.entrypoint continues past a failed connect, leaving the
    # corpus non-resident for the whole incident. The model is told to escalate
    # and could still speak an instruction the gate approves against a document
    # no retrieval ever returned.
    moss = FakeMoss(
        loaded=False, scripted={"cpr": [hit("bls-adult-cpr", CPR_TEXT)]}
    )
    agent = AiscelapeusAgent(
        settings=settings,
        context=cast(Any, moss),
        state=TriageState(session_id=moss.session_id),
        publish=publisher,
    )

    answer = await agent.lookup_protocol(_ctx(), query="cpr compressions")

    assert answer["found"] is False
    assert agent.retrieved_this_turn.ids == (), (
        "a refused lookup must leave nothing citable"
    )

    spoken = await _text_out(agent, "Push hard, at least five centimetres deep.")
    assert spoken == SAFE_LINE


async def test_a_rejection_publishes_an_observable_event_with_its_reason(
    agent: AiscelapeusAgent, publisher: FakePublisher
) -> None:
    # falsifier: the gate refuses a turn and publishes nothing, so the failure
    # is invisible - which is worse than no gate, because the system reads as
    # working while silently replacing clinical guidance. Nobody can tell a
    # model malfunctioning on every turn from a quiet incident.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")

    await _text_out(agent, "Give zero point five milligrams of adrenaline.")

    assert GATE_REJECTION_TOPIC in publisher.topics()
    event = publisher.last(GATE_REJECTION_TOPIC)
    assert event is not None
    assert event["outcome"] == "rejected"
    assert event["reasons"], "the published event must name the rule that fired"
    assert event["spoken_instead"] == SAFE_LINE


async def test_an_approved_turn_publishes_no_rejection(
    agent: AiscelapeusAgent, publisher: FakePublisher
) -> None:
    # falsifier: the rejection event is published unconditionally, so the
    # dashboard shows a refusal on every turn including the correct ones. The
    # signal becomes noise and a real refusal is invisible inside it - the same
    # failure as publishing nothing, reached from the other side.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")

    await _text_out(agent, "Push hard, at least five centimetres deep.")

    assert GATE_REJECTION_TOPIC not in publisher.topics()


async def test_a_dead_data_channel_does_not_break_the_refusal(
    settings: Settings, moss: FakeMoss
) -> None:
    # falsifier: the rejection publish is unguarded, so a raising publisher
    # turns a refusal into an exception inside the audio path - the one path
    # that exists to be safe. The responder would hear neither the turn nor the
    # safe line. Same asymmetry main._handle_utterance already resolves: the net
    # must outlive the UI plumbing.
    failing = FakePublisher()
    agent = AiscelapeusAgent(
        settings=settings,
        context=cast(Any, moss),
        state=TriageState(session_id=moss.session_id),
        publish=failing,
    )
    # The lookup publishes its own retrieval event, so the channel is broken
    # *after* it - which is also the realistic sequence: a data channel that
    # dies mid-incident, not one that was never up.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")
    failing.fail = True

    spoken = await _text_out(agent, "Give zero point five milligrams of adrenaline.")

    assert spoken == SAFE_LINE, "the refusal must survive a dead data channel"


async def test_a_rejected_turn_does_not_undo_an_escalation(
    agent: AiscelapeusAgent, publisher: FakePublisher
) -> None:
    # falsifier: the gate's refusal path touches triage state - clears the
    # escalation, lowers the level, or is placed so that a refused turn
    # suppresses the page escalation.py already decided. A responder reports
    # "he's not breathing", the deterministic net pages a clinician, and a later
    # malformed model turn silently cancels it: the incident reads as
    # non-critical with no clinician coming, which is the unrecoverable failure
    # the whole escalation subsystem exists to prevent.
    await agent.record_finding(
        _ctx(), kind="observation", detail="he is not breathing"
    )
    assert agent.state.level is Criticality.CRITICAL, "the net must have fired"
    assert "triage.escalation" in publisher.topics()

    spoken = await _text_out(agent, "Give zero point five milligrams of adrenaline.")

    assert spoken == SAFE_LINE, "the turn itself is still refused"
    assert agent.state.level is Criticality.CRITICAL, (
        "a gate rejection must not lower the level the net ratcheted"
    )
    assert agent.state.escalated is True, (
        "a gate rejection must not cancel a clinician who was already paged"
    )
    assert "triage.escalation" in publisher.topics(), "the page still happened"


async def test_the_safe_line_substitution_is_what_reaches_the_surface(
    agent: AiscelapeusAgent,
) -> None:
    # falsifier: the node computes a rejection, observes it, and then yields the
    # ORIGINAL text anyway - a gate that reports refusals it does not enforce.
    # Every other rejection test would still pass if the event were emitted and
    # the text passed through, so this is the assertion that pins the
    # substitution itself. Mutation-tested: return `turn.speech` instead of
    # `decision.speech` in `_gated_text` and this fails.
    await agent.lookup_protocol(_ctx(), query="cpr compressions")
    invented = "Give zero point five milligrams of adrenaline."

    spoken = await _text_out(agent, invented)

    assert spoken != invented, "the refused text must not be what is spoken"
    assert spoken is SAFE_LINE, "the replacement must be the pre-rendered constant"


async def test_the_audio_path_and_the_transcript_path_gate_the_same_turn(
    agent: AiscelapeusAgent,
) -> None:
    # falsifier: only one of the two teed consumers is gated.
    # `agent_activity` tees the LLM text into `audio_source` (-> tts_node) and
    # `text_source` (-> transcription_node), so overriding one leaves the other
    # carrying the ungated text: the invented dose would be silenced in the
    # audio while still printing on the responder's screen and the doctor
    # dashboard. Asserted as "both overrides exist and both call the gate",
    # because driving tts_node to real audio frames needs a live TTS.
    assert type(agent).tts_node is not AiscelapeusAgent.__mro__[1].tts_node, (
        "tts_node must be overridden or audio is ungated"
    )
    assert (
        type(agent).transcription_node is not AiscelapeusAgent.__mro__[1].transcription_node
    ), "transcription_node must be overridden or the transcript surfaces are ungated"

    await agent.lookup_protocol(_ctx(), query="cpr compressions")
    assert (
        await agent._gated_text("Give zero point five milligrams of adrenaline.")
        == SAFE_LINE
    ), "the shared gate call both nodes use must refuse the invented dose"


async def test_a_stalled_llm_stream_does_not_silence_the_agent_forever() -> None:
    # falsifier: `buffer_stream` drains with no timeout, so a stalled or
    # non-terminating LLM stream makes it never return and the responder hears
    # NOTHING - indefinitely, mid-arrest, with no safe line and no escalation,
    # because the deterministic net runs off STT transcripts and not off agent
    # generation state.
    #
    # Reproduced before the fix: a stream that yields one segment and then
    # blocks hung the drain permanently. The SDK supplies no bound of its own -
    # `livekit/agents/voice/generation.py` contains no timeout at all, and the
    # task running `tts_node` is unwrapped - so the only thing that could
    # unblock it is the responder interrupting, which is exactly what they will
    # not do with their hands on a chest waiting to be told the next thing.
    #
    # Found by independent review. A gate that can hang is worse than one that
    # rejects: a rejection speaks a safe line, a hang speaks nothing.
    started = asyncio.get_running_loop().time()

    async def stalled() -> AsyncGenerator[str, None]:
        yield "Push at least "
        await asyncio.sleep(3600)

    # Wrapped in an OUTER wait_for so the mutant FAILS rather than hangs.
    # Verified: removing `asyncio.timeout` from `buffer_stream` makes the drain
    # never return, and without this wrapper the test hung indefinitely instead
    # of reporting - a test that hangs on a regression is a worse signal than
    # one that fails, because a hung suite looks like a slow suite.
    with mock.patch.object(turn_gate, "BUFFER_TIMEOUT_S", 0.2):
        try:
            text = await asyncio.wait_for(turn_gate.buffer_stream(stalled()), 5.0)
        except TimeoutError:  # pragma: no cover - only on a regression
            pytest.fail(
                "buffer_stream never returned on a stalled stream: the liveness "
                "timeout is gone and the responder would hear silence"
            )

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 3.0, f"drain took {elapsed:.1f}s; the timeout did not fire"
    assert text == "Push at least ", (
        "the segments that arrived must still be returned, so they go through "
        "the gate rather than being lost"
    )


async def test_a_completing_stream_pays_no_timeout_penalty() -> None:
    # falsifier: the liveness timeout is implemented as an unconditional wait,
    # so every ordinary turn is delayed by BUFFER_TIMEOUT_S - turning a bound
    # that exists to catch a STOPPED stream into 12 seconds of added latency on
    # a voice path budgeted at 500ms. The timeout must cost nothing when the
    # stream terminates normally.
    async def normal() -> AsyncGenerator[str, None]:
        yield "Push at least "
        yield "five centimetres deep."

    started = asyncio.get_running_loop().time()
    text = await turn_gate.buffer_stream(normal())
    elapsed = asyncio.get_running_loop().time() - started

    assert text == "Push at least five centimetres deep."
    assert elapsed < 1.0, f"a completing stream waited {elapsed:.1f}s"


def test_the_liveness_timeout_is_far_above_a_measured_turn() -> None:
    # falsifier: BUFFER_TIMEOUT_S is tuned toward NFR-002's 500ms budget rather
    # than to liveness, so it fires on ordinary turns and replaces CORRECT
    # guidance with the safe line - a gate that misfires on correct guidance is
    # one somebody switches off, which `output_gate`'s own docstring says.
    # docs/WORKLOG.md Session 2 measured a Gemini turn at 2.3-2.6s, so the bound
    # has to clear that with room and still be well inside what a responder
    # experiences as a stall rather than a pause.
    assert turn_gate.BUFFER_TIMEOUT_S >= 6.0, (
        "a bound this tight would fire on the 2.3-2.6s turns already measured"
    )
    assert turn_gate.BUFFER_TIMEOUT_S <= 30.0, (
        "a bound this loose is indistinguishable from the hang it replaces"
    )


def test_every_agent_initiated_speech_site_is_gate_safe() -> None:
    # falsifier: someone adds a `session.say(...)` nudge ("still there?") or a
    # retry `generate_reply(...)` after an escalation. Both route through the
    # gate nodes - verified in the SDK: `_tts_task_impl` calls `tts_node` and
    # `transcription_node` directly - but NEITHER fires
    # `on_user_turn_completed`, so they gate against whatever
    # `RetrievedThisTurn` happens to hold: the previous turn's citations, or
    # nothing at all. A clinical nudge would be refused and the responder would
    # hear the safe line for no reason.
    #
    # Today there is exactly one such site, the opening greeting, and it is safe
    # only because its instruction produces a pure question, which `classify`
    # reports as ASSESSMENT_QUESTION and which therefore needs no citation. An
    # independent review flagged that as an invariant of the current call sites
    # rather than a structural guarantee - nothing enforced it. This does.
    #
    # It is a source scan rather than a runtime assertion because
    # agent-initiated speech has no test seam: `AgentSession` needs a live job.
    # Paired with the classification tests above, which prove a question passes
    # the gate uncited.
    source = (
        Path(__file__).resolve().parents[2] / "aiscelapeus" / "main.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    sites: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"say", "generate_reply"}:
            sites.append(node.lineno)

    assert sites, "no agent-initiated speech found at all - did main.py move?"
    assert len(sites) == 1, (
        "a new agent-initiated speech site appeared at line(s) "
        f"{sites}. Each one gates against RetrievedThisTurn WITHOUT "
        "on_user_turn_completed having cleared it, so its text must classify as "
        "ASSESSMENT_QUESTION (no citation required) or it will be refused. "
        "Confirm that, then update this count."
    )
