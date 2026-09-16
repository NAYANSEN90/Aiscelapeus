"""Test doubles for the ports.

These ship inside the package rather than under tests/ so that the
signature-equality test can import both a fake and its real counterpart, and
so any future consumer of these ports gets the same double.

What a fake may and may not be used for: a fake stands in for a dependency
whose behaviour the test controls and whose contract is verified elsewhere
against the real thing. It must never be the subject of an assertion about a
real-world property - latency, filter semantics, wire format, durability.
Asserting "retrieval takes under 10ms" against an in-process dict measures
dictionary access and passes on a machine with no index installed at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..moss_context import RetrievalResult, RetrievalTrace, Retrieved
from ..ports import (
    ArchiveOutcome,
    ConnectOutcome,
    FactKind,
    FactRecord,
    LatencyClass,
    TierViolation,
)


@dataclass
class RecordedCall:
    """One call made against a fake, for order-sensitive assertions."""

    method: str
    query: str | None = None
    latency_class: LatencyClass | None = None
    kind: FactKind | None = None
    text: str | None = None


@dataclass
class FakeMoss:
    """An in-process stand-in for the incident's storage.

    Scripted results, recorded calls, and failure injection. Failure injection
    is the part that matters: the real `connect` used to raise into an
    unguarded caller, and there was no way to write a test for that because
    there was nothing to fail.
    """

    session_id: str = "test-session"
    protocols_index: str = "test-protocols"
    # query substring -> hits to return. A query matching nothing returns [],
    # which is the "no protocol matched" branch the agent must handle.
    scripted: dict[str, list[Retrieved]] = field(default_factory=dict)
    # Method names that should fail: {"connect", "query", "record", "archive"}.
    fail_on: set[str] = field(default_factory=set)
    # Reported per call, so budget-adjacent logic can be exercised. This is a
    # scripted number, never evidence about real latency.
    reported_wall_ms: float = 1.0
    latency_budget_ms: float = 10.0
    loaded_doc_count: int = 0

    calls: list[RecordedCall] = field(default_factory=list)
    facts: list[FactRecord] = field(default_factory=list)
    connected: bool = False
    closed: bool = False
    checkpoints: int = 0
    archived: bool = False
    _seq: int = 0

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> ConnectOutcome:
        self.calls.append(RecordedCall(method="connect"))
        if "connect" in self.fail_on:
            return ConnectOutcome(
                ok=False,
                protocols_index=self.protocols_index,
                error="fake connect failure",
            )
        self.connected = True
        return ConnectOutcome(
            ok=True,
            protocols_index=self.protocols_index,
            loaded_doc_count=self.loaded_doc_count,
            load_ms=self.reported_wall_ms,
        )

    async def aclose(self) -> None:
        self.closed = True

    # ------------------------------------------------------------- retrieval

    async def lookup_protocol(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        top_k: int | None = None,
        latency_class: LatencyClass = LatencyClass.VOICE_TURN,
    ) -> RetrievalResult:
        self.calls.append(
            RecordedCall(method="lookup_protocol", query=query, latency_class=latency_class)
        )
        if "query" in self.fail_on:
            raise RuntimeError("fake query failure")
        return self._result(self.protocols_index, query, top_k)

    async def recall(
        self,
        query: str,
        *,
        top_k: int | None = None,
        latency_class: LatencyClass = LatencyClass.VOICE_TURN,
    ) -> RetrievalResult:
        self.calls.append(
            RecordedCall(method="recall", query=query, latency_class=latency_class)
        )
        if "query" in self.fail_on:
            raise RuntimeError("fake query failure")
        return self._result(f"session-{self.session_id}", query, top_k)

    def _result(self, index: str, query: str, top_k: int | None) -> RetrievalResult:
        hits: list[Retrieved] = []
        for needle, scripted in self.scripted.items():
            if needle.lower() in query.lower():
                hits = list(scripted)
                break
        if top_k is not None:
            hits = hits[:top_k]
        trace = RetrievalTrace(
            index=index,
            query=query,
            wall_ms=self.reported_wall_ms,
            moss_ms=int(self.reported_wall_ms),
            hits=len(hits),
            within_budget=self.reported_wall_ms <= self.latency_budget_ms,
        )
        return RetrievalResult(hits=hits, trace=trace)

    # ---------------------------------------------------------------- writing

    async def record_fact(
        self,
        kind: FactKind,
        text: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> FactRecord:
        self.calls.append(RecordedCall(method="record_fact", kind=kind, text=text))
        if "record" in self.fail_on:
            raise RuntimeError("fake write failure")
        # Rejects an unknown kind rather than coercing it, matching the real
        # adapter's contract. A fake that is more permissive than the thing it
        # replaces lets a test pass against code that would fail in production.
        if not isinstance(kind, FactKind):
            raise ValueError(f"unknown fact kind {kind!r}")
        self._seq += 1
        record = FactRecord(
            id=f"{self.session_id}-fact-{self._seq:05d}",
            text=text,
            kind=kind,
            seq=self._seq,
            recorded_at=f"2026-01-01T00:00:{self._seq:02d}.000+00:00",
            elapsed_s=float(self._seq),
            metadata=dict(extra or {}),
        )
        self.facts.append(record)
        return record

    async def timeline(self) -> list[FactRecord]:
        self.calls.append(RecordedCall(method="timeline"))
        return list(self.facts)

    async def checkpoint(self) -> None:
        self.calls.append(RecordedCall(method="checkpoint"))
        self.checkpoints += 1

    async def archive(self) -> ArchiveOutcome:
        self.calls.append(RecordedCall(method="archive"))
        if "archive" in self.fail_on:
            return ArchiveOutcome(ok=False, error="fake archive failure")
        self.archived = True
        return ArchiveOutcome(ok=True, doc_count=len(self.facts))

    # ------------------------------------------------------------- assertions

    def methods_called(self) -> list[str]:
        return [c.method for c in self.calls]

    def voice_turn_calls(self) -> list[RecordedCall]:
        return [c for c in self.calls if c.latency_class is LatencyClass.VOICE_TURN]


@dataclass
class FakePublisher:
    """Records what reached the UI, in order.

    The publish callable is already injected, so this needs no patching. Topic
    order is the assertion that matters: the escalation sequence is a contract
    the dashboard depends on.
    """

    events: list[tuple[str, dict]] = field(default_factory=list)
    fail: bool = False

    async def __call__(self, topic: str, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("fake publish failure")
        self.events.append((topic, dict(payload)))

    def topics(self) -> list[str]:
        return [topic for topic, _ in self.events]

    def payloads_for(self, topic: str) -> list[dict]:
        return [payload for name, payload in self.events if name == topic]

    def last(self, topic: str) -> dict | None:
        found = self.payloads_for(topic)
        return found[-1] if found else None


@dataclass
class FakeNetworkStore:
    """A store that must never be reached on the voice path.

    Stands in for the deep-tier caches. Its only real job is to raise when
    asked to serve a Class-0 call, so a test can prove the voice path never
    touches it.
    """

    name: str = "redis"
    calls: list[RecordedCall] = field(default_factory=list)

    async def search_deep(
        self,
        query: str,
        *,
        latency_class: LatencyClass = LatencyClass.PREFETCH,
        **_: object,
    ) -> list[Retrieved]:
        if latency_class is LatencyClass.VOICE_TURN:
            raise TierViolation(self.name, latency_class)
        self.calls.append(
            RecordedCall(method="search_deep", query=query, latency_class=latency_class)
        )
        return []
