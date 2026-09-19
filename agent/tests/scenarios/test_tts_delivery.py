"""The ElevenLabs delivery planner, asserted.

`synthesize_voice.plan_delivery` decides how each turn is performed: which
audio tags lead the utterance, how stable the voice is, how fast it reads. It
is derived from the scenario table rather than hand-written per turn, so a
defect in it silently mis-performs many turns at once - and the corpus would
then test the wrong thing while looking complete.

Hermetic: `synthesize_voice` imports only the standard library plus pyyaml, so
these run in the default suite with no key and no network.

TWO DEFECTS THIS FILE PINS
--------------------------
Both were found by reading the planner's output over the real scenario table
rather than by testing it in the abstract:

  1. Keying tags off `criticality_peak` (the scenario MAXIMUM) delivered s12's
     relaxed opening line - a twisted ankle, Level 1 - as urgently as the
     cardiac arrest it becomes at Level 5, flattening the deterioration arc
     that scenario exists to test.

  2. `"panic" in direction` matched the direction "Concerned, controlled. Not
     panicking." and put `[panicked]` on the negation scenario's composed
     caller. The same substring-ignores-the-preceding-word bug as F-001 in
     `phrases.py`, in new code.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import synthesize_voice as tts  # noqa: E402

SCENARIOS_PATH = Path(__file__).resolve().parent.parent / "audio" / "scenarios.yaml"
with SCENARIOS_PATH.open(encoding="utf-8") as _handle:
    DOC: dict[str, Any] = yaml.safe_load(_handle)

SCENARIOS: list[dict[str, Any]] = DOC["scenarios"]

# Everything the probe could plausibly return. Using the full set makes these
# tests exercise the SELECTION logic; what the probe actually verifies is a
# separate question, handled at generation time.
ALL_TAGS = frozenset(
    tts.DOCUMENTED_TAGS + tts.BLOG_TAGS + tts.UNVERIFIED_TAGS
)


def by_id(scenario_id: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == scenario_id)


def turn_of(scenario: dict[str, Any], n: int) -> dict[str, Any]:
    return next(t for t in scenario["turns"] if t["n"] == n)


def plan(scenario_id: str, n: int, tags: frozenset[str] = ALL_TAGS) -> tts.Delivery:
    scenario = by_id(scenario_id)
    return tts.plan_delivery(scenario, turn_of(scenario, n), tags)


# ------------------------------------------------------------- voice roster


def test_every_scenario_has_a_tts_voice() -> None:
    """A scenario with no voice cannot be synthesised, and the script would
    report it as a failure after the others had already been paid for."""
    for scenario in SCENARIOS:
        block = scenario.get("tts")
        assert block, f"{scenario['id']} has no tts block"
        assert block.get("voice_id"), scenario["id"]
        assert block.get("voice_name"), scenario["id"]


def test_every_scenario_uses_a_different_voice() -> None:
    """Twelve callers should be twelve people. A duplicated voice id would make
    two scenarios sound like the same caller, which is the thing distinct
    voices were chosen to avoid."""
    ids = [s["tts"]["voice_id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids)), "a voice id is used twice"


def test_pitch_shift_is_disabled_now_voices_are_distinct() -> None:
    """`voice.semitones` existed to fake variety from ONE human reader. With a
    different ElevenLabs voice per scenario it is redundant, and stacking a
    formant shift on already-synthetic audio adds artifacts that would muddy
    attribution when a transcription fails - was it the noise, or the
    processing?"""
    for scenario in SCENARIOS:
        assert scenario["voice"]["semitones"] == 0.0, (
            f"{scenario['id']} still pitch-shifts; remove it or drop the "
            "distinct-voice assignment, not both"
        )


# ------------------------------------------------------ the deterioration arc


def test_delivery_follows_the_turns_own_level_not_the_scenario_peak() -> None:
    """REGRESSION TEST for defect 1.

    s12 opens with a twisted ankle at Level 1 and ends in cardiac arrest at
    Level 5. Keying off `criticality_peak` delivered every line, including the
    relaxed opening, at arrest intensity.
    """
    opening = plan("s12_trail_deterioration", 1)
    arrest = plan("s12_trail_deterioration", 3)

    assert opening.stability > arrest.stability, (
        "the calm opening must be delivered more steadily than the arrest; "
        f"got {opening.stability} vs {arrest.stability}"
    )
    assert not opening.tags, (
        f"the Level 1 opening should carry no emotion tag, got {opening.tags}"
    )


def test_stability_falls_as_a_scenario_deteriorates() -> None:
    """The arc has to be monotonic where the level is: a caller does not become
    calmer as their friend arrests."""
    scenario = by_id("s12_trail_deterioration")
    stabilities = [
        tts.plan_delivery(scenario, turn, ALL_TAGS).stability
        for turn in scenario["turns"]
    ]
    for earlier, later in zip(stabilities, stabilities[1:]):
        assert later <= earlier, f"stability rose mid-deterioration: {stabilities}"


# ----------------------------------------------------------- composed callers


def test_a_caller_described_as_not_panicking_is_not_performed_as_panicked() -> None:
    """REGRESSION TEST for defect 2.

    s11's direction reads "Concerned, controlled. Not panicking." A substring
    check for "panic" matched it and applied `[panicked]` - to the one scenario
    whose entire purpose is that a calm caller's negated phrasing must not be
    treated as an emergency.
    """
    for turn in by_id("s11_platform_negation")["turns"]:
        delivery = plan("s11_platform_negation", turn["n"])
        assert "panicked" not in delivery.tags, (
            f"s11 t{turn['n']} performed as panicked; its caller is composed"
        )
        assert "crying" not in delivery.tags, f"s11 t{turn['n']}"


def test_a_loud_scene_still_raises_the_voice_of_a_composed_caller() -> None:
    """Shouting is about the ENVIRONMENT, not the emotion. s11 is a railway
    platform at 9 dB SNR: the caller is composed and still has to be heard over
    a tannoy. Suppressing `[shouts]` along with the emotion tags would make the
    scene inaudible in the delivery."""
    delivery = plan("s11_platform_negation", 1)
    assert "shouts" in delivery.tags


def test_the_minor_scenario_carries_no_emotion_tags_at_all() -> None:
    """s01 is a cut thumb in a quiet kitchen. An agent that treats this as an
    emergency is useless in the field, and a corpus that PERFORMS it as one
    cannot test that."""
    for turn in by_id("s01_kitchen_cut")["turns"]:
        assert not plan("s01_kitchen_cut", turn["n"]).tags


# ----------------------------------------------------------------- tag safety


@pytest.mark.parametrize("allowed", sorted(ALL_TAGS))
def test_no_tag_is_used_that_the_probe_has_not_verified(allowed: str) -> None:
    """THE GUARD THAT MAKES THE PROBE MEANINGFUL.

    ElevenLabs documents some tags, mentions others only in a blog post, and
    says nothing about `[panicked]` or `[breathless]`. An unsupported tag may
    be READ ALOUD, which would put "open square bracket panicked" into the
    transcript - a corpus defect that looks exactly like an STT failure.

    Parametrised over EVERY tag individually, each run permitting exactly one.
    An earlier version passed only `{"shouts"}`, which exercised a single
    branch of `plan_delivery`: every other `wanted.append` is guarded by
    `tag in usable_tags`, so the assertion would have held just as well against
    a planner with all those branches deleted. One tag at a time forces each
    guard to be exercised on its own.
    """
    permitted = frozenset({allowed})
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            delivery = tts.plan_delivery(scenario, turn, permitted)
            assert set(delivery.tags) <= permitted, (
                f"{scenario['id']} t{turn['n']} emitted {delivery.tags} when "
                f"only {allowed!r} was verified"
            )


def test_each_tag_is_reachable_by_some_turn() -> None:
    """The mirror of the test above, and it is what stops that one being
    vacuous in the other direction.

    A planner that emitted NOTHING would satisfy every "no unverified tag"
    assertion perfectly. So at least some tags must actually be selected when
    permitted - otherwise the probe, the tag lists and the delivery directions
    are all decoration.
    """
    reachable = {
        tag
        for scenario in SCENARIOS
        for turn in scenario["turns"]
        for tag in tts.plan_delivery(scenario, turn, ALL_TAGS).tags
    }
    assert len(reachable) >= 4, (
        f"only {sorted(reachable)} are ever selected; the delivery planner is "
        "barely doing anything"
    )


def test_no_tags_at_all_when_the_probe_found_nothing() -> None:
    """The conservative fallback. With no probe report the script uses no tags
    rather than sending unverified ones and hoping - delivery then comes from
    the scripted wording and the speed setting alone."""
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            assert not tts.plan_delivery(scenario, turn, frozenset()).tags


INTELLIGIBILITY_COST = ("whispers", "crying")


def test_no_intelligibility_reducing_tag_on_a_turn_whose_markers_must_fire() -> None:
    """The corpus has to be able to attribute its own failures.

    `[whispers]` and `[crying]` both reduce articulation. If Deepgram dropped
    "unresponsive" because the line was whispered, the manifest would record it
    as an STT-robustness finding at that scene's SNR - blaming ambient noise for
    something the DELIVERY caused. So an expressive tag is never allowed to
    compete with a marker assertion.

    Turns carrying only `expect_no_markers` are exempt: a quieter delivery
    cannot invent a marker, so there is nothing to confound.
    """
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            if not turn.get("expect_markers"):
                continue
            tags = tts.plan_delivery(scenario, turn, ALL_TAGS).tags
            clashing = sorted(set(tags) & set(INTELLIGIBILITY_COST))
            assert not clashing, (
                f"{scenario['id']} t{turn['n']} must fire "
                f"{turn['expect_markers']} but is delivered with {clashing}"
            )


def test_expressive_tags_are_still_used_where_nothing_must_fire() -> None:
    """The rule above must not quietly disable expressiveness everywhere - that
    would trade one silent failure for another. At least one turn should still
    receive an intelligibility-costing tag."""
    used = {
        tag
        for scenario in SCENARIOS
        for turn in scenario["turns"]
        for tag in tts.plan_delivery(scenario, turn, ALL_TAGS).tags
        if tag in INTELLIGIBILITY_COST
    }
    assert used, "no turn uses an expressive tag; the suppression is too broad"


def test_at_most_two_tags_per_utterance() -> None:
    """Tags affect roughly the next 4-5 words. Three or more on one line fight
    each other and the delivery becomes mush."""
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            delivery = tts.plan_delivery(scenario, turn, ALL_TAGS)
            assert len(delivery.tags) <= 2, f"{scenario['id']} t{turn['n']}"


def test_tags_are_never_repeated_within_one_utterance() -> None:
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            tags = tts.plan_delivery(scenario, turn, ALL_TAGS).tags
            assert len(tags) == len(set(tags))


# ------------------------------------------------------------------ rendering


def test_apply_prefixes_tags_before_the_text() -> None:
    delivery = tts.Delivery(tags=("shouts", "breathless"), stability=0.3, speed=1.0)
    assert delivery.apply("He's not breathing") == (
        "[shouts] [breathless] He's not breathing"
    )


def test_apply_leaves_text_untouched_when_there_are_no_tags() -> None:
    """The line must survive verbatim: it is what the marker oracle asserts
    against, and an injected character would change what Deepgram hears."""
    delivery = tts.Delivery(tags=(), stability=0.5, speed=1.0)
    assert delivery.apply("He's not breathing") == "He's not breathing"


def test_the_spoken_text_always_contains_the_scripted_line() -> None:
    """Whatever the planner prepends, the words being tested must still be
    there in full."""
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            spoken = tts.plan_delivery(scenario, turn, ALL_TAGS).apply(
                " ".join(turn["text"].split())
            )
            assert " ".join(turn["text"].split()) in spoken


# -------------------------------------------------------------------- bounds


def test_speed_stays_within_the_natural_range() -> None:
    """Beyond about +/-20 % the reading stops sounding like a person in a hurry
    and starts sounding like a tape played wrong."""
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            speed = tts.plan_delivery(scenario, turn, ALL_TAGS).speed
            assert 0.85 <= speed <= 1.2, f"{scenario['id']} t{turn['n']}: {speed}"


def test_stability_stays_within_the_usable_range() -> None:
    """ElevenLabs treats stability as 0-1. Below ~0.2 v3 becomes erratic enough
    to change the words; above ~0.7 it stops responding to audio tags at all,
    which would silently defeat the reason for using v3."""
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            stability = tts.plan_delivery(scenario, turn, ALL_TAGS).stability
            assert 0.2 <= stability <= 0.7, f"{scenario['id']} t{turn['n']}"


# ------------------------------------------------------------------ composure


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        # Genuinely settled.
        ("Calm, faintly embarrassed. Normal pace.", tts.Composure.COMPOSED),
        ("Matter-of-fact. Looking at your own thumb.", tts.Composure.COMPOSED),
        ("Reporting, not panicking. Slight emphasis.", tts.Composure.COMPOSED),
        # Working at calm and not getting there. Each of these contains a
        # composure word, which is precisely why the boolean version failed.
        ("Trying to be the calm one. Slightly clipped.", tts.Composure.STRAINED),
        ("Trying to stay calm but her voice shakes.", tts.Composure.STRAINED),
        ("Slightly calmer because someone is helping.", tts.Composure.STRAINED),
        ("Reassuring yourself as much as the agent.", tts.Composure.STRAINED),
        ("You believe you are giving reassuring news.", tts.Composure.STRAINED),
        # Everything else.
        ("Full panic. Words running together.", tts.Composure.DISTRESSED),
        ("Begging. Voice completely broken.", tts.Composure.DISTRESSED),
        ("", tts.Composure.DISTRESSED),
    ],
)
def test_composure_classification(direction: str, expected: tts.Composure) -> None:
    assert tts.read_composure(direction) is expected


def test_reassuring_content_does_not_imply_a_reassured_speaker() -> None:
    """s09 t2 is the most important line in the corpus: the caller reports that
    her husband IS breathing, believing it is good news, while he is in cardiac
    arrest with agonal gasps.

    Its direction says "reassuring". That describes what she is SAYING. She is
    terrified. The boolean read the word and delivered the line serenely.
    """
    scenario = by_id("s09_bedroom_agonal")
    turn = turn_of(scenario, 2)
    assert tts.read_composure(turn["direction"]) is not tts.Composure.COMPOSED


def test_a_strained_caller_is_never_steadier_than_a_composed_one() -> None:
    """Held-together is not calm. If the strained bonus reached the composed
    one, a Level-5 turn could end up delivered more steadily than a Level-3
    turn - which is what happened."""
    strained: list[float] = []
    composed: list[float] = []
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            state = tts.read_composure(turn.get("direction") or "")
            value = tts.plan_delivery(scenario, turn, ALL_TAGS).stability
            if state is tts.Composure.STRAINED:
                strained.append(value)
            elif state is tts.Composure.COMPOSED:
                composed.append(value)

    assert strained and composed, "corpus should contain both states"
    assert max(strained) < max(composed)


def test_a_critical_turn_always_carries_some_urgency() -> None:
    """A Level-5 turn delivered flat, with no tag at all, is the failure the
    composure boolean actually caused - three of them, including the agonal
    line. Whatever the direction says, an arrest does not sound casual."""
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            if turn["triage"]["level"] < 5:
                continue
            delivery = tts.plan_delivery(scenario, turn, ALL_TAGS)
            assert delivery.tags, (
                f"{scenario['id']} t{turn['n']} is Level 5 and would be "
                f"delivered with no emotional colour at all"
            )


# ------------------------------------------------------------- audio validation


def test_empty_audio_is_rejected() -> None:
    """An HTTP 200 with an empty body would otherwise become a 0-frame WAV.
    `build_corpus.read_mono` loads that as an empty array and `rms` returns 0.0
    without raising, so the turn mixes as pure ambience - and the manifest
    records a transcription failure at that scene's SNR, blaming the noise for
    a line that was never synthesised."""
    with pytest.raises(tts.SynthError, match="empty body"):
        tts.validate_pcm(b"", "He is not breathing")


def test_truncated_audio_is_rejected() -> None:
    with pytest.raises(tts.SynthError, match="below the"):
        tts.validate_pcm(b"\x00\x10" * 100, "He is not breathing")


def test_digital_silence_is_rejected_even_at_a_plausible_length() -> None:
    """The same failure wearing a convincing duration."""
    with pytest.raises(tts.SynthError, match="near-silence"):
        tts.validate_pcm(b"\x00\x00" * (tts.SAMPLE_RATE * 2), "He is not breathing")


def test_plausible_audio_is_accepted() -> None:
    import math

    pcm = bytearray()
    for i in range(tts.SAMPLE_RATE * 2):
        value = int(8000 * math.sin(2 * math.pi * 140 * i / tts.SAMPLE_RATE))
        pcm += int(value).to_bytes(2, "little", signed=True)
    tts.validate_pcm(bytes(pcm), "He is not breathing")


def test_the_model_supports_audio_tags() -> None:
    """Audio tags need v3. `eleven_turbo_v2_5` - what this project's old config
    used - ignores them silently, so every delivery direction would go
    nowhere and the corpus would be uniformly calm."""
    assert tts.MODEL == "eleven_v3"


def test_output_format_matches_what_the_mixer_expects() -> None:
    """16 kHz mono PCM: what build_corpus wants, so nothing is resampled, and
    what a phone channel delivers anyway."""
    assert tts.OUTPUT_FORMAT == f"pcm_{tts.SAMPLE_RATE}"
    assert tts.SAMPLE_RATE == 16_000
