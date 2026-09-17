"""The retrieval request type: the last place an out-of-range query can be caught.

The Moss SDK accepts `QueryOptions(alpha=1.5)` silently - no exception, no
warning, just worse hits. Nothing below this type validates, so every assertion
in the first section is load-bearing: if construction stops raising, a bad alpha
reaches the store and the failure surfaces as the wrong protocol being read to a
responder, which is indistinguishable from a retrieval-quality problem.

Assertions here observe the constructed object or the raised error, never a
"did it validate" flag, following test_triage.py: a flag can be right about a
state that is wrong.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from aiscelapeus.config import MossConfig
from aiscelapeus.ports import LatencyClass
from aiscelapeus.retrieval import PROTOCOL_ALPHA, SESSION_ALPHA, RetrievalRequest


@pytest.fixture
def config() -> MossConfig:
    # Deliberately not the dataclass defaults (3 and 5): a test asserting a
    # classmethod "reads config" passes against a hardcoded 3 if config says 3.
    return MossConfig(
        project_id="test-project",
        project_key="test-key",
        protocol_top_k=7,
        state_top_k=9,
    )


def _request(**overrides: object) -> RetrievalRequest:
    """A valid request, so each test perturbs exactly one field."""
    kwargs: dict = {
        "query": "casualty is bleeding heavily from the leg",
        "top_k": 3,
        "alpha": 1.0,
        "latency_class": LatencyClass.VOICE_TURN,
    }
    kwargs.update(overrides)
    return RetrievalRequest(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------- alpha invariant


@pytest.mark.parametrize("alpha", [-0.1, -1.0, 1.01, 1.5, 2.0, math.inf, -math.inf])
def test_alpha_outside_the_unit_interval_raises(alpha: float) -> None:
    # falsifier: alpha arithmetic upstream produces 1.5, the SDK accepts it
    # without complaint, and retrieval quality silently degrades - the wrong
    # protocol is read aloud and reads as a corpus problem, not a caller bug.
    with pytest.raises(ValueError) as excinfo:
        _request(alpha=alpha)
    assert str(alpha) in str(excinfo.value), (
        "the message must name the offending value, or the caller cannot find it"
    )


def test_nan_alpha_raises() -> None:
    # falsifier: the range check is written `not 0.0 <= alpha <= 1.0`, every
    # comparison against NaN is False, so NaN passes validation and reaches the
    # SDK as a weighting with no defined meaning.
    with pytest.raises(ValueError):
        _request(alpha=math.nan)


@pytest.mark.parametrize("alpha", [0.0, 1.0, 0, 1])
def test_alpha_boundaries_are_accepted(alpha: float) -> None:
    # falsifier: validation uses a strict comparison, so the tuned production
    # value alpha=1.0 is itself rejected and every retrieval raises. The int
    # cases guard the other direction: a type check narrowed to `float` alone
    # would reject `alpha=1`, which is the same weighting written differently.
    assert _request(alpha=alpha).alpha == alpha


def test_out_of_range_alpha_is_never_clamped() -> None:
    # falsifier: a "helpful" clamp turns 1.5 into 1.0, so a malfunctioning
    # caller produces plausible output forever and nothing is investigated.
    # (CLAUDE.md: a raise over a silent clamp.)
    with pytest.raises(ValueError):
        _request(alpha=1.5)


# ---------------------------------------------------------------- top_k invariant


@pytest.mark.parametrize("top_k", [0, -1, -10])
def test_non_positive_top_k_raises(top_k: int) -> None:
    # falsifier: top_k=0 is accepted, the store returns zero hits, and the agent
    # answers from the model alone with no protocol underneath it - an uncited
    # instruction that looks exactly like a cited one.
    with pytest.raises(ValueError) as excinfo:
        _request(top_k=top_k)
    assert str(top_k) in str(excinfo.value)


def test_boolean_top_k_raises() -> None:
    # falsifier: bool subclasses int, so `top_k=True` satisfies both isinstance
    # and `>= 1` and is stored as True - a flag threaded into a count argument
    # reaches the store as a silent top-1 query.
    with pytest.raises(ValueError) as excinfo:
        _request(top_k=True)
    assert "bool" in str(excinfo.value), "the message must name what arrived"


def test_top_k_of_one_is_accepted() -> None:
    # falsifier: the bound is written `top_k <= 1`, making the legitimate
    # single-hit lookup impossible.
    assert _request(top_k=1).top_k == 1


# ---------------------------------------------------------------- query invariant


@pytest.mark.parametrize("query", ["", "   ", "\t\n", " "])
def test_blank_query_raises(query: str) -> None:
    # falsifier: an empty STT result or a whitespace-only transcript is sent to
    # the store, which returns arbitrary nearest neighbours to nothing; the agent
    # then cites a protocol that has no relationship to what was said.
    with pytest.raises(ValueError):
        _request(query=query)


def test_non_string_query_raises_valueerror_not_attributeerror() -> None:
    # falsifier: validation is written `self.query.strip()`, so a missing STT
    # transcript arriving as None raises AttributeError from inside the
    # constructor - an unhandled crash instead of the loggable ValueError this
    # type promises, with no message naming the offending value.
    with pytest.raises(ValueError) as excinfo:
        _request(query=None)
    assert "NoneType" in str(excinfo.value), (
        "the message must say what arrived, or the caller cannot find the source"
    )
    with pytest.raises(ValueError):
        _request(query=b"not breathing")


def test_query_with_surrounding_whitespace_is_kept_verbatim() -> None:
    # falsifier: validation "helpfully" strips the query as a side effect, so the
    # text sent to the store differs from the text logged in the trace and a
    # retrieval cannot be reproduced from the record.
    assert _request(query="  not breathing  ").query == "  not breathing  "


# -------------------------------------------------------------- illegal states


def test_latency_class_has_no_default() -> None:
    # falsifier: latency_class gains a default, so a background SOAP query that
    # forgets the argument is typed as the 10ms synchronous voice path and tier
    # enforcement guards a class the call does not belong to.
    fields = {f.name: f for f in dataclasses.fields(RetrievalRequest)}
    assert fields["latency_class"].default is dataclasses.MISSING
    assert fields["latency_class"].default_factory is dataclasses.MISSING
    with pytest.raises(TypeError):
        RetrievalRequest(query="chest pain", top_k=3, alpha=1.0)  # type: ignore[call-arg]


def test_request_is_frozen() -> None:
    # falsifier: a request is mutable, so code between validation and the store
    # can set alpha=1.5 and the __post_init__ check guards nothing.
    request = _request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.alpha = 1.5  # type: ignore[misc]
    assert request.alpha == 1.0


def test_request_is_hashable() -> None:
    # falsifier: categories is a list, the instance is unhashable, and the
    # request cannot be used as a prefetch cache key - which is the Class-1
    # design in BUILD-PLAN 8.3.
    assert hash(_request(categories=("cardiac",))) == hash(
        _request(categories=("cardiac",))
    )


def test_requests_differing_only_in_tier_are_not_equal() -> None:
    # falsifier: latency_class is excluded from equality (e.g. someone marks it
    # `field(compare=False)`, reasoning that the tier is metadata rather than
    # part of the query). Then a BACKGROUND result is served from the prefetch
    # cache to the 10ms VOICE_TURN path, and no TierViolation can fire because
    # the cache hit *is* the fast path - there is no network hop to detect.
    voice = _request(latency_class=LatencyClass.VOICE_TURN)
    background = _request(latency_class=LatencyClass.BACKGROUND)

    assert voice != background
    assert hash(voice) != hash(background)
    assert len({voice, background}) == 2, "two tiers must be two cache keys"


def test_identical_requests_are_equal_and_share_a_key() -> None:
    # falsifier: equality is identity-based (eq=False, or a field holding an
    # unhashable/generator value), so two identical requests never share a
    # cache entry and the prefetch cache has a permanent 0% hit rate.
    assert _request(categories=("cardiac",)) == _request(categories=("cardiac",))
    assert len({_request(), _request()}) == 1


def test_a_list_of_categories_is_coerced_not_trusted() -> None:
    # falsifier: the annotation says tuple but nothing enforces it, so a list
    # is stored as-is: the request is unhashable (TypeError at the cache
    # insertion, far from the construction site) and the caller can append
    # after validation, changing the filter that actually reaches Moss.
    mutable = ["cardiac"]
    request = _request(categories=mutable)
    mutable.append("injected")

    assert request.categories == ("cardiac",)
    assert isinstance(request.categories, tuple)
    assert hash(request), "a request that cannot be hashed cannot be a cache key"
    assert request.moss_filter() == {"category": {"$in": ["cardiac"]}}


def test_a_generator_of_categories_is_materialised_before_inspection() -> None:
    # falsifier: the blank check consumes the iterable and the exhausted
    # generator is stored, so moss_filter() emits {"$in": []} - a *valid*
    # filter matching zero documents. Same silent zero-hit failure as
    # {"$in": [""]}, but it triggers only on well-formed input.
    request = _request(categories=(c for c in ("cardiac", "airway")))
    assert request.categories == ("cardiac", "airway")
    assert request.moss_filter() == {"category": {"$in": ["cardiac", "airway"]}}


def test_a_bare_string_of_categories_raises() -> None:
    # falsifier: a bare str is itself an iterable of str, so `tuple("cardiac")`
    # silently becomes ('c','a','r','d','i','a','c') and the lookup filters on
    # seven single letters, matching nothing.
    with pytest.raises(ValueError) as excinfo:
        _request(categories="cardiac")
    assert "bare str" in str(excinfo.value)


def test_duplicate_categories_collapse_to_one_cache_key() -> None:
    # falsifier: duplicates are passed through, so `$in` (set semantics) gets
    # the same category twice and - worse - two tuples describing the identical
    # Moss query are different cache keys, so one prefetch never serves the
    # other.
    request = _request(categories=("cardiac", "airway", "cardiac"))
    assert request.categories == ("cardiac", "airway"), "first occurrence wins"
    assert request.moss_filter() == {"category": {"$in": ["cardiac", "airway"]}}


def test_categories_from_a_list_become_a_tuple(config: MossConfig) -> None:
    # falsifier: the classmethod stores the caller's list by reference, so the
    # caller can append a category after validation and the filter sent to Moss
    # differs from the one the request was built with.
    mutable = ["cardiac", "airway"]
    request = RetrievalRequest.for_protocol("chest pain", config, categories=mutable)
    mutable.append("haemorrhage")

    assert isinstance(request.categories, tuple)
    assert request.categories == ("cardiac", "airway")
    assert request.moss_filter() == {"category": {"$in": ["cardiac", "airway"]}}


# ------------------------------------------------------------ tuned alpha values


def test_protocol_alpha_is_the_measured_value() -> None:
    # falsifier: someone raises alpha to 1.0 on the strength of BUILD-PLAN
    # 8.2 item 1, which directs exactly that. Re-measurement against real Moss
    # on a committed query set showed the opposite: 0.8 scores 14/20 top-1 and
    # 1.0 scores 13/20, and 1.0 drops the life-critical airway-choking-adult
    # query from rank 2 to rank 4. See
    # docs/evidence/2026-09-17-retrieval-alpha-measurement.md. This test is the
    # only thing standing between that artifact and a plausible-sounding
    # revert; nothing else in the suite would notice.
    assert PROTOCOL_ALPHA == 0.8
    assert SESSION_ALPHA == 0.8


def test_constructors_use_the_tuned_alphas(config: MossConfig) -> None:
    # falsifier: the constants exist but a classmethod hardcodes its own alpha,
    # so the constant is documentation rather than the source of truth.
    assert RetrievalRequest.for_protocol("burn", config).alpha == PROTOCOL_ALPHA
    assert RetrievalRequest.for_session_state("burn", config).alpha == SESSION_ALPHA


# ---------------------------------------------------------------- constructors


def test_for_protocol_reads_top_k_from_config(config: MossConfig) -> None:
    # falsifier: the constructor hardcodes top_k, so MOSS_PROTOCOL_TOP_K is an
    # env var that changes nothing - the config knob silently does not work.
    request = RetrievalRequest.for_protocol("severe burn to the forearm", config)
    assert request.top_k == config.protocol_top_k == 7
    assert request.latency_class is LatencyClass.VOICE_TURN
    assert request.categories == ()
    assert request.moss_filter() is None


def test_for_session_state_reads_its_own_top_k_from_config(config: MossConfig) -> None:
    # falsifier: both constructors read protocol_top_k, so recall silently
    # returns 7 hits where the session design asks for 9 and MOSS_STATE_TOP_K
    # does nothing.
    request = RetrievalRequest.for_session_state("what did we give already", config)
    assert request.top_k == config.state_top_k == 9


def test_explicit_top_k_overrides_config(config: MossConfig) -> None:
    # falsifier: the override is implemented as `top_k or config.protocol_top_k`
    # and the explicit value is discarded - so the retrieve-wide re-rank path in
    # BUILD-PLAN 8.3 silently keeps querying at the narrow default.
    assert RetrievalRequest.for_protocol("chest pain", config, top_k=8).top_k == 8
    assert (
        RetrievalRequest.for_session_state("chest pain", config, top_k=2).top_k == 2
    )


def test_explicit_latency_class_is_carried_through(config: MossConfig) -> None:
    # falsifier: the constructor pins VOICE_TURN regardless of the argument, so
    # a prefetch or background call is indistinguishable from a voice-path one
    # and in-process-only enforcement cannot be applied where it belongs.
    request = RetrievalRequest.for_protocol(
        "chest pain", config, latency_class=LatencyClass.BACKGROUND
    )
    assert request.latency_class is LatencyClass.BACKGROUND


def test_constructors_validate_too(config: MossConfig) -> None:
    # falsifier: the classmethods bypass __post_init__ (e.g. by building the
    # instance through object.__new__ or a dict), so the invariant only holds on
    # the path nobody uses.
    with pytest.raises(ValueError):
        RetrievalRequest.for_protocol("   ", config)
    with pytest.raises(ValueError):
        RetrievalRequest.for_session_state("chest pain", config, top_k=0)


# ----------------------------------------------------------------- moss_filter


@pytest.mark.parametrize(
    "categories",
    [("",), ("   ",), ("cardiac", ""), ("", "airway")],
)
def test_blank_category_raises(categories: tuple[str, ...]) -> None:
    # falsifier: a blank category is passed through, producing the *valid*
    # filter {"category": {"$in": [""]}} which matches no document - so the
    # store returns zero hits and the agent answers with no protocol under it.
    # This fails silently: the SDK does not object and the result looks like a
    # corpus gap rather than a caller bug. The existing lookup_protocol instead
    # drops blanks (moss_context.py:191), which makes the same caller bug look
    # like a deliberately unfiltered search. Either way nobody is told.
    with pytest.raises(ValueError) as excinfo:
        _request(categories=categories)
    assert "categories" in str(excinfo.value), (
        "the message must name the field, not just fail"
    )


def test_blank_category_is_rejected_through_the_constructor_too(
    config: MossConfig,
) -> None:
    # falsifier: the emptiness rule is enforced only on the direct constructor,
    # so the classmethod every real caller uses lets blanks through and there
    # are two definitions of "no categories" again.
    with pytest.raises(ValueError):
        RetrievalRequest.for_protocol("chest pain", config, categories=("cardiac", ""))


def test_moss_filter_is_none_with_no_categories() -> None:
    # falsifier: an empty filter `{}` is sent instead of None. "Filter by
    # nothing" is a different request to the store than "do not filter", and it
    # is not what a caller passing no categories asked for.
    assert _request(categories=()).moss_filter() is None


def test_moss_filter_wraps_a_single_category() -> None:
    # falsifier: a single category is passed as a bare string rather than a
    # list, so the $in operator matches nothing and the filtered lookup returns
    # zero hits instead of the cardiac protocols.
    assert _request(categories=("cardiac",)).moss_filter() == {
        "category": {"$in": ["cardiac"]}
    }


def test_moss_filter_preserves_order_of_several_categories() -> None:
    # falsifier: the filter is built from a set, so the dict differs run to run
    # and a golden-fixture or cache-key comparison of the same request fails
    # nondeterministically.
    categories = ("cardiac", "airway", "haemorrhage")
    assert _request(categories=categories).moss_filter() == {
        "category": {"$in": ["cardiac", "airway", "haemorrhage"]}
    }


def test_moss_filter_returns_a_fresh_list_each_call() -> None:
    # falsifier: the method hands out the internal sequence, so a caller that
    # mutates the returned filter changes every later request built from the
    # same frozen instance.
    request = _request(categories=("cardiac",))
    first = request.moss_filter()
    assert first is not None
    first["category"]["$in"].append("injected")
    assert request.moss_filter() == {"category": {"$in": ["cardiac"]}}
