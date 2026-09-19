"""The CC0 gate in `fetch_beds.py`, asserted.

Every ambient bed committed to this repository must be CC0. That is what makes
the corpus redistributable without an attribution obligation travelling with
it, and it is a claim the repository makes in `beds/LICENSES.md`.

A claim like that has to be enforced by something that runs. The gate had a
real hole, found by an independent reviewer: a result whose `license` field was
empty or absent skipped the check entirely and was accepted as though verified,
because the condition was `if result.get("license", "") and CC0 not in ...`.
Falsy licence, no check. The guarantee then rested wholly on Freesound's
server-side filter.

These tests pin the corrected behaviour. They are pure-function tests over
`_acceptable` - no network, no key - so they run in the default suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_beds  # noqa: E402


def result(**overrides: Any) -> dict[str, Any]:
    """A plausible Freesound search result, CC0 and acceptable by default."""
    base: dict[str, Any] = {
        "id": 12345,
        "name": "city traffic ambience loop",
        "license": "http://creativecommons.org/publicdomain/zero/1.0/ Creative Commons 0",
        "duration": 45.0,
        "tags": ["traffic", "road", "cars", "ambience"],
        "username": "someone",
        "url": "https://freesound.org/s/12345/",
    }
    base.update(overrides)
    return base


SPEC = fetch_beds.BedSpec(
    key="roadside_rain_night",
    query="road traffic rain night",
    require_any=("traffic", "road", "rain", "cars"),
)


# ------------------------------------------------------------- the CC0 gate


def test_a_cc0_result_is_accepted() -> None:
    assert fetch_beds._acceptable(result(), SPEC) is None


@pytest.mark.parametrize(
    "licence",
    [
        "http://creativecommons.org/licenses/by/3.0/ Attribution",
        "http://creativecommons.org/licenses/by-nc/3.0/ Attribution Noncommercial",
        "Sampling+",
    ],
)
def test_a_non_cc0_licence_is_refused(licence: str) -> None:
    """Attribution licences are the common case on Freesound and are exactly
    what must not end up committed here."""
    reason = fetch_beds._acceptable(result(license=licence), SPEC)
    assert reason is not None and "not CC0" in reason


@pytest.mark.parametrize("licence", ["", None])
def test_a_missing_licence_is_refused(licence: str | None) -> None:
    """THE REGRESSION TEST.

    Unknown provenance is precisely what this gate exists for. Treating a
    falsy licence as "nothing to check" inverted the gate's purpose: the one
    result whose licence cannot be confirmed was the one waved through.
    """
    reason = fetch_beds._acceptable(result(license=licence), SPEC)
    assert reason is not None, "a result with no licence field was accepted"
    assert "licence" in reason.lower()


def test_a_result_with_no_licence_key_at_all_is_refused() -> None:
    """Not merely empty - absent. A partial field response must not pass."""
    payload = result()
    del payload["license"]
    reason = fetch_beds._acceptable(payload, SPEC)
    assert reason is not None
    assert "licence" in reason.lower()


# ------------------------------------------------------ the other guard rails


def test_a_bed_shorter_than_the_minimum_is_refused() -> None:
    """A short bed has to loop so often that the repetition becomes a periodic
    artefact an STT model can latch onto in a way real ambience never allows."""
    reason = fetch_beds._acceptable(
        result(duration=fetch_beds.MIN_DURATION_S - 1), SPEC
    )
    assert reason is not None and "shorter" in reason


def test_an_over_long_bed_is_refused() -> None:
    reason = fetch_beds._acceptable(
        result(duration=fetch_beds.MAX_DURATION_S + 1), SPEC
    )
    assert reason is not None and "longer" in reason


def test_music_is_refused_even_when_it_matches_the_query() -> None:
    """Freesound's relevance ranking returns music and foley for these queries
    often enough that taking the top hit produced a guitar loop for one bed."""
    reason = fetch_beds._acceptable(
        result(name="rainy day guitar music loop", tags=["rain", "guitar", "music"]),
        SPEC,
    )
    assert reason is not None and "excluded" in reason


def test_a_result_matching_none_of_the_required_words_is_refused() -> None:
    reason = fetch_beds._acceptable(
        result(name="birdsong in a forest", tags=["birds", "forest", "nature"]),
        SPEC,
    )
    assert reason is not None and "none of" in reason


def test_required_words_are_matched_case_insensitively() -> None:
    assert fetch_beds._acceptable(
        result(name="Heavy TRAFFIC on a wet road", tags=["Ambience"]), SPEC
    ) is None


# ------------------------------------------------------------ the bed roster


def test_every_scenario_bed_has_a_spec() -> None:
    """A scenario naming a bed nobody can fetch would silently mix against
    silence, and the scene described in docs/TESTS.md would be fiction."""
    import yaml

    path = Path(__file__).resolve().parent.parent / "audio" / "scenarios.yaml"
    with path.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)

    known = {spec.key for spec in fetch_beds.BED_SPECS}
    for scenario in doc["scenarios"]:
        bed = scenario["scene"]["bed"]
        assert bed in known, (
            f"{scenario['id']} wants bed {bed!r}, which fetch_beds.py cannot fetch"
        )


def test_bed_spec_keys_are_unique() -> None:
    keys = [spec.key for spec in fetch_beds.BED_SPECS]
    assert len(keys) == len(set(keys))


def test_every_bed_spec_documents_what_it_is_for() -> None:
    """The note becomes a row in beds/LICENSES.md; an empty one leaves a bed
    in the repository with no recorded purpose."""
    for spec in fetch_beds.BED_SPECS:
        assert spec.note.strip(), spec.key
