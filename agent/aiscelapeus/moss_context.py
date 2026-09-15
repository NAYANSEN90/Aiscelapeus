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
from typing import Any, Iterable

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
from .protocols import as_documents
from .telemetry import span

logger = logging.getLogger(__name__)

# Kinds of fact we record about a live emergency.
FACT_KINDS = ("vital", "intervention", "observation", "escalation", "dispatch")


@dataclass(frozen=True)
class Retrieved:
    """One retrieval hit, flattened for prompt injection."""

    id: str
    text: str
    score: float
    metadata: dict[str, str]

    @property
    def title(self) -> str:
        return self.metadata.get("title", self.id)


@dataclass
class RetrievalTrace:
    """What a retrieval cost. Surfaced to the UI so latency is visible live."""

    index: str
    query: str
    wall_ms: float
    moss_ms: int | None
    hits: int
    within_budget: bool


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
    last_trace: RetrievalTrace | None = field(default=None, init=False)

    # ---------------------------------------------------------------- lifecycle

    @property
    def session_index_name(self) -> str:
        return f"{self.config.session_index_prefix}-{self.session_id}"

    @property
    def elapsed_seconds(self) -> float:
        return (_now() - self.started_at).total_seconds()

    async def connect(self) -> None:
        """Open the Moss client, warm the protocol index, open the session index.

        `load_index` is the expensive call and it happens exactly once, before
        the responder says a word. Everything after it is in-process.
        """
        with span("moss.connect", **{"session.id": self.session_id}) as current:
            self._client = MossClient(self.config.project_id, self.config.project_key)

            load_started = time.perf_counter()
            await self._client.load_index(self.config.protocols_index)
            load_ms = (time.perf_counter() - load_started) * 1000
            current.set_attribute("moss.index", self.config.protocols_index)
            current.set_attribute("moss.load_ms", round(load_ms, 2))

            self._session = await self._client.session(self.session_index_name)
            current.set_attribute("moss.session_index", self.session_index_name)

        logger.info(
            "Moss ready: protocols=%s loaded in %.0fms, session index=%s",
            self.config.protocols_index,
            load_ms,
            self.session_index_name,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.unload_index(self.config.protocols_index)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                logger.debug("unload_index failed during shutdown", exc_info=True)

    # ---------------------------------------------------------------- retrieval

    async def lookup_protocol(
        self,
        query: str,
        *,
        categories: Iterable[str] | None = None,
        top_k: int | None = None,
    ) -> list[Retrieved]:
        """Semantic + keyword search over the verified protocol corpus."""
        if self._client is None:
            raise RuntimeError("EmergencyContext.connect() was never awaited")

        filter_: dict[str, Any] | None = None
        cats = [c for c in (categories or []) if c]
        if cats:
            filter_ = {"category": {"$in": cats}}

        options = QueryOptions(
            top_k=top_k or self.config.protocol_top_k,
            # 0.8 leans semantic but keeps keyword recall for exact terms like
            # "tourniquet" or "AED" that a responder will say verbatim.
            alpha=0.8,
            filter=filter_,
        )

        with span(
            "moss.query.protocols",
            **{
                "moss.index": self.config.protocols_index,
                "moss.top_k": options.top_k,
                "moss.filter.categories": ",".join(cats) if cats else "none",
                "session.id": self.session_id,
            },
        ) as current:
            started = time.perf_counter()
            result = await self._client.query(self.config.protocols_index, query, options)
            wall_ms = (time.perf_counter() - started) * 1000
            hits = _hits(result)
            self._annotate(current, self.config.protocols_index, query, wall_ms, result, hits)

        return hits

    async def recall(self, query: str, *, top_k: int | None = None) -> list[Retrieved]:
        """Semantic recall over what has already happened in this emergency."""
        session = self._require_session()

        options = QueryOptions(top_k=top_k or self.config.state_top_k, alpha=0.7)

        with span(
            "moss.query.session_state",
            **{
                "moss.index": self.session_index_name,
                "moss.top_k": options.top_k,
                "session.id": self.session_id,
            },
        ) as current:
            started = time.perf_counter()
            result = await session.query(query, options)
            wall_ms = (time.perf_counter() - started) * 1000
            hits = _hits(result)
            self._annotate(current, self.session_index_name, query, wall_ms, result, hits)

        return hits

    def _annotate(
        self,
        current: Any,  # noqa: ANN401 - opentelemetry Span
        index: str,
        query: str,
        wall_ms: float,
        result: SearchResult,
        hits: list[Retrieved],
    ) -> None:
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
        self.last_trace = RetrievalTrace(
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
        kind: str,
        text: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Append one fact to the live emergency state.

        Returns the stored record so the caller can echo it to the UI.
        """
        session = self._require_session()
        if kind not in FACT_KINDS:
            kind = "observation"

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

        return {"id": doc_id, "text": indexed_text, **metadata}

    async def timeline(self) -> list[dict[str, str]]:
        """Full ordered history of the incident -- the input to the SOAP note."""
        session = self._require_session()
        with span("moss.read.timeline", **{"session.id": self.session_id}) as current:
            docs = await session.get_docs(
                GetDocumentsOptions(sort_by="seq", ascending=True)
            )
            current.set_attribute("moss.doc_count", len(docs))
        return [
            {"id": d.id, "text": d.text, **(dict(d.metadata or {}))}
            for d in docs
        ]

    async def archive(self) -> str | None:
        """Promote the live session index to Moss Cloud for audit and review."""
        session = self._require_session()
        if session.doc_count == 0:
            return None
        with span("moss.push_index", **{"session.id": self.session_id}) as current:
            result = await session.push_index()
            current.set_attribute("moss.index", result.index_name)
            current.set_attribute("moss.doc_count", result.doc_count)
            current.set_attribute("moss.job_id", result.job_id)
        logger.info(
            "Archived %d facts to Moss index %s (job %s)",
            result.doc_count,
            result.index_name,
            result.job_id,
        )
        return result.job_id

    def _require_session(self) -> SessionIndex:
        if self._session is None:
            raise RuntimeError("EmergencyContext.connect() was never awaited")
        return self._session


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
