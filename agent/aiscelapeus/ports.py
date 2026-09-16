"""The boundaries between the agent and everything outside the process.

`agent.py` already never imported the Moss SDK - it went through
`EmergencyContext`. This names that seam so it can be substituted, and adds the
one thing the informal version could not express: which latency class a call
belongs to.

Latency classes come from the retrieval design. Class 0 is the synchronous
responder voice turn, budgeted at 10ms, and only the in-process index may serve
it; a Class-0 call that reaches a network store is a build-failing defect
rather than a slow path. Making the class an argument means the rule is
enforced by the port that receives it, at the call site, rather than by an
alert watching span attributes after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Protocol, runtime_checkable

from .moss_context import RetrievalResult


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


class FactKind(str, Enum):
    """What kind of thing was recorded.

    An unrecognised kind used to be silently rewritten to "observation", so a
    typo became a plausible-looking record with the wrong classification and no
    log line. These are the only legal values; anything else is an error.
    """

    VITAL = "vital"
    INTERVENTION = "intervention"
    OBSERVATION = "observation"
    SYMPTOM = "symptom"
    ESCALATION = "escalation"


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
    """Retrieval and recording for one incident."""

    async def connect(self) -> ConnectOutcome: ...

    async def lookup_protocol(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        top_k: int | None = None,
        latency_class: LatencyClass = LatencyClass.VOICE_TURN,
    ) -> RetrievalResult: ...

    async def recall(
        self,
        query: str,
        *,
        top_k: int | None = None,
        latency_class: LatencyClass = LatencyClass.VOICE_TURN,
    ) -> RetrievalResult: ...

    async def record_fact(
        self,
        kind: FactKind,
        text: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> FactRecord: ...

    async def timeline(self) -> list[FactRecord]: ...

    async def checkpoint(self) -> None: ...

    async def archive(self) -> ArchiveOutcome: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class PublisherPort(Protocol):
    """Send one typed event to whoever is watching this incident."""

    async def __call__(self, topic: str, payload: dict) -> None: ...
