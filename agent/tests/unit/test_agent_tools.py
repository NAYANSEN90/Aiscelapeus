"""The agent's tool layer, which had no test coverage at all.

`AiscelapeusAgent`'s `@function_tool` methods are the boundary between a model
emitting free text and the incident's storage. Everything below them was
tested; the layer that builds the request, handles a refusal and shapes the
answer the model reads was not.

Two failure modes matter more than the rest and both are asserted here:

1. **An exception out of a `function_tool` ends the voice turn.** So a tool must
   *return* a refusal, never raise one. An independent review found exactly this
   defect in the first version of this subsystem: `TierViolation` is a
   `RuntimeError`, the tool caught only `ValueError`, and `main.entrypoint`
   deliberately continues past a failed `connect` - so in a degraded session
   every protocol lookup would have killed the turn, repeatedly, mid-emergency.
2. **A refusal must not read like an answer.** `found: False` with guidance to
   escalate is safe; a silent empty result the model narrates around is not.

`AiscelapeusAgent.__init__` calls `Agent.__init__` from the LiveKit SDK, and
`RunContext` is an SDK type the tools never touch, so the tool methods are
invoked as plain coroutines with a `None` context. That is deliberate: it keeps
these tests hermetic and about our logic rather than about the SDK's wiring.

`aiscelapeus.agent` imports `livekit.agents` at module scope, so this module is
skipped where that SDK is not installed. That skip is declared with
`importorskip` rather than a silent try/except: CI installs `.[dev]`, which
pins `livekit-agents==1.8.1`, so these tests *do* run in the blocking gate -
and on a developer machine without the SDK they report as skipped rather than
quietly not existing. A test that vanishes without saying so is the same defect
class as a CI job that passes while collecting nothing.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

pytest.importorskip(
    "livekit.agents",
    reason="aiscelapeus.agent imports the LiveKit SDK; installed via .[dev] in CI",
)

from aiscelapeus.agent import AiscelapeusAgent  # noqa: E402
from aiscelapeus.config import Settings  # noqa: E402
from aiscelapeus.ports import LatencyClass  # noqa: E402
from aiscelapeus.retrieval import (  # noqa: E402
    PROTOCOL_ALPHA,
    SESSION_ALPHA,
    Retrieved,
)
from aiscelapeus.testing.fakes import FakeMoss, FakePublisher  # noqa: E402
from aiscelapeus.triage import Criticality, FactKind, TriageState  # noqa: E402

from ..conftest import FAKE_ENV  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return Settings.load(environ=dict(FAKE_ENV))


@pytest.fixture
def moss() -> FakeMoss:
    return FakeMoss(
        scripted={
            "choking": [
                Retrieved(
                    id="airway-choking-adult",
                    text="Five back blows, then five abdominal thrusts.",
                    score=0.93,
                    metadata={"title": "Choking, adult", "category": "airway"},
                )
            ]
        }
    )


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


# ------------------------------------------------- the request the tool builds


async def test_the_protocol_tool_sends_the_tuned_alpha_and_configured_top_k(
    agent: AiscelapeusAgent, moss: FakeMoss, settings: Settings
) -> None:
    # falsifier: the tool builds a request by hand instead of going through
    # RetrievalRequest.for_protocol, so the tuned alpha and the configured
    # top_k have a second definition at the one call site that actually runs.
    # The constants would then be documentation, and MOSS_PROTOCOL_TOP_K an env
    # var that changes nothing.
    await agent.lookup_protocol(_ctx(), query="choking adult")

    request = moss.requests("lookup_protocol")[-1]
    assert request.query == "choking adult"
    assert request.alpha == PROTOCOL_ALPHA
    assert request.top_k == settings.moss.protocol_top_k
    assert request.latency_class is LatencyClass.VOICE_TURN, (
        "a synchronous tool call in the responder's turn is Class 0"
    )


async def test_a_known_category_becomes_a_filter_and_an_unknown_one_does_not(
    agent: AiscelapeusAgent, moss: FakeMoss
) -> None:
    # falsifier: the model's free-text category is passed through unchecked, so
    # a hallucinated category like "respiratory" becomes the *valid* filter
    # {"category": {"$in": ["respiratory"]}} which matches no document. The
    # search returns zero hits and reads as a corpus gap rather than a bad
    # filter - a silent failure on the voice path.
    await agent.lookup_protocol(_ctx(), query="choking adult", category="airway")
    assert moss.requests("lookup_protocol")[-1].moss_filter() == {
        "category": {"$in": ["airway"]}
    }

    await agent.lookup_protocol(_ctx(), query="choking adult", category="respiratory")
    assert moss.requests("lookup_protocol")[-1].moss_filter() is None, (
        "an unrecognised category must widen the search, not filter to nothing"
    )


async def test_the_recall_tool_sends_the_session_alpha_and_its_own_top_k(
    agent: AiscelapeusAgent, moss: FakeMoss, settings: Settings
) -> None:
    # falsifier: the recall tool reuses the protocol constructor, so session
    # recall runs with the protocol corpus's alpha and top_k - MOSS_STATE_TOP_K
    # silently does nothing and the two retrieval problems share one tuning.
    await agent.recall_state(_ctx(), query="when was the tourniquet applied")

    request = moss.requests("recall")[-1]
    assert request.alpha == SESSION_ALPHA
    assert request.top_k == settings.moss.state_top_k
    assert request.categories == (), "the session index has no category taxonomy"


async def test_a_blank_query_is_refused_without_reaching_the_store(
    agent: AiscelapeusAgent, moss: FakeMoss
) -> None:
    # falsifier: an empty STT transcript reaches the store, which returns
    # arbitrary nearest neighbours to nothing, and the agent cites a protocol
    # with no relationship to what was said. The refusal must also be a return
    # value: a raise out of a function_tool ends the responder's turn.
    answer = await agent.lookup_protocol(_ctx(), query="   ")

    assert answer["found"] is False
    assert "error" in answer
    assert moss.requests("lookup_protocol") == [], "nothing may reach the store"


# ----------------------------------------- a refused tier must not kill the turn


async def test_a_tier_violation_is_returned_as_guidance_not_raised(
    settings: Settings, publisher: FakePublisher
) -> None:
    # falsifier: TierViolation propagates out of the tool. It is a RuntimeError,
    # not a ValueError, so the request-validation clause does not catch it - and
    # it is reachable in production, because main.entrypoint deliberately
    # continues past a failed connect, leaving the corpus non-resident for the
    # whole incident. Every lookup then raises, and an exception out of a
    # function_tool ends the turn: the agent would go silent on the responder,
    # repeatedly, on the one tool call that matters most. Found by independent
    # review; this test is the only thing that observes the layer above the gate.
    moss = FakeMoss(loaded=False)
    agent = AiscelapeusAgent(
        settings=settings,
        context=cast(Any, moss),
        state=TriageState(session_id=moss.session_id),
        publish=publisher,
    )

    answer = await agent.lookup_protocol(_ctx(), query="choking adult")

    assert answer["found"] is False
    guidance = answer["guidance"].lower()
    assert "escalate" in guidance, (
        "a responder denied a protocol must be routed to a clinician, not left "
        "with an unexplained refusal"
    )
    assert "protocols" not in answer, "no protocol may be presented as found"


async def test_a_refused_recall_still_answers_the_model(
    settings: Settings, publisher: FakePublisher
) -> None:
    # falsifier: the recall tool raises TierViolation, ending the turn, when the
    # session store cannot serve the voice path. The model then gets no answer
    # at all rather than "I cannot search the record", and the responder hears
    # silence mid-incident.
    moss = FakeMoss(session_in_process=False)
    agent = AiscelapeusAgent(
        settings=settings,
        context=cast(Any, moss),
        state=TriageState(session_id=moss.session_id),
        publish=publisher,
    )

    answer = await agent.recall_state(_ctx(), query="what have we given")

    assert "error" in answer
    assert answer["facts"], "the model must receive something to say"


# ------------------------------------------------------- the recorded finding


async def test_a_recorded_finding_is_published_with_one_wire_shape(
    agent: AiscelapeusAgent, moss: FakeMoss, publisher: FakePublisher
) -> None:
    # falsifier: the tool reconstructs the payload itself rather than using
    # FactRecord.to_payload, so the finding feed and the stored record can
    # disagree - and the `hasattr(record, "to_payload")` bridge that used to sit
    # here typed elapsed_s as a string on one path and a float on the other,
    # with only the fake ever taking one of the two branches.
    answer = await agent.record_finding(
        _ctx(), kind="intervention", detail="tourniquet applied to left thigh"
    )

    assert answer["recorded"] is True
    published = publisher.last("triage.finding")
    assert published is not None
    record = moss.facts[-1]
    assert published == record.to_payload(), (
        "the published payload must be the record's own wire shape"
    )
    assert isinstance(published["elapsed_s"], float)
    assert answer["elapsed_s"] == record.elapsed_s


async def test_an_unknown_finding_kind_is_refused_with_the_valid_options(
    agent: AiscelapeusAgent, moss: FakeMoss
) -> None:
    # falsifier: a model-invented kind is coerced to "observation", so a vital
    # sign or an intervention is filed under the wrong clinical classification
    # and the SOAP note a clinician signs is wrong with nothing flagging it.
    answer = await agent.record_finding(_ctx(), kind="vitals", detail="pulse 120")

    assert answer["recorded"] is False
    assert "vitals" in answer["error"]
    assert FactKind.VITAL.value in answer["error"], "the model needs the valid set"
    assert moss.facts == [], "a refused finding must not be stored"


async def test_escalation_is_not_offered_as_a_recordable_kind(
    agent: AiscelapeusAgent,
) -> None:
    # falsifier: ESCALATION is listed in the error's valid-kinds hint, so the
    # model learns it can file an escalation as an ordinary finding - bypassing
    # _do_escalate, which is what actually pages the clinician and publishes the
    # event the dashboard renders. The record would say a clinician was
    # requested while no request was ever made.
    answer = await agent.record_finding(_ctx(), kind="nonsense", detail="x")
    assert FactKind.ESCALATION.value not in answer["error"]


async def test_a_hard_escalation_phrase_forces_critical_and_pages(
    agent: AiscelapeusAgent, moss: FakeMoss, publisher: FakePublisher
) -> None:
    # falsifier: the deterministic safety net is wired to the tool but never
    # fires, so a reported "not breathing" depends entirely on the model
    # choosing to call assess_criticality. If it does not, a critical casualty
    # sits at whatever level was last set and no clinician is paged - the exact
    # failure the rule-based net exists to make impossible.
    answer = await agent.record_finding(
        _ctx(), kind="observation", detail="he is not breathing"
    )

    assert answer["recorded"] is True
    assert agent.state.level is Criticality.CRITICAL
    assert "triage.escalation" in publisher.topics()
    assert any(fact.kind is FactKind.ESCALATION for fact in moss.facts)


async def test_a_routine_finding_does_not_escalate(
    agent: AiscelapeusAgent, publisher: FakePublisher
) -> None:
    # falsifier: the marker match is too loose and any recorded finding trips
    # the hard escalation, so a clinician is paged for a grazed knee. The net
    # then gets tuned down or switched off, and the case it exists for goes
    # unprotected.
    await agent.record_finding(
        _ctx(), kind="observation", detail="small graze to the left knee"
    )
    assert agent.state.level is not Criticality.CRITICAL
    assert "triage.escalation" not in publisher.topics()


# ------------------------------------------------------------ the tool's answer


async def test_a_found_protocol_is_returned_with_its_text(
    agent: AiscelapeusAgent, publisher: FakePublisher
) -> None:
    # falsifier: the tool returns titles without the protocol text, so the model
    # has nothing to read from and narrates from its own weights while the
    # answer still reports found: True - an uncited instruction that is
    # indistinguishable, downstream, from a cited one.
    answer = await agent.lookup_protocol(_ctx(), query="choking adult")

    assert answer["found"] is True
    protocol = answer["protocols"][0]
    assert protocol["title"] == "Choking, adult"
    assert "back blows" in protocol["text"]
    assert publisher.last("triage.retrieval") is not None, (
        "the retrieval must reach the UI so latency is visible live"
    )


async def test_no_matching_protocol_tells_the_model_to_escalate(
    agent: AiscelapeusAgent
) -> None:
    # falsifier: a zero-hit search returns an empty list with found: True, so
    # the model treats "no protocol exists for this" as "here are no protocols"
    # and improvises a clinical instruction instead of escalating.
    answer = await agent.lookup_protocol(_ctx(), query="something with no protocol")

    assert answer["found"] is False
    assert "escalate" in answer["guidance"].lower()
