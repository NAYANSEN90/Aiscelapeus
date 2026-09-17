"""Moss context layer -- the part of the system the latency story rests on.

Two indexes, two different jobs:

1. **Protocol index** (cloud-built, loaded into process memory at startup).
   The verified first-aid corpus. `load_index` pulls the built index into the
   agent process once, so every subsequent `query` is an in-process lookup with
   no network hop. This is what replaces a traditional vector database.

2. **Session index** (`MossClient.session`, purely in-process).
   The live state of this one emergency: vitals, interventions, timestamps,
   observations. It is written to mid-conversation and queried semantically --
   "what have we already done", "when was the tourniquet applied" -- at
   in-memory speed. At the end of the incident `push_index()` promotes it to
   Moss Cloud so the SOAP note and the audit trail can be rebuilt later.

Every call is traced, and every result carries Moss's own `time_taken_ms`
alongside our wall-clock measurement, so the sub-10ms claim is evidenced rather
than asserted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from moss import (
    DocumentInfo,
    GetDocumentsOptions,
    MossClient,
    MutationOptions,
    QueryOptions,
    SearchResult,
    SessionIndex,
)

from .config import MossConfig
from .ports import ArchiveOutcome, ConnectOutcome, FactRecord, MossPort
from .protocols import as_documents
from .retrieval import (
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    Retrieved,
    require_tier,
)
from .telemetry import span
from .triage import FactKind

logger = logging.getLogger(__name__)

# Fact kinds live in triage.FactKind: what the record says happened is a domain
# classification, not a storage concern.

# Re-exported. `Retrieved`, `RetrievalTrace` and `RetrievalResult` moved to
# `retrieval.py` alongside `RetrievalRequest`, because `ports` needs
# `RetrievalResult` to declare `MossPort` and importing it from here forced
# `ports -> moss_context`, which in turn barred this module from ever importing
# `ports` or `retrieval`. They are re-exported so `from .moss_context import
# RetrievalResult` keeps working for existing importers.
__all__ = [
    "EmergencyContext",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalTrace",
    "Retrieved",
    "seed_protocol_index",
]


def _hits(result: SearchResult) -> list[Retrieved]:
    out: list[Retrieved] = []
    for doc in result.docs:
        out.append(
            Retrieved(
                id=doc.id,
                text=doc.text,
                score=float(doc.score),
                metadata=dict(doc.metadata or {}),
            )
        )
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Metadata keys that are promoted to typed fields on `FactRecord`. Listed once
# so `_as_fact_record` cannot promote a key and also leave it in `metadata`,
# which would put the same value on the wire twice under two types.
_PROMOTED_KEYS = frozenset({"kind", "seq", "recorded_at", "elapsed_s", "session_id"})


def _as_fact_record(doc: DocumentInfo) -> FactRecord:
    """Parse one stored document back into the domain type.

    Moss metadata is string-valued, so `seq` and `elapsed_s` come back as
    strings. They are parsed here, once, at the boundary - not at each use site
    in `soap.py`. A record written by an older build, or by hand, can be missing
    or malformed: a missing `kind` is a real possibility and it must not become
    a plausible-looking OBSERVATION, so it raises. `seq`/`elapsed_s` fall back
    to 0 because an unsortable timeline is still readable, whereas a fact filed
    under the wrong clinical classification is not.
    """
    metadata = dict(doc.metadata or {})
    raw_kind = metadata.get("kind")
    try:
        kind = FactKind(raw_kind)
    except ValueError as exc:
        raise ValueError(
            f"stored fact {doc.id!r} has unknown kind {raw_kind!r}; expected one of "
            f"{', '.join(k.value for k in FactKind)}"
        ) from exc

    def _number(key: str, cast: Any) -> Any:  # noqa: ANN401 - int or float
        try:
            return cast(metadata.get(key, 0))
        except (TypeError, ValueError):
            logger.warning(
                "stored fact %s has unparseable %s=%r; defaulting to 0",
                doc.id,
                key,
                metadata.get(key),
            )
            return cast(0)

    return FactRecord(
        id=doc.id,
        text=doc.text,
        kind=kind,
        seq=_number("seq", int),
        recorded_at=metadata.get("recorded_at", ""),
        elapsed_s=_number("elapsed_s", float),
        metadata={k: v for k, v in metadata.items() if k not in _PROMOTED_KEYS},
    )


@dataclass
class EmergencyContext:
    """Owns both Moss indexes for the lifetime of one emergency session."""

    config: MossConfig
    session_id: str
    started_at: datetime = field(default_factory=_now)

    _client: MossClient | None = field(default=None, init=False, repr=False)
    _session: SessionIndex | None = field(default=None, init=False, repr=False)
    _seq: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # Whether `load_index` has completed, i.e. whether the protocol corpus is
    # resident in this process. Separate from `_client is not None`: the client
    # exists the instant it is constructed, while the corpus is only local once
    # the load returns, and it is that window - client up, index not yet
    # resident - in which a Class-0 query silently becomes a network call.
    _loaded: bool = field(default=False, init=False)

    # ---------------------------------------------------------------- lifecycle

    @property
    def session_index_name(self) -> str:
        return f"{self.config.session_index_prefix}-{self.session_id}"

    @property
    def elapsed_seconds(self) -> float:
        return (_now() - self.started_at).total_seconds()

    async def connect(self) -> ConnectOutcome:
        """Open the Moss client, warm the protocol index, open the session index.

        `load_index` is the expensive call and it happens exactly once, before
        the responder says a word. Everything after it is in-process.

        Returns an outcome rather than raising, which is what `MossPort` has
        always declared and what the port's own docstring says it is for: the
        one caller (`main.entrypoint`) did not guard this, so a Moss failure
        meant the responder heard silence. `_loaded` stays False on failure, so
        a Class-0 lookup after a failed connect raises `TierViolation` instead
        of quietly issuing a network query on the voice path.
        """
        with span("moss.connect", **{"session.id": self.session_id}) as current:
            load_ms = 0.0
            try:
                self._client = MossClient(self.config.project_id, self.config.project_key)

                load_started = time.perf_counter()
                await self._client.load_index(self.config.protocols_index)
                load_ms = (time.perf_counter() - load_started) * 1000
                self._loaded = True
                current.set_attribute("moss.index", self.config.protocols_index)
                current.set_attribute("moss.load_ms", round(load_ms, 2))

                self._session = await self._client.session(self.session_index_name)
                current.set_attribute("moss.session_index", self.session_index_name)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                current.set_attribute("moss.connect_failed", True)
                logger.exception("Moss connect failed for %s", self.session_id)
                return ConnectOutcome(
                    ok=False,
                    protocols_index=self.config.protocols_index,
                    load_ms=round(load_ms, 2),
                    error=f"{type(exc).__name__}: {exc}",
                )

            doc_count = self._session.doc_count
            current.set_attribute("moss.session_doc_count", doc_count)

        logger.info(
            "Moss ready: protocols=%s loaded in %.0fms, session index=%s (%d existing docs)",
            self.config.protocols_index,
            load_ms,
            self.session_index_name,
            doc_count,
        )
        return ConnectOutcome(
            ok=True,
            protocols_index=self.config.protocols_index,
            loaded_doc_count=doc_count,
            load_ms=round(load_ms, 2),
        )

    async def aclose(self) -> None:
        # Cleared before the unload is attempted, not after: once shutdown has
        # begun the corpus must not be treated as resident, and an unload that
        # raises would otherwise leave `_loaded` True and let a late Class-0
        # query past the tier gate on its way to a network round-trip.
        self._loaded = False
        if self._client is not None:
            try:
                await self._client.unload_index(self.config.protocols_index)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                logger.debug("unload_index failed during shutdown", exc_info=True)

    # ---------------------------------------------------------------- retrieval

    async def lookup_protocol(self, request: RetrievalRequest) -> RetrievalResult:
        """Semantic + keyword search over the verified protocol corpus.

        Takes a `RetrievalRequest` rather than loose arguments. `alpha` used to
        be the literal `0.8` here and `0.7` in `recall`, so the tuned weighting
        had three definitions - two literals and a comment - and no test could
        pin any of them. Now it has one, `retrieval.PROTOCOL_ALPHA`, and the
        request is validated before this method is entered.
        """
        if self._client is None:
            raise RuntimeError("EmergencyContext.connect() was never awaited")

        # The tier gate, at the call site, as ports.py describes. `_loaded` -
        # not a constant - is what makes this bite: `load_index` is what makes
        # the protocol corpus in-process, so a Class-0 lookup issued before it
        # completes resolves over the network and is exactly the defect the
        # rule names. Keyed on the adapter's current state, so it fires on a
        # real ordering mistake rather than only on a hypothetical store.
        require_tier(
            self.config.protocols_index,
            request.latency_class,
            in_process=self._loaded,
        )

        options = QueryOptions(
            top_k=request.top_k,
            alpha=request.alpha,
            filter=request.moss_filter(),
        )

        with span(
            "moss.query.protocols",
            **{
                "moss.index": self.config.protocols_index,
                "moss.top_k": options.top_k,
                "moss.alpha": request.alpha,
                "moss.latency_class": int(request.latency_class),
                "moss.filter.categories": ",".join(request.categories)
                if request.categories
                else "none",
                "session.id": self.session_id,
            },
        ) as current:
            started = time.perf_counter()
            result = await self._client.query(
                self.config.protocols_index, request.query, options
            )
            wall_ms = (time.perf_counter() - started) * 1000
            hits = _hits(result)
            trace = self._annotate(
                current, self.config.protocols_index, request.query, wall_ms, result, hits
            )

        return RetrievalResult(hits=hits, trace=trace)

    async def recall(self, request: RetrievalRequest) -> RetrievalResult:
        """Semantic recall over what has already happened in this emergency."""
        session = self._require_session()

        # `MossClient.session` is an in-memory index by construction, so a
        # Class-0 recall is legitimate today and this never raises. The call is
        # here anyway, with the answer stated rather than assumed: when B3 puts
        # a network-backed session tier behind this port, the enforcement point
        # already exists and one `False` turns it on.
        require_tier(self.session_index_name, request.latency_class, in_process=True)

        options = QueryOptions(top_k=request.top_k, alpha=request.alpha)

        with span(
            "moss.query.session_state",
            **{
                "moss.index": self.session_index_name,
                "moss.top_k": options.top_k,
                "moss.alpha": request.alpha,
                "moss.latency_class": int(request.latency_class),
                "session.id": self.session_id,
            },
        ) as current:
            started = time.perf_counter()
            result = await session.query(request.query, options)
            wall_ms = (time.perf_counter() - started) * 1000
            hits = _hits(result)
            trace = self._annotate(
                current, self.session_index_name, request.query, wall_ms, result, hits
            )

        return RetrievalResult(hits=hits, trace=trace)

    def _annotate(
        self,
        current: Any,  # noqa: ANN401 - opentelemetry Span
        index: str,
        query: str,
        wall_ms: float,
        result: SearchResult,
        hits: list[Retrieved],
    ) -> RetrievalTrace:
        moss_ms = result.time_taken_ms
        within = wall_ms <= self.config.latency_budget_ms
        current.set_attribute("moss.wall_ms", round(wall_ms, 3))
        if moss_ms is not None:
            current.set_attribute("moss.time_taken_ms", moss_ms)
        current.set_attribute("moss.hits", len(hits))
        current.set_attribute("moss.within_budget", within)
        if not within:
            current.set_attribute(
                "moss.budget_exceeded_by_ms",
                round(wall_ms - self.config.latency_budget_ms, 3),
            )
            logger.warning(
                "Moss query on %s took %.2fms, over the %.0fms budget",
                index,
                wall_ms,
                self.config.latency_budget_ms,
            )
        return RetrievalTrace(
            index=index,
            query=query,
            wall_ms=round(wall_ms, 3),
            moss_ms=moss_ms,
            hits=len(hits),
            within_budget=within,
        )

    # ------------------------------------------------------------------ writing

    async def record_fact(
        self,
        kind: FactKind | str,
        text: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> FactRecord:
        """Append one fact to the live emergency state.

        Returns the stored record so the caller can echo it to the UI.

        An unrecognised kind is an error. It used to be rewritten to
        "observation" with no log line, so a typo produced a plausible-looking
        record filed under the wrong classification and nothing ever noticed.

        Returns `FactRecord`, which is what `MossPort` declares. It used to
        return `dict[str, str]`, and `agent.record_finding` bridged the gap
        with `hasattr(record, "to_payload")` - a branch that could not be
        tested against the real adapter because only the fake ever took it, and
        which typed `elapsed_s` as a string on one path and a float on the
        other. The wire shape now has one definition, `FactRecord.to_payload`.
        """
        session = self._require_session()
        try:
            fact_kind = FactKind(kind)
        except ValueError as exc:
            raise ValueError(
                f"unknown fact kind {kind!r}; expected one of "
                f"{', '.join(k.value for k in FactKind)}"
            ) from exc
        kind = fact_kind.value

        async with self._lock:
            self._seq += 1
            seq = self._seq

        timestamp = _now()
        elapsed = round((timestamp - self.started_at).total_seconds(), 1)
        doc_id = f"fact-{seq:05d}"

        metadata: dict[str, str] = {
            "kind": kind,
            "seq": f"{seq:05d}",
            "session_id": self.session_id,
            "recorded_at": timestamp.isoformat(timespec="milliseconds"),
            "elapsed_s": f"{elapsed:.1f}",
        }
        if extra:
            metadata.update({k: str(v) for k, v in extra.items()})

        # The indexed text carries the elapsed clock inline, because "how long
        # since the last breath" is a semantic question the responder will ask
        # out loud and Moss has to be able to match against it.
        indexed_text = f"[T+{elapsed:.0f}s] {kind}: {text}"

        with span(
            "moss.write.session_state",
            **{
                "moss.index": self.session_index_name,
                "triage.fact_kind": kind,
                "session.id": self.session_id,
            },
        ) as current:
            started = time.perf_counter()
            added, updated = await session.add_docs(
                [DocumentInfo(id=doc_id, text=indexed_text, metadata=metadata)],
                MutationOptions(upsert=True),
            )
            current.set_attribute("moss.wall_ms", round((time.perf_counter() - started) * 1000, 3))
            current.set_attribute("moss.docs_added", added)
            current.set_attribute("moss.docs_updated", updated)
            current.set_attribute("moss.doc_count", session.doc_count)

        return FactRecord(
            id=doc_id,
            text=indexed_text,
            kind=fact_kind,
            seq=seq,
            recorded_at=metadata["recorded_at"],
            # float, not the formatted string that went into the index
            # metadata. `elapsed_s` is a quantity the UI does arithmetic on;
            # the metadata copy is a string because Moss metadata is
            # string-valued, and that is a storage detail, not the contract.
            elapsed_s=elapsed,
            metadata=dict(extra or {}),
        )

    async def timeline(self) -> list[FactRecord]:
        """Full ordered history of the incident -- the input to the SOAP note.

        Returns `FactRecord`s, as `MossPort` declares. The stored metadata is
        parsed back into typed fields here, at the boundary, so `soap.py` and
        the SOAP prompt receive numbers and enums rather than re-parsing
        strings out of a dict at every use site (CLAUDE.md: parse once, at the
        edge).
        """
        session = self._require_session()
        with span("moss.read.timeline", **{"session.id": self.session_id}) as current:
            docs = await session.get_docs(
                GetDocumentsOptions(sort_by="seq", ascending=True)
            )
            current.set_attribute("moss.doc_count", len(docs))
        return [_as_fact_record(d) for d in docs]

    async def archive(self) -> ArchiveOutcome:
        """Promote the live session index to Moss Cloud for audit and review.

        Returns an outcome, as `MossPort` declares. This is the only durable
        write in the system and it used to fail into a `logger.exception` in
        `main._shutdown`, so the incident record silently not reaching storage
        was indistinguishable from it reaching storage.
        """
        session = self._require_session()
        if session.doc_count == 0:
            # Not a failure: an incident with no recorded facts has nothing to
            # archive. `ok=True` with a zero count says exactly that, where a
            # bare `None` left the caller unable to tell it from an error.
            return ArchiveOutcome(ok=True, doc_count=0)
        try:
            with span("moss.push_index", **{"session.id": self.session_id}) as current:
                result = await session.push_index()
                current.set_attribute("moss.index", result.index_name)
                current.set_attribute("moss.doc_count", result.doc_count)
                current.set_attribute("moss.job_id", result.job_id)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.exception("Moss archive failed for %s", self.session_id)
            return ArchiveOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
        logger.info(
            "Archived %d facts to Moss index %s (job %s)",
            result.doc_count,
            result.index_name,
            result.job_id,
        )
        return ArchiveOutcome(ok=True, doc_count=result.doc_count)

    def _require_session(self) -> SessionIndex:
        if self._session is None:
            raise RuntimeError("EmergencyContext.connect() was never awaited")
        return self._session


# D4: the annotation that makes mypy check this adapter against its port.
#
# Nothing in the repo was annotated `MossPort`, so neither mypy nor
# `@runtime_checkable` could observe that `EmergencyContext` was missing
# `checkpoint` entirely and disagreed with the port on four return types and
# two parameter lists. `runtime_checkable` would not have caught it either: it
# checks attribute *existence*, not signatures, so a deliberate `isinstance`
# would have passed five of the six methods.
#
# A module-level annotated assignment is the cheapest mechanism that actually
# checks: mypy verifies the protocol structurally at this line, in CI, and the
# error names the offending method and type. It costs one constructor call at
# import - which is why the arguments are placeholders: `EmergencyContext`
# performs no I/O in `__init__`, all of that is in `connect`.
_: MossPort = EmergencyContext(
    config=MossConfig(project_id="", project_key=""), session_id="conformance-check"
)


async def seed_protocol_index(config: MossConfig, *, recreate: bool = False) -> None:
    """One-off build of the protocol index in Moss Cloud.

    Safe to re-run: without --recreate it upserts into the existing index.
    """
    client = MossClient(config.project_id, config.project_key)
    docs = [
        DocumentInfo(id=d["id"], text=d["text"], metadata={k: str(v) for k, v in d["metadata"].items()})
        for d in as_documents()
    ]

    existing = {index.name for index in await client.list_indexes()}

    if recreate and config.protocols_index in existing:
        logger.info("Deleting existing index %s", config.protocols_index)
        await client.delete_index(config.protocols_index)
        existing.discard(config.protocols_index)

    if config.protocols_index in existing:
        result = await client.add_docs(config.protocols_index, docs, MutationOptions(upsert=True))
        logger.info("Upserted %d protocols into %s", len(docs), result.index_name)
    else:
        result = await client.create_index(config.protocols_index, docs)
        logger.info("Built index %s with %d protocols", result.index_name, result.doc_count)
