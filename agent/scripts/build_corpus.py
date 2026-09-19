"""Build the voice test corpus: transform, mix, and transcribe.

    raw/<scenario>/tNN.wav   (a human reading RECORDING-SCRIPT.md)
        + beds/<bed>.mp3     (CC0 ambience, fetched once by fetch_beds.py)
        -> mixed/<scenario>/tNN.wav
        -> manifest.json     (what Deepgram ACTUALLY heard)

Two stages, deliberately separable:

    python scripts/build_corpus.py               # mix only. Offline, deterministic.
    python scripts/build_corpus.py --transcribe  # mix, then call Deepgram.

Mixing is offline and seeded, so re-running it byte-for-byte reproduces the same
WAVs. Only `--transcribe` touches the network, and the transcripts it captures
are committed, which is what keeps the default pytest run hermetic.

WHY THE TRANSCRIPT IS THE ORACLE
--------------------------------
The manifest records what `nova-3-medical` returned, not the line the reader was
given. Those differ, and the difference is the entire point:

  - Deepgram's `smart_format` emits typographic apostrophes, so "isn't
    breathing" arrives as "isn’t breathing". `phrases.py` normalises for exactly
    this, and asserting against the real transcript is what proves it.
  - Ambient noise at 8 dB SNR genuinely corrupts words. When it corrupts a
    safety-critical phrase, that is a finding about STT robustness, recorded
    with its SNR - not something to tune away by raising the SNR until the suite
    goes green.

MISSING RECORDINGS ARE NOT AN ERROR
-----------------------------------
A turn with no raw WAV is skipped and reported. The corpus is built
incrementally while someone is still recording, so a partial run has to be
useful rather than fatal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import yaml

AGENT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = AGENT_ROOT.parent
AUDIO_DIR = AGENT_ROOT / "tests" / "audio"
SCENARIOS = AUDIO_DIR / "scenarios.yaml"
RAW_DIR = AUDIO_DIR / "raw"
BEDS_DIR = AUDIO_DIR / "beds"
MIXED_DIR = AUDIO_DIR / "mixed"
MANIFEST = AUDIO_DIR / "manifest.json"

# Deepgram wants 16 kHz for speech; higher rates cost bandwidth and buy nothing.
# This also matches what a phone channel would deliver, so the corpus is not
# secretly higher-fidelity than production.
TARGET_SR = 16_000

# Seeded so a rebuild is reproducible. Any randomness below (dither, noise
# bursts, jitter) draws from this.
SEED = 20260917


# --------------------------------------------------------------------- channels


@dataclass(frozen=True)
class Channel:
    """A phone-channel profile.

    Real emergency calls do not arrive as studio audio. Each profile is a
    plausible combination of handset, distance and codec, and each degrades the
    signal in a different way - so the corpus tests STT across channels rather
    than across one idealised one.
    """

    name: str
    # Band-pass, in Hz. Narrowband telephony is 300-3400; a speakerphone in a
    # loud room is effectively narrower still.
    low_hz: float
    high_hz: float
    # Peak level before soft-clipping. Lower = more distortion.
    clip_at: float
    # Room reflection: (delay_s, gain). A speakerphone on tarmac has a slapback
    # a close-held handset does not.
    reflection: tuple[float, float] | None
    description: str


CHANNELS: dict[str, Channel] = {
    "handset_close": Channel(
        name="handset_close",
        low_hz=250.0,
        high_hz=3800.0,
        clip_at=0.97,
        reflection=None,
        description="Phone held to the ear. The cleanest channel in the corpus.",
    ),
    "handset_outdoor": Channel(
        name="handset_outdoor",
        low_hz=300.0,
        high_hz=3400.0,
        clip_at=0.92,
        reflection=(0.012, 0.10),
        description="Handset outdoors: narrowband, slight wind buffeting.",
    ),
    "speakerphone": Channel(
        name="speakerphone",
        low_hz=350.0,
        high_hz=3200.0,
        clip_at=0.80,
        reflection=(0.030, 0.28),
        description="Phone on speaker, set down. Distant, boxy, clipping.",
    ),
    "speakerphone_wet": Channel(
        name="speakerphone_wet",
        low_hz=380.0,
        high_hz=3000.0,
        clip_at=0.72,
        reflection=(0.045, 0.34),
        description="Speaker on wet tarmac at night. Worst channel in the corpus.",
    ),
}


# ------------------------------------------------------------------- utilities


def read_env_key(*names: str) -> str:
    """A credential, from the environment or the repo's dotenv files.

    The environment wins, matching `config.read_env`'s precedence. Reading the
    dotenv here is not a hermeticity hole: this is the generation step, it
    already talks to a vendor, and requiring a shell export for a key the repo
    already holds is friction with no safety gain. The test suite imports
    nothing from this module.

    Several names are accepted because vendors and repos disagree on spelling -
    ElevenLabs' docs say ELEVENLABS_API_KEY while this repo's .env says
    ELEVEN_LABS_API_KEY - and failing with "not set" while the key sits in the
    file under another name wastes the operator's time.
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value

    for candidate in (REPO_ROOT / ".env.local", REPO_ROOT / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in names:
                value = value.strip().strip("'\"")
                if value:
                    return value
    return ""


def load_scenarios() -> dict[str, Any]:
    with SCENARIOS.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def read_mono(path: Path, sr: int = TARGET_SR) -> np.ndarray:
    """Load any audio file as mono float32 at `sr`."""
    import librosa

    audio, _ = librosa.load(str(path), sr=sr, mono=True)
    return np.asarray(audio, dtype=np.float32)


def rms(signal: np.ndarray) -> float:
    if signal.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))


def speech_rms(signal: np.ndarray, sr: int = TARGET_SR) -> float:
    """RMS of the *speech*, ignoring the silence around it.

    Measuring plain RMS over a whole utterance makes a line with long pauses
    look quiet, so the mixer would push the ambient bed down to compensate and
    the requested SNR would not be the delivered one. Frames below a floor
    relative to the loudest frame are treated as silence and excluded.
    """
    frame = int(0.02 * sr)
    if signal.size < frame:
        return rms(signal)

    usable = (signal.size // frame) * frame
    frames = signal[:usable].reshape(-1, frame)
    energies = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    if energies.max() <= 0:
        return 0.0

    # 20 dB below the loudest frame. Anything quieter is room tone, not speech.
    voiced = energies[energies > energies.max() * 0.1]
    return float(np.sqrt(np.mean(np.square(voiced)))) if voiced.size else rms(signal)


def bandpass(signal: np.ndarray, low_hz: float, high_hz: float, sr: int) -> np.ndarray:
    """Band-limit with an FFT brick wall plus a soft roll-off.

    A real codec's response is not this sharp, but the roll-off below keeps it
    from sounding synthetically abrupt, and the point is to remove the
    information a phone channel removes rather than to model a specific codec.
    """
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(signal.size, 1.0 / sr)

    gain = np.ones_like(freqs)
    gain[freqs < low_hz] = 0.0
    gain[freqs > high_hz] = 0.0

    # Cosine roll-off over the octave inside each edge, so the transition is not
    # a discontinuity that rings.
    low_edge = (freqs >= low_hz) & (freqs < low_hz * 1.5)
    if low_edge.any():
        span = (freqs[low_edge] - low_hz) / (low_hz * 0.5)
        gain[low_edge] = 0.5 - 0.5 * np.cos(np.pi * span)

    high_edge = (freqs > high_hz * 0.8) & (freqs <= high_hz)
    if high_edge.any():
        span = (freqs[high_edge] - high_hz * 0.8) / (high_hz * 0.2)
        gain[high_edge] = 0.5 + 0.5 * np.cos(np.pi * span)

    return np.fft.irfft(spectrum * gain, n=signal.size).astype(np.float32)


def soft_clip(signal: np.ndarray, threshold: float) -> np.ndarray:
    """tanh saturation above `threshold`.

    Hard clipping generates broadband splatter that is acoustically unlike a
    phone amplifier being driven too hard; tanh is closer to what a shouted
    line into a cheap mic actually does.
    """
    if threshold >= 1.0:
        return signal
    peak = float(np.max(np.abs(signal))) or 1.0
    normalised = signal / peak
    clipped = np.tanh(normalised / threshold) * threshold
    return (clipped * peak).astype(np.float32)


def add_reflection(signal: np.ndarray, delay_s: float, gain: float, sr: int) -> np.ndarray:
    delay = int(delay_s * sr)
    if delay <= 0 or delay >= signal.size:
        return signal
    out = signal.copy()
    out[delay:] += signal[:-delay] * gain
    return out.astype(np.float32)


def loop_to_length(bed: np.ndarray, length: int, sr: int) -> np.ndarray:
    """Tile an ambient bed to `length` with equal-power cross-faded seams.

    A butt-jointed loop clicks, and a click is an impulsive event an STT model
    can mistake for a consonant - so every seam is cross-faded over 250 ms.

    Three details a first attempt here got wrong, all caught by testing against
    brown noise rather than a sine - a periodic tone hides every one of them,
    because its loop points happen to align:

    CONTINUITY OF THE FADE TARGET. The fade must blend toward the samples that
    actually play next. The first version faded toward `bed[0:span]` while the
    audio resuming after the seam began at `bed[fade]`, so the crossfade
    reached full gain on material that was then immediately replaced by an
    unrelated slice - reintroducing, at the END of the fade, exactly the
    discontinuity the fade existed to remove. Measured as a consistent 0.75x
    level dip at every seam. Here `head` is the upcoming audio itself, so the
    overlap is continuous with what follows it by construction.

    EQUAL-POWER, NOT LINEAR. Two points in a noise bed are uncorrelated, and
    uncorrelated signals sum in power rather than amplitude, so a linear ramp
    loses 3 dB at the midpoint - an audible dip once per loop. `sqrt` windows
    hold the summed power constant.

    WRITE ORDER. The overlap is built from both contributions and assigned,
    never accumulated into and then scaled: scaling after accumulating
    multiplies samples the same iteration just wrote, which produced a 28x
    discontinuity on brown noise.
    """
    if bed.size == 0:
        return np.zeros(length, dtype=np.float32)
    if bed.size >= length:
        return bed[:length].astype(np.float32)

    fade = int(min(0.25 * sr, bed.size // 4))
    out = np.zeros(length, dtype=np.float32)

    if fade <= 0:
        # Bed too short to cross-fade; plain tile. Seams may click, but this is
        # a degenerate input (< 4 samples) rather than a normal path.
        repeats = int(np.ceil(length / bed.size))
        return np.tile(bed, repeats)[:length].astype(np.float32)

    # Equal-power windows over the overlap.
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    fade_in = np.sqrt(ramp)
    fade_out = np.sqrt(1.0 - ramp)

    # Each copy contributes `body` new samples: its head is consumed by the
    # overlap with the previous copy's tail.
    body = bed.size - fade

    # First copy, written whole.
    written = min(bed.size, length)
    out[:written] = bed[:written]

    # `position` is where the NEXT copy's overlap begins - i.e. `fade` samples
    # before the end of what has been written, so the seam is hidden inside
    # audio that already exists.
    position = body

    while position < length:
        overlap = min(fade, length - position)
        if overlap <= 0:
            break

        # The tail already in `out` fades out; `bed[:overlap]` - the head of the
        # next copy, which is what continues past the seam - fades in. Because
        # the copy then continues from `bed[overlap:]`, the material arriving at
        # full gain is the material that keeps playing. That continuity is the
        # whole point.
        out[position : position + overlap] = (
            out[position : position + overlap] * fade_out[:overlap]
            + bed[:overlap] * fade_in[:overlap]
        )

        # The rest of this copy, past the overlap, written clean.
        rest_start = position + overlap
        rest = min(bed.size - overlap, length - rest_start)
        if rest > 0:
            out[rest_start : rest_start + rest] = bed[overlap : overlap + rest]

        position += body

    return out.astype(np.float32)


# --------------------------------------------------------------- voice identity


def transform_voice(
    signal: np.ndarray, semitones: float, rate: float, sr: int = TARGET_SR
) -> np.ndarray:
    """Make one reader sound like a different caller.

    Formant-preserving. Naive resampling scales pitch and formants together,
    which models a person whose whole head changed size - the chipmunk effect.
    Shifting F0 while holding the spectral envelope models a different larynx in
    a similarly-sized vocal tract, which is what actually distinguishes two
    adults. `librosa.effects.pitch_shift` works on the STFT and leaves the
    envelope largely intact, which is why it is used here rather than `resample`.

    Kept to +/-2 semitones by scenarios.yaml. Beyond that it stops sounding like
    a person and starts sounding like processing, and processing artefacts would
    contaminate the STT result - the corpus could no longer attribute a
    mis-transcription to ambient noise.
    """
    import librosa

    out = signal
    if abs(rate - 1.0) > 1e-3:
        out = librosa.effects.time_stretch(out, rate=float(rate))
    if abs(semitones) > 1e-3:
        out = librosa.effects.pitch_shift(out, sr=sr, n_steps=float(semitones))
    return np.asarray(out, dtype=np.float32)


# ------------------------------------------------------------------ the mixer


def mix_turn(
    voice: np.ndarray,
    bed: np.ndarray,
    *,
    snr_db: float,
    snr_db_end: float | None,
    channel: Channel,
    rng: np.random.Generator,
    sr: int = TARGET_SR,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Mix one recorded line with its scene, and report what was done.

    Order matters and mirrors physical reality: the ambience exists in the room
    with the speaker, so voice and bed are summed FIRST and the channel is
    applied to the sum. Band-limiting the voice and then adding full-bandwidth
    noise would produce audio no telephone could ever deliver.
    """
    # Lead-in and tail, so the scene exists before and after the speech. An
    # utterance that begins the instant the file does sounds like an edit, and
    # VAD behaves differently on it.
    lead = int(0.6 * sr)
    tail = int(0.5 * sr)
    padded = np.concatenate(
        [np.zeros(lead, dtype=np.float32), voice, np.zeros(tail, dtype=np.float32)]
    )

    bed_full = loop_to_length(bed, padded.size, sr)

    voice_level = speech_rms(padded, sr)
    bed_level = rms(bed_full)
    if voice_level <= 0 or bed_level <= 0:
        scaled_bed = np.zeros_like(padded)
        achieved_start = achieved_end = float("inf")
    else:
        # A ramp when the scene changes under the call (s12: wind -> helicopter),
        # otherwise a constant.
        start_gain = voice_level / (bed_level * (10 ** (snr_db / 20.0)))
        if snr_db_end is None:
            envelope = np.full(padded.size, start_gain, dtype=np.float32)
            achieved_start = achieved_end = snr_db
        else:
            end_gain = voice_level / (bed_level * (10 ** (snr_db_end / 20.0)))
            envelope = np.linspace(start_gain, end_gain, padded.size, dtype=np.float32)
            achieved_start, achieved_end = snr_db, snr_db_end
        scaled_bed = bed_full * envelope

    mixed = padded + scaled_bed

    if channel.reflection is not None:
        mixed = add_reflection(mixed, *channel.reflection, sr=sr)

    mixed = bandpass(mixed, channel.low_hz, channel.high_hz, sr)
    mixed = soft_clip(mixed, channel.clip_at)

    # Quantisation dither at -90 dBFS. Present in any real 16-bit capture, and
    # its absence is one of the things that makes synthetic audio sound wrong.
    mixed = mixed + rng.normal(0.0, 10 ** (-90 / 20.0), mixed.size).astype(np.float32)

    peak = float(np.max(np.abs(mixed)))
    if peak > 0:
        # -1 dBFS. Leaves headroom so nothing clips on the way into the encoder.
        mixed = (mixed / peak * 0.891).astype(np.float32)

    report = {
        "snr_db": achieved_start,
        "snr_db_end": achieved_end if snr_db_end is not None else None,
        "channel": channel.name,
        "duration_s": round(mixed.size / sr, 2),
        "voice_rms": round(voice_level, 6),
        "bed_rms": round(bed_level, 6),
    }
    return mixed, report


# ---------------------------------------------------------------- transcription


class TranscribeError(RuntimeError):
    pass


def transcribe(path: Path, stt: dict[str, Any], token: str) -> dict[str, Any]:
    """Send one mixed WAV to Deepgram and return what it heard.

    Uses the same model and options as `main.py`, so the corpus is transcribed
    by the production configuration rather than by a convenient approximation.
    """
    params: list[tuple[str, str]] = [
        ("model", str(stt.get("model", "nova-3-medical"))),
        ("language", str(stt.get("language", "en-US"))),
        ("smart_format", "true" if stt.get("smart_format", True) else "false"),
        ("numerals", "true" if stt.get("numerals", True) else "false"),
        ("punctuate", "true"),
    ]
    for term in stt.get("keyterms") or []:
        params.append(("keyterm", str(term)))

    url = "https://api.deepgram.com/v1/listen?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        data=path.read_bytes(),
        headers={"Authorization": f"Token {token}", "Content-Type": "audio/wav"},
        method="POST",
    )

    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                last = exc
                continue
            raise TranscribeError(f"Deepgram HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            if attempt < 2:
                time.sleep(2 ** attempt)
                last = exc
                continue
            raise TranscribeError(f"Deepgram call failed: {exc}") from exc
    else:  # pragma: no cover - exhausted without breaking
        raise TranscribeError(f"Deepgram call failed after retries: {last}")

    alternatives = (
        payload.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])
    )
    best = alternatives[0] if alternatives else {}
    words = best.get("words") or []

    return {
        "transcript": (best.get("transcript") or "").strip(),
        "confidence": round(float(best.get("confidence", 0.0)), 4),
        "word_count": len(words),
        "mean_word_confidence": (
            round(sum(float(w.get("confidence", 0)) for w in words) / len(words), 4)
            if words
            else None
        ),
        "model": payload.get("metadata", {}).get("models", [None])[0],
        "request_id": payload.get("metadata", {}).get("request_id"),
    }


# ------------------------------------------------------------------------ main


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="after mixing, send each WAV to Deepgram (needs DEEPGRAM_API_KEY)",
    )
    parser.add_argument(
        "--only", default="", help="build just this scenario id"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-mix turns whose output already exists"
    )
    args = parser.parse_args()

    doc = load_scenarios()
    scenarios = [
        s for s in doc["scenarios"] if not args.only or s["id"] == args.only
    ]
    if not scenarios:
        print(f"no scenario named {args.only!r}", file=sys.stderr)
        return 2

    token = ""
    if args.transcribe:
        token = read_env_key("DEEPGRAM_API_KEY")
        if not token:
            print(
                "No DEEPGRAM_API_KEY found in the environment or in .env.local "
                "/ .env at the repo root; cannot transcribe.\n"
                "Mixing works without it - drop --transcribe.",
                file=sys.stderr,
            )
            return 2

    rng = np.random.default_rng(SEED)
    beds: dict[str, np.ndarray] = {}

    manifest: dict[str, Any] = {
        "version": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_rate": TARGET_SR,
        "seed": SEED,
        "stt": doc.get("stt", {}),
        "scenarios": {},
    }
    if MANIFEST.exists():
        try:
            previous = json.loads(MANIFEST.read_text(encoding="utf-8"))
            manifest["scenarios"] = previous.get("scenarios", {})
        except json.JSONDecodeError:
            print("existing manifest.json is unreadable; starting fresh", file=sys.stderr)

    missing: list[str] = []
    built = 0
    transcribed = 0

    for scenario in scenarios:
        sid = scenario["id"]
        scene = scenario["scene"]
        voice_cfg = scenario["voice"]
        channel = CHANNELS[voice_cfg["channel"]]

        bed_key = scene["bed"]
        if bed_key not in beds:
            # Freesound previews arrive as mp3; a locally-supplied or
            # hand-replaced bed is usually wav or flac. Accept any of them so a
            # bed can be swapped without touching this script.
            bed_path = next(
                (
                    candidate
                    for suffix in (".mp3", ".wav", ".flac", ".ogg")
                    if (candidate := BEDS_DIR / f"{bed_key}{suffix}").exists()
                ),
                BEDS_DIR / f"{bed_key}.mp3",
            )
            if not bed_path.exists():
                print(
                    f"  [bed missing] {bed_key} - run scripts/fetch_beds.py --live",
                    file=sys.stderr,
                )
                beds[bed_key] = np.zeros(0, dtype=np.float32)
            else:
                beds[bed_key] = read_mono(bed_path)

        turns_out: dict[str, Any] = manifest["scenarios"].get(sid, {}).get("turns", {})

        for turn in scenario["turns"]:
            n = turn["n"]
            key = f"t{n:02d}"
            raw_path = RAW_DIR / sid / f"{key}.wav"
            out_path = MIXED_DIR / sid / f"{key}.wav"

            if not raw_path.exists():
                missing.append(f"{sid}/{key}")
                continue

            if out_path.exists() and not args.force and key in turns_out:
                pass  # already mixed; may still need transcribing below
            else:
                voice = read_mono(raw_path)
                voice = transform_voice(
                    voice, voice_cfg["semitones"], voice_cfg["rate"]
                )
                mixed, report = mix_turn(
                    voice,
                    beds[bed_key],
                    snr_db=float(scene["snr_db"]),
                    snr_db_end=(
                        float(scene["snr_db_end"]) if "snr_db_end" in scene else None
                    ),
                    channel=channel,
                    rng=rng,
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(str(out_path), mixed, TARGET_SR, subtype="PCM_16")
                built += 1

                turns_out[key] = {
                    "turn": n,
                    "script": " ".join(turn["text"].split()),
                    "raw": str(raw_path.relative_to(AUDIO_DIR)).replace("\\", "/"),
                    "wav": str(out_path.relative_to(AUDIO_DIR)).replace("\\", "/"),
                    "sha256": sha256(out_path),
                    "mix": report,
                    "expect_markers": turn.get("expect_markers") or [],
                    "expect_no_markers": turn.get("expect_no_markers") or [],
                    "defect": turn.get("defect"),
                    "triage": turn["triage"],
                    "stt": turns_out.get(key, {}).get("stt"),
                }
                print(f"  [mix]  {sid}/{key}  {report['duration_s']}s")

            if args.transcribe and not turns_out[key].get("stt"):
                try:
                    result = transcribe(out_path, doc.get("stt", {}), token)
                except TranscribeError as exc:
                    print(f"  [stt FAIL] {sid}/{key}: {exc}", file=sys.stderr)
                else:
                    turns_out[key]["stt"] = result
                    transcribed += 1
                    print(
                        f"  [stt]  {sid}/{key}  conf={result['confidence']:.2f}  "
                        f"{result['transcript'][:60]!r}"
                    )

        if turns_out:
            manifest["scenarios"][sid] = {
                "title": scenario["title"],
                "criticality_peak": scenario["criticality_peak"],
                "scene": {
                    "place": " ".join(scene["place"].split()),
                    "bed": bed_key,
                    "snr_db": scene["snr_db"],
                    "snr_db_end": scene.get("snr_db_end"),
                },
                "voice": dict(voice_cfg),
                "turns": turns_out,
            }

    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    total = sum(len(s["turns"]) for s in scenarios)
    have = sum(len(v.get("turns", {})) for v in manifest["scenarios"].values())
    print(
        f"\nmixed {built} turn(s), transcribed {transcribed}. "
        f"manifest covers {have}/{total}."
    )
    if missing:
        print(f"\n{len(missing)} recording(s) not yet made:")
        for item in missing[:20]:
            print(f"  raw/{item}.wav")
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")
        print("\nSee agent/tests/audio/RECORDING-SCRIPT.md.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
