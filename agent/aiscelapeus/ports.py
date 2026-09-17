"""The boundaries between the agent and everything outside the process.

`agent.py` already never imported the Moss SDK - it went through
`EmergencyContext`. This names that seam so it can be substituted, and adds the
one thing the informal version could not express: which latency class a call
belongs to.

Latency classes come from the retrieval design. Class 0 is the synchronous
responder voice turn, budgeted at 10ms, and only the in-process index may serve
it; a Class-0 call that reaches a network store is a build-failing defect
rather than a slow path. Making the class an argument means the rule *can* be
enforced at the call site rather than by an alert watching span attributes
after the fact.

Where that enforcement actually lives: `retrieval.require_tier`, called by
every adapter method that accepts a `latency_class`. This docstring previously
claimed the rule was "enforced by the port that receives it" - it was not. A
`Protocol` cannot enforce anything at runtime; it only describes a shape. The
single `TierViolation` raise in the repo was in `testing/fakes.py`, so the
guarantee existed only inside the double that stood in for the real thing.
Naming the enforcement function here keeps the claim and the code in the same
place, and `EmergencyContext` is annotated against `MossPort` below so mypy
reports a divergence instead of leaving it to be discovered by reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

# Re-exported: FactKind is a domain classification and lives with the other
# domain types, but it is part of this port's signature.
from .triage import FactKind

if TYPE_CHECKING:
    # Import-time cycle, broken deliberately. `retrieval` imports this module
    # for `LatencyClass` and `TierViolation`; this module needs
    # `RetrievalResult` only inside an annotation, and `from __future__ import
    # annotations` makes every annotation here a string. So the dependency is
    # real for the type checker and absent at runtime, and the runtime graph
    # stays a DAG: moss_context -> retrieval -> ports -> triage -> phrases.
    from .retrieval import RetrievalRequest, RetrievalResult

__all__ = [
    "ArchiveOutcome",
    "ConnectOutcome",
    "FactKind",
    "FactRecord",
    "LatencyClass",
    "MossPort",
    "PublisherPort",
    "TierViolation",
]


class LatencyClass(IntEnum):
    """Where a retrieval sits relative to the responder's voice turn."""

    # Synchronous, in the responder's turn. In-process store only.
    VOICE_TURN = 0
    # Anticipatory prefetch, concurrent with speech. Never awaited on the turn.
    PREFETCH = 1
    # A clinician's own query. Slower stores permitted.
    CLINICIAN = 2
    # SOAP, audit, catch-up. Off the call entirely.
    BACKGROUND = 3


class TierViolation(RuntimeError):
    """A store was asked to serve a latency class it must never serve.

    Raised rather than logged: a network hop on the voice path is a defect in
    the caller, and a warning in a span attribute is something nobody reads
    until after the incident.
    """

    def __init__(self, store: str, latency_class: LatencyClass) -> None:
        self.store = store
        self.latency_class = latency_class
        super().__init__(
            f"{store} may not serve latency class {int(latency_class)} "
            f"({latency_class.name}); the voice path is in-process only"
        )


@dataclass(frozen=True)
class ConnectOutcome:
    """Whether the incident's storage came up, and what it found.

    `connect` used to raise, and the one caller did not guard it, so a Moss
    failure meant the responder heard silence. An outcome makes degrading the
    normal path rather than the exceptional one.

    `loaded_doc_count` is the signal that a previous run's documents are still
    present under this index name - the condition that silently merges two
    incidents' records. It was previously discarded.
    """

    ok: bool
    protocols_index: str
    loaded_doc_count: int = 0
    load_ms: float = 0.0
    error: str | None = None

    @property
    def resumed_existing_session(self) -> bool:
        return self.loaded_doc_count > 0


@dataclass(frozen=True)
class FactRecord:
    """One fact as it was stored."""

    id: str
    text: str
    kind: FactKind
    seq: int
    recorded_at: str
    elapsed_s: float
    metadata: dict[str, str]

    def to_payload(self) -> dict[str, Any]:
        """The wire shape for the UI's finding feed.

        Built explicitly so storage bookkeeping does not leak into the contract
        and a field the UI needs cannot quietly stop being written.
        """
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind.value,
            "seq": self.seq,
            "recorded_at": self.recorded_at,
            "elapsed_s": self.elapsed_s,
            **self.metadata,
        }


@dataclass(frozen=True)
class ArchiveOutcome:
    """Whether the incident record reached durable storage.

    Archiving used to fail into a log line, so the only durable write in the
    system could not fail loudly.
    """

    ok: bool
    doc_count: int = 0
    error: str | None = None


@runtime_checkable
class MossPort(Protocol):
    """Retrieval and recording for one incident.

    `runtime_checkable` is kept, but it is not the conformance check. It only
    verifies that the named attributes *exist*: an `isinstance` against this
    protocol passed `EmergencyContext` on five of six methods while two of them
    had the wrong signature and one was missing entirely. What catches that is
    the `MossPort` annotation at the bottom of this module plus mypy in CI.
    """

    # `session_id` is deliberately NOT declared here, though `agent.py` used to
    # reach for `context.session_id` eight times. An independent review caught
    # that adding it to this port creates a second source of truth: the incident
    # id already lives on `TriageState.session_id`, and `main.py` hands the same
    # local to both objects. Two accessors for one value, kept in sync by
    # convention, is the DRY failure CLAUDE.md names - and it was already
    # visible in the tool-layer fixtures, which had to write
    # `TriageState(session_id=moss.session_id)` to keep them agreeing.
    # `agent.py` now reads `self.state.session_id`. `EmergencyContext` keeps its
    # own `session_id` for index naming and spans; that is a storage concern and
    # not part of this contract.
    #
    # `elapsed_seconds` is still declared below, and that is a KNOWN WART rather
    # than a decision. The same review observed that `clock.py` already defines
    # a `Clock` Protocol with a `ManualClock` fake, precisely so time is
    # injectable - and that `EmergencyContext.elapsed_seconds` bypasses it via a
    # private module-level `_now()`. So a storage port is currently carrying a
    # clock, and there is a second, differently-shaped fake for a concept
    # `ManualClock` already fakes properly. The right fix routes elapsed time
    # through `Clock` (most likely on `TriageState`, which already holds one).
    # Not done here: it changes `EmergencyContext`'s time source and the
    # `record_fact` elapsed values that land in the medico-legal timeline, which
    # is its own subsystem with its own tests, not a tail-end edit to this diff.
    @property
    def elapsed_seconds(self) -> float: ...

    async def connect(self) -> ConnectOutcome: ...

    async def lookup_protocol(self, request: RetrievalRequest) -> RetrievalResult: ...

    async def recall(self, request: RetrievalRequest) -> RetrievalResult: ...

    async def record_fact(
        self,
        kind: FactKind,
        text: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> FactRecord: ...

    async def timeline(self) -> list[FactRecord]: ...

    async def archive(self) -> ArchiveOutcome: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class PublisherPort(Protocol):
    """Send one typed event to whoever is watching this incident."""

    async def __call__(self, topic: str, payload: dict) -> None: ...
