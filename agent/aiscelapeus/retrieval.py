"""What a retrieval call *is*, as one frozen domain type.

Today the shape of a retrieval is scattered: `moss_context.lookup_protocol`
builds its own `QueryOptions` with `alpha=0.8` written inline, `recall` builds
another with `0.7`, each rebuilds the category filter dict, and the latency
class travels as a defaulted keyword that a caller can forget to think about.
Three copies of the same rule, each able to drift on its own.

`RetrievalRequest` is that rule in one place, and it carries the invariant the
vendor will not:

    The Moss SDK accepts `QueryOptions(alpha=1.5)` without complaint.

There is no validation anywhere below this type, and a silently out-of-range
alpha does not raise - it quietly returns worse hits, which on this system means
the wrong protocol read aloud to a responder. So this is the only place the
invariant can live, and it raises rather than clamping: an alpha of 1.5 means
the caller's arithmetic is wrong, and coercing it to 1.0 hides the defect behind
plausible-looking output.

`latency_class` is required for the same reason. `MossPort` defaults it to
`VOICE_TURN`, so a background SOAP query that forgets the argument is typed as
the 10ms synchronous voice path and gets whatever tier enforcement that implies.
Making it a required field means the caller must state which tier the call
belongs to - that is the whole point of having the type.

The *result* types live here too, alongside the request. They were in
`moss_context` and that placement forced a cycle: `ports` needs
`RetrievalResult` to declare `MossPort`, so `ports` imported `moss_context`,
which meant `moss_context` could never import `ports` or this module - i.e. the
one module that performs retrievals could not name the type that describes one.
`Retrieved`, `RetrievalTrace` and `RetrievalResult` are pure domain values with
no vendor dependency, so they belong on the domain side of the seam with the
request. The graph is now `moss_context -> retrieval -> ports`, acyclic, and
`moss_context` re-exports all three so existing importers are unaffected.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .config import MossConfig
from .ports import LatencyClass, TierViolation

__all__ = [
    "PROTOCOL_ALPHA",
    "SESSION_ALPHA",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalTrace",
    "Retrieved",
    "require_tier",
]

# Hybrid-search weighting: 1.0 is fully dense/semantic, 0.0 fully keyword.
#
# 0.8 is the MEASURED best of the three values tested, and it is the value the
# code already had. See docs/evidence/2026-09-17-retrieval-alpha-measurement.md:
#
#     alpha    top-1        critical top-1
#     0.7      12/20 (60%)  4/8
#     0.8      14/20 (70%)  5/8      <- best
#     1.0      13/20 (65%)  5/8
#
# This CONTRADICTS docs/BUILD-PLAN.md section 8.2 item 1, which directed
# 0.8 -> 1.0 on the strength of a claimed 52% -> 64% gain. Re-measured on a
# committed, colloquially-phrased query set (tests/data/retrieval_queries.yaml)
# against real Moss, the change is a 5-point REGRESSION and degrades a
# life-critical query (airway-choking-adult falls from rank 2 to rank 4).
# Reproduced independently, twice, with identical figures.
#
# The keyword component earns its weight: the hypothesis behind 0.8 - that
# verbatim terms like "tourniquet" or "AED" need keyword recall - is supported,
# not refuted. Do not raise these to 1.0 without a new measurement that
# supersedes that artifact.
PROTOCOL_ALPHA = 0.8

# UNMEASURED, and deliberately flagged as such. The measurement above covers the
# protocol corpus only. Session recall queries a per-incident index of facts the
# responder just dictated, which is a different retrieval problem: tiny corpus,
# high verbatim overlap ("the tourniquet", "his glucose"), no topical clustering.
# 0.8 is set to match PROTOCOL_ALPHA so there is one story about hybrid
# weighting rather than two unexplained numbers - it is an inference from the
# protocol result, not evidence about session state. The previous value was 0.7,
# also unmeasured. Measuring it needs a live incident index, which arrives with
# B3.
SESSION_ALPHA = 0.8


def require_tier(store: str, latency_class: LatencyClass, *, in_process: bool) -> None:
    """The Class-0 rule, in the one place that defines it.

    `ports.py`'s module docstring has always claimed this rule is "enforced by
    the port that receives it, at the call site". Until now it was not: the only
    `TierViolation` in the repo was raised by a test double
    (`testing/fakes.py`), so the mechanism the docstring described existed
    exclusively in the fake that was supposed to be standing in for it. A
    guarantee whose only implementation is a test double is a guarantee about
    the tests, not about the system.

    `in_process` is the adapter's own answer to "can I serve this without a
    network hop right now", not a static property of the store. That
    distinction is the whole point: the protocol index *becomes* in-process
    partway through startup, when `load_index` completes. A Class-0 query
    issued before that resolves over the network - the exact defect the rule
    names - and a check keyed on the store's name rather than its current
    state would wave it through. `EmergencyContext.recall` passes
    `in_process=True` unconditionally because `MossClient.session` is an
    in-memory index by construction; when B3 puts a network-backed tier behind
    the same port, it passes `False` and this raises without anything else
    changing.

    Raises on VOICE_TURN only. PREFETCH and above are permitted to reach a
    slower store by design - that is what the tiers are for - so widening this
    to any non-zero class would break the prefetch path it exists to protect.
    """
    if latency_class is LatencyClass.VOICE_TURN and not in_process:
        raise TierViolation(store, latency_class)


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


@dataclass(frozen=True)
class RetrievalTrace:
    """What a retrieval cost. Surfaced to the UI so latency is visible live."""

    index: str
    query: str
    wall_ms: float
    moss_ms: int | None
    hits: int
    within_budget: bool


@dataclass(frozen=True)
class RetrievalResult:
    """Hits plus the measurement of the call that produced them.

    The trace used to be stashed on the context as a single shared slot and
    read back by the caller after an await. LiveKit runs one task per tool
    call, so when the model asked for a protocol lookup and a state recall in
    the same turn, one task could publish the other's latency under its own
    query string - self-consistent, plausible, and wrong. Returning the
    measurement with its own result makes that misattribution unrepresentable.
    """

    hits: list[Retrieved]
    trace: RetrievalTrace

    def __iter__(self) -> Iterator[Retrieved]:
        """Kept iterable so existing `for hit in result` call sites still read naturally."""
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __bool__(self) -> bool:
        return bool(self.hits)


@dataclass(frozen=True)
class RetrievalRequest:
    """One retrieval call, validated at construction.

    Frozen and hashable: a request can be a cache key, logged, or replayed, and
    nothing downstream can mutate the alpha it was validated with. `categories`
    is a tuple rather than a list for exactly that reason - a list field would
    make the instance unhashable and let a caller append after validation - and
    `__post_init__` coerces it to one rather than trusting the annotation, so
    the guarantee holds against an untyped caller too.

    Equality covers every field, `latency_class` included. Two otherwise
    identical requests on different tiers are different requests: if they
    compared equal, a BACKGROUND result could be served from the prefetch cache
    to the 10ms voice path, and no `TierViolation` would ever fire because the
    cache hit *is* the fast path.
    """

    query: str
    top_k: int
    alpha: float
    latency_class: LatencyClass
    categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        problems: list[str] = []

        # Type-checked before use, not assumed. `query` is the one field sourced
        # straight from a live STT event, where a missing transcript arrives as
        # None; `self.query.strip()` on None raises AttributeError, which is not
        # the loggable ValueError the rest of this type promises.
        if not isinstance(self.query, str):
            problems.append(
                f"query must be a str (got {type(self.query).__name__}: {self.query!r})"
            )
        elif not self.query.strip():
            problems.append(f"query must not be blank (got {self.query!r})")

        if not isinstance(self.top_k, int) or isinstance(self.top_k, bool):
            problems.append(
                f"top_k must be an int (got {type(self.top_k).__name__}: {self.top_k!r})"
            )
        elif self.top_k < 1:
            problems.append(f"top_k must be at least 1 (got {self.top_k})")

        # `categories` is normalised, not merely checked, because the annotation
        # alone does not make the field a tuple, and three defects come in
        # through that gap:
        #
        #   - A list is accepted, so the instance is unhashable - it cannot be
        #     the prefetch cache key this type exists to be - and the caller can
        #     append after validation, changing the filter that gets sent.
        #   - A generator is consumed by the blank check and stored exhausted,
        #     producing `{"$in": []}`: a *valid* filter matching no document.
        #     Same silent zero-hit failure as `{"$in": [""]}`, and it triggers
        #     only on well-formed input - the worst detection profile there is.
        #   - A bare `str` is itself an iterable, so `tuple("cardiac")` becomes
        #     ('c','a','r','d','i','a','c') and filters on seven letters.
        #
        # mypy catches all three, and it runs in CI. That is not sufficient
        # here: this type's whole purpose is to be the last gate in front of a
        # vendor that validates nothing, and `lookup_protocol` already types
        # this parameter as the looser `Iterable[str]` (moss_context.py:183),
        # so the value can arrive correctly-typed and still be a generator.
        if isinstance(self.categories, str):
            problems.append(
                "categories must be a tuple of category names, not a bare str "
                f"(got {self.categories!r}, which would filter on its letters)"
            )
            materialised: tuple[Any, ...] = ()
        else:
            # Materialised once, before inspection, so a generator is examined
            # and stored rather than consumed and discarded.
            materialised = tuple(self.categories)

        # A blank category is the same defect class as a blank query: it is
        # never what a caller meant, and it does not fail loudly downstream.
        # `{"$in": [""]}` is a *valid* filter that matches no document, so the
        # store returns zero hits and the agent answers with no protocol under
        # it. `lookup_protocol` currently drops blanks silently
        # (moss_context.py:191); dropping is the wrong half of the choice - it
        # makes a caller bug look like an unfiltered search. Raise instead, so
        # the emptiness rule has one definition and it is enforced here.
        blanks = [c for c in materialised if not (isinstance(c, str) and c.strip())]
        if blanks:
            problems.append(f"categories must not contain blank entries (got {blanks!r})")

        # Duplicates are dropped order-preservingly rather than raised on.
        # `$in` has set semantics, so ("cardiac","airway") and
        # ("airway","cardiac") are the *same query* to Moss - yet as raw tuples
        # they are different cache keys, so one prefetch would never serve the
        # other. Dedup makes equal queries equal keys; order is preserved
        # rather than sorted so the filter stays readable in a trace, which is
        # all determinism requires.
        if not problems:
            object.__setattr__(self, "categories", tuple(dict.fromkeys(materialised)))

        if not isinstance(self.alpha, (int, float)) or isinstance(self.alpha, bool):
            problems.append(
                f"alpha must be a float (got {type(self.alpha).__name__}: {self.alpha!r})"
            )
        # NaN next: every comparison against NaN is False, so a bare range check
        # passes it straight through to the SDK. `inf` is caught by the range
        # check itself.
        elif math.isnan(self.alpha):
            problems.append("alpha must be a number in [0.0, 1.0] (got nan)")
        elif not 0.0 <= self.alpha <= 1.0:
            problems.append(f"alpha must be in [0.0, 1.0] (got {self.alpha})")

        if problems:
            raise ValueError("; ".join(problems))

    # ------------------------------------------------------------ constructors

    @classmethod
    def for_protocol(
        cls,
        query: str,
        config: MossConfig,
        *,
        categories: tuple[str, ...] = (),
        top_k: int | None = None,
        latency_class: LatencyClass = LatencyClass.VOICE_TURN,
    ) -> "RetrievalRequest":
        """A search over the verified protocol corpus.

        `top_k` and `alpha` have one definition site each: `config` and
        `PROTOCOL_ALPHA`. A caller may override `top_k` (the re-rank design in
        BUILD-PLAN 8.3 retrieves wide) but not `alpha`, which is a tuned
        property of the corpus rather than of the call.
        """
        return cls(
            query=query,
            top_k=config.protocol_top_k if top_k is None else top_k,
            alpha=PROTOCOL_ALPHA,
            latency_class=latency_class,
            # No `tuple(...)` here: `__post_init__` normalises, so there is one
            # place that decides what `categories` becomes.
            categories=categories,
        )

    @classmethod
    def for_session_state(
        cls,
        query: str,
        config: MossConfig,
        *,
        top_k: int | None = None,
        latency_class: LatencyClass = LatencyClass.VOICE_TURN,
    ) -> "RetrievalRequest":
        """Recall over what has already happened in this incident.

        No `categories`: the session index holds one incident's own facts, and
        there is no category taxonomy over them to filter by. Offering the
        parameter would imply a filter that the store cannot honour.
        """
        return cls(
            query=query,
            top_k=config.state_top_k if top_k is None else top_k,
            alpha=SESSION_ALPHA,
            latency_class=latency_class,
        )

    # ------------------------------------------------------------ vendor shape

    def moss_filter(self) -> dict[str, Any] | None:
        """The Moss metadata filter, or None when nothing is being filtered.

        One place knows the filter's shape. Both call sites previously built
        `{"category": {"$in": [...]}}` by hand, so a change to the operator or
        the key was a change in two files that nothing checked agreed.

        None rather than `{}`: an empty filter dict is a different request to
        the SDK than no filter at all, and "filter by nothing" is not what a
        caller passing no categories means.

        This can test emptiness with a bare `if` because `__post_init__` has
        already rejected blank entries - so "no categories" is the only way to
        get here with nothing to filter by, and there is one definition of it.
        """
        if not self.categories:
            return None
        return {"category": {"$in": list(self.categories)}}
