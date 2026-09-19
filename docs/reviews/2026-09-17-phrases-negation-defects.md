# Safety-net defects in `phrases.py` — negation, coverage, and first-match-only

**Date:** 17 September 2026
**Found by:** scenario-oracle verification while building the voice test corpus
**Confirmed by:** independent `feature-dev:code-reviewer` pass (no self-review, per CLAUDE.md §3)
**Status:** all five verified by execution against the real module. **None fixed.**

---

## How these were found

Building `agent/tests/audio/scenarios.yaml`, I wrote the expected `phrases.py`
markers for 47 responder turns *before* running anything, then executed the real
`find_markers` against every turn. Five turns mismatched. Two were errors in my
expectations. **Three were defects in the module.** An independent reviewer then
confirmed those three, and found a fourth and fifth I had not.

This is the CLAUDE.md §2 discipline working as intended: the oracle was checked
against reality before any test code was built on top of it.

---

## Why these matter more than an ordinary bug

`phrases.py` is not a heuristic. It is the deterministic net that exists
*specifically to catch model failure* — its own docstring says putting a model in
front of it "reintroduces the failure mode". Every defect below is a case where
the net stays silent on a reported life threat.

The error asymmetry is stark: a **missed** escalation can kill a patient; a
**spurious** escalation costs a clinician's time. Every fix below narrows
suppression, which can only make the net fire *more* often. That is the safe
direction.

---

## F-001 — Negation window crosses clause boundaries · **CRITICAL**

`_is_negated` (`phrases.py:191-201`) looks back a flat `_NEGATION_WINDOW = 3`
words for any negator, with no awareness of clause boundaries. A negator
belonging to a *different* clause suppresses the following finding.

| Utterance | Returns | Should return |
|---|---|---|
| `"he's not moving, he's not breathing"` | `['unresponsive']` | + `not_breathing` |
| `"no pulse, he's not breathing"` | `['no_pulse']` | + `not_breathing` |
| `"no, no, he's not breathing"` | **`NONE`** | `not_breathing` |
| `"no he's not breathing"` | **`NONE`** | `not_breathing` |
| `"he's not — no, he's not responding"` | **`NONE`** | `unresponsive` |
| `"he's not breathing"` *(control)* | `['not_breathing']` | correct |

Two distinct mechanisms, one root cause:

1. **Adjacent-clause leakage.** Reporting two life threats in one breath makes
   the second vanish.
2. **Discourse-marker "no".** `"no, he's not breathing"` — the single most
   natural answer to *"is he breathing?"* — returns **nothing**. The "no" means
   *"no, in answer to your question"*, not *"not"*. A caller who stutters under
   stress disarms the safety net.

**Clinical consequence.** A plain, unambiguous cardiac-arrest report is invisible
to the deterministic layer. Escalation falls back entirely on the LLM — the exact
failure this module exists to catch.

**Fix direction** (reviewer-endorsed): stop the lookback at punctuation and
coordinating conjunctions, *and* tighten adjacency. Punctuation-stopping alone
does not fix unpunctuated `"no no he's not breathing"`. Both legitimate
suppression cases survive, because in each the negator is immediately adjacent:
`"the casualty is NOT unresponsive"` and `"he was not breathing but now he is
breathing again"`.

**Verdict:** `_NEGATION_WINDOW = 3` as a flat word count is not defensible. It is
the direct cause of F-001 and will keep generating variants for every new marker.

---

## F-002 — Recovery narrative escalates when the past tense is contracted · **LOW**

The final guard in `find_markers` (`phrases.py:254-256`) requires a bare
`was|were|had` token *before* the match. When the past tense is fused into a
contraction, the guard misses.

| Utterance | Returns | Intended |
|---|---|---|
| `"he wasn't breathing properly but he is breathing now"` | `['not_breathing']` | suppressed |
| `"he was not breathing but now he is breathing again"` | `NONE` | suppressed ✓ |

`_contradicted_later` correctly returns `True`; the `was/were/had` prefix check
fails because the text says `wasn't`.

**Clinical consequence.** A recovered faint escalates to Critical. This is the
**safe** direction of error, which is why this is scheduled rather than hot-fixed.
Exercised by `s11_platform_negation` turn 2.

---

## F-003 — `unresponsive` misses "can't wake" phrasing · **HIGH**

`phrases.py:151-164` covers `won't\s+wake\s+up` but no `can't wake` form.

| Utterance | Returns |
|---|---|
| `"he won't wake up"` | `['unresponsive']` |
| `"I can't wake him up"` | **`NONE`** |
| `"she can't wake him up"` | **`NONE`** |

First-person "I can't wake him up" is at least as common as "he won't wake up" —
it is what someone says at 3am about the person beside them. Exercised by
`s09_bedroom_agonal` turn 1.

---

## F-004 — `drowning` requires the literal word "water" · **HIGH**

`phrases.py:185` hard-codes `out\s+of\s+the\s+water`.

| Utterance | Returns |
|---|---|
| `"pulled him out of the water"` | `['drowning']` |
| `"pulled a little boy out of the pool"` | **`NONE`** |
| `"pulled her out of the pool"` | **`NONE`** |

Nobody says "water" about a swimming pool. Missing `drowning` means the
hypoxic-arrest sequence — **five rescue breaths before compressions**, which
inverts adult BLS — is never signalled, so a paediatric drowning is handled as a
standard adult arrest.

**The existing corpus gives false confidence.** `tests/data/utterances.yaml:126`
(`"We pulled him out of the pool, he's drowning"`) passes **only** because the
literal word "drowning" appears separately. Verified:

```
"We pulled him out of the pool, he's drowning"  -> ['drowning']
"We pulled him out of the pool"                 -> NONE
```

Missing vocabulary: pool, lake, river, canal, pond, bath, sea, hot tub.

---

## F-005 — First match only: a suppressed match drops the marker for the whole utterance · **CRITICAL**

*Found by the independent reviewer, not by me. Confirmed by execution.*

`find_markers` calls `marker.pattern.search(normalised)` once per marker
(`phrases.py:245`) and makes a single accept/suppress decision on that one span.
If the **first** occurrence is legitimately suppressed, the marker is dropped for
the entire utterance — no later occurrence is ever examined.

| Utterance | Returns | Should return |
|---|---|---|
| `"he is not unresponsive - wait, no, he IS unresponsive, he's gone"` | **`NONE`** | `unresponsive` |
| `"he's not unresponsive. he's unresponsive now."` | **`NONE`** | `unresponsive` |
| `"she was not breathing but now she is breathing again, wait, she's not breathing"` | **`NONE`** | `not_breathing` |

**Clinical consequence.** This is the recovery narrative *in reverse*: a
responder who first denies a finding and then **corrects toward danger** — a
deterioration report, the most urgent thing they can say — is silently dropped.
The module handles "was bad, now fine" and has no path for "was fine, now bad"
within one utterance.

**Fix direction:** iterate with `finditer` and accept the marker if **any**
occurrence survives suppression, rather than deciding on the first.

Note `test_all_markers_are_reported_not_just_the_first`
(`test_phrases.py:97-101`) does *not* cover this — it tests two **different**
markers in one utterance, not two occurrences of the **same** marker.

---

## Test-coverage verdict

`tests/unit/test_phrases.py` (121 lines) and `tests/data/utterances.yaml` would
have caught **none** of the five. The negation section
(`utterances.yaml:136-160`) only exercises single-clause negation with the
negator immediately adjacent. There are zero multi-clause utterances, zero
stutter/self-repair phrasings, zero "can't wake" forms, and zero non-"water"
drowning locations.

Per CLAUDE.md evidence discipline: the negation and coverage claims in this
module's docstring are **not currently backed by anything that runs**.

---

## Status and recommended order

| ID | Severity | Fix complexity | Recommended |
|---|---|---|---|
| F-001 | Critical | Medium — clause-scoped lookback + adjacency | **1st** |
| F-005 | Critical | Low — `search` → `finditer` | **2nd** |
| F-003 | High | Trivial — one alternative | 3rd |
| F-004 | High | Trivial — vocabulary list | 3rd |
| F-002 | Low | Low — contraction-aware prefix | 4th |

**Nothing here has been fixed.** `phrases.py` is untouched. Every defect is
pinned by an assertion in `agent/tests/audio/scenarios.yaml` recording the
*observed* behaviour, so the suite is green today and **fixing any defect will
break its assertion** — which is the signal to update the oracle. The tests are a
tripwire for the fix, not a nag.

Fixing F-001 and F-005 requires its own red-then-green cycle and an independent
review, and must not be bundled into the test-corpus change.
