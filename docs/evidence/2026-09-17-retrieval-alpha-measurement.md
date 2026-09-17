# Evidence — Moss retrieval accuracy vs `alpha`, re-measured and reproducible

**Date:** 17 Sept 2026
**Subsystem:** S4 (retrieval correctness re-measurement), per `docs/BUILD-PLAN.md` §8.4
**Scope:** read-only with respect to `agent/aiscelapeus/`. No project code was modified. Nothing committed.
**Harness:** `agent/spikes/spike1_retrieval_accuracy.py` (new)
**Query set:** `agent/tests/data/retrieval_queries.yaml` (new — this is what satisfies **RET-C04**)
**Independent review:** `feature-dev:code-reviewer`, per `CLAUDE.md` §3. Findings in §6; one was
material and **changed the headline numbers before publication**.
**Builds on:** `docs/evidence/2026-09-17-moss-retrieval-verified.md`, whose four methodology
traps this harness is written to avoid.

---

## Headline: BUILD-PLAN §8's central recommendation is not supported

`docs/BUILD-PLAN.md` §8.2 item 1 is **"`alpha` 0.8 → 1.0, measured effect 52 % → 64 % top-1"**,
called "the cheapest real win first" and worth "12 points". §8.4 directs it be done before
anything is built on top.

**On a reproducible 20-query set over the real 20-document corpus, that gain does not exist.
`alpha=1.0` is not better than `0.8`. It is measurably worse.**

| | BUILD-PLAN §8 claim | This measurement |
|---|---|---|
| top-1 @ alpha 0.8 | 52 % | **70 %** |
| top-1 @ alpha 1.0 | 64 % | **65 %** |
| **effect of 0.8 → 1.0** | **+12 points** | **−5 points** |
| recall@5 | 88 % | 100 % |
| recall@8 | 96 % | 100 % |

The recommended change is a **one-query regression**, not a 12-point win. Acting on §8.2 item 1
as written would have moved `moss_context.py:199` the wrong way, then credited a later re-rank
layer with recovering ground the config change had itself lost.

**The alpha comparison is the trustworthy part of this document** — it is internal to one run,
one set, one index, so query-set bias cannot produce it. **The absolute 70 % is the fragile
part.** See §5, which is the section that most constrains trust here.

---

## 1. What was measured

| Parameter | Value |
|---|---|
| Date of run | 17 Sept 2026 |
| `moss` SDK version | **1.12.0** (`moss.__version__`, read at runtime) |
| Embedding model | `moss-minilm` (from `MOSS_MODEL_ID`) |
| Corpus | **20 documents**, built from `agent/aiscelapeus/protocols.py` via `as_documents()` |
| Index | `aiscelapeus-protocols-measure`, built by the harness; server-confirmed `doc_count=20` |
| Query set | **20 labelled queries**, one per protocol |
| Critical queries | **8** (cardiac ×3, haemorrhage ×2, airway ×2, drowning ×1) |
| `top_k` at query time | 20 (so rank-of-correct-doc is visible past k=8) |
| Alphas | 0.7, 0.8, 1.0 |
| Stability | **4 independent full passes per alpha; every headline figure identical in all 4** |

```bash
cd agent && python spikes/spike1_retrieval_accuracy.py --recreate
```

### 1.1 A blocker found on the way, which matters more than the numbers

**The index named by `MOSS_PROTOCOLS_INDEX` (`aiscelapeus-protocols`) does not exist in Moss
Cloud.** `load_index` fails with `INDEX_NOT_FOUND` / HTTP 404. The only index in the project is
`aiscelapeus-protocols-spike`, holding **5 documents** — the leftover corpus from the original
(since-refuted) Spike 1, with ids `cpr-adult`, `bleeding`, `choking`, `anaphylaxis`, `drowning`.

Two consequences:

1. **`EmergencyContext.connect()` cannot currently succeed against real Moss.** Any claim that
   the end-to-end voice path works is, as of this run, unproven against live infrastructure.
2. **The 20-doc index that the prior evidence file reports measuring is not present in the
   project now.** It was built under another name, built transiently and deleted, or built in a
   different project. This is itself a reason the 52 %/64 % figures cannot be checked directly.

The harness therefore builds its own index under a **distinct** name
(`aiscelapeus-protocols-measure`) and never touches the configured name. Fixing the missing
index means changing `agent/aiscelapeus/` and is out of scope for S4.

---

## 2. Per-alpha results

20 queries over 20 documents. Identical across 4 independent full passes — not single-shot.

| alpha | top-1 | recall@5 | recall@8 | critical top-1 | **RET-C02** |
|---|---|---|---|---|---|
| 0.7 *(used by `recall()`)* | 12/20 (**60.0 %**) | 20/20 (100 %) | 20/20 (100 %) | 4/8 (50.0 %) | **FAIL** |
| **0.8** *(current protocol default)* | 14/20 (**70.0 %**) | 20/20 (100 %) | 20/20 (100 %) | 5/8 (62.5 %) | **FAIL** |
| 1.0 *(§8.2 recommends this)* | 13/20 (**65.0 %**) | 20/20 (100 %) | 20/20 (100 %) | 5/8 (62.5 %) | **FAIL** |

Percentages are exact `n/20`, not rounded favourably.

### 2.1 Acceptance criteria (BUILD-PLAN §8.5)

| ID | Criterion | Verdict at the best alpha (0.8) |
|---|---|---|
| RET-C01 | ≥ 18/20 top-1 | **FAIL** — 14/20. Four short. |
| RET-C02 | **100 %** top-1 on cardiac / airway / haemorrhage / drowning | **FAIL** — 5/8. Fails at every alpha tested. |
| RET-C03 | Wrong protocol is a test failure, not a warning | **NOT MET** — no test asserts this. The harness is a spike: it reports, it does not fail a suite. |
| RET-C04 | Query set lives in the repo, runs in CI against a fake and on demand against real Moss | **HALF MET** — the set is in the repo and runs on demand against real Moss. **No CI wiring and no fake was added.** |

**RET-C02 fails at every alpha.** BUILD-PLAN calls it "the one that matters", and it is the
criterion no alpha value rescues. The three critical failures at alpha 0.8:

- **`bls-adult-cpr`** — *"he just collapsed and he's not waking up and I don't think he's
  breathing what do I do"* → returns **`drowning-rescue`**; correct doc at **rank 5**.
- **`bleed-pressure-packing`** — *"the wound is right up in his armpit and it keeps filling with
  blood I can't get a strap round it"* → returns **`minor-wound`**; correct doc at **rank 3**.
- **`airway-choking-adult`** — *"she's got food stuck, no sound coming out at all now, can't get
  any air in and she's going blue"* → returns **`drowning-rescue`**; correct doc at **rank 2**.

The first is precisely the failure mode BUILD-PLAN §8.5 says the system exists to prevent: an
unresponsive, non-breathing adult routed to the drowning protocol, which prescribes 5 rescue
breaths *before* compressions. That is wrong for a primary cardiac arrest, and the B4 output
gate cannot catch it — the citation is well-formed and points at the wrong document.

Note the pattern: **`drowning-rescue` is a magnet**, winning two of the three critical misses.

### 2.2 Per-query rank of the correct document

| expected doc | category | critical | a=0.7 | a=0.8 | a=1.0 |
|---|---|:---:|---:|---:|---:|
| bls-adult-cpr | cardiac | YES | 4 | **5** | **5** |
| bls-aed | cardiac | YES | 1 | 1 | 1 |
| chest-pain-cardiac | cardiac | YES | 1 | 1 | 1 |
| bleed-tourniquet | haemorrhage | YES | 1 | 1 | 1 |
| bleed-pressure-packing | haemorrhage | YES | 3 | **3** | **3** |
| airway-choking-adult | airway | YES | 2 | **2** | **4** |
| airway-recovery-position | airway | YES | 1 | 1 | 1 |
| drowning-rescue | drowning | YES | **2** | 1 | 1 |
| shock-management | circulation | | 4 | 4 | 5 |
| burns-thermal | burns | | 1 | 1 | 1 |
| fracture-immobilise | trauma | | 2 | 1 | 1 |
| spinal-precaution | trauma | | 1 | 1 | 1 |
| anaphylaxis | allergy | | 1 | 1 | 1 |
| seizure | neuro | | 3 | 3 | 3 |
| stroke-fast | neuro | | 1 | 1 | 1 |
| hypoglycaemia | metabolic | | 1 | 1 | 1 |
| minor-wound | wounds | | 1 | 1 | 1 |
| heat-stroke | environmental | | 4 | 3 | 2 |
| hypothermia | environmental | | 1 | 1 | 1 |
| scene-safety | general | | 1 | 1 | **2** |

Where alpha 1.0 loses against 0.8: `airway-choking-adult` 2 → 4 (**a critical query, moving
away from rank 1**), `shock-management` 4 → 5, `scene-safety` 1 → 2. Where it gains: `heat-stroke`
3 → 2, which changes no top-1. **Alpha 1.0 does not fix a single top-1 miss that 0.8 gets wrong,
and it degrades one critical query.** The only critical query alpha helps at all is
`drowning-rescue` (2 → 1 from 0.7 to 0.8), which argues for keeping 0.8 — not for moving to 1.0.

### 2.3 The structural finding survives, and it is the one to build on

Worst observed rank of the correct document across all 20 queries:

| alpha | worst rank |
|---|---|
| 0.7 | 4 |
| **0.8** | **5** |
| 1.0 | 5 |

**The correct protocol was inside the top 5 for every query at every alpha.** BUILD-PLAN §8.3's
"retrieve wide, then re-rank once" is therefore *better* supported by this measurement than by
the one it was based on: the residual ~4 % that §8.3's fallback multi-pull exists to catch did
not appear here at all.

**Do not read recall@5 = 100 % as comfortable.** It passes by exactly one rank — one query sits
at rank 5 of 5, zero headroom. A retrieve-at-5 design has no margin. **Top-8 is the defensible
width**, and even that rests on a 20-query set over a 20-doc corpus (§5).

---

## 3. Latency

Two clocks, because they measure different things:

- **wall** — wall-clock around `await client.query(...)`: what the agent process actually waits
  for, and therefore what NFR-001's 10 ms budget must be judged against.
- **moss** — the SDK's self-reported `SearchResult.time_taken_ms`: search time excluding the
  SDK's own call overhead.

Harness run, 3 repeats × 20 queries = 60 samples per alpha:

| alpha | p50 wall ms | p100 wall ms | p50 moss ms | p100 moss ms |
|---|---|---|---|---|
| 0.7 | 11.656 | 16.696 | 0 | 1 |
| 0.8 | 11.778 | 15.839 | 0 | 1 |
| 1.0 | 11.862 | 18.337 | 0 | 1 |

A separate, more careful probe — **30 warm-up calls discarded**, then 10 samples × 20 queries =
200 samples, alpha 0.8:

| top_k | p50 | p95 | p100 | min |
|---|---|---|---|---|
| **3** *(production default, `MOSS_PROTOCOL_TOP_K`)* | **12.021** | 16.849 | 28.588 | 8.841 |
| 8 | 12.930 | 17.948 | 21.828 | 9.017 |
| 20 | 13.186 | 18.136 | 22.792 | 9.455 |

### 3.1 The 10 ms budget is not met, and "p100 7.42 ms" did not reproduce

BUILD-PLAN §8 opens with *"Spike 1 measured Moss at p100 7.42 ms (budget PASS)"*.

**Measured here: p50 ≈ 12 ms, p100 ≈ 29 ms at the production `top_k=3`, after warm-up.** Not one
of 200 warm samples came in under 8.8 ms.

This is **not** a cold-start or `top_k` artifact: 30 calls were discarded before sampling, and
`top_k=3` is the *fastest* of the three widths. The gap between `moss_ms` (0–1 ms) and wall
(≈12 ms) locates the cost in **SDK/client call overhead, not in search itself** — a real cost
the agent pays on every lookup, so it counts against the budget.

**Comparability caveat:** measured on one Windows developer machine against an index loaded into
that process. BUILD-PLAN's 7.42 ms may have used a different machine, `top_k`, or clock —
plausibly `moss_ms` rather than wall, which would reconcile the two figures, since `moss_ms` here
is 0–1 ms. I did not reproduce that run's conditions and cannot say which clock it used. What I
can say: **under the clock NFR-001 cares about, the budget fails by ~1.2× at p50 and ~2.9× at
p100.**

Per §8.1 the budget is explicitly **not a gate** this phase, so this is a finding, not a blocker.

---

## 4. Methodology — the four traps, and how each is structurally avoided

`docs/evidence/2026-09-17-moss-retrieval-verified.md` documents four mistakes that produced
published-but-wrong numbers. Handled structurally, not by assertion:

| Trap | Handling |
|---|---|
| Ascending score-sort, or `max(d.id ...)`, picks the wrong doc from a *correct* result set | Rank 1 is `result.docs[0]` **exactly as the SDK returns it**. The harness contains no `sorted()` and no `max()` over results. Rank is `ranked_ids.index(expect) + 1`. |
| `.score` is an RRF (rank-derived) output, not similarity | Recorded for completeness only. Never thresholded, never compared across queries, never called confidence. **No metric in this document depends on a score value.** |
| `session.model_id` does not exist; `getattr` yields `None` and proves nothing | Not relied on. Model comes from `MOSS_MODEL_ID`; the index's real `doc_count` is read back from the server. |
| Falling back to a fake and presenting it as real | **No fake path exists.** Missing credentials → `SystemExit`; Moss failures propagate. The `INDEX_NOT_FOUND` in §1.1 was reported, not worked around. |

Additional guards, each blocking a way a silent error could masquerade as a retrieval result:

- **Label validation** — every `expect` must be a real corpus id and every protocol must have a
  query. A typo'd label would otherwise read as a permanent retrieval failure.
- **`critical` cross-checked against the corpus** — a query's `category` must match the
  protocol's own, and `critical` must agree with the RET-C02 category set. **RET-C02 cannot be
  gamed by mislabelling a critical query as ordinary.**
- **Server-side doc count** — `doc_count` from `list_indexes()` is compared to the corpus size;
  a mismatch aborts rather than measuring a stale or partial index.

### 4.1 Nondeterminism in the SDK's ranking — disclosed

Repeated identical queries **do not always return identical orderings**. Observed: two adjacent
documents swapping, first divergence at **rank 4** and **rank 8** in the cases seen.

Bounding it: across 4–5 independent full passes per alpha, **every headline number was
identical** — top-1, recall@5, recall@8 and critical top-1 all unchanged.

**The caveat to keep:** divergence *at rank 8* was observed, so recall@8 is not provably immune;
it merely never moved. Top-1 and recall@5 were never affected. Run multiple passes before
treating a recall@8 delta as signal. The harness prints a warning with the divergence depth
whenever it sees this.

---

## 5. The caveat that most constrains trust in these numbers

**The prior 52 %/64 % and this 70 %/65 % were measured on different query sets, and the older
set is not in the repo.** That is exactly why RET-C04 exists, and why this document ships a
query set rather than only numbers.

| | Prior measurement | This measurement |
|---|---|---|
| Queries | 25, split "easy"/"hard" | **20, one per protocol** |
| Query set available? | **No** — never committed | **Yes** — `agent/tests/data/retrieval_queries.yaml` |
| Index | a 20-doc index not present in the project now | `aiscelapeus-protocols-measure`, built from `protocols.py` at run time |

**A 5-point top-1 difference is well inside what query phrasing alone can explain** — and §6
below shows that on this very set, a phrasing change moved top-1 by exactly 5 points. So:

- The claim **"§8's 52 % → 64 % gain does not reproduce"** is **well supported.** It rests on
  0.8 and 1.0 compared *on the same set, in the same run, against the same index* — an internal
  comparison immune to query-set bias. The effect is reversed in direction, not merely smaller.
- The claim **"retrieval is better than previously thought (70 % vs 64 %)"** is **NOT
  supported** and must not be quoted as an improvement. The two absolute numbers are not
  comparable instruments. **Treat 70 % as this set's figure and nothing more.**
- **RET-C01 and RET-C02 fail regardless.** No reading of the caveat rescues them: 14/20 is far
  short of 18/20, and three critical protocols are not at rank 1.

A reviewer may reasonably dispute two of my labels; neither affects the RET-C02 verdict:

- **`bleed-pressure-packing`** for the armpit query. The corpus text names the armpit as a site
  where a tourniquet cannot go, so the label is right — but `bleed-tourniquet` (returned at
  alpha 0.7) is a *plausible* retrieval, not an absurd one. Note also that "armpit" appears
  verbatim in three corpus documents (`bleed-pressure-packing`, `bls-aed`, `heat-stroke`), so
  this query is lexically ambiguous by corpus construction.
- **`scene-safety`** for the downed-power-lines query — arguably also a trauma query.

---

## 6. Independent review, and what it changed

Per `CLAUDE.md` §3 an independent `feature-dev:code-reviewer` pass was run on the harness and
query set, briefed to be adversarial toward the numbers. **It found a material defect, and the
headline figures in this document are the post-fix ones.**

**Acted on — query-set vocabulary leakage (the review's finding #1).** The reviewer audited all
20 queries against `protocols.py` and identified six sharing distinctive protocol content words,
contrary to the YAML's own stated colloquial-only rule — the exact mechanism that inflated an
earlier measurement from 64 % to 80 %.

I made this testable rather than arguable by computing, for every query, the words shared with
its target document that appear in **no other document** in the corpus — unique-to-target
giveaways. That found **9 such words across 8 queries**: `crushing`, `silent`, `clammy`, `pale`,
`straighten`, `lips`, `shaky`, `dirty`, `shivering`. I rewrote those 8 queries to preserve the
clinical presentation while removing the giveaway (e.g. "crushing" → "a big weight pressing
down"; "shivering" → "shaking uncontrollably"), then re-verified: **0 unique-to-target words
remain.** Re-measuring on the harder set:

| | leaky set (first run) | de-leaked set (published) |
|---|---|---|
| top-1 @ 0.8 | 15/20 (75.0 %) | **14/20 (70.0 %)** |
| critical top-1 @ 0.8 | 6/8 | **5/8** |
| effect of 0.8 → 1.0 | −5 points | **−5 points** |

**This is the most useful thing the review produced**: it puts a number on query-set
sensitivity. Five points and one critical query hung on eight word choices — which is why §5
insists the absolute figure is not comparable to the prior 64 %. It also **strengthens** the
headline: the alpha effect was −5 points on *both* sets, so the finding that 1.0 is worse than
0.8 survived a deliberate hardening of the instrument.

**Acted on — misleading nondeterminism warning.** My first version printed only the top 3 ids
when flagging instability, so a real tail-level divergence looked like a bug in the comparison.
It now reports the divergence *depth* (§4.1).

**Noted, not acted on:**

- **`recall_20` is near-tautological** on a 20-doc corpus with `top_k=20` — it asks "did Moss
  return the document at all". Correct. It is computed but deliberately **not** reported as a
  headline metric here; `top_k=20` exists so rank-of-correct-doc is visible past 8. Anyone
  citing "recall@20 = 100 %" as quality evidence is misreading it.
- **Recall@5/@8 are easier on a 20-doc corpus** than they would be at realistic scale (top-5 is
  25 % of the corpus). Agreed, and it is why §2.3 warns against reading 100 % as comfortable.
  Cannot be fixed without a larger corpus, which is §8.2 item 4's territory.
- **"Collapsed, not breathing" → `drowning-rescue` is partly corpus ambiguity, not purely an
  embedder defect.** Agreed — both documents describe an unresponsive non-breathing patient,
  differing only by a context clause. I have kept it scored as a miss because *the responder's
  utterance contains no water context*, so ranking drowning first is wrong in the field
  regardless of why. But the reviewer is right that this points at **corpus** work (§8.2 item 4)
  rather than at a retrieval parameter.
- **Comparing to BUILD-PLAN at all is questionable** given different instruments. Agreed and
  adopted: §5 now states plainly that only the *within-run* alpha comparison is trustworthy.
- **Warm-up not controlled in the harness itself.** True; the harness reports raw numbers with
  `repeats=3`. The warmed 200-sample probe in §3 exists precisely to cover this, and both are
  reported separately rather than merged.

**Reviewer items I verified as clean:** none of the four documented traps is committed; the
rank/recall arithmetic is correct; `first_score`/`ranked` are always bound before use (`attempt
== 0` always runs first) and empty `result.docs` is handled; the `ensure_index` ternary works as
intended.

---

## 7. What this changes

| BUILD-PLAN §8 item | Status after this measurement |
|---|---|
| §8.2 item 1 — `alpha` 0.8 → 1.0 for +12 points | **Not supported. Do not make this change.** Measured at −5 points on two independent phrasings of the set, and it degrades a *critical* query (`airway-choking-adult`, rank 2 → 4). `alpha=0.8` at `moss_context.py:199` is already the best of the three tested. |
| §8.2 item 2 — retrieve top-5/8, LLM re-ranks | **Better supported than before.** Correct doc within top-5 on 20/20 queries at every alpha. Use **top-8, not top-5**: rank 5 was hit with zero headroom. |
| §8.2 item 3 — audit `.score` as confidence | **Unchanged and still correct.** Re-confirmed: scores are RRF rank artifacts, not similarity. |
| §8.2 item 4 — corpus augmentation with a symptom line | **Still unmeasured — and now the most promising lever.** All three critical misses are symptom-phrased queries losing to a topically-adjacent document, which is exactly what item 4 targets. `drowning-rescue` acts as a magnet for two of them. |
| §8.3 fallback "bounded multi-pull" for the residual ~4 % | **No residual observed on this set.** Do not build it yet; there is currently nothing for it to catch. |
| §8 opening "p100 7.42 ms, budget PASS" | **Did not reproduce.** p50 ≈ 12 ms, p100 ≈ 29 ms wall at production `top_k=3`, warmed. Possibly a `moss_ms`-vs-wall mix-up (§3.1). |
| RET-C04 | **Half met** — query set is in the repo and runs on demand against real Moss. CI-against-a-fake still to do. |
| New finding | **`MOSS_PROTOCOLS_INDEX` does not exist in Moss Cloud** (§1.1). Blocks `connect()` against real infrastructure. |

### 7.1 Recommended next step

**RET-C02 is the gate and no alpha value passes it.** The three failures are stable, specific and
diagnosable: each is a symptom-phrased query losing to a topically-adjacent protocol
(`bls-adult-cpr` → `drowning-rescue`, `airway-choking-adult` → `drowning-rescue`,
`bleed-pressure-packing` → `minor-wound`). That is the within-topic discrimination weakness the
prior evidence file identified as root cause, and `alpha` demonstrably does not touch it.

So: **skip §8.2 item 1 entirely** (it is a regression), and go to **§8.2 item 4 — corpus
augmentation, a symptom/presentation line per protocol — measured against this now-committed
query set**, with the re-rank layer (§8.2 item 2, at **top-8**) as the structural fix. Re-run
`agent/spikes/spike1_retrieval_accuracy.py` after any corpus change; that is what it is for.

Also worth closing: **build the missing `aiscelapeus-protocols` index** (§1.1), and **wire the
query set into CI against a fake** to finish RET-C04 and satisfy RET-C03.

---

## 8. Honesty notes

- **Every number here came from an executed run** against real Moss Cloud with real credentials.
  Nothing is projected, estimated, or carried over from another document.
- **No value is rounded favourably.** Percentages are exact `n/20`.
- **The published numbers are the *worse*, post-review ones.** The first run gave 75 % top-1 and
  6/8 critical; hardening the query set against vocabulary leakage cut that to 70 % and 5/8, and
  those are the figures above. The more flattering numbers are shown in §6 only to quantify
  query-set sensitivity.
- **Unmeasured things are labelled unmeasured:** corpus augmentation (§8.2 item 4),
  `moss-mediumlm` (not re-tested — the prior file's "it ties" finding is neither confirmed nor
  refuted here), and CI-against-a-fake.
- **I wrote both the query set and the harness that scores it.** That is a real conflict of
  interest, partially mitigated by the independent review in §6 and by the leakage metric being
  mechanical rather than a judgement. It is not fully mitigated. The next reviewer should
  re-audit `retrieval_queries.yaml` phrasing directly.
- **The headline contradicts this project's own plan**, produced by a harness and query set now
  both in the repo, so the next session can overturn *this* document the same way. The multi-pass
  stability checks and §5 exist because a single run quoted as fact is the failure mode this
  project already has twice on record.
- **`agent/aiscelapeus/` was not modified. Nothing was committed.**
