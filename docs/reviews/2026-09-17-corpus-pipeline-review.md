# Independent review — voice test corpus pipeline

**Date:** 17 September 2026
**Reviewer:** `feature-dev:code-reviewer`, independent pass (CLAUDE.md §3 — no self-review)
**Scope:** `scripts/build_corpus.py`, `scripts/fetch_beds.py`, `scripts/render_docs.py`,
`tests/scenarios/test_voice_scenarios.py`, `tests/audio/scenarios.yaml`

---

## Outcome

Two findings, both real, both fixed, both now pinned by regression tests.
The reviewer also **corrected my own diagnosis** of the more serious one.

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | `loop_to_length` cross-faded toward the wrong source material | Critical | Fixed + 6 tests |
| 2 | `fetch_beds._acceptable` accepted a result with no licence field | Important | Fixed + 4 tests |
| 3 | *(found while fixing 2)* exclusion words matched inside other words | Moderate | Fixed + test |

---

## Finding 1 — the cross-fade faded toward material that never played

**This is the one worth reading.** It took three passes to get right, and each
pass was wrong in a different way.

### What I found first

Testing `loop_to_length` on brown noise — the closest synthetic analogue to
traffic and machinery rumble, which dominates this corpus' beds — showed a
**28× discontinuity** and a 46% level swing:

```
brown noise:  max|delta| source 0.01561  looped 0.43880   ratio 28.12x
white noise:  max|delta| source 0.66548  looped 0.66548   ratio  1.00x
sine 220 Hz:  max|delta| source 0.08637  looped 0.08637   ratio  1.00x
```

**The sine test passed.** A periodic tone's loop points align by coincidence,
so the defect was invisible on the signal I had originally checked it with.
That is the lesson: a DSP test on a pure tone proves almost nothing.

### What I thought the cause was

That the overlap was accumulated into and *then* scaled —
`out[start:start+span] *= window[:span]` multiplying samples the same iteration
had just written. I rewrote it to assign rather than accumulate, and switched
to equal-power (`sqrt`) windows, since uncorrelated signals sum in power and a
linear ramp loses 3 dB at every seam midpoint.

The click went away. **The seam dip did not.**

### What the cause actually was

The reviewer traced it properly and found my diagnosis was wrong:

> The author's original suspicion (same-iteration double-scaling) is **not**
> correct — the scaled region is always disjoint from the current iteration's
> own new raw write. The actual bug is different: **wrong source material for
> the fade-in.**

The fade blended toward `bed[0:span]`, but the audio resuming after the seam
began at `bed[fade]`. So the crossfade reached full gain on material that was
then immediately replaced by an unrelated slice — **reintroducing, at the end
of the fade, exactly the discontinuity the fade existed to remove.**

The fix is to fade toward the samples that actually continue playing.

### Verification after the fix

```
brown noise       ratio 1.00x   worst delta 19485 samples from nearest seam
white noise       ratio 1.15x   worst delta  2049 samples from nearest seam
sine 220 Hz       ratio 1.41x   worst delta  1963 samples from nearest seam
crowd-like        ratio 1.22x   worst delta  2777 samples from nearest seam
```

The worst discontinuity is now ordinary signal content in every case, never a
seam. RMS is preserved within 1%.

### A measurement error of my own, worth recording

My first seam test reported a "0.75× dip" both before *and* after the fix,
identically. It was comparing RMS at fixed offsets either side of the seam —
but brown noise wanders by nature (the source's own per-second spread was
28%), so the test was measuring the source's drift, not the seam. Replaced
with a local-continuity test that compares against the signal's own typical
sample-to-sample delta.

**A test that returns the same number before and after a real fix is not
measuring the thing it claims to measure.**

---

## Finding 2 — a missing licence field was treated as "nothing to check"

```python
if result.get("license", "") and CC0 not in result["license"]:
    return f"licence {result['license']!r} is not CC0"
```

If `license` is empty or absent, the `and` short-circuits and **the check is
skipped entirely**. The result then falls through to the duration and tag
checks and can be accepted as though its licence had been verified.

The reviewer's framing is the right one: the docstring claims this fetcher
*refuses* non-CC0 audio, but in isolation the local check did not — the
guarantee rested wholly on Freesound's server-side filter. And the one result
whose provenance cannot be confirmed is precisely the one the gate exists for.

Fixed to reject on absent-or-empty, and pinned by
`test_a_missing_licence_is_refused` and
`test_a_result_with_no_licence_key_at_all_is_refused`.

---

## Finding 3 — substring matching rejected legitimate beds

Found by a test written for Finding 2. `"birdsong in a forest"` was rejected
for containing the excluded word **"song"**. The same flaw would reject
"crossing" for "cross" and "seasons" for "season".

Fixed with word-boundary matching. Multi-word terms like `"loop pack"` still
work, because the boundary applies to the phrase.

---

## What the reviewer confirmed as sound

- **Hermeticity.** `test_voice_scenarios.py` imports no audio library and makes
  no network call. Independently verified by importing it with `librosa`,
  `soundfile`, `scipy`, `numba` and `numpy` all blocked — 45 turns load fine.
- **Test quality.** The marker tests use exact-set equality, so they do not
  pass against a gutted `find_markers`. `test_marker_ids_in_the_oracle_all_exist`
  closes the vacuous-assertion gap from a typo'd marker name.
- **`speech_rms`.** The −20 dB voiced-frame floor is reasonable, with a sound
  fallback for fragments shorter than one frame.
- **`mix_turn` ordering.** Summing voice and bed *before* applying the channel
  matches the physical model: both signals exist in the room together.
- **DRY.** No duplicated constants between `scenarios.yaml` and the renderer;
  `render_docs.py --check` catches hand-edits to generated files.
- **Error handling.** `build_corpus.py` skips and reports missing recordings
  rather than crashing.

### Noted, not actioned

The reviewer observed that turns with both `expect_markers: []` and
`expect_no_markers: []` are unconstrained by the marker layer. That is the
oracle's per-turn scoping choice rather than a harness defect — those turns are
asserted by the triage layer instead. Recorded here rather than silently
dropped.

---

## Also fixed in this pass

`pyproject.toml` sets `filterwarnings = ["error"]` — correct for this project,
since a warning on a safety-critical path is a defect. Three warnings surfaced
in the DSP tests:

- `hop_length` / `n_fft` deprecations — **librosa 1.0.0 passing its own
  deprecated parameters internally** (`effects.py:448 → spectrum.py:1452`).
  Verified by traceback that the call sites already use the current
  keyword-only API, so no call avoids it. Suppressed narrowly, per-module, with
  the reason in code.
- `RuntimeWarning: invalid value encountered in cast` from numba — this one
  can mean NaN reached the output, so it was **checked rather than assumed**:
  across every shift the corpus uses, output is fully finite with zero NaN and
  zero inf. Suppression is backed by `test_transform_output_is_always_finite`,
  which asserts finiteness over all 48 (semitones, rate) pairs plus every real
  scenario setting — so the filter can never hide a genuine numerical failure.

---

---

# Second pass — the ElevenLabs synthesis path

**Date:** 18 September 2026
**Scope:** `scripts/synthesize_voice.py`, `tests/scenarios/test_tts_delivery.py`,
the `tts:` blocks added to `scenarios.yaml`

The user had no time to record 45 lines, so ElevenLabs synthesises them into the
same `raw/` paths. Independent review found **five** issues. All five were real;
all five are fixed.

| # | Finding | Severity | Status |
|---|---|---|---|
| 6 | `composed` heuristic misread *attempted* calm as calm | Critical | Fixed — 3-state type |
| 7 | 6 of 12 voice ids are deprecated and silently reroute | Critical | Fixed — live audit |
| 8 | Empty/garbage audio written and mixed as silence | Important | Fixed — validated |
| 9 | Tag probe had no noise floor; single sample | Important | Fixed — 3 baselines |
| 10 | The "unverified tag" test was near-vacuous | Important | Fixed — parametrised |

## Finding 6 — a boolean conflated three different states

The reviewer found two turns where a direction describing *effortful* calm was
read as composure. Checking all 45 turns found **four**, and the worst was one
the reviewer did not name:

| Turn | Direction | Was | Should be |
|---|---|---|---|
| s03 t1 | "Trying to be the calm one" | composed | strained |
| s03 t2 | "reassuring yourself as much as the agent" | composed | strained |
| **s09 t2** | "you believe you are giving reassuring news" | composed | strained |
| s10 t3 | "slightly calmer because someone is helping" | composed | strained |

**s09 t2 is the most important utterance in the corpus** — the agonal-breathing
line, where the caller reports her husband IS breathing while he is in cardiac
arrest. Its direction contains the word "reassuring", which describes *what she
is saying*, not her state. She is terrified.

All three Level-5 turns received **no emotion tag at all** plus a stability
bonus (0.43 against the 0.28 their level assigns), making them steadier than any
other critical turn in the corpus.

The root error was a boolean where three states exist. Replaced with
`Composure.{COMPOSED, STRAINED, DISTRESSED}` — the same
make-illegal-states-unrepresentable failure CLAUDE.md names, committed in new
code. Strained phrasings are matched *before* composure words, because every one
of them contains a composure word.

## Finding 7 — six voice ids silently reroute to other speakers

Verified against ElevenLabs' own documentation. Two deprecation tracks, both
silent: Legacy voices "automatically route to their replacement voice IDs";
Default voices expire 31 Dec 2026.

**Two real collisions in this corpus:**

- **Arnold (s12) → Adam**, and s08 uses **Adam** directly.
- **Freya (s10) → Maisie**, and **Matilda (s02) → Maisie**.

Twelve configured ids serve **ten** distinct speakers today, nine after the
expiry — while `test_every_scenario_uses_a_different_voice` passes, because the
id *strings* are genuinely distinct. Only the server can answer this.

Added `--audit-voices`, which queries `GET /v2/voices`, compares each returned
name against the configured one, and reports collisions. `--live` runs it
automatically before spending anything: a rerouted voice is paid for and then
heard as a caller the corpus already has.

## Finding 8 — corrupt audio mixed as silence

Confirmed by execution: a 0-frame WAV loads as an empty array,
`rms()` returns `0.0` without raising, and the turn mixes as pure ambience. The
manifest would then record a transcription failure *at that scene's SNR* —
blaming ambient noise for a line that was never synthesised.

`validate_pcm` now rejects an empty body, anything under 0.4 s, and digital
silence at a plausible duration. It runs **before** the file is written, since
an existing file is treated as done by the next run's skip logic.

## Finding 9 — the probe measured nothing

The probe compared one tagged call against one untagged call and called an 8%
difference proof. v3 at stability 0.35 is deliberately non-deterministic, so two
calls of the *same* text differ by some unmeasured amount. Without that amount,
the threshold could not distinguish "the tag worked" from "the model re-rolled".

Now samples the baseline three times, derives the noise floor from their spread,
and requires a tag to beat it by half again. The report records the floor and
each tag's margin, so a marginal result reads as marginal.

## Finding 10 — a test that proved one branch

`test_no_tag_is_used_that_the_probe_has_not_verified` passed
`frozenset({"shouts"})`, exercising a single branch. Every other tag's guard
would have passed just as well had the branch been deleted. Now parametrised
over each tag individually, plus `test_each_tag_is_reachable_by_some_turn` to
stop a planner that emits nothing from satisfying it vacuously.

---

## Final state

```
1351 passed, 12 skipped
```

231 of those are new, in `tests/scenarios/`:

| File | Tests | Asserts |
|---|---|---|
| `test_voice_scenarios.py` | 129 | Oracle shape, markers, triage, ratchet, escalation lifecycle |
| `test_corpus_dsp.py` | 87 | Loop continuity, SNR accuracy, filters, voice transform, finiteness |
| `test_bed_licensing.py` | 15 | CC0 gate, duration bounds, tag filtering |
- Hermetic suite proven to load with all audio libraries blocked
- DSP tests skip cleanly where the `audio` extra is absent, including CI
