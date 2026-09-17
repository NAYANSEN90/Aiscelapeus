"""The adapter: what actually reaches the vendor, and what comes back typed.

Two things are pinned here that nothing pinned before.

**The ranking inputs.** Threading `RetrievalRequest` through `lookup_protocol`
and `recall` was a refactor with no intended behaviour change, and "no
behaviour change" is a claim about the values that reach `QueryOptions`, not
about the suite still being green. So the `QueryOptions` handed to Moss is
captured and asserted field by field. Before this change `alpha` was the
literal `0.8` in `lookup_protocol` and `0.7` in `recall`, and no test could
observe either.

**The record shape.** `record_fact` returned `dict[str, str]` while `MossPort`
declared `FactRecord`, and `agent.py` bridged the two with
`hasattr(record, "to_payload")`. The dict path is gone; these tests hold the
adapter to the port so it cannot come back.

The Moss client is a stub, so nothing here is evidence about latency or about
Moss's own filter semantics - only about what this adapter sends and parses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from moss import DocumentInfo

from aiscelapeus.config import MossConfig
from aiscelapeus.moss_context import EmergencyContext, _as_fact_record
from aiscelapeus.ports import FactRecord, LatencyClass
from aiscelapeus.retrieval import (
    PROTOCOL_ALPHA,
    SESSION_ALPHA,
    RetrievalRequest,
)
from aiscelapeus.triage import FactKind


@dataclass
class _StubResult:
    """What the Moss SDK returns from a query, reduced to what the adapter reads."""

    docs: list[Any] = field(default_factory=list)
    time_taken_ms: int | None = 2


@dataclass
class _StubDoc:
    id: str
    text: str
    score: float
    metadata: dict[str, str]


@dataclass
class _SpyClient:
    """Captures the query arguments the adapter sends."""

    calls: list[tuple[str, str, Any]] = field(default_factory=list)

    async def query(self, index: str, query: str, options: Any) -> _StubResult:
        self.calls.append((index, query, options))
        return _StubResult(
            docs=[
                _StubDoc(
                    id="airway-choking-adult",
                    text="Five back blows.",
                    score=0.91,
                    metadata={"title": "Choking, adult", "category": "airway"},
                )
            ]
        )


@dataclass
class _SpySession:
    """Captures session queries and writes."""

    doc_count: int = 0
    queries: list[tuple[str, Any]] = field(default_factory=list)
    added: list[list[DocumentInfo]] = field(default_factory=list)
    docs: list[Any] = field(default_factory=list)

    async def query(self, query: str, options: Any) -> _StubResult:
        self.queries.append((query, options))
        return _StubResult(docs=[])

    async def add_docs(
        self, docs: list[DocumentInfo], options: Any
    ) -> tuple[int, int]:
        self.added.append(list(docs))
        self.doc_count += len(docs)
        return len(docs), 0

    async def get_docs(self, options: Any) -> list[Any]:
        return list(self.docs)


@pytest.fixture
def config() -> MossConfig:
    # Not the dataclass defaults: a test asserting "top_k came from config"
    # passes against a hardcoded 3 if config also says 3.
    return MossConfig(
        project_id="test-project",
        project_key="test-key",
        protocol_top_k=7,
        state_top_k=9,
    )


@pytest.fixture
def context(config: MossConfig) -> EmergencyContext:
    """A connected-looking adapter with stubs in place of the SDK. No network."""
    ctx = EmergencyContext(config=config, session_id="incident-7")
    ctx._client = _SpyClient()  # type: ignore[assignment]
    ctx._session = _SpySession()  # type: ignore[assignment]
    ctx._loaded = True
    return ctx


# -------------------------------------------- the refactor changed no ranking


async def test_the_protocol_lookup_sends_the_measured_alpha(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: threading RetrievalRequest through changed the weighting that
    # reaches Moss. The suite would stay green - nothing else observes alpha -
    # while retrieval quality shifts, and the measured 0.8 result in
    # docs/evidence/2026-09-17-retrieval-alpha-measurement.md would no longer
    # describe the running system. This is the assertion that makes "a refactor
    # with no behaviour change" a checked statement rather than an intention.
    await context.lookup_protocol(
        RetrievalRequest.for_protocol("choking adult", config)
    )

    client: Any = context._client
    index, query, options = client.calls[-1]
    assert index == config.protocols_index
    assert query == "choking adult"
    # `pytest.approx`, and the reason is worth stating: QueryOptions is a
    # Rust-backed type that stores alpha as a 32-bit float, so 0.8 reads back
    # as 0.800000011920929. An exact comparison here would fail on a value that
    # is correct, and the obvious "fix" for that failure - asserting the f32
    # literal - would pin the vendor's representation instead of our constant,
    # so a real change from 0.8 to 0.7 would need the test updated either way.
    # The tolerance is f32 epsilon, far tighter than the 0.1 gap between the
    # measured candidates, so this still fails if the wrong alpha is sent.
    assert options.alpha == pytest.approx(PROTOCOL_ALPHA, abs=1e-6), (
        "the alpha reaching Moss must be the measured constant, not a literal"
    )
    assert PROTOCOL_ALPHA == 0.8, "and that constant is the measured value"
    assert options.top_k == config.protocol_top_k == 7
    assert options.filter is None


async def test_the_session_recall_sends_the_session_alpha(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: recall's alpha was the inline literal 0.7 and the refactor was
    # supposed to move it to SESSION_ALPHA. If the two disagree, the constant is
    # documentation and the running weighting is something else - the exact
    # split this subsystem existed to remove.
    await context.recall(
        RetrievalRequest.for_session_state("when was the tourniquet applied", config)
    )

    session: Any = context._session
    query, options = session.queries[-1]
    assert query == "when was the tourniquet applied"
    # approx: QueryOptions stores alpha as f32 (see the protocol test above).
    assert options.alpha == pytest.approx(SESSION_ALPHA, abs=1e-6)
    assert SESSION_ALPHA == 0.8
    # The old literal, pinned as a negative: recall used to send 0.7 and the
    # refactor's whole claim is that it now sends the constant instead.
    assert options.alpha != pytest.approx(0.7, abs=1e-6)
    assert options.top_k == config.state_top_k == 9


async def test_no_alpha_literal_survives_in_the_adapter() -> None:
    # falsifier: a future edit reintroduces `alpha=0.8` inline "for clarity",
    # so the rule has two definition sites again and they can drift silently -
    # which is the state this subsystem was found in, with 0.8 in one method
    # and 0.7 in the other. A value assertion cannot catch a *second* copy that
    # happens to agree today, so this reads the source.
    import inspect

    from aiscelapeus import moss_context

    source = inspect.getsource(moss_context)
    body = "\n".join(
        line
        for line in source.splitlines()
        # Comments and docstrings discuss the old literals on purpose.
        if not line.lstrip().startswith("#")
    )
    assert "alpha=0.8" not in body
    assert "alpha=0.7" not in body
    assert "alpha=request.alpha" in body.replace(" ", ""), (
        "alpha must come from the request, which is the single definition site"
    )


async def test_the_category_filter_comes_from_the_request(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: the adapter keeps building `{"category": {"$in": [...]}}` by
    # hand, so the filter's shape has two definitions - one here and one in
    # RetrievalRequest.moss_filter - and a change to the operator or the key is
    # a change in two files that nothing checks agree. A wrong key is a *valid*
    # filter matching no document, so the failure is zero hits, not an error.
    await context.lookup_protocol(
        RetrievalRequest.for_protocol(
            "choking adult", config, categories=("airway", "general")
        )
    )
    client: Any = context._client
    _, _, options = client.calls[-1]
    assert options.filter == {"category": {"$in": ["airway", "general"]}}


async def test_an_explicit_tier_reaches_the_adapter_unchanged(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: the adapter ignores the request's latency_class and assumes
    # VOICE_TURN, so a Class-3 background query is gated - and traced - as
    # though it were on the 10ms voice path. Tier enforcement then guards a
    # class the call does not belong to, and the span attribute that a latency
    # investigation would read is wrong.
    request = RetrievalRequest.for_protocol(
        "choking adult", config, latency_class=LatencyClass.BACKGROUND
    )
    assert request.latency_class is LatencyClass.BACKGROUND
    result = await context.lookup_protocol(request)
    assert result.trace.query == "choking adult"


async def test_hits_are_flattened_with_their_measurement(
    context: EmergencyContext, config: MossConfig
) -> None:
    # falsifier: the result loses its trace, or carries another call's trace.
    # LiveKit runs one task per tool call, so a shared trace slot let a protocol
    # lookup publish a state recall's latency under its own query string -
    # self-consistent, plausible, and wrong.
    result = await context.lookup_protocol(
        RetrievalRequest.for_protocol("choking adult", config)
    )

    assert len(result) == 1
    hit = result.hits[0]
    assert hit.id == "airway-choking-adult"
    assert hit.title == "Choking, adult"
    assert result.trace.index == config.protocols_index
    assert result.trace.query == "choking adult"
    assert result.trace.hits == 1


# ------------------------------------------------------- the record is typed


async def test_record_fact_returns_the_domain_type(
    context: EmergencyContext,
) -> None:
    # falsifier: the adapter returns a dict again while MossPort declares
    # FactRecord, so agent.py needs the `hasattr(record, "to_payload")` branch
    # back - a branch only the fake ever took, so it was untestable against the
    # real adapter, and one that typed elapsed_s as a string on one path and a
    # float on the other.
    record = await context.record_fact(
        FactKind.INTERVENTION, "tourniquet applied to left thigh"
    )

    assert isinstance(record, FactRecord)
    assert record.kind is FactKind.INTERVENTION
    assert record.seq == 1
    assert isinstance(record.elapsed_s, float), (
        "elapsed_s is a quantity the UI does arithmetic on, not a string"
    )
    assert record.text.endswith("tourniquet applied to left thigh")


async def test_record_fact_puts_the_indexed_text_in_the_record(
    context: EmergencyContext,
) -> None:
    # falsifier: the record's `text` and the text written to the index diverge,
    # so the finding the UI shows is not the text the recall search can match -
    # a responder asking "what did we give" gets a different answer from the one
    # on screen.
    record = await context.record_fact(FactKind.VITAL, "pulse 120")
    session: Any = context._session
    written = session.added[-1][0]
    assert record.text == written.text
    assert record.id == written.id


async def test_the_wire_payload_has_one_definition(
    context: EmergencyContext,
) -> None:
    # falsifier: the adapter builds its own dict for the UI instead of going
    # through FactRecord.to_payload, so a field the finding feed needs can stop
    # being written on one path while the other still has it, and the dashboard
    # shows a blank where a vital should be.
    record = await context.record_fact(
        FactKind.SYMPTOM, "complains of chest pain", extra={"site": "left arm"}
    )
    payload = record.to_payload()

    assert payload["kind"] == FactKind.SYMPTOM.value
    assert payload["text"] == record.text
    assert payload["site"] == "left arm", "caller-supplied extras must survive"
    assert payload["elapsed_s"] == record.elapsed_s


async def test_an_unknown_fact_kind_is_refused_not_relabelled(
    context: EmergencyContext,
) -> None:
    # falsifier: an unrecognised kind is rewritten to "observation", so a typo
    # files a plausible-looking record under the wrong clinical classification
    # and the SOAP note a clinician signs is wrong in a way nothing flags.
    with pytest.raises(ValueError) as excinfo:
        await context.record_fact("vitals", "pulse 120")  # type: ignore[arg-type]
    assert "vitals" in str(excinfo.value), "the message must name what arrived"

    session: Any = context._session
    assert session.added == [], "a refused fact must not be written"


async def test_sequence_numbers_do_not_repeat_across_facts(
    context: EmergencyContext,
) -> None:
    # falsifier: the sequence counter is read and incremented without the lock,
    # or reset, so two facts share a doc id. `add_docs` upserts, so the second
    # silently overwrites the first and an intervention disappears from the
    # incident record entirely.
    first = await context.record_fact(FactKind.VITAL, "pulse 120")
    second = await context.record_fact(FactKind.VITAL, "pulse 118")
    assert (first.seq, second.seq) == (1, 2)
    assert first.id != second.id


# ------------------------------------------------- the timeline is parsed back


async def test_the_timeline_returns_typed_records(
    context: EmergencyContext,
) -> None:
    # falsifier: the timeline returns dicts, so soap.py re-parses elapsed_s with
    # `float(fact.get("elapsed_s", 0) or 0)` and defaults a missing kind to
    # "observation" at the point of rendering - turning an unclassified fact
    # into a plausible-looking observation in the document a clinician signs.
    session: Any = context._session
    session.docs = [
        DocumentInfo(
            id="fact-00001",
            text="[T+12s] intervention: tourniquet applied",
            metadata={
                "kind": "intervention",
                "seq": "00001",
                "elapsed_s": "12.4",
                "recorded_at": "2026-09-17T10:00:00.000+00:00",
                "session_id": "incident-7",
                "site": "left thigh",
            },
        )
    ]

    timeline = await context.timeline()

    assert [type(record) for record in timeline] == [FactRecord]
    record = timeline[0]
    assert record.kind is FactKind.INTERVENTION
    assert record.seq == 1, "the zero-padded string must parse to an int"
    assert record.elapsed_s == 12.4
    assert record.metadata == {"site": "left thigh"}, (
        "promoted keys must not also remain in metadata, or one value is on the "
        "wire twice under two types"
    )


def test_a_stored_fact_with_an_unknown_kind_raises() -> None:
    # falsifier: a record written by an older build, or by hand, comes back with
    # a kind the enum does not have and is silently defaulted. It then appears
    # in the audit trail and the SOAP note under a classification nobody chose,
    # which is the same defect as relabelling on write - just deferred to read.
    doc = DocumentInfo(
        id="fact-00009",
        text="[T+30s] vitals: pulse 120",
        metadata={"kind": "vitals", "seq": "00009", "elapsed_s": "30.0"},
    )
    with pytest.raises(ValueError) as excinfo:
        _as_fact_record(doc)
    assert "fact-00009" in str(excinfo.value), "name the document, not just the kind"


def test_an_unparseable_elapsed_falls_back_rather_than_raising() -> None:
    # falsifier: a malformed numeric field raises, so one corrupt record makes
    # the entire timeline unreadable and no SOAP note is produced at all for
    # that incident. The clinical classification is worth raising over because a
    # wrong one is actively misleading; an unsortable timestamp still leaves a
    # readable, ordered-by-retrieval record, so it degrades instead.
    doc = DocumentInfo(
        id="fact-00003",
        text="[T+?] vital: pulse 120",
        metadata={"kind": "vital", "seq": "not-a-number", "elapsed_s": ""},
    )
    record = _as_fact_record(doc)
    assert record.kind is FactKind.VITAL
    assert record.seq == 0
    assert record.elapsed_s == 0.0


async def test_a_recorded_fact_round_trips_through_the_timeline(
    context: EmergencyContext,
) -> None:
    # falsifier: `record_fact` writes metadata under keys `_as_fact_record` does
    # not read - or in a format it cannot parse - so every fact survives the
    # write and is then silently degraded on the way back out. Each side has its
    # own test above and both would still pass; only writing and reading through
    # the real pair catches a key or format mismatch between them.
    written = await context.record_fact(
        FactKind.INTERVENTION, "tourniquet applied", extra={"site": "left thigh"}
    )
    session: Any = context._session
    session.docs = list(session.added[-1])

    read_back = (await context.timeline())[0]

    assert read_back.id == written.id
    assert read_back.kind is written.kind
    assert read_back.seq == written.seq
    assert read_back.text == written.text
    assert read_back.recorded_at == written.recorded_at
    assert read_back.metadata == written.metadata


# ----------------------------------------------------------------- archiving


async def test_archiving_nothing_is_a_success_not_a_failure(
    context: EmergencyContext,
) -> None:
    # falsifier: an incident with no recorded facts returns a falsy value that
    # the caller reads as an error, so main.py logs a failed archive for every
    # call that ended before anything was recorded - and a real archive failure
    # is then indistinguishable from an uneventful call in the logs.
    outcome = await context.archive()
    assert outcome.ok is True
    assert outcome.doc_count == 0
    assert outcome.error is None


async def test_a_failed_archive_is_reported_not_raised(
    context: EmergencyContext,
) -> None:
    # falsifier: archive raises instead of reporting. It is the only durable
    # write in the system and it runs in a shutdown callback, so an exception
    # there means the incident record silently never reached storage - the
    # medico-legal record of a real emergency, lost with a log line at best.
    session: Any = context._session
    session.doc_count = 3

    async def _boom() -> None:
        raise ConnectionError("moss push failed")

    session.push_index = _boom  # type: ignore[attr-defined]

    outcome = await context.archive()
    assert outcome.ok is False
    assert "moss push failed" in (outcome.error or "")
