"""Synthesise the responder turns with ElevenLabs, in place of a human reader.

Writes into the same `raw/<scenario>/tNN.wav` paths a human would have recorded
into, so everything downstream - `build_corpus.py`'s mixing, the ambient beds,
the Deepgram transcription, the whole test suite - is unchanged.

    python scripts/synthesize_voice.py --probe    # which audio tags actually work
    python scripts/synthesize_voice.py --live     # synthesise everything missing
    python scripts/synthesize_voice.py --live --only s09_bedroom_agonal
    python scripts/synthesize_voice.py --live --force   # overwrite existing

Needs ELEVENLABS_API_KEY. Nothing else here touches the network.

WHY ELEVENLABS AND NOT DEEPGRAM
-------------------------------
Deepgram Aura-2 is this project's production TTS and covers STT on the same key,
which is why it is the runtime vendor. It cannot be used for THIS job: Aura-2
has no emotion control at all - no SSML, no pitch, no emphasis, only a `speed`
parameter between 0.7 and 1.5 (verified against Deepgram's voice-control docs).
A line reading "HE'S NOT BREATHING" comes out in the same pleasant, level voice
as a weather report, and a corpus of calmly-delivered emergencies tests nothing
about how this system behaves under stress.

So ElevenLabs appears here and ONLY here: a generation-time dependency of a
script that runs once to produce committed artefacts. It is not in `dependencies`
and not in `dev`. `pyproject.toml` records that elevenlabs and openai were
dropped as runtime vendors in WORKLOG Session 2; that stays true. The runtime
stack is Deepgram + Gemini.

THE HONEST LIMITATION
---------------------
Synthetic panic is an imitation of panic's acoustic signature, not the thing
itself. A real terrified caller's speech degrades in ways a model performing
distress does not reproduce: the breath is genuinely disordered, the articulation
genuinely collapses. Deepgram will likely transcribe these more cleanly than it
would a real 999 call.

What this corpus therefore DOES prove: marker logic, criticality and escalation
behaviour, robustness to ambient noise and channel degradation, and that the
whole pipeline runs. What it does NOT prove: performance on genuinely stressed
human speech. The critical scenarios (s08, s09, s10) are worth re-recording with
a human later; this makes that an improvement rather than a prerequisite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

AGENT_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = AGENT_ROOT / "tests" / "audio"
SCENARIOS = AUDIO_DIR / "scenarios.yaml"
RAW_DIR = AUDIO_DIR / "raw"
PROBE_REPORT = AUDIO_DIR / "tts-tag-probe.json"

API = "https://api.elevenlabs.io/v1"

# Audio tags need v3. The project's old config used `eleven_turbo_v2_5`, which
# ignores them silently - the tag is simply not acted on, and a scenario's
# delivery direction would go nowhere.
MODEL = "eleven_v3"

# PCM at 16 kHz mono: exactly what `build_corpus.py` wants, so nothing has to be
# resampled afterwards, and it matches what a phone channel delivers anyway.
OUTPUT_FORMAT = "pcm_16000"
SAMPLE_RATE = 16_000

# Creative keeps the model responsive to audio tags. `robust` suppresses them,
# which would quietly defeat the entire point of using v3.
DEFAULT_STABILITY = 0.35
DEFAULT_SIMILARITY = 0.75

# Tags this corpus wants, split by how well ElevenLabs documents them. The
# split is not pedantry: `--probe` measures which of these actually change the
# audio, and only verified ones are used. An unsupported tag is worse than no
# tag, because a model that does not recognise it may READ IT ALOUD - and
# "open square bracket panicked" in a transcript would be a corpus defect that
# looks like an STT failure.
DOCUMENTED_TAGS = ("crying", "whispers", "sighs", "wheezing", "exhales", "laughs")
BLOG_TAGS = ("shouts", "rushed")
UNVERIFIED_TAGS = ("panicked", "breathless", "frantic", "gasps", "urgent", "shaky")

PROBE_LINE = "He's on the floor and he is not moving at all."


class SynthError(RuntimeError):
    """Synthesis failed in a way the operator has to resolve."""


class MissingPermission(SynthError):
    """The key works but is not scoped for this call.

    Distinct from `SynthError` because a key scoped to `text_to_speech` alone is
    a perfectly reasonable way to issue a key for this job. Synthesis proceeds;
    only the roster check is unavailable, and the operator is told so plainly
    rather than being stopped.
    """


# Both spellings are accepted. The repo's own .env uses ELEVEN_LABS_API_KEY;
# ElevenLabs' docs and SDK use ELEVENLABS_API_KEY. Guessing one and failing with
# "not set" while the key sits in the file under the other name is a waste of
# the operator's time, and picking a winner would silently ignore a key that is
# plainly there.
KEY_NAMES = ("ELEVENLABS_API_KEY", "ELEVEN_LABS_API_KEY")


def read_api_key() -> str:
    """The ElevenLabs key, from the environment or the repo's dotenv files.

    The environment wins, matching `config.read_env`'s precedence. Reading the
    dotenv here is deliberate and is NOT a hermeticity hole: this script is the
    generation step, it already talks to a vendor, and requiring a shell export
    for a key the repo already holds would be friction with no safety gain. The
    test suite never imports this function.
    """
    for name in KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value

    repo_root = AGENT_ROOT.parent
    for candidate in (repo_root / ".env.local", repo_root / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() in KEY_NAMES:
                # Strip quotes the way dotenv does.
                value = value.strip().strip("'\"")
                if value:
                    return value
    return ""


# ------------------------------------------------------------------- delivery


class Composure(str, Enum):
    """How settled the caller actually is, as distinct from what they are saying.

    This was a boolean, and the boolean conflated three different things that
    demand different deliveries - the same "make illegal states unrepresentable"
    failure CLAUDE.md names, in new code:

      COMPOSED   The caller is genuinely settled. s01's cut thumb.
      STRAINED   The caller is *working* at being calm and not succeeding.
                 "Trying to be the calm one" over a colleague having a seizure;
                 "slightly calmer" relative to full panic at a drowning. Effort
                 under pressure is not the absence of pressure.
      DISTRESSED Everything else.

    The boolean read STRAINED as COMPOSED, which had a specific and bad
    consequence: three Level-5 turns received no emotion tag at all AND a
    stability bonus that made them steadier than the level table allows for any
    other critical turn. s09 t2 was among them - the agonal-breathing line, the
    most important utterance in the corpus - because its direction says
    "reassuring". That word describes what she is SAYING. She is terrified.
    """

    COMPOSED = "composed"
    STRAINED = "strained"
    DISTRESSED = "distressed"


# Phrasings that mean "calm is being attempted", not "calm is present". Checked
# BEFORE the plain composure words, because every one of them contains a
# composure word - which is the whole reason the boolean got them wrong.
_STRAINED_MARKERS = (
    "trying to be",
    "trying to stay",
    "trying to sound",
    "trying to keep",
    "attempting to",
    "calmer",  # comparative: calmer *than what*
    "reassuring yourself",
    "you believe you are",
    "forcing",
    "struggling to stay",
)

# Words describing genuine composure in the SPEAKER.
_COMPOSED_MARKERS = (
    "calm",
    "matter-of-fact",
    "controlled",
    "composed",
    "professional",
    "relaxed",
    "practical",
    "dismissive",
    "amused",
    "ordinary",
)

# Negated distress: "not panicking" means composed, and a substring check for
# "panic" gets it exactly backwards. Same bug class as F-001 in phrases.py.
_NEGATED_DISTRESS = (
    "not panicking",
    "not panicked",
    "without panic",
    "no panic",
    "not frightened",
)

# Words that describe the CONTENT of the utterance rather than the speaker's
# state. "Reassuring news" delivered by a terrified woman is still terrified.
# Listed so they are visibly excluded rather than silently absent.
_CONTENT_NOT_STATE = ("reassuring",)


def read_composure(direction: str) -> Composure:
    """Classify a turn's delivery direction.

    Order matters: strained phrasings are checked first because each of them
    contains a composure word.
    """
    text = " ".join(direction.split()).lower()
    if not text:
        return Composure.DISTRESSED

    if any(marker in text for marker in _STRAINED_MARKERS):
        return Composure.STRAINED
    if any(phrase in text for phrase in _NEGATED_DISTRESS):
        return Composure.COMPOSED
    if any(word in text for word in _COMPOSED_MARKERS):
        return Composure.COMPOSED
    return Composure.DISTRESSED


@dataclass(frozen=True)
class Delivery:
    """How one turn should be performed.

    Derived from the scenario's own fields rather than hand-written per turn:
    `scene.snr_db` says how loud the environment is (so how much the caller is
    shouting over it), `voice.rate` says how rushed they are, and
    `criticality_peak` says how frightened. One rule, applied uniformly - the
    DRY requirement in CLAUDE.md - instead of 45 independent judgement calls
    that would drift from the scenario table.
    """

    tags: tuple[str, ...]
    stability: float
    speed: float

    def apply(self, text: str) -> str:
        """Prefix the line with its tags. Tags affect roughly the next 4-5
        words, so they lead the utterance rather than being scattered."""
        if not self.tags:
            return text
        return " ".join(f"[{tag}]" for tag in self.tags) + " " + text


def plan_delivery(
    scenario: dict[str, Any],
    turn: dict[str, Any],
    usable_tags: frozenset[str],
) -> Delivery:
    """Choose tags and settings for one turn.

    `usable_tags` comes from the probe. A tag that did not demonstrably change
    the audio is dropped here rather than sent and hoped for.

    THE LEVEL USED HERE IS THE TURN'S OWN, NOT THE SCENARIO'S PEAK. Using the
    peak flattened exactly the arc some scenarios exist to test: s12 opens at
    Level 1 with a twisted ankle and ends at 5 in cardiac arrest, and keying
    off `criticality_peak` delivered the relaxed opening line as urgently as
    the arrest. The turn's own `triage.level` is what the caller knows at the
    moment they speak.
    """
    level = int(turn["triage"]["level"])
    snr = float(scenario["scene"]["snr_db"])
    rate = float(scenario["voice"]["rate"])
    direction = " ".join((turn.get("direction") or "").split()).lower()

    # Direction words that mean the caller is deliberately NOT emoting. Without
    # this, the loud-scene rule below put "[shouts] [panicked]" on the negation
    # scenario - a man calmly reporting a recovered faint on a railway platform.
    # He is raising his voice over the tannoy; he is not frightened, and a
    # panicked read would make the scenario test something it is not about.
    composure = read_composure(direction)
    composed = composure is Composure.COMPOSED

    wanted: list[str] = []

    # Loud scene -> the caller raises their voice to be heard. This is about
    # the ENVIRONMENT, not their emotional state, so it applies even when they
    # are composed.
    if snr <= 12 and "shouts" in usable_tags:
        wanted.append("shouts")

    # Tags that degrade intelligibility are withheld from any turn whose
    # markers MUST fire. `[whispers]` and `[crying]` both reduce articulation,
    # and Deepgram dropping "unresponsive" because the line was whispered would
    # be recorded as an STT-robustness finding at that scene's SNR - blaming
    # the ambient noise for something the delivery caused. The corpus must be
    # able to attribute its own failures, so an expressive tag never competes
    # with a marker assertion.
    #
    # Turns with only `expect_no_markers` are unaffected: a quieter delivery
    # cannot invent a marker, so there is nothing to confound.
    must_fire = bool(turn.get("expect_markers"))

    # The per-turn direction is the most specific signal available and is
    # authored by hand, so it outranks every heuristic below it.
    if "whisper" in direction and not must_fire and "whispers" in usable_tags:
        wanted.append("whispers")
    if not composed:
        if (
            "breaking" in direction
            or "begging" in direction
            or "broken" in direction
            or "crying" in direction
        ) and not must_fire and "crying" in usable_tags:
            wanted.append("crying")
        if (
            "out of breath" in direction or "breathing hard" in direction
        ) and "breathless" in usable_tags:
            wanted.append("breathless")
        if (
            "panic" in direction or "frightened" in direction or "terror" in direction
        ) and "panicked" in usable_tags:
            wanted.append("panicked")

        # A critical turn with nothing else chosen still must not sound
        # conversational - but only once the turn itself is critical.
        if level >= 5 and not wanted and "urgent" in usable_tags:
            wanted.append("urgent")

    if rate >= 1.25 and not composed and "rushed" in usable_tags:
        wanted.append("rushed")

    # More than two tags on one utterance fight each other and the delivery
    # becomes mush; the first two are the most specific by construction above.
    tags = tuple(dict.fromkeys(wanted))[:2]

    # Lower stability = more expressive range. A minor injury should not be
    # delivered with the same volatility as an arrest. Keyed to the turn's own
    # level for the same reason the tags are.
    stability = {1: 0.55, 2: 0.50, 3: 0.45, 4: 0.35, 5: 0.28}[level]
    if composure is Composure.COMPOSED:
        # A genuinely settled caller is held steadier than their level implies.
        stability = min(0.60, stability + 0.15)
    elif composure is Composure.STRAINED:
        # Held-together, not calm. A small bump - the effort is audible as
        # control - but never past the point where the delivery stops sounding
        # pressured, and never the full composed bonus. Capped below 0.50 so a
        # Level-5 strained turn cannot end up steadier than a Level-3 one.
        stability = min(0.47, stability + 0.06)

    # `rate` in scenarios.yaml drives build_corpus's time-stretch. Asking
    # ElevenLabs for part of it produces a genuinely faster READING - different
    # phrasing and breath - rather than a resampled one.
    speed = min(1.2, max(0.85, 1.0 + (rate - 1.0) * 0.5))

    return Delivery(tags=tags, stability=stability, speed=speed)


# ---------------------------------------------------------------------- api


def list_voices(key: str, voice_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """What the API actually serves for each configured id, keyed by REQUESTED id.

    Not a nicety. ElevenLabs runs two deprecation tracks, and both are silent:

      Legacy voices are "fully deprecated and removed from all products", and
      their ids "automatically route to their replacement voice IDs" - no error,
      no warning, a different speaker.

      Default voices still work but expire 31 Dec 2026 and are unavailable to
      accounts created after March 2026.

    The consequence for this corpus is concrete. Arnold (s12) reroutes to Adam,
    which s08 uses directly, so two scenarios become the same caller. Freya
    (s10) reroutes to Maisie, and Matilda (s02) reroutes to Maisie too. Twelve
    configured ids serve ten distinct speakers today and nine after the expiry -
    while a test comparing the configured id STRINGS passes, because they really
    are distinct. Only the server can answer this.
    """
    query = urllib.parse.urlencode({"voice_ids": ",".join(voice_ids), "page_size": 100})
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v2/voices?{query}",
        headers={"xi-api-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        # A key scoped to text_to_speech only - which is a perfectly reasonable
        # way to issue a key for this job - cannot read the voice list. That is
        # a missing capability, not a broken roster, and it gets its own error
        # so the caller can fall back to comparing the audio instead.
        if exc.code == 401 and "voices_read" in body:
            raise MissingPermission(
                "this API key lacks the `voices_read` permission"
            ) from exc
        raise SynthError(f"could not list voices: HTTP {exc.code}: {body[:200]}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SynthError(f"could not list voices: {exc}") from exc

    served = {v["voice_id"]: v for v in payload.get("voices", [])}
    # An id absent from the response was rerouted: the API answered with the
    # replacement's id instead. Report it as missing rather than pretending.
    return {vid: served.get(vid, {}) for vid in voice_ids}


def audit_voices(doc: dict[str, Any], key: str) -> int:
    """Check every configured voice resolves to a distinct, live speaker.

    Returns the number of problems found, and prints each one.
    """
    configured = [(s["id"], s["tts"]) for s in doc["scenarios"] if s.get("tts")]
    ids = tuple(t["voice_id"] for _, t in configured)

    print(f"auditing {len(ids)} configured voices against the live API...\n")
    try:
        served = list_voices(key, ids)
    except MissingPermission as exc:
        # No fallback, deliberately. An acoustic comparison was written for this
        # case and then DELETED, because measuring it showed it does not work:
        # two takes of Adam differed by 5.6 % / 13.9 % on zero-crossing rate and
        # brightness, while Sarah (female) against Adam (male) differed by only
        # 2.6 % / 12.9 %. Those statistics describe delivery, not identity, and
        # a check that rates a woman and a man as the same speaker - while
        # rating one man against himself as two - cannot answer this question at
        # any threshold. Tuning it until it agreed with what was already
        # believed would have been the "performance number with no correctness
        # evidence under it" failure CLAUDE.md names.
        #
        # So the roster goes unverified, and says so, rather than being blessed
        # by a measurement that does not measure.
        print(
            f"{exc}.\n\n"
            "The voice roster CANNOT be verified with this key. Grant it the\n"
            "`voices_read` permission at\n"
            "https://elevenlabs.io/app/settings/api-keys to enable the check.\n\n"
            "Why it matters: ElevenLabs silently reroutes deprecated voice ids\n"
            "to replacements, so two scenarios can end up sharing one caller\n"
            "while their configured ids remain distinct. Synthesis will proceed;\n"
            "listen to one turn per scenario before trusting the corpus.",
            file=sys.stderr,
        )
        return 0

    problems = 0
    seen: dict[str, str] = {}

    for scenario_id, tts_block in configured:
        vid = tts_block["voice_id"]
        expected = tts_block.get("voice_name", "?")
        entry = served.get(vid) or {}

        if not entry:
            print(
                f"  [GONE]  {scenario_id:26} {expected:9} {vid}\n"
                f"          not returned by the API - rerouted or unavailable "
                f"to this account"
            )
            problems += 1
            continue

        actual = entry.get("name", "?")
        labels = entry.get("labels") or {}
        descriptor = " ".join(
            str(labels.get(k, "")) for k in ("gender", "age", "accent")
        ).strip()

        if actual.lower() != expected.lower():
            print(
                f"  [RENAMED] {scenario_id:26} configured {expected!r} "
                f"but the API serves {actual!r}  ({descriptor})"
            )
            problems += 1
        else:
            print(f"  [ok]    {scenario_id:26} {actual:9} {descriptor}")

        # The collision check the id-string test cannot make.
        if actual in seen:
            print(
                f"  [CLASH] {scenario_id} and {seen[actual]} both resolve to "
                f"{actual!r} - two scenarios, one caller"
            )
            problems += 1
        seen[actual] = scenario_id

    print(
        f"\n{len(seen)} distinct speaker(s) across {len(configured)} scenarios; "
        f"{problems} problem(s)."
    )
    if problems:
        print(
            "\nFix the `tts.voice_id` entries in scenarios.yaml before "
            "synthesising - a rerouted voice is paid for twice and heard once."
        )
    return problems


def _post(path: str, key: str, payload: dict[str, Any], *, timeout: int = 120) -> bytes:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
            "Accept": "audio/*",
        },
        method="POST",
    )

    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            # 401/422 are the operator's problem (bad key, bad voice id, model
            # not on this plan) and retrying cannot fix them.
            if exc.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                last = exc
                continue
            raise SynthError(f"ElevenLabs HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            if attempt < 2:
                time.sleep(2 ** attempt)
                last = exc
                continue
            raise SynthError(f"ElevenLabs call failed: {exc}") from exc
    raise SynthError(f"ElevenLabs call failed after retries: {last}")


def synthesize(
    text: str,
    voice_id: str,
    key: str,
    *,
    stability: float,
    speed: float,
) -> bytes:
    """One utterance, returned as raw PCM16 at 16 kHz."""
    payload: dict[str, Any] = {
        "text": text,
        "model_id": MODEL,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": DEFAULT_SIMILARITY,
            "speed": speed,
        },
    }
    return _post(
        f"/text-to-speech/{voice_id}?output_format={OUTPUT_FORMAT}", key, payload
    )


# Below this, the "utterance" cannot be a spoken sentence. An HTTP 200 with an
# empty or truncated body is not hypothetical, and the failure it causes is
# invisible: `build_corpus.read_mono` loads a 0-frame WAV as an empty array,
# `rms` returns 0.0 without raising, and the turn mixes as pure ambience. The
# manifest then records a transcription failure at that scene's SNR - blaming
# the noise for a line that was never synthesised.
MIN_SPEECH_S = 0.4
# A response that is entirely (or almost entirely) digital silence is the same
# failure wearing a plausible duration.
MIN_SPEECH_RMS = 0.005


def validate_pcm(pcm: bytes, text: str) -> None:
    """Refuse audio that cannot be the line that was asked for.

    Raises rather than returning a flag: there is no sensible way for a caller
    to carry on with an unusable utterance, and the whole point is that this
    failure must be loud instead of silently becoming a corpus defect.
    """
    if not pcm:
        raise SynthError("the API returned an empty body")

    seconds = len(pcm) / 2 / SAMPLE_RATE
    if seconds < MIN_SPEECH_S:
        raise SynthError(
            f"returned only {seconds:.2f}s of audio for {len(text)} characters "
            f"of text; below the {MIN_SPEECH_S}s floor"
        )

    rms, peak, _ = _pcm_stats(pcm)
    if rms < MIN_SPEECH_RMS:
        raise SynthError(
            f"returned {seconds:.2f}s of near-silence (rms {rms:.5f}, "
            f"peak {peak:.3f}); nothing was spoken"
        )


def write_wav(pcm: bytes, destination: Path) -> float:
    """Wrap raw PCM16 in a WAV container. Returns duration in seconds."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return len(pcm) / 2 / SAMPLE_RATE


# -------------------------------------------------------------------- probe


def _pcm_stats(pcm: bytes) -> tuple[float, float, int]:
    """(rms, peak, samples) from raw PCM16, without numpy.

    This script deliberately has no numpy dependency: it belongs to the
    generation step, and keeping it importable with the standard library alone
    means the probe can run on a machine that has not installed the `audio`
    extra.
    """
    import array

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0, 0.0, 0
    total = sum(float(s) * float(s) for s in samples)
    rms = (total / len(samples)) ** 0.5 / 32768.0
    peak = max(abs(s) for s in samples) / 32768.0
    return rms, peak, len(samples)


def probe_tags(key: str, voice_id: str, *, repeats: int = 3) -> dict[str, Any]:
    """Find out which audio tags actually do something.

    ElevenLabs documents `[crying]`, `[whispers]` and a few others, mentions
    `[shouts]` and `[rushed]` only in a blog post, and says nothing about
    `[panicked]` or `[breathless]` - while also saying the list is not
    exhaustive. Designing around an unverified tag is exactly the kind of
    unevidenced claim CLAUDE.md forbids, so this measures instead.

    A tag counts as usable when the audio differs from the untagged baseline by
    MORE THAN THE MODEL'S OWN TAKE-TO-TAKE VARIANCE.

    That last clause is the whole method, and an earlier version of this
    function did not have it: it compared one tagged call against one untagged
    call and called an 8 % difference proof. v3 at stability 0.35 is
    deliberately non-deterministic, so two calls of the SAME text differ by
    some amount - and without measuring that amount, an 8 % threshold cannot
    tell "the tag did something" from "the model re-rolled the delivery". The
    verified-tags claim would have been theatre.

    So the baseline is sampled `repeats` times first, and the spread among
    those identical calls becomes the noise floor. A tag has to beat that floor
    by a margin before it counts. This costs a few hundred extra characters and
    is the difference between a measurement and an assertion.
    """
    tags = DOCUMENTED_TAGS + BLOG_TAGS + UNVERIFIED_TAGS
    print(f"probing {len(tags)} tags against voice {voice_id}")
    print(f"  ({repeats} baseline repeats first, to measure the noise floor)\n")

    # --- noise floor: the same text, the same settings, several times --------
    baselines = []
    for i in range(repeats):
        pcm = synthesize(PROBE_LINE, voice_id, key, stability=0.35, speed=1.0)
        rms, _, length = _pcm_stats(pcm)
        baselines.append((rms, length))
        print(f"  baseline {i + 1}: {length / SAMPLE_RATE:5.2f}s rms={rms:.4f}")

    base_rms = sum(r for r, _ in baselines) / len(baselines)
    base_len = sum(n for _, n in baselines) / len(baselines)

    # Widest observed spread among identical calls, as a fraction of the mean.
    dur_noise = (
        (max(n for _, n in baselines) - min(n for _, n in baselines)) / max(base_len, 1)
    )
    rms_noise = (
        (max(r for r, _ in baselines) - min(r for r, _ in baselines))
        / max(base_rms, 1e-9)
    )

    # A tag must exceed the observed noise by half again, and clear an absolute
    # floor so that a freakishly consistent set of baselines cannot make a
    # trivial difference look significant.
    dur_threshold = max(0.08, dur_noise * 1.5)
    rms_threshold = max(0.08, rms_noise * 1.5)

    print(
        f"\n  noise floor: duration +/-{dur_noise * 100:.1f}%, "
        f"level +/-{rms_noise * 100:.1f}%"
    )
    print(
        f"  a tag must beat: duration {dur_threshold * 100:.1f}% or "
        f"level {rms_threshold * 100:.1f}%\n"
    )

    results: dict[str, Any] = {
        "model": MODEL,
        "voice_id": voice_id,
        "probe_line": PROBE_LINE,
        "baseline_repeats": repeats,
        "baseline": {
            "duration_s": round(base_len / SAMPLE_RATE, 3),
            "rms": round(base_rms, 5),
        },
        # Recorded so the report can be judged later: a noise floor near zero
        # means the model was unusually consistent that day and the thresholds
        # fell back to the absolute 8 % floor.
        "noise_floor": {
            "duration_pct": round(dur_noise * 100, 2),
            "rms_pct": round(rms_noise * 100, 2),
        },
        "thresholds": {
            "duration_pct": round(dur_threshold * 100, 2),
            "rms_pct": round(rms_threshold * 100, 2),
        },
        "tags": {},
    }

    for tag in tags:
        source = (
            "documented" if tag in DOCUMENTED_TAGS
            else "blog" if tag in BLOG_TAGS
            else "unverified"
        )
        try:
            pcm = synthesize(
                f"[{tag}] {PROBE_LINE}", voice_id, key, stability=0.35, speed=1.0
            )
        except SynthError as exc:
            print(f"  [{tag:11}] FAILED: {exc}")
            results["tags"][tag] = {"source": source, "usable": False, "error": str(exc)}
            continue

        rms, peak, length = _pcm_stats(pcm)
        duration_delta = abs(length - base_len) / max(base_len, 1)
        rms_delta = abs(rms - base_rms) / max(base_rms, 1e-9)

        # Measured against the model's own variance, not a guessed constant.
        usable = duration_delta > dur_threshold or rms_delta > rms_threshold

        results["tags"][tag] = {
            "source": source,
            "usable": usable,
            "duration_s": round(length / SAMPLE_RATE, 3),
            "duration_delta_pct": round(duration_delta * 100, 1),
            "rms_delta_pct": round(rms_delta * 100, 1),
            # How far past the noise floor, so a marginal result is legible as
            # marginal rather than as a clean pass.
            "margin": round(
                max(duration_delta / dur_threshold, rms_delta / rms_threshold), 2
            ),
        }
        mark = "USABLE  " if usable else "no effect"
        print(
            f"  [{tag:11}] {mark}  dur {length / SAMPLE_RATE:5.2f}s "
            f"({duration_delta * 100:+5.1f}%)  rms {rms_delta * 100:+5.1f}%  [{source}]"
        )

    usable = sorted(t for t, v in results["tags"].items() if v.get("usable"))
    results["usable_tags"] = usable

    PROBE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    PROBE_REPORT.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    print(f"\n{len(usable)} usable tag(s): {', '.join(usable) or 'none'}")
    print(f"written to {PROBE_REPORT.relative_to(AGENT_ROOT.parent)}")
    return results


def load_usable_tags() -> frozenset[str]:
    """Tags the probe verified. Empty when no probe has been run.

    Falling back to "use everything" would defeat the probe's purpose, so the
    fallback is the conservative direction: no tags, and the delivery comes
    from the scripted wording and the speed setting alone.
    """
    if not PROBE_REPORT.exists():
        return frozenset()
    try:
        report = json.loads(PROBE_REPORT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return frozenset()
    return frozenset(report.get("usable_tags") or ())


# ------------------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call ElevenLabs")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="measure which audio tags work, write the report, and stop",
    )
    parser.add_argument(
        "--audit-voices",
        action="store_true",
        help="check every configured voice id resolves to a distinct live speaker",
    )
    parser.add_argument(
        "--skip-voice-audit",
        action="store_true",
        help="synthesise even if the voice audit finds problems",
    )
    parser.add_argument("--only", default="", help="one scenario id")
    parser.add_argument(
        "--force", action="store_true", help="overwrite existing raw WAVs"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the tagged text for every turn without calling the API",
    )
    args = parser.parse_args()

    with SCENARIOS.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)

    scenarios = [s for s in doc["scenarios"] if not args.only or s["id"] == args.only]
    if not scenarios:
        print(f"no scenario named {args.only!r}", file=sys.stderr)
        return 2

    usable = load_usable_tags()

    if args.dry_run:
        if not usable:
            print("NOTE: no probe report, so no audio tags will be used.")
            print("      Run --probe --live first to find out which ones work.\n")
        for scenario in scenarios:
            tts = scenario.get("tts") or {}
            print(f"\n{scenario['id']}  [{tts.get('voice_name', '?')}] "
                  f"{tts.get('voice_note', '')}")
            for turn in scenario["turns"]:
                delivery = plan_delivery(scenario, turn, usable)
                text = " ".join(turn["text"].split())
                print(
                    f"  t{turn['n']:02d} stab={delivery.stability:.2f} "
                    f"speed={delivery.speed:.2f}"
                )
                print(f"       {delivery.apply(text)[:150]}")
        return 0

    if not args.live and not args.probe and not args.audit_voices:
        print(
            "Nothing done. Pass --live to synthesise, --probe --live to test tags,\n"
            "--audit-voices --live to verify the voice roster, or --dry-run to\n"
            "see the prompts without calling the API."
        )
        return 0

    key = read_api_key()
    if not key:
        print(
            f"No ElevenLabs key found. Looked for {' or '.join(KEY_NAMES)} in the\n"
            "environment, then in .env.local and .env at the repo root.\n\n"
            "  1. Get a key at https://elevenlabs.io/app/settings/api-keys\n"
            "  2. Add to .env:  ELEVEN_LABS_API_KEY=...\n"
            "     or export it: $env:ELEVENLABS_API_KEY='...'\n\n"
            "It is used only by this script, at generation time. It is not a\n"
            "runtime dependency and the test suite never reads it.",
            file=sys.stderr,
        )
        return 2

    if args.audit_voices:
        return 1 if audit_voices(doc, key) else 0

    if args.probe:
        probe_tags(key, scenarios[0]["tts"]["voice_id"])
        return 0

    # Verify the roster BEFORE spending anything. A rerouted voice is paid for
    # and then heard as a caller the corpus already has, so the audit belongs in
    # front of the 45 synthesis calls rather than after them.
    if not args.skip_voice_audit:
        try:
            problems = audit_voices(doc, key)
        except SynthError as exc:
            print(f"\nvoice audit could not run: {exc}", file=sys.stderr)
            print("Continuing; pass --audit-voices to investigate.", file=sys.stderr)
        else:
            if problems:
                print(
                    "\nRefusing to synthesise with an unverified voice roster.\n"
                    "Fix scenarios.yaml, or pass --skip-voice-audit to proceed anyway.",
                    file=sys.stderr,
                )
                return 1
            print()

    if not usable:
        print(
            "WARNING: no tag probe report found, so NO audio tags will be used.\n"
            "         Delivery will come from wording and speed alone.\n"
            "         Run: python scripts/synthesize_voice.py --probe --live\n"
        )

    made = skipped = 0
    failures: list[str] = []

    for scenario in scenarios:
        tts = scenario.get("tts")
        if not tts:
            failures.append(f"{scenario['id']}: no tts block in scenarios.yaml")
            continue

        print(f"\n{scenario['id']}  [{tts['voice_name']}]")
        for turn in scenario["turns"]:
            destination = RAW_DIR / scenario["id"] / f"t{turn['n']:02d}.wav"
            if destination.exists() and not args.force:
                skipped += 1
                continue

            delivery = plan_delivery(scenario, turn, usable)
            text = delivery.apply(" ".join(turn["text"].split()))

            try:
                pcm = synthesize(
                    text,
                    tts["voice_id"],
                    key,
                    stability=delivery.stability,
                    speed=delivery.speed,
                )
            except SynthError as exc:
                failures.append(f"{scenario['id']}/t{turn['n']:02d}: {exc}")
                print(f"  t{turn['n']:02d} FAILED: {exc}", file=sys.stderr)
                continue

            # Validate BEFORE writing. A file that exists is treated as done by
            # the next run's skip logic, so a corrupt one would persist.
            try:
                validate_pcm(pcm, text)
            except SynthError as exc:
                failures.append(f"{scenario['id']}/t{turn['n']:02d}: {exc}")
                print(f"  t{turn['n']:02d} REJECTED: {exc}", file=sys.stderr)
                continue

            seconds = write_wav(pcm, destination)
            made += 1
            tag_note = " ".join(f"[{t}]" for t in delivery.tags) or "-"
            print(f"  t{turn['n']:02d} {seconds:5.2f}s  {tag_note}")

    print(f"\nsynthesised {made}, skipped {skipped} (already present).")
    if made:
        print("\nNext:\n  python scripts/build_corpus.py --transcribe")
    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
