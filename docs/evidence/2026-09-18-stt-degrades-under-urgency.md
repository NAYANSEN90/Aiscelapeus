# Two delivery styles break transcription — on clean audio

> **Title corrected 18 Sept 2026.** This file was first written as "Deepgram
> accuracy falls as criticality rises", on the strength of a mean-confidence
> table. Re-examined with medians and per-scenario figures, that claim is
> **stronger than the data supports** and the corrected version is below. The
> original framing is left visible here rather than quietly rewritten, because
> the error is instructive: a table of means over 45 samples looked like a trend
> and was actually two outliers.

**Date:** 18 September 2026
**Corpus:** 45 synthesised responder turns, 12 scenarios, `agent/tests/audio/`
**STT:** `nova-3-medical`, the production configuration from `main.py`
**Ambience:** **none.** No beds were mixed. This is the clean-channel baseline.

---

## The finding

Two of twelve scenarios transcribe badly. Both are urgent deliveries, and both
carry safety-critical failures. The other ten — including three Level-5
scenarios — transcribe essentially perfectly.

| Scenario | Peak | Mean confidence |
|---|---|---|
| `s10_poolside_drowning` | 5 | **0.502** |
| `s05_roadside_bleed` | 4 | **0.567** |
| `s08_site_arrest` | 5 | 0.913 |
| `s07_garden_anaphylaxis` | 5 | 0.958 |
| `s06_livingroom_stroke` | 4 | 0.976 |
| `s11_platform_negation` | 3 | 0.978 |
| `s02_playground_wrist` | 2 | 0.981 |
| `s04_kitchen_burn` | 3 | 0.984 |
| `s09_bedroom_agonal` | 5 | 0.990 |
| `s12_trail_deterioration` | 5 | 0.998 |
| `s03_office_seizure` | 3 | 0.999 |
| `s01_kitchen_cut` | 1 | **1.000** |

### What the aggregate table hides

Grouping by criticality invites a conclusion the data does not support:

| Peak | Mean | **Median** | Min | n |
|---|---|---|---|---|
| 1 | 1.000 | 1.000 | 1.00 | 3 |
| 2 | 0.981 | 0.993 | 0.95 | 3 |
| 3 | 0.986 | 0.992 | 0.95 | 10 |
| 4 | **0.772** | 0.871 | 0.00 | 8 |
| 5 | 0.878 | **0.981** | 0.00 | 21 |

Level 5's **median is 0.981**. Most critical turns transcribe fine. The means
are dragged down by the two scenarios above, and the ordering is not monotonic —
Level 4 measures worse than Level 5, because `s05` happens to sit in the
four-turn Level-4 group and skews it.

**The defensible claim is therefore narrower than "accuracy falls as criticality
rises":** certain urgent delivery styles break transcription, two of them appear
in this corpus, and both failures are safety-critical. That is a claim about
*delivery*, not about severity — and it points at something potentially fixable
rather than an inherent limit.

**This is before any ambient noise.** The corpus was designed to test noise
robustness at 8–28 dB SNR; the beds are not yet fetched, so every number here
comes from clean speech through the phone-channel simulation alone.

## Marker survival

43 of 45 turns preserved their full marker set. **Zero false positives** — noise
never invented a life threat, which is the more dangerous direction.

Two turns lost markers, both in `s10_poolside_drowning`:

| Turn | Confidence | Markers lost |
|---|---|---|
| t01 | 0.46 | `drowning`, `not_breathing`, `unresponsive` |
| t02 | 0.00 | `cyanosis` |

## The two transcriptions that matter

**s08 t02 — meaning inverted.**

> **Script:** "No. Nothing. I've got my hand on his chest, there's nothing.
> **He's grey, he's gone grey.**"
>
> **Heard:** "No. Nothing. I I handle it yet. There's nothing.
> **He's great. He's doing great.**"

A responder reporting cyanosis in a cardiac-arrest patient was transcribed as
reporting that the patient is *doing well*. The words "grey" and "great" differ
by one phoneme, and under shouted delivery Deepgram chose the wrong one. Nothing
downstream can recover from this: the LLM receives a sentence that says the
opposite of what was said.

Note the `cyanosis` marker does not cover "grey" — only "blue" phrasings — so
the deterministic net was never in play here. Worth considering as a marker
gap in its own right.

**s10 t02 — total loss.**

> **Script:** "His lips are blue. He's blue round his mouth. Please tell me what
> to do, please —"
>
> **Heard:** *(nothing — empty transcript, confidence 0.00)*

The `cyanosis` marker cannot fire on an empty string. A drowning child's most
visible sign of hypoxia produced silence.

## What separates the two bad scenarios from the rest

Every failure in this corpus is explained by a **conjunction of two generation
parameters**, and the separation is complete:

| Group | Mean confidence | Min | n |
|---|---|---|---|
| `[shouts]` **and** rate ≥ 1.20 | **0.661** | 0.00 | 12 |
| everything else | **0.985** | **0.93** | 33 |

All eight turns below 0.90 carry both. No turn without both falls below 0.93.

**Neither factor alone does it.** Cross-tabulated:

| Rate | With `[shouts]` | Without |
|---|---|---|
| < 1.10 | 0.978 (n=4) | 0.991 (n=18) |
| 1.10–1.25 | **0.567** (n=4) | 0.987 (n=7) |
| ≥ 1.25 | **0.708** (n=8) | 0.958 (n=4) |

`s11_platform_negation` shouts at rate 1.05 and scores 0.95–0.99. `s08` shouts
at 1.30 and its turns range 0.82–0.99. It is loud *and* fast together that
breaks transcription.

Correlations over all 45 turns, for reference:

```
shouts   r = -0.528     rate  r = -0.507
rushed   r = -0.278     ntags r = -0.344
panicked r = -0.052     stab  r = +0.109
```

`[panicked]` is essentially uncorrelated. The intuitive culprit — emotional
distress — is not the mechanism; **loudness and speed** are.

### The caveat that limits this

These are *synthesis* parameters, not measurements of the audio. The finding is
"the model transcribes poorly what ElevenLabs produces when asked to shout
quickly", which is a claim about this corpus. Whether it generalises to human
speech is untested, and a real shouting caller may differ in either direction.

It is still the useful result, because it is **actionable**: the conjunction is
known at generation time, so the hypothesis is directly testable by re-rendering
the same twelve turns at rate 1.0 with `[shouts]` removed and re-transcribing.
If confidence recovers, the cause is confirmed and the mitigation question
becomes concrete. That experiment costs about 1,300 characters.

## Why this happens

**Superseded — see the section above.** This originally read: *"the failures
cluster on the scenarios whose turns carry `[shouts]`, `[panicked]` and
`[frantic]`. Shouted and panicked speech is acoustically harder."*

The measurement does not support the `[panicked]` half. That tag correlates at
**r = −0.052** — no relationship at all — and turns carrying it average 0.873
against 0.904 for turns without. Emotional distress was the intuitive
explanation and it is the wrong one. Loudness and speed, together, are the
mechanism.

Kept visible rather than deleted, for the same reason as the title correction:
the plausible-sounding cause survived two readings of this file before anyone
cross-tabulated it.

### What does hold

The synthesis is doing its job. The delivery directions asked for panic, the
probe verified the tags change the audio, and the resulting audio is measurably
harder to transcribe — which is the property a corpus of *calmly-read*
emergencies could never have exposed. That remains the methodological point;
only the attributed cause was wrong.

## Sample size, stated plainly

**45 turns across 12 scenarios, one take each.** Every per-level group is 3–21
turns, and the two bad scenarios are 4 turns apiece. Nothing here supports a
statistical claim: a single re-run with different TTS seeds could move any of
these means materially, because the model is non-deterministic and no turn was
sampled twice.

What survives that weakness is the part that does not depend on aggregates: two
specific transcripts are wrong in specific, safety-critical ways, and those are
reproducible facts about committed audio rather than estimates. The tables are
context for those two findings, not the findings themselves.

## The caveat that keeps this honest

This is **synthetic** panic. A TTS model performing distress is an imitation of
the acoustic signature, and a real terrified caller almost certainly degrades
further, not less. So this result is best read as a **lower bound** on the
problem: real emergency speech will not transcribe better than this.

It also cannot be read as a measurement of Deepgram's performance on human
speech in general. What it establishes is narrower and still useful: within this
system, the STT layer is least reliable exactly where the stakes are highest,
and that relationship is measurable rather than speculative.

## What this does not yet show

The ambient beds are not fetched (no `FREESOUND_API_KEY`), so the scenarios'
designed SNRs — 8 dB on a building site, 9 dB on a railway platform — have not
been exercised. Those are expected to be materially worse than the numbers
above.

## Reproducing

```bash
cd agent
python scripts/synthesize_voice.py --probe --live   # verify audio tags
python scripts/synthesize_voice.py --live           # 45 turns
python scripts/build_corpus.py --transcribe         # mix + Deepgram
python -m pytest tests/scenarios/ -q
```

The manifest at `agent/tests/audio/manifest.json` holds every transcript,
confidence score and mix parameter. `test_markers_survive_the_real_audio_channel`
fails on `s10 t01`, which is the corpus reporting this finding rather than a
broken test.
