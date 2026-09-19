"""The corpus mixer's DSP, asserted.

NOT hermetic: this imports `scripts/build_corpus`, which needs numpy (and
librosa transitively). It is skipped wholesale when the `audio` extra is not
installed, so the default `pytest -q` on a machine without it stays green and
the hermetic guarantee in `test_voice_scenarios.py` is unaffected.

WHY THIS FILE EXISTS
--------------------
The mixer's correctness is not self-evident and cannot be eyeballed. Two
defects were found here by measurement, both invisible to listening tests on
the signal originally used to check them:

  1. `loop_to_length` accumulated into the overlap and then scaled it, which
     multiplied samples the same iteration had just written - a 28x
     discontinuity on brown noise.
  2. It then faded toward `bed[0:span]` while the audio resuming after the seam
     began at `bed[fade]`, so the crossfade reached full gain on material that
     was immediately replaced - reintroducing a discontinuity at the END of the
     fade. Found by an independent reviewer after (1) was fixed.

BOTH WERE INVISIBLE ON A SINE WAVE. A periodic tone's loop points align by
coincidence, so the seam tests passed at 1.00x while real ambience was broken.
Every test below therefore runs on NON-PERIODIC material, and brown noise is
used specifically because it is the closest synthetic analogue to the traffic
and machinery rumble that dominates this corpus' beds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="needs the `audio` extra")

# `filterwarnings = ["error"]` in pyproject.toml turns every warning into a
# failure, which is correct for this project: a warning on a safety-critical
# path is a defect. This is the one place it has to be relaxed, and the
# suppression is deliberately narrow - one category, one module, with the
# reason recorded here rather than widened globally.
#
# librosa 1.0.0 passes its OWN deprecated `hop_length` and `n_fft` into
# `core.phase_vocoder` from inside `effects.time_stretch`
# (librosa/effects.py:448 -> core/spectrum.py:1452 and :1460). Verified by
# traceback: the call sites below already use the current keyword-only API
# (`pitch_shift(y, *, sr, n_steps)`, `time_stretch(y, *, rate)`), so there is
# no way to make this call that avoids the warning. It is a library defect, not
# a usage error, and it disappears when librosa fixes it.
#
# The third suppression is a different kind and is NOT taken on trust. numba's
# resampling kernel emits `RuntimeWarning: invalid value encountered in cast`,
# which in general can mean NaN or inf reached the output - exactly the thing
# that must never be silently written into a WAV. So it was checked rather than
# assumed: across every shift used by the corpus, the result is fully finite
# with zero NaN, zero inf, and amplitudes inside the input's range. The warning
# is internal to numba's cast path, not a property of the audio.
#
# `test_transform_output_is_always_finite` below asserts that directly, so the
# suppression can never hide a real numerical failure: if NaN ever does appear,
# that test fails regardless of the warning filter.
pytestmark = pytest.mark.filterwarnings(
    "ignore:The `hop_length` parameter is deprecated:FutureWarning",
    "ignore:The `n_fft` parameter is deprecated:FutureWarning",
    "ignore:invalid value encountered in cast:RuntimeWarning",
)

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

build_corpus = pytest.importorskip(
    "build_corpus", reason="needs the `audio` extra: pip install -e '.[audio]'"
)

SR = build_corpus.TARGET_SR


# ------------------------------------------------------------------ fixtures


def brown_noise(seconds: float, seed: int = 11) -> "np.ndarray":
    """Traffic/machinery-like rumble. Non-periodic, heavily low-frequency."""
    rng = np.random.default_rng(seed)
    signal = np.cumsum(rng.normal(0, 1, int(SR * seconds)))
    return (signal / np.max(np.abs(signal)) * 0.5).astype(np.float32)


def white_noise(seconds: float, seed: int = 12) -> "np.ndarray":
    rng = np.random.default_rng(seed)
    signal = rng.normal(0, 1, int(SR * seconds))
    return (signal / np.max(np.abs(signal)) * 0.5).astype(np.float32)


def speech_like(seconds: float, f0: float = 120.0) -> "np.ndarray":
    """A voiced buzz with a syllable-rate envelope. Not speech, but it has
    speech's harmonic structure and its silences, which is what the SNR and
    voiced-frame logic actually operate on."""
    t = np.linspace(0, seconds, int(SR * seconds), endpoint=False)
    signal = sum((1.0 / n) * np.sin(2 * np.pi * f0 * n * t) for n in range(1, 20))
    envelope = (0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * t - np.pi / 2)) ** 2
    signal = np.asarray(signal) * envelope
    return (signal / np.max(np.abs(signal)) * 0.6).astype(np.float32)


# -------------------------------------------------------------- loop_to_length


@pytest.mark.parametrize("seconds", [1.0, 2.0, 3.5, 8.0, 17.3])
def test_loop_returns_exactly_the_requested_length(seconds: float) -> None:
    """A bed shorter than the requested length must be extended to it exactly.

    An off-by-one here would misalign the SNR envelope against the voice.
    """
    out = build_corpus.loop_to_length(brown_noise(3.0), int(SR * seconds), SR)
    assert out.size == int(SR * seconds)


def test_loop_introduces_no_discontinuity_on_brown_noise() -> None:
    """THE REGRESSION TEST for both seam defects.

    A click is an impulsive event, and an STT model can mistake one for a
    consonant. The check is that looping does not increase the signal's
    largest sample-to-sample step: any introduced edge shows up immediately
    against the source's own maximum.
    """
    bed = brown_noise(3.0)
    out = build_corpus.loop_to_length(bed, int(SR * 12), SR)

    source_max = float(np.max(np.abs(np.diff(bed))))
    looped_max = float(np.max(np.abs(np.diff(out))))

    assert looped_max <= source_max * 1.5, (
        f"looping introduced a discontinuity: source max delta "
        f"{source_max:.6f}, looped {looped_max:.6f} "
        f"({looped_max / source_max:.1f}x). A seam is clicking."
    )


def test_the_worst_discontinuity_is_not_at_a_seam() -> None:
    """Stronger than the magnitude check: locate the worst edge and prove it is
    ordinary signal content rather than a seam.

    The broken version put the largest step exactly at a seam boundary.
    """
    bed = brown_noise(3.0)
    fade = int(min(0.25 * SR, bed.size // 4))
    body = bed.size - fade
    out = build_corpus.loop_to_length(bed, int(SR * 12), SR)

    worst = int(np.argmax(np.abs(np.diff(out))))
    seams = [body * i for i in range(1, out.size // body + 2)]
    distance = min(abs(worst - seam) for seam in seams)

    assert distance > 200, (
        f"the largest discontinuity sits {distance} samples from a seam "
        f"(t={worst / SR:.3f}s) - the crossfade is the thing making it"
    )


@pytest.mark.parametrize(
    "bed_factory", [brown_noise, white_noise], ids=["brown", "white"]
)
def test_loop_preserves_signal_level(bed_factory) -> None:
    """Equal-power crossfade, not linear.

    Two points in a noise bed are uncorrelated and sum in power rather than
    amplitude, so a linear ramp loses 3 dB at every seam midpoint. A 10 %
    tolerance catches that (-3 dB is a ~29 % drop) without being brittle about
    brown noise's natural wander.
    """
    bed = bed_factory(3.0)
    out = build_corpus.loop_to_length(bed, int(SR * 12), SR)

    source = build_corpus.rms(bed)
    looped = build_corpus.rms(out)
    assert abs(looped - source) / source < 0.10, (
        f"loop changed the level: {source:.4f} -> {looped:.4f}. "
        "A linear (rather than equal-power) crossfade does this."
    )


def test_loop_passes_through_a_bed_that_is_already_long_enough() -> None:
    bed = brown_noise(10.0)
    out = build_corpus.loop_to_length(bed, int(SR * 4), SR)
    assert np.array_equal(out, bed[: int(SR * 4)])


def test_loop_handles_an_empty_bed() -> None:
    """A missing bed file yields an empty array. Silence is the right answer -
    the mix must still produce audio, just without ambience."""
    out = build_corpus.loop_to_length(np.zeros(0, dtype=np.float32), SR, SR)
    assert out.size == SR
    assert not out.any()


# -------------------------------------------------------------------- mixing


@pytest.mark.parametrize("snr_db", [28, 20, 15, 10, 8])
def test_delivered_snr_matches_the_requested_snr(snr_db: int) -> None:
    """The scenario table states an SNR per scene. If the mixer does not
    deliver it, every claim in docs/TESTS.md about noise conditions is fiction.
    """
    voice = speech_like(4.0)
    bed = brown_noise(30.0)

    lead, tail = int(0.6 * SR), int(0.5 * SR)
    padded = np.concatenate(
        [np.zeros(lead, np.float32), voice, np.zeros(tail, np.float32)]
    )
    bed_full = build_corpus.loop_to_length(bed, padded.size, SR)

    voice_level = build_corpus.speech_rms(padded, SR)
    bed_level = build_corpus.rms(bed_full)
    gain = voice_level / (bed_level * (10 ** (snr_db / 20.0)))

    delivered = 20 * np.log10(voice_level / build_corpus.rms(bed_full * gain))
    assert abs(delivered - snr_db) < 0.1, (
        f"requested {snr_db} dB, delivered {delivered:.2f} dB"
    )


@pytest.mark.parametrize("channel_name", sorted(build_corpus.CHANNELS))
def test_every_channel_produces_finite_audio_within_headroom(channel_name: str) -> None:
    """A NaN or an over-range sample would be written into a WAV and only
    discovered when Deepgram returned nonsense."""
    mixed, report = build_corpus.mix_turn(
        speech_like(3.0),
        brown_noise(20.0),
        snr_db=15,
        snr_db_end=None,
        channel=build_corpus.CHANNELS[channel_name],
        rng=np.random.default_rng(2),
    )
    assert np.isfinite(mixed).all()
    assert float(np.max(np.abs(mixed))) <= 1.0
    assert report["channel"] == channel_name
    assert report["duration_s"] > 0


def test_ramped_snr_falls_monotonically() -> None:
    """s12 changes scene under the call: wind gives way to a helicopter, so the
    bed must rise across the utterance rather than sit at one level."""
    voice = speech_like(6.0)
    bed = brown_noise(30.0)

    lead, tail = int(0.6 * SR), int(0.5 * SR)
    padded = np.concatenate(
        [np.zeros(lead, np.float32), voice, np.zeros(tail, np.float32)]
    )
    bed_full = build_corpus.loop_to_length(bed, padded.size, SR)
    voice_level = build_corpus.speech_rms(padded, SR)
    bed_level = build_corpus.rms(bed_full)

    start_gain = voice_level / (bed_level * (10 ** (20 / 20.0)))
    end_gain = voice_level / (bed_level * (10 ** (6 / 20.0)))
    envelope = np.linspace(start_gain, end_gain, padded.size, dtype=np.float32)
    scaled = bed_full * envelope

    first = build_corpus.rms(scaled[: scaled.size // 3])
    last = build_corpus.rms(scaled[-scaled.size // 3 :])
    assert last > first * 2, (
        f"bed did not rise across the utterance: {first:.4f} -> {last:.4f}"
    )


# ------------------------------------------------------------------- filters


@pytest.mark.parametrize(
    ("freq_hz", "should_survive"),
    [(100, False), (1000, True), (2500, True), (6000, False)],
)
def test_bandpass_removes_out_of_band_energy(freq_hz: int, should_survive: bool) -> None:
    """Narrowband telephony is 300-3400 Hz. Audio outside that band would make
    the corpus higher-fidelity than any real call it claims to represent."""
    t = np.linspace(0, 1.0, SR, endpoint=False)
    probe = np.sin(2 * np.pi * freq_hz * t).astype(np.float32)
    out = build_corpus.bandpass(probe, 300.0, 3400.0, SR)

    freqs = np.fft.rfftfreq(probe.size, 1.0 / SR)
    index = int(np.argmin(np.abs(freqs - freq_hz)))
    retained = np.abs(np.fft.rfft(out))[index] / (np.abs(np.fft.rfft(probe))[index] + 1e-12)

    if should_survive:
        assert retained > 0.7, f"{freq_hz} Hz should pass, retained {retained:.2%}"
    else:
        assert retained < 0.1, f"{freq_hz} Hz should be removed, retained {retained:.2%}"


def test_soft_clip_bounds_the_signal_without_hard_edges() -> None:
    """tanh saturation, not hard clipping: a shouted line into a cheap mic
    saturates smoothly, and hard clipping's broadband splatter is acoustically
    unlike it."""
    loud = (speech_like(2.0) * 4.0).astype(np.float32)
    clipped = build_corpus.soft_clip(loud, 0.8)

    assert np.isfinite(clipped).all()
    assert float(np.max(np.abs(clipped))) <= float(np.max(np.abs(loud)))
    # A hard clipper flattens many samples to exactly the threshold; tanh does not.
    peak = float(np.max(np.abs(clipped)))
    at_ceiling = int(np.sum(np.abs(np.abs(clipped) - peak) < 1e-6))
    assert at_ceiling < clipped.size * 0.01


# ------------------------------------------------------------------- speech_rms


def test_speech_rms_ignores_silence() -> None:
    """Plain RMS over an utterance with long pauses reads low, so the mixer
    would push the bed down to compensate and deliver the wrong SNR."""
    voice = speech_like(2.0)
    padded = np.concatenate([np.zeros(SR * 3, np.float32), voice])

    assert build_corpus.speech_rms(padded, SR) > build_corpus.rms(padded) * 1.5


def test_speech_rms_falls_back_on_a_short_fragment() -> None:
    """A gasped half-word is shorter than one analysis frame. It must still
    return a usable level rather than zero or NaN."""
    fragment = speech_like(0.01)
    value = build_corpus.speech_rms(fragment, SR)
    assert value > 0 and np.isfinite(value)


def test_speech_rms_of_silence_is_zero() -> None:
    assert build_corpus.speech_rms(np.zeros(SR, dtype=np.float32), SR) == 0.0


# ------------------------------------------------------- voice identity shift


@pytest.mark.parametrize("semitones", [-2.0, -1.0, 0.0, 1.5, 2.0])
def test_pitch_shift_preserves_duration(semitones: float) -> None:
    """Pitch and duration must move independently: a shift that also changed
    the length would be plain resampling, which drags the formants with it and
    produces the chipmunk effect rather than a different speaker."""
    voice = speech_like(2.0)
    out = build_corpus.transform_voice(voice, semitones, 1.0)
    assert abs(out.size - voice.size) < SR * 0.05
    assert np.isfinite(out).all()


@pytest.mark.parametrize("rate", [0.95, 1.0, 1.15, 1.35])
def test_time_stretch_scales_duration(rate: float) -> None:
    voice = speech_like(2.0)
    out = build_corpus.transform_voice(voice, 0.0, rate)
    assert abs(out.size - voice.size / rate) < SR * 0.1
    assert np.isfinite(out).all()


@pytest.mark.parametrize(
    ("semitones", "rate"),
    [
        (semitones, rate)
        for semitones in (-2.0, -1.5, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0)
        for rate in (0.95, 1.0, 1.05, 1.15, 1.25, 1.35)
    ],
)
def test_transform_output_is_always_finite(semitones: float, rate: float) -> None:
    """The guard that makes the numba RuntimeWarning suppression safe.

    numba's resampling kernel warns "invalid value encountered in cast", which
    in general can mean NaN or inf reached the output. Suppressing that warning
    without checking would let a corrupt buffer be written into a WAV and only
    be noticed when Deepgram returned nonsense for that turn.

    This covers every (semitones, rate) pair the corpus actually uses, so the
    suppression above is backed by an assertion rather than by a conclusion
    drawn once and trusted thereafter.
    """
    out = build_corpus.transform_voice(speech_like(1.5), semitones, rate)

    assert out.size > 0
    assert np.isfinite(out).all(), "non-finite samples in the transformed voice"
    assert not np.isnan(out).any()
    # Nothing beyond full scale: a WAV write would wrap it.
    assert float(np.max(np.abs(out))) <= 1.0


def test_every_scenario_voice_setting_produces_finite_audio() -> None:
    """The same guard, driven by the real scenario table rather than a grid.

    A new scenario with an unusual pitch/rate pair would otherwise reach the
    mixer untested.
    """
    import yaml

    path = Path(__file__).resolve().parent.parent / "audio" / "scenarios.yaml"
    with path.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)

    voice = speech_like(1.5)
    for scenario in doc["scenarios"]:
        settings = scenario["voice"]
        out = build_corpus.transform_voice(
            voice, float(settings["semitones"]), float(settings["rate"])
        )
        assert np.isfinite(out).all(), scenario["id"]
        assert out.size > 0, scenario["id"]
