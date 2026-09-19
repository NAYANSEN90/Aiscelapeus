# Voice test corpus

Twelve emergency-call scenarios, recorded by a human, mixed with real ambient
noise, and transcribed by the production STT model.

## Layout

| Path | Committed? | What |
|---|---|---|
| `scenarios.yaml` | yes | **Source of truth.** Dialogue + the three-layer oracle |
| `RECORDING-SCRIPT.md` | yes | *Generated.* What a human reader reads from |
| `tts-tag-probe.json` | yes | Which ElevenLabs audio tags demonstrably work |
| `raw/<scenario>/tNN.wav` | yes | Voice, clean — synthesised or recorded |
| `beds/*.mp3` | yes | CC0 ambience + `LICENSES.md` provenance |
| `mixed/<scenario>/tNN.wav` | yes | Voice + ambience + phone channel |
| `manifest.json` | yes | **What Deepgram actually heard**, per turn |

`RECORDING-SCRIPT.md` and `docs/TESTS.md` are both rendered from
`scenarios.yaml` by `agent/scripts/render_docs.py` — **never edit them by hand**,
the next render reverts it.

## Building it

```bash
# 1. Ambience — once, needs a free FREESOUND_API_KEY
python scripts/fetch_beds.py --live

# 2. Voice. EITHER synthesise it (needs ELEVENLABS_API_KEY) …
python scripts/synthesize_voice.py --audit-voices --live  # 12 distinct speakers?
python scripts/synthesize_voice.py --probe --live         # which audio tags work
python scripts/synthesize_voice.py --live                 # 45 turns -> raw/

#    … OR record it yourself. See RECORDING-SCRIPT.md; same paths either way.
#    Missing turns are skipped and reported, so partial runs are fine and you
#    can mix the two (synthesise most, record the critical ones).

# 3. Mix — offline, deterministic, seeded
python scripts/build_corpus.py

# 4. Transcribe — needs DEEPGRAM_API_KEY
python scripts/build_corpus.py --transcribe
```

Steps 1, 2 (if synthesising) and 4 touch the network. Step 3 does not. The
default `pytest` run touches none of them: it reads `scenarios.yaml` and
`manifest.json` only.

### Why ElevenLabs for the voice, when Deepgram is the runtime vendor

Deepgram Aura-2 is this project's production TTS and rides the same key as STT.
It **cannot** do this job: Aura-2 has no emotion control at all — no SSML, no
pitch, no emphasis, only `speed` between 0.7 and 1.5. "HE'S NOT BREATHING" comes
out in the same level voice as a weather report, and a corpus of calmly-read
emergencies tests nothing about behaviour under stress.

ElevenLabs therefore appears **only** in `scripts/synthesize_voice.py`, a
generation-time script that runs once to produce committed artefacts. It is not
in `dependencies` and not in `dev`. The runtime stack stays Deepgram + Gemini.

### The voice roster is audited, not assumed

ElevenLabs runs **two silent deprecation tracks**. Legacy voices are "fully
deprecated" and their ids *automatically reroute* to a replacement — no error,
a different speaker. Default voices still work but expire **31 Dec 2026** and
are unavailable to accounts created after March 2026.

This bites here concretely: **Arnold** (s12) reroutes to **Adam**, which s08
uses directly, and **Freya** (s10) and **Matilda** (s02) both resolve to
**Maisie**. Twelve configured ids can serve ten distinct speakers — while a
test comparing the id *strings* passes, because they genuinely are distinct.

`--audit-voices` asks the API what it actually serves for each id, compares the
returned name against the configured one, and fails on a collision. `--live`
runs it automatically before spending anything, because a rerouted voice is
paid for and then heard as a caller the corpus already has.

### Audio tags are probed, not assumed

ElevenLabs documents `[crying]`, `[whispers]` and a few others; mentions
`[shouts]` and `[rushed]` only in a blog post; and says nothing about
`[panicked]` or `[breathless]` — while stating the list is non-exhaustive.

`--probe` measures which tags actually change the audio and writes
`tts-tag-probe.json`. Only verified tags are used. **With no probe report, no
tags are used at all** — the conservative fallback, because an unsupported tag
may be *read aloud*, and "open square bracket panicked" in a transcript is a
corpus defect that looks exactly like an STT failure.

The probe samples the **untagged baseline three times first** to measure the
model's own take-to-take variance, then requires a tag to beat that noise floor
by half again. v3 at stability 0.35 is deliberately non-deterministic, so a
single tagged-vs-untagged comparison cannot distinguish "the tag worked" from
"the model re-rolled the delivery" — without the noise floor, "verified tags"
would be theatre.

### The honest limitation

Synthetic panic imitates panic's acoustic signature; it is not panic. A real
terrified caller's articulation collapses in ways a model performing distress
does not reproduce, and Deepgram will likely transcribe these **more** cleanly
than a real emergency call.

So this corpus proves: marker logic, criticality and escalation behaviour,
robustness to ambient noise and channel degradation, and that the pipeline runs.
It does **not** prove performance on genuinely stressed human speech. The
critical scenarios (`s08`, `s09`, `s10`) are worth re-recording with a human
later — `--force` on just those ids replaces them.

## Why the transcript is the oracle

The manifest records what `nova-3-medical` returned — **not** the line the
reader was given. That difference is the whole point:

- Deepgram's `smart_format` emits typographic apostrophes, so `isn't breathing`
  arrives as `isn’t breathing`. `phrases.py` normalises for exactly this;
  asserting against the real transcript is what proves it.
- Ambient noise at 8 dB SNR genuinely corrupts words. When it corrupts a
  safety-critical phrase, that is a **finding** recorded with its SNR — not
  something to tune away by raising the SNR until the suite goes green.

## Adding a scenario

1. Add it to `scenarios.yaml`.
2. Run `python scripts/render_docs.py`.
3. Record the new lines.
4. `python scripts/build_corpus.py --transcribe`.

The marker expectations must be **verified against the real `phrases.py`**, not
guessed. Writing them by intuition is how the five defects in
`docs/reviews/2026-09-17-phrases-negation-defects.md` were nearly missed — they
were found precisely because the oracle was executed before it was trusted.

## Known defects pinned here

Several turns assert *observed* rather than *desired* behaviour, tagged
`defect: F-00N`. The suite is green today, and **fixing `phrases.py` will break
those assertions** — that is the signal to update the oracle. They are tripwires
for the fix, not a nag. See
[`docs/reviews/2026-09-17-phrases-negation-defects.md`](../../../docs/reviews/2026-09-17-phrases-negation-defects.md).
