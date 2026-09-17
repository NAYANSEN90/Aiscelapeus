# Evidence — Moss retrieval, independently verified

**Date:** 17 Sept 2026
**Method:** independent verification subagent, per `CLAUDE.md` §1.3. Briefed with the prior
claim and instructed to be adversarial toward it *and* toward its own findings.
**Supersedes:** `2026-09-16-tool-verification.md` §3.3–3.7
**Scope:** read-only probes against real Moss; no project code modified.

---

## Verdict: the prior claim was half right, for entirely the wrong reasons

Retrieval quality **is** mediocre. Every piece of diagnostic evidence offered for *why* was
a misreading, and the headline reproduction was false.

---

## 1. What was refuted

### 1.1 "not breathing no pulse returns drowning" — does not reproduce

On the real 20-document corpus the query returns **`bls-adult-cpr` at rank 1** (drowning is
rank 2). A rebuilt 5-document corpus *also* ranks `bls-adult-cpr` first, at both alpha 1.0
and 0.8. The reported symptom could not be reproduced under any configuration.

**Probable cause — a bug in the original probe, not in Moss.** On that 5-doc result,
`max(d.id for d in r.docs)` evaluates to exactly `drowning-rescue` (alphabetically last),
and an ascending score-sort — `sorted(...)` without `reverse=True` — selects the *worst*
match. Either produces precisely the reported output from a correct result set.

### 1.2 "Scores are quantised, so embeddings are dead" — wrong

The scores are **Reciprocal Rank Fusion** outputs. They encode **rank, not similarity**.

Decisive test: a corpus containing a *verbatim copy of the query* plus four unrelated noise
documents, and separately a corpus of five *near-identical clones*, produced a
**byte-identical** score ladder:

```
[1.0, 0.96875, 0.9393939971923828, 0.9117647409439087, 0.8857142925262451]
```

The ladder is `62/(62+k)` renormalised and lengthens with corpus size. **`0.775` is simply
the 10th rung.**

> **Consequence — this is the most actionable finding in the document.** `.score` is
> **useless as a confidence signal**. Any relevance threshold on it is meaningless.
> **PE-006 specifies exactly such a "score floor"** and would have been built on it — a
> safety feature that looks like it works and cannot.

### 1.3 "alpha has no effect" — wrong

`alpha` reorders results correctly on the real corpus. `"not breathing no pulse"`:

| alpha | ranking |
|---|---|
| 1.0 | `[cpr, drowning, choking]` |
| 0.5 | `[drowning, cpr, shock]` |
| 0.0 | `[shock, aed, drowning]` |

`alpha=0.0` (keyword-only) returns zero results only when no query token appears literally
in any document — correct behaviour, confirmed: `"windlass"` → 1 hit, `"zzzznotaword"` → 0.

The original test used a corpus too small for ordering to change.

### 1.4 "session.model_id is None" — wrong

The SDK stores it as private `_model_id`; **the public attribute does not exist**, so
`getattr` yields `None` regardless. The real value is at **`session._inner.model_id`**,
which correctly reports `moss-minilm` by default and `moss-mediumlm` when passed.

### 1.5 Embeddings are definitively live

On an adversarial corpus where lexical and semantic signals oppose each other,
`"cardiac emergency"` — **zero word overlap** — correctly ranks the myocardial-infarction
document first. Ranking is also stable under shuffled insertion order (10/10).

---

## 2. What holds: quality is genuinely poor

25 labelled queries, real 20-document corpus:

| Model | alpha | easy t1 | hard t1 | **all t1** | all t3 |
|---|---|---|---|---|---|
| minilm | **1.0** | 12/15 | 4/10 | **16/25 (64 %)** | 19/25 (76 %) |
| minilm | 0.8 *(current default)* | 9/15 | 4/10 | 13/25 (52 %) | 18/25 (72 %) |
| mediumlm | **1.0** | 10/15 | 6/10 | **16/25 (64 %)** | 20/25 (80 %) |

**The verifier was adversarial toward its own result:** a first pass showed 80 % top-1, but
the query phrasing leaked protocol vocabulary. On colloquial paraphrases with zero verified
content-word overlap, top-1 falls to 40 %. **The honest figure is ~64 %**, against a 5 %
random-guess floor.

### 2.1 Root cause

The embedder **clusters by broad topic but discriminates weakly within a topic**.
Illustration: `"puppy"` ranks *"The cat purred"* above *"The dog barked"*.

On a corpus where all 20 documents are "medical emergency", fine within-topic distinction
is exactly what is required and exactly what the model is weakest at. `moss-mediumlm` does
not fix this — both tie at 64 % top-1; mediumlm wins marginally on top-3 (80 % vs 76 %).

### 2.2 The number that determines the design

| Metric | Value |
|---|---|
| Top-1 | 64 % |
| **Recall@5** | **88 %** |
| **Recall@8** | **96 %** |

**The right protocol is almost always in the candidate set — just not first.** This is what
makes retrieve-wide-then-re-rank the correct architecture, rather than iterative
re-querying.

---

## 3. Recommendations

| # | Action | Effect | Where |
|---|---|---|---|
| 1 | **`alpha` 0.8 → 1.0** | 52 % → 64 % top-1 | `moss_context.py:199`, `:226` (which uses 0.7) |
| 2 | **Retrieve top-5/8, LLM re-ranks** | 64 % → 88–96 % coverage | agent tool layer |
| 3 | **Audit every use of `.score` as confidence** | removes a meaningless safety signal | PE-006, anywhere thresholding |
| 4 | **Keep the title prefix** in `as_documents()` | dropping it costs top-1 (4/10 → 3/10) | `protocols.py:295` — do not "clean up" |

**Do not replace Moss.** The "embeddings aren't engaging" diagnosis was wrong and would
have sent the project chasing a non-bug.

**64 % top-1 is not demo-ready for a safety-critical triage path on its own.** The top-5 +
re-rank design is what makes it viable.

---

## 4. Process note

This is the second time an independent pass has overturned a confident conclusion in this
project (previously `set_level` and the `last_trace` race). The rule in `CLAUDE.md` §1.3
exists because of that pattern, and it paid for itself here: acting on the original
diagnosis would have meant re-indexing the corpus, switching embedding models, or replacing
Moss — none of which addresses the real defect, and all of which would have consumed a day.
