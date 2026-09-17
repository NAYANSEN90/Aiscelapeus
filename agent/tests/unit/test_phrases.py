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
