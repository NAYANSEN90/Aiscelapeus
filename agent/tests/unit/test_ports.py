"""The port/adapter contract, and the tier rule that was previously only a claim.

Three of the four defects this module covers were invisible because nothing
executed them:

- `TierViolation` was raised in exactly one place in the repo - `testing/fakes.py`
  - so the Class-0 rule that `ports.py` describes existed only inside the double
  that stood in for the thing meant to enforce it.
- `EmergencyContext` had no `checkpoint` at all while `MossPort` declared one,
  and disagreed with the port on four return types.
- Nothing was annotated `MossPort`, so neither mypy nor `@runtime_checkable`
  could see any of it.

The `runtime_checkable` test below is the one worth reading twice: it asserts
that `isinstance` is *not* sufficient, which is why the conformance check is a
type annotation rather than an assertion.
"""

from __future__ import annotations

import asyncio
import inspect
import ast
from pathlib import Path

import pytest

from aiscelapeus.config import MossConfig
from aiscelapeus.moss_context import EmergencyContext
from aiscelapeus.ports import LatencyClass, MossPort, TierViolation
from aiscelapeus.retrieval import RetrievalRequest, require_tier
from aiscelapeus.testing.fakes import FakeMoss, FakeNetworkStore


@pytest.fixture
def config() -> MossConfig:
    return MossConfig(
        project_id="test-project",
        project_key="test-key",
        protocol_top_k=7,
        state_top_k=9,
    )


@pytest.fixture
def context(config: MossConfig) -> EmergencyContext:
    """An unconnected adapter. No network: `__init__` does no I/O."""
    return EmergencyContext(config=config, session_id="incident-1")


# ------------------------------------------------------- the tier rule itself


def test_a_class_0_call_to_a_network_store_raises() -> None:
    # falsifier: the Class-0 rule is documented in ports.py but not implemented,
    # so a voice-turn retrieval silently takes a network round-trip and the 10ms
    # budget is missed live, mid-incident, with nothing raised. This is the whole
    # of D1: before this change the only TierViolation raise in the repo was in
    # a test double, so the rule was enforced against the fake and nowhere else.
    with pytest.raises(TierViolation) as excinfo:
        require_tier("redis", LatencyClass.VOICE_TURN, in_process=False)

    message = str(excinfo.value)
    assert "redis" in message, "the message must name the store that was reached"
    assert "VOICE_TURN" in message, "and the class it must not serve"
    assert excinfo.value.store == "redis"
    assert excinfo.value.latency_class is LatencyClass.VOICE_TURN


def test_a_class_0_call_to_an_in_process_store_is_allowed() -> None:
    # falsifier: the gate is written to reject VOICE_TURN outright rather than
    # "VOICE_TURN reaching a non-local store", so the in-process protocol
    # lookup - the single most common call in the system, and the one the whole
    # in-process design exists to make fast - raises on every voice turn and the
    # agent can never cite a protocol at all.
    require_tier("protocols", LatencyClass.VOICE_TURN, in_process=True)
    # Nothing raised. Asserted explicitly rather than left implicit, so this
    # cannot be mistaken for a test that forgot its assertion.
    assert require_tier("protocols", LatencyClass.VOICE_TURN, in_process=True) is None


@pytest.mark.parametrize(
    "latency_class",
    [LatencyClass.PREFETCH, LatencyClass.CLINICIAN, LatencyClass.BACKGROUND],
)
def test_slower_tiers_may_reach_a_network_store(latency_class: LatencyClass) -> None:
    # falsifier: the gate is widened to "any tier, any network store", so the
    # Class-1 prefetch and the Class-3 SOAP query - both designed to use slower
    # stores off the voice turn - start raising. That breaks the tiering the
    # rule exists to protect, and the natural fix under pressure is to delete
    # the gate rather than narrow it.
    require_tier("redis", latency_class, in_process=False)
    assert require_tier("redis", latency_class, in_process=False) is None


def test_the_network_store_double_uses_the_same_gate() -> None:
    # falsifier: FakeNetworkStore keeps its own hand-written `if latency_class
    # is VOICE_TURN: raise`, so the rule has two implementations. They drift,
    # and the tests keep passing against the copy in the double while
    # production runs the other one. That is precisely the state this subsystem
    # was found in.
    store = FakeNetworkStore()
    source = inspect.getsource(type(store).search_deep)
    assert "require_tier" in source, (
        "the double must call the production gate, not reimplement it"
    )
    assert "raise TierViolation" not in source, "no second implementation of the rule"

    with pytest.raises(TierViolation):
        asyncio.run(store.search_deep("chest pain", latency_class=LatencyClass.VOICE_TURN))
    assert store.calls == [], "a rejected call must not be recorded as served"


# ------------------------------------------- the gate inside the real adapter


async def test_the_real_adapter_rejects_a_class_0_lookup_before_the_index_loads(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: the tier gate lives only in the fake, so a Class-0 protocol
    # lookup issued in the window between the Moss client coming up and
    # `load_index` completing resolves over the network on the voice path. The
    # responder hears a pause instead of an instruction and nothing in the run
    # records that the 10ms path was never in-process. This is the enforcement
    # point ports.py claims to have; it is asserted here against
    # EmergencyContext, not against a double.
    context._client = object()  # type: ignore[assignment]
    assert context._loaded is False, "the corpus is not resident until load_index returns"

    request = RetrievalRequest.for_protocol("severe bleeding from the thigh", config)
    assert request.latency_class is LatencyClass.VOICE_TURN

    with pytest.raises(TierViolation) as excinfo:
        await context.lookup_protocol(request)
    assert config.protocols_index in str(excinfo.value)


async def test_a_prefetch_lookup_is_allowed_before_the_index_loads(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: the gate rejects on `not _loaded` regardless of tier, so the
    # Class-1 warm-up prefetch that is supposed to run *while* the index loads
    # cannot run at all - and the fix that suggests itself is to remove the
    # gate. Asserts the call gets past the gate and fails later, at the vendor
    # call, rather than being refused by tier.
    context._client = object()  # type: ignore[assignment]
    request = RetrievalRequest.for_protocol(
        "severe bleeding", config, latency_class=LatencyClass.PREFETCH
    )
    with pytest.raises(AttributeError):
        # Past the tier gate; `object()` has no `.query`. A TierViolation here
        # would mean the gate refused a tier it must permit.
        await context.lookup_protocol(request)


async def test_aclose_makes_the_corpus_non_resident_again(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: `_loaded` is never cleared on shutdown, so a retrieval racing
    # the shutdown callback - LiveKit runs tool calls in their own tasks, so
    # this is a real interleaving, not a hypothetical - passes the tier gate
    # against an index that has just been unloaded, and the "in-process" voice
    # path becomes a network call precisely when the process is least able to
    # serve one.
    # The realistic post-shutdown state: the client object is still referenced
    # (aclose does not None it), only the corpus is gone. Setting `_client` is
    # what makes this test exercise the tier gate rather than the earlier
    # "connect was never awaited" guard.
    context._client = object()  # type: ignore[assignment]
    context._loaded = True
    await context.aclose()
    assert context._loaded is False

    with pytest.raises(TierViolation):
        await context.lookup_protocol(
            RetrievalRequest.for_protocol("chest pain", config)
        )


async def test_a_failed_connect_leaves_retrieval_failing_closed(
    context: EmergencyContext,
    config: MossConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # falsifier: `connect` reports failure but leaves `_loaded` True, so after
    # Moss is unreachable the agent still accepts Class-0 lookups. Those either
    # hit the network on the voice path or return zero hits, and the agent then
    # answers from the model alone - an uncited clinical instruction that is
    # indistinguishable, to the responder, from a protocol-backed one. Failing
    # closed is the only safe direction here.
    #
    # `load_index` is stubbed rather than left to fail on its own: without the
    # stub this test reaches Moss Cloud and "passes" on an auth error, which
    # would make it a network test that happens to be green offline. The suite
    # is hermetic by construction, not by luck.
    class _Unreachable:
        async def load_index(self, name: str) -> None:
            raise ConnectionError("moss unreachable")

    monkeypatch.setattr(
        "aiscelapeus.moss_context.MossClient",
        lambda *args, **kwargs: _Unreachable(),
    )

    outcome = await context.connect()

    assert outcome.ok is False
    assert "moss unreachable" in (outcome.error or ""), (
        "a failed outcome must carry why, or the operator cannot act on it"
    )
    assert outcome.protocols_index == config.protocols_index
    assert context._loaded is False

    with pytest.raises(TierViolation):
        await context.lookup_protocol(
            RetrievalRequest.for_protocol("chest pain", config)
        )


async def test_a_successful_connect_reports_what_it_found(
    context: EmergencyContext,
    config: MossConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # falsifier: `connect` returns ok=True but discards `doc_count`, so
    # documents left under this index name by a previous run - the condition
    # that silently merges two incidents into one audit trail - are invisible to
    # the caller. `resumed_existing_session` is the signal main.py logs on, and
    # it is derived, so a zero here would make the warning unreachable.
    class _Session:
        doc_count = 4

    class _Client:
        async def load_index(self, name: str) -> None:
            return None

        async def session(self, name: str) -> _Session:
            return _Session()

    monkeypatch.setattr(
        "aiscelapeus.moss_context.MossClient", lambda *a, **k: _Client()
    )

    outcome = await context.connect()

    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.loaded_doc_count == 4
    assert outcome.resumed_existing_session is True
    assert context._loaded is True, "a successful load makes the corpus resident"


async def test_the_fake_reproduces_the_not_yet_loaded_window(
    config: MossConfig,
) -> None:
    # falsifier: the fake is unconditionally permissive, so no test of the
    # agent's tool layer can ever exercise the tier-rejection branch, and a
    # caller that issues a Class-0 lookup too early is provably untested.
    moss = FakeMoss(loaded=False)
    with pytest.raises(TierViolation):
        await moss.lookup_protocol(RetrievalRequest.for_protocol("chest pain", config))

    moss.loaded = True
    result = await moss.lookup_protocol(
        RetrievalRequest.for_protocol("chest pain", config)
    )
    assert result.trace.index == moss.protocols_index


async def test_the_fake_can_stand_in_for_a_network_backed_session_tier(
    config: MossConfig,
) -> None:
    # falsifier: `recall`'s gate is written as an unconditional pass rather than
    # a real check, so when B3 introduces a network-backed session store there
    # is no enforcement point to switch on and the Class-0 recall silently
    # becomes a network call. This is the test that proves the gate bites on
    # recall too, without waiting for that store to exist.
    moss = FakeMoss(session_in_process=False)
    with pytest.raises(TierViolation) as excinfo:
        await moss.recall(RetrievalRequest.for_session_state("tourniquet", config))
    assert moss.session_id in str(excinfo.value)


# ----------------------------------------------------- port/adapter conformance


def test_the_adapter_satisfies_the_port() -> None:
    # falsifier: the adapter drifts from the port again - a renamed method, a
    # changed return type - and, because nothing in the repo was annotated
    # MossPort, neither mypy nor a runtime check noticed. That is exactly how
    # `checkpoint` came to be declared on the port and absent from the only
    # implementation, and how record_fact returned a dict where the port said
    # FactRecord.
    context = EmergencyContext(
        config=MossConfig(project_id="", project_key=""), session_id="s"
    )
    assert isinstance(context, MossPort)
    assert isinstance(FakeMoss(), MossPort)


def test_runtime_checkable_alone_does_not_check_signatures() -> None:
    # falsifier: someone replaces the `_: MossPort = ...` annotation in
    # moss_context.py with the isinstance check above, reasoning that a runtime
    # assertion is stronger than a type annotation. It is not: this asserts that
    # `isinstance` passes an object whose method has entirely the wrong
    # signature, which is why five of the six divergent methods went unnoticed.
    # If this test ever fails because isinstance got stricter, the annotation is
    # merely redundant rather than wrong - but until then, deleting it removes
    # the only check that works.
    class WrongSignatures:
        """Every attribute name present, every signature wrong."""

        session_id = "s"
        elapsed_seconds = 0.0

        async def connect(self, unexpected: int) -> None: ...
        async def lookup_protocol(self) -> None: ...
        async def recall(self, a: int, b: int, c: int) -> None: ...
        async def record_fact(self) -> None: ...
        async def timeline(self, why: str) -> None: ...
        async def archive(self, extra: bool) -> None: ...
        async def aclose(self, force: bool) -> None: ...

    assert isinstance(WrongSignatures(), MossPort), (
        "isinstance checks attribute existence only - this is the gap the "
        "MossPort annotation in moss_context.py exists to close"
    )


def test_the_port_declares_no_method_the_adapter_lacks() -> None:
    # falsifier: a method is added to MossPort as a design intention and never
    # implemented - which is what `checkpoint` was. The port then documents a
    # capability the system does not have, and any caller written against the
    # port fails with AttributeError at runtime rather than at type-check time.
    declared = {
        name
        for name in dir(MossPort)
        if not name.startswith("_") and callable(getattr(MossPort, name, None))
    }
    assert declared, "the port must declare methods for this test to mean anything"
    missing = sorted(name for name in declared if not hasattr(EmergencyContext, name))
    assert missing == [], f"MossPort declares methods EmergencyContext lacks: {missing}"


def test_the_port_declares_everything_the_agent_layer_uses() -> None:
    # falsifier: `agent.py` reaches for a `self.context.<attr>` the port does not
    # declare - which it did, for `session_id` and `elapsed_seconds` - so a
    # double built to satisfy MossPort is still missing something the agent
    # needs, and the failure is an AttributeError at the first tool call rather
    # than a type-check error.
    #
    # The attribute list is READ OUT OF agent.py rather than hardcoded here. A
    # hardcoded list is a second source of truth that goes stale the moment the
    # agent layer reaches for something new: it would keep passing while the
    # exact defect it names walks back in. That is not hypothetical - this test
    # was hardcoded to ("session_id", "elapsed_seconds") and `session_id` was
    # then correctly removed from the port, so the test failed for the one
    # reason it should never fail: the truth moved and the fixture did not.
    # Parsed with `ast`, not a regex over the text. The first version of this
    # test used a regex plus `not name.startswith("a")` to "skip awaited
    # methods", and an independent review caught that for what it was: a
    # name-prefix guess, not a test of await-ness. It silently discarded any
    # access beginning with `a` - so the day `agent.py` grows an
    # `await self.context.archive()` (a plausible "wrap up the incident" tool),
    # the regex would capture `archive`, the filter would drop it before any
    # check ran, and this test would pass while `archive` went unverified
    # against the port. That is precisely the staleness this rewrite existed to
    # remove, reintroduced one level down. It also needed a second hardcoded
    # method list, which was the original problem again.
    #
    # The syntax already carries the distinction, so no heuristic is needed: a
    # method is `self.context.foo(...)` - an Attribute inside a Call - and a
    # property is a bare Attribute. Distinguishing on that drops both the prefix
    # guess and the exclusion list. `tests/meta/collect.py` already parses with
    # `ast` for the same reason.
    agent_source = (
        Path(__file__).resolve().parents[2] / "aiscelapeus" / "agent.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(agent_source)

    called: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "context"
        ):
            called.add(node.func.attr)

    accessed: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "context"
        ):
            accessed.add(node.attr)

    # Bare attribute access only. Awaited/called methods are covered by the
    # structural conformance test above, which compares real signatures.
    attributes = accessed - called

    # Guard the parse, not the local: if `agent.py` is renamed or the attribute
    # is reached through a different receiver, both sets come back empty and
    # every assertion below is skipped by an empty loop - the gate would pass by
    # finding nothing to check. Asserted against the known-present method rather
    # than against `accessed` being truthy, which cannot fail because the loop
    # above populates it.
    assert "lookup_protocol" in called, (
        "expected agent.py to call self.context.lookup_protocol; the parse found "
        f"calls={sorted(called)} attributes={sorted(attributes)} - did agent.py move?"
    )

    adapter = EmergencyContext(
        config=MossConfig(project_id="", project_key=""), session_id="s"
    )
    for attribute in sorted(attributes):
        assert hasattr(MossPort, attribute), (
            f"agent.py uses context.{attribute}; the port must declare it"
        )
        # Checked on an instance, not the class: a dataclass field exists on the
        # instance and not on the class object, so asserting against the class
        # would fail on a correctly-implemented adapter.
        assert hasattr(adapter, attribute)
        assert hasattr(FakeMoss(), attribute)


def test_the_fake_reports_a_scripted_clock_not_a_real_one() -> None:
    # falsifier: the fake's elapsed clock is wired to wall time, so a test
    # asserting on an elapsed-time answer depends on how fast the suite runs -
    # it passes on a fast machine and flakes in CI, and the usual response to a
    # flaky assertion is to delete it.
    moss = FakeMoss(elapsed_s=42.5)
    assert moss.elapsed_seconds == 42.5
    assert FakeMoss().elapsed_seconds == 0.0


def test_checkpoint_is_absent_from_both_sides_of_the_port() -> None:
    # falsifier: `checkpoint` is reinstated on the port without an
    # implementation, putting the contract back in the state where it declared
    # a durability guarantee the Moss SDK offers no primitive for - `push_index`
    # is the only durable write and that is already `archive`. If periodic
    # durability is genuinely required, it needs a real implementation and a
    # test, not a re-declared signature.
    assert not hasattr(MossPort, "checkpoint"), (
        "the port must not declare a capability nothing implements"
    )
    assert not hasattr(EmergencyContext, "checkpoint")
    assert not hasattr(FakeMoss, "checkpoint")
