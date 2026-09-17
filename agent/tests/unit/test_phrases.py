"""The deterministic escalation rule, judged against the utterance corpus.

This is the system's central safety claim: that a phrase indicating a
life threat forces Level 5 regardless of what the model concluded. The rule is
only as good as the phrasings it recognises, so the corpus in
tests/data/utterances.yaml is the specification and this file just drives it.

Every case carries its own `why` in the corpus. Failures name the utterance,
so a red run reads as a list of things a responder could say that the system
would not hear.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aiscelapeus.phrases import find_markers, hard_escalation_triggered, normalize_for_match

CORPUS_PATH = Path(__file__).parent.parent / "data" / "utterances.yaml"


def _corpus() -> list[dict]:
    with CORPUS_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


CORPUS = _corpus()
ESCALATING = [case for case in CORPUS if case["expect"] is not None]
NON_ESCALATING = [case for case in CORPUS if case["expect"] is None]
MULTI_MARKER = [case for case in CORPUS if case.get("expect_also")]


def _id(case: dict) -> str:
    return case["text"][:60]


@pytest.mark.parametrize("case", ESCALATING, ids=_id)
def test_life_threat_is_recognised(case: dict) -> None:
    # falsifier: a responder says this and the rule returns None, so the
    # deterministic net does not fire and escalation depends entirely on the model.
    hit = hard_escalation_triggered(case["text"])
    assert hit is not None, (
        f"no marker fired for {case['text']!r}\n"
        f"this phrasing must escalate because: {case['why'].strip()}"
    )
    assert hit.marker_id == case["expect"], (
        f"{case['text']!r} matched {hit.marker_id!r}, expected {case['expect']!r}"
    )


@pytest.mark.parametrize("case", NON_ESCALATING, ids=_id)
def test_non_threat_does_not_escalate(case: dict) -> None:
    # falsifier: a responder says this and the rule fires, pinning the incident
    # at Critical irreversibly - the ratchet never comes back down.
    hit = hard_escalation_triggered(case["text"])
    assert hit is None, (
        f"{case['text']!r} wrongly matched {hit.marker_id if hit else None!r}\n"
        f"this phrasing must not escalate because: {case['why'].strip()}"
    )


@pytest.mark.parametrize("case", MULTI_MARKER, ids=_id)
def test_every_co_reported_finding_is_recognised(case: dict) -> None:
    # falsifier: a responder reports two life threats in one breath and only
    # one reaches the record, so the second finding - which may be the one that
    # dictates the protocol - is never signalled and nobody can tell, because
    # the surviving marker still produces a correct-looking criticality level.
    fired = {hit.marker_id for hit in find_markers(case["text"])}
    expected = set(case["expect_also"])
    assert expected <= fired, (
        f"{case['text']!r} reported {sorted(fired)}, missing "
        f"{sorted(expected - fired)}\n"
        f"all of these must fire because: {case['why'].strip()}"
    )


def test_multi_marker_corpus_entries_exercise_more_than_one_marker() -> None:
    # falsifier: an `expect_also` entry names a single marker, making the
    # co-reporting assertion above a duplicate of `expect` and therefore
    # vacuous - it would pass against a matcher that still drops every second
    # finding, which is the defect the field exists to catch.
    assert MULTI_MARKER, "no utterance in the corpus reports co-occurring findings"
    for case in MULTI_MARKER:
        assert len(set(case["expect_also"])) >= 2, case["text"]
        assert case["expect"] in case["expect_also"], (
            f"{case['text']!r}: `expect` must be one of `expect_also`"
        )


# --------------------------------------------------- clause-scoped suppression


@pytest.mark.parametrize(
    "text",
    [
        "no, he's not breathing",
        "no he's not breathing",
        "no, no, he is not breathing",
        "no - he's not breathing",
        "no. he's not breathing",
    ],
)
def test_a_discourse_no_does_not_disarm_the_net(text: str) -> None:
    # falsifier: a caller answers "is he breathing?" with the single most
    # natural possible phrasing and the deterministic net returns nothing, so
    # escalation on a plain cardiac-arrest report falls entirely to the model -
    # the exact failure this module exists to catch.
    hit = hard_escalation_triggered(text)
    assert hit is not None, text
    assert hit.marker_id == "not_breathing"


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("he's not moving, he's not breathing", "not_breathing"),
        ("no pulse, he is not breathing", "not_breathing"),
        ("he's not breathing, he's got no pulse", "no_pulse"),
        ("he isn't moving and he's not breathing", "not_breathing"),
    ],
)
def test_a_negator_in_a_previous_clause_does_not_suppress(text: str, marker: str) -> None:
    # falsifier: a negator belonging to an earlier clause swallows the finding
    # in the next one, so reporting two life threats in one breath records
    # fewer of them than reporting one - the net penalises the caller for
    # saying more.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert marker in fired, f"{text!r} fired {sorted(fired)}"


@pytest.mark.parametrize(
    "text",
    [
        "he is not unresponsive",
        "the casualty is NOT unresponsive",
        "he's never had a cardiac arrest",
        "there is no cardiac arrest here",
        "he's not really breathing badly",
    ],
)
def test_an_adjacent_negator_still_suppresses(text: str) -> None:
    # falsifier: narrowing the suppression rule broke the negations it was
    # already getting right, so a casualty who is explicitly FINE pins the
    # incident at Critical - and the ratchet refuses to bring it back down, so
    # one false firing is permanent for the rest of the call.
    hit = hard_escalation_triggered(text)
    assert hit is None, f"{text!r} wrongly fired {hit.marker_id if hit else None}"


def test_suppression_does_not_depend_on_distance() -> None:
    # falsifier: the lookback is really still a flat word window, so whether a
    # finding is heard depends on how many words the caller happened to put in
    # front of it rather than on what they meant - which is how a caller who
    # gives more detail gets less safety.
    near = find_markers("no, he's not breathing")
    far = find_markers("no, I checked him properly and he's not breathing")
    assert {hit.marker_id for hit in near} == {hit.marker_id for hit in far}
    assert "not_breathing" in {hit.marker_id for hit in near}


def test_suppression_does_not_depend_on_punctuation() -> None:
    # falsifier: the suppression rule is really about commas, so whether a life
    # threat is heard is decided by whether the STT vendor inserted punctuation
    # into a panicked, unpunctuated sentence - a decision with no clinical
    # content at all. Every pair below is the same utterance; each pair must
    # agree, and the negation must be read the same way in both.
    pairs = [
        ("no, he's not breathing", "no he's not breathing"),
        ("he's not moving, he's not breathing", "he's not moving he's not breathing"),
        ("the casualty is not, unresponsive", "the casualty is not unresponsive"),
    ]
    for punctuated, bare in pairs:
        assert {hit.marker_id for hit in find_markers(punctuated)} == {
            hit.marker_id for hit in find_markers(bare)
        }, f"{punctuated!r} and {bare!r} disagree"


@pytest.mark.parametrize(
    ("text", "marker", "should_fire"),
    [
        # A negator separated from the finding by content words does not govern
        # it, however few words away it is.
        ("no, he's not breathing", "not_breathing", True),
        ("he's not moving, still not breathing", "not_breathing", True),
        ("there's no water, he is not breathing", "not_breathing", True),
        # A negator separated only by function words does govern it.
        ("he is not unresponsive", "unresponsive", False),
        ("he's never had a cardiac arrest", "cardiac_arrest", False),
        ("he has not been breathing properly", "not_breathing", False),
    ],
)
def test_only_a_negator_reaching_across_function_words_suppresses(
    text: str, marker: str, should_fire: bool
) -> None:
    # falsifier: the single rule that decides suppression stops distinguishing
    # a logical negation from a discourse marker, and it fails in whichever
    # direction the change happened to push - either a plain arrest report goes
    # unheard, or a casualty who is explicitly fine pins the incident at
    # Critical for the rest of the call. Both directions are asserted here
    # because a fix for one is the classic way to cause the other.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert (marker in fired) is should_fire, (
        f"{text!r} fired {sorted(fired)}; expected {marker!r} "
        f"{'to fire' if should_fire else 'to be suppressed'}"
    )


# ----------------------------------------- deterioration after a denial (F-005)


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("he is not unresponsive - wait, no, he IS unresponsive", "unresponsive"),
        ("he's not unresponsive. he's unresponsive now.", "unresponsive"),
        (
            "she was not breathing but now she is breathing again, "
            "wait, she's not breathing",
            "not_breathing",
        ),
    ],
)
def test_a_correction_toward_danger_fires(text: str, marker: str) -> None:
    # falsifier: a responder denies a finding and then corrects toward danger -
    # a deterioration report, the most urgent thing they can say - and the
    # denial silently cancels the correction, so the net reports the history
    # instead of the emergency.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert marker in fired, f"{text!r} fired {sorted(fired)}"


def test_the_reported_span_points_at_the_live_finding_not_the_denied_one() -> None:
    # falsifier: the hit's span points at the DENIED occurrence, so an audit
    # reviewing why a Level 5 was forced is shown a phrase the responder was
    # explicitly ruling out - the attribution argues against its own escalation.
    text = "he is not unresponsive - wait, no, he IS unresponsive"
    hit = hard_escalation_triggered(text)
    assert hit is not None
    normalised = normalize_for_match(text)
    start, _ = hit.span
    assert "not" not in normalised[:start].split()[-2:], (
        f"span {hit.span} points at the negated occurrence in {normalised!r}"
    )


def test_a_suppressed_occurrence_alone_still_suppresses_the_marker() -> None:
    # falsifier: accepting any surviving occurrence degenerates into accepting
    # every occurrence, so a negation with no later correction fires anyway and
    # the whole suppression mechanism is dead while looking alive.
    assert find_markers("he is not unresponsive") == ()
    assert find_markers("the casualty is not unresponsive, he is talking") == ()


# ------------------------------------------------------- vocabulary (F-003/F-004)


@pytest.mark.parametrize(
    "text",
    [
        "I can't wake him",
        "I can't wake him up",
        "she can't wake him up",
        "I cannot wake her",
        "we can't wake him up",
    ],
)
def test_cannot_wake_reports_unresponsive(text: str) -> None:
    # falsifier: the first-person phrasing a real caller uses at 3am about the
    # person beside them is not recognised, so the deterministic net stays
    # silent on a witnessed cardiac arrest.
    hit = hard_escalation_triggered(text)
    assert hit is not None, text
    assert hit.marker_id == "unresponsive"


@pytest.mark.parametrize(
    "text",
    [
        "we pulled a little boy out of the pool",
        "we dragged her out of the lake",
        "I got the baby out of the bath",
        "they pulled him out of the canal",
        "we lifted her out of the hot tub",
        "he pulled the child out of the river",
    ],
)
def test_submersion_is_recognised_without_the_word_water(text: str) -> None:
    # falsifier: nobody says "water" about a swimming pool, so a paediatric
    # drowning never signals `drowning` and is handled as a standard adult
    # arrest - which inverts the correct sequence, because hypoxic arrest needs
    # five rescue breaths BEFORE compressions.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert "drowning" in fired, f"{text!r} fired {sorted(fired)}"


@pytest.mark.parametrize(
    "text",
    [
        "he got out of the pool and sat down",
        "she climbed out of the bath",
        "there's no pool here",
    ],
)
def test_leaving_the_water_unaided_is_not_a_submersion(text: str) -> None:
    # falsifier: the widened location vocabulary fires on anyone who merely
    # mentions a pool, so an ordinary poolside collapse is routed to the
    # rescue-breath sequence and compressions are delayed on a patient who
    # needs them.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert "drowning" not in fired, f"{text!r} fired {sorted(fired)}"


# --------------------------------------- contracted recovery narrative (F-002)


@pytest.mark.parametrize(
    "text",
    [
        "he wasn't breathing properly but he is breathing now",
        "he hadn't been breathing but he's talking to me now",
        "she weren't breathing but she's awake now",
    ],
)
def test_a_contracted_past_tense_recovery_is_suppressed(text: str) -> None:
    # falsifier: a recovered faint escalates to Critical because the past tense
    # was spelled as a contraction, and since the ratchet refuses to undo an
    # escalation, one spurious firing bridges a clinician and stays for the
    # rest of the incident.
    hit = hard_escalation_triggered(text)
    assert hit is None, f"{text!r} wrongly fired {hit.marker_id if hit else None}"


@pytest.mark.parametrize(
    "text",
    [
        "he wasn't breathing",
        "he wasn't breathing when I got here",
        "she wasn't breathing, I've started CPR",
    ],
)
def test_a_contracted_past_tense_without_a_recovery_still_fires(text: str) -> None:
    # falsifier: suppressing the contracted past tense went too far and now
    # swallows "he wasn't breathing" outright, which is a live arrest report
    # and the most important sentence the module will ever be given.
    hit = hard_escalation_triggered(text)
    assert hit is not None, text
    assert hit.marker_id == "not_breathing"


# ------------------------------------------- found by independent review


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        (
            "He was talking to me fine a minute ago, now he's not breathing, "
            "but earlier he seemed totally ok",
            "not_breathing",
        ),
        (
            "She was talking to me, now she's not breathing, but she was ok before",
            "not_breathing",
        ),
        ("He was fine five minutes ago but now he's not breathing", "not_breathing"),
    ],
)
def test_narrating_the_timeline_is_not_a_recovery_narrative(
    text: str, marker: str
) -> None:
    # falsifier: a caller reports the arrest and then keeps talking, adding how
    # the patient seemed a minute earlier - which is what frightened people
    # actually do - and a past-tense verb belonging to that BACKGROUND swallows
    # the live finding. The finding is present tense and nothing retracts it.
    # This is the recovery-narrative guard firing on a deterioration report,
    # which is the one direction of error that kills patients.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert marker in fired, f"{text!r} fired {sorted(fired)}"


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("No, her lips are blue", "cyanosis"),
        ("No, his heart has stopped", "cardiac_arrest"),
        ("No, their child isn't breathing", "not_breathing"),
        ("No, her breathing has stopped", "not_breathing"),
    ],
)
def test_a_discourse_no_does_not_reach_across_a_possessive(
    text: str, marker: str
) -> None:
    # falsifier: a possessive determiner is treated as a function word, so a
    # discourse "no" reaches across it into a finding about a DIFFERENT subject
    # and suppresses it - "no, her lips are blue" reports nothing. A possessive
    # heads the subject of a fresh clause exactly as a pronoun does, so a
    # negator that has to cross one is not negating what follows.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert marker in fired, f"{text!r} fired {sorted(fired)}"


@pytest.mark.parametrize(
    "text",
    [
        "pulled the plug out of the bath",
        "got the dog out of the pool",
        "I got the bags out of the pool",
        "he pulled the cover out of the pool",
    ],
)
def test_extracting_an_object_is_not_a_submersion(text: str) -> None:
    # falsifier: the drowning marker fires on anything pulled out of water, so
    # draining a bath or rescuing a pet pages a clinician and requests the
    # paediatric rescue-breath protocol. Over-firing is the safe direction, but
    # the pattern claims the extracted object is the casualty and this is what
    # holds it to that claim.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert "drowning" not in fired, f"{text!r} fired {sorted(fired)}"


def test_a_casualty_is_still_extracted_despite_the_object_exclusion() -> None:
    # falsifier: the exclusion that stops "the plug" also stops "the little
    # boy", so the fix for an over-fire silently reopened the F-004 miss it was
    # layered on top of - and a paediatric drowning is handled as an adult
    # arrest again.
    for text in (
        "we pulled a little boy out of the pool",
        "I got the baby out of the bath",
        "we dragged her out of the lake",
        "they pulled him out of the canal",
        "we pulled the little girl out of the hot tub",
    ):
        fired = {hit.marker_id for hit in find_markers(text)}
        assert "drowning" in fired, f"{text!r} fired {sorted(fired)}"


def test_only_an_adjacent_past_tense_can_suppress_a_finding() -> None:
    # falsifier: the recovery-narrative guard goes back to scanning the whole
    # utterance for a past-tense verb at unbounded distance, which is the flat
    # window F-001 removed - reintroduced in the suppressing direction, where
    # its failures are fatal rather than merely noisy. The two utterances below
    # differ only in WHERE the past tense sits relative to the finding, and
    # they must be classified differently.
    recovery = "he was not breathing but now he is breathing again"
    background = (
        "he was talking to me a minute ago, now he's not breathing, but he was ok"
    )
    assert find_markers(recovery) == (), "an adjacent past tense must suppress"
    assert "not_breathing" in {hit.marker_id for hit in find_markers(background)}, (
        "a past tense in the background narration must not suppress"
    )


def test_curly_and_straight_apostrophes_agree() -> None:
    # falsifier: the two apostrophe forms disagree, so whether a life threat is
    # heard depends on which character the STT vendor happened to emit.
    straight = hard_escalation_triggered("she isn't breathing")
    curly = hard_escalation_triggered("she isn’t breathing")
    assert straight is not None and curly is not None
    assert straight.marker_id == curly.marker_id


def test_matching_is_case_insensitive() -> None:
    # falsifier: an STT capitalisation change silently disables the safety net.
    for text in ("HE IS NOT BREATHING", "He Is Not Breathing", "he is not breathing"):
        assert hard_escalation_triggered(text) is not None, text


def test_normalisation_is_idempotent() -> None:
    # falsifier: normalising twice differs from normalising once, so matching
    # depends on how many times the text has been through the pipeline.
    for case in CORPUS:
        once = normalize_for_match(case["text"])
        assert normalize_for_match(once) == once


def test_marker_reports_where_it_matched() -> None:
    # falsifier: the hit carries no span, so an escalation cannot be explained
    # after the fact - an audit sees a Level 5 with no attributable cause.
    hit = hard_escalation_triggered("the patient is not breathing at all")
    assert hit is not None
    start, end = hit.span
    assert 0 <= start < end <= len("the patient is not breathing at all")
    assert hit.matched_text


def test_all_markers_are_reported_not_just_the_first() -> None:
    # falsifier: only one marker is returned for an utterance describing
    # several findings, so the record understates what the responder reported.
    hits = find_markers("he's unresponsive and not breathing")
    assert {hit.marker_id for hit in hits} == {"unresponsive", "not_breathing"}


def test_empty_and_whitespace_are_safe() -> None:
    # falsifier: an empty transcript raises or matches, so a dropped STT frame
    # either crashes the turn or fabricates an escalation.
    for text in ("", "   ", "\n\t"):
        assert hard_escalation_triggered(text) is None


def test_corpus_covers_every_declared_marker() -> None:
    # falsifier: a marker exists in the implementation with no utterance
    # exercising it, so it could be broken or unreachable and nothing notices.
    from aiscelapeus.phrases import MARKERS

    declared = {marker.marker_id for marker in MARKERS}
    exercised = {case["expect"] for case in ESCALATING}
    assert declared == exercised, (
        f"markers never exercised by the corpus: {sorted(declared - exercised)}\n"
        f"corpus expects markers that do not exist: {sorted(exercised - declared)}"
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("he was not breathing, but now, is he talking?", "not_breathing"),
        ("he was unresponsive, but now is he alert? I cannot say", "unresponsive"),
        (
            "he was not breathing, but now, is he talking? "
            "I don't think he ever will be",
            "not_breathing",
        ),
    ],
)
def test_a_question_is_not_a_retraction(text: str, expected: str) -> None:
    # falsifier: `_contradicted_later` reads a recovery word inside a QUESTION as
    # a recovery narrative, so a responder who reports a life threat and then
    # asks what to check next has the finding silently suppressed. Confirmed by
    # execution before this fix: all three returned no marker at all. The
    # responder is asking, not reporting that the casualty recovered, and a
    # missed escalation has no backstop in this module.
    #
    # The real-time phrasing was never at risk - "he's not breathing, but now
    # what, is he talking?" has no past-tense auxiliary adjacent to the match, so
    # the suppression gate never opens. This needed retrospective framing AND an
    # interrogative together, which is why the earlier sweep of declaratives
    # missed it.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert expected in fired, f"{text!r} reported {sorted(fired)}"


@pytest.mark.parametrize(
    "text",
    [
        "he was not breathing but he is breathing again now",
        "he wasn't breathing properly but he is breathing now",
        "he was unresponsive but he is awake now",
        "she was not responding but she is talking now",
        "he was not breathing but now he is breathing",
    ],
)
def test_a_genuine_recovery_narrative_is_still_suppressed(text: str) -> None:
    # falsifier: the question fix above is written as "any inversion disables
    # suppression" and over-reaches into real recovery narratives, so every
    # "he was not breathing but he is breathing now" starts paging a clinician.
    # Pinned in the same file as its counterpart because a fix for a missed
    # escalation is exactly where a false one gets introduced, and the two must
    # be read together.
    # `find_markers` returns a tuple, so compare emptiness rather than a literal
    # `[]` - an earlier draft of this test asserted `== []` and failed against
    # correct behaviour, which would have invited "fixing" the module instead.
    assert not find_markers(text), f"{text!r} should be suppressed as a recovery"


@pytest.mark.parametrize(
    "text",
    [
        "pulled my dog walker out of the lake",
        "got the dog handler out of the canal",
        "we pulled the boy out of the pool",
        "dragged the catering manager out of the pool",
    ],
)
def test_a_casualty_named_with_an_excluded_word_is_still_a_submersion(
    text: str,
) -> None:
    # falsifier: `_NON_CASUALTY_OBJECTS` matches a PREFIX of a longer word rather
    # than the noun actually extracted, so "dog" inside "dog walker" excludes a
    # real drowning casualty. Confirmed by execution before this fix: "pulled my
    # dog walker out of the lake" reported NOTHING. An exclusion list added to
    # stop an over-fire had opened a missed escalation underneath it - the unsafe
    # direction. A `\b` on each side is NOT sufficient, because the trailing
    # boundary is satisfied by the space inside the compound noun; the exclusion
    # has to be anchored on the full "<object> out of the" tail.
    fired = {hit.marker_id for hit in find_markers(text)}
    assert "drowning" in fired, f"{text!r} reported {sorted(fired)}"


@pytest.mark.parametrize(
    "text",
    [
        "pulled the plug out of the bath",
        "got the dog out of the pool",
        "got the cat out of the pond",
        "pulled the bags out of the water",
        "pulled the phone out of the pool",
    ],
)
def test_extracting_a_non_casualty_is_still_not_a_submersion(text: str) -> None:
    # falsifier: the word-boundary fix above is written loosely enough that the
    # exclusion stops working at all, so draining a bath pages a clinician for a
    # drowning. The pair of tests is the point: the exclusion must be narrow
    # enough to admit "dog walker" and still wide enough to reject "dog".
    fired = {hit.marker_id for hit in find_markers(text)}
    assert "drowning" not in fired, f"{text!r} wrongly reported a drowning"
