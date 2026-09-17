"""Spike 1 -- real measurement of Moss retrieval accuracy against alpha.

This is a **spike**: it verifies the *vendor*, not our code. It therefore talks to the
`moss` SDK directly and imports nothing from `aiscelapeus` except `protocols` (the corpus
under test -- the thing being measured, not the code doing the measuring). Env loading is
re-implemented here on purpose: if this script imported `config.py`, a bug in our config
layer could silently change what gets measured.

Run:
    cd agent && python spikes/spike1_retrieval_accuracy.py

    --index NAME     index to measure (default: aiscelapeus-protocols-measure)
    --alphas a,b,c   alpha values (default: 0.7,0.8,1.0)
    --recreate       delete and rebuild the index before measuring
    --repeats N      query repeats per (alpha, query) for latency sampling (default: 3)
    --json PATH      also write the raw per-query results as JSON

METHODOLOGY TRAPS this script is written to avoid. Each one produced a *wrong published
number* in an earlier session; see `docs/evidence/2026-09-17-moss-retrieval-verified.md`.

1. **Rank 1 is `result.docs[0]`, as the SDK returns it.** Do not re-sort. An ascending
   `sorted(...)` by score selects the *worst* hit, and `max(d.id for d in docs)` selects the
   alphabetically-last id. Both produce a plausible wrong answer from a *correct* result set.
2. **`.score` is a Reciprocal Rank Fusion output -- it encodes RANK, not similarity.** It is
   reported here for completeness and is never thresholded, compared across queries, or
   called confidence.
3. **The model id is at `session._inner.model_id`**; the public `model_id` attribute does not
   exist, so `getattr(x, "model_id", None)` yields `None` and proves nothing.
4. **No fallback to a fake.** If Moss fails, this raises. A fabricated measurement is worse
   than a blocked one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from moss import DocumentInfo, MossClient, MutationOptions, QueryOptions

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from aiscelapeus.protocols import PROTOCOLS, as_documents  # noqa: E402

QUERY_SET = AGENT_DIR / "tests" / "data" / "retrieval_queries.yaml"

# Widest k we retrieve. Recall@5 and recall@8 both have to come out of one result set,
# and rank-of-correct-doc is only informative if we can see past k=8.
TOP_K = 20

# RET-C02 (BUILD-PLAN 8.5): these categories demand 100% top-1.
CRITICAL_CATEGORIES = frozenset({"cardiac", "airway", "haemorrhage", "drowning"})


# --------------------------------------------------------------------------- env


def load_credentials() -> dict[str, str]:
    """Resolve credentials the way the project does: repo root .env.local, then .env.

    Mirrors `aiscelapeus.config.read_env` precedence (real environment wins over the
    dotenv file) without importing it. No value is ever printed.
    """
    import os

    resolved: dict[str, str] = {}
    for candidate in (REPO_ROOT / ".env.local", REPO_ROOT / ".env"):
        if candidate.is_file():
            for key, value in dotenv_values(candidate).items():
                if value is not None:
                    resolved.setdefault(key, value)
    resolved.update({k: v for k, v in os.environ.items() if v is not None})

    missing = [k for k in ("MOSS_PROJECT_ID", "MOSS_PROJECT_KEY") if not resolved.get(k, "").strip()]
    if missing:
        raise SystemExit(
            f"BLOCKED: missing credentials {', '.join(missing)}. "
            f"Expected in {REPO_ROOT / '.env.local'} or {REPO_ROOT / '.env'}."
        )
    return resolved


# ------------------------------------------------------------------- query set


@dataclass(frozen=True)
class Labelled:
    text: str
    expect: str
    category: str
    critical: bool


def load_queries() -> list[Labelled]:
    raw = yaml.safe_load(QUERY_SET.read_text(encoding="utf-8"))
    out = [
        Labelled(
            text=q["text"],
            expect=q["expect"],
            category=q["category"],
            critical=bool(q.get("critical", False)),
        )
        for q in raw["queries"]
    ]

    # The query set is only meaningful if its labels are real doc ids and it covers the
    # corpus. A typo'd `expect` would otherwise read as a retrieval failure forever.
    corpus_ids = {p["id"] for p in PROTOCOLS}
    bad = sorted({q.expect for q in out} - corpus_ids)
    if bad:
        raise SystemExit(f"query set references ids not in the corpus: {bad}")
    uncovered = sorted(corpus_ids - {q.expect for q in out})
    if uncovered:
        raise SystemExit(f"query set has no query for protocols: {uncovered}")

    # RET-C02 is only enforceable if `critical` agrees with the corpus categories.
    by_id = {p["id"]: p for p in PROTOCOLS}
    for q in out:
        actual = by_id[q.expect]["category"]
        if q.category != actual:
            raise SystemExit(
                f"query for {q.expect} is labelled category {q.category!r} "
                f"but the corpus says {actual!r}"
            )
        if q.critical != (actual in CRITICAL_CATEGORIES):
            raise SystemExit(
                f"query for {q.expect} has critical={q.critical} but category "
                f"{actual!r} implies critical={actual in CRITICAL_CATEGORIES}"
            )
    return out


# ----------------------------------------------------------------------- index


async def ensure_index(client: MossClient, name: str, *, recreate: bool, model_id: str | None) -> str:
    """Build or reuse the measurement index, then report its real doc count."""
    existing = {ix.name: ix for ix in await client.list_indexes()}

    if recreate and name in existing:
        print(f"  deleting existing index {name}")
        await client.delete_index(name)
        existing.pop(name)

    docs = [
        DocumentInfo(
            id=d["id"],
            text=d["text"],
            metadata={k: str(v) for k, v in d["metadata"].items()},
        )
        for d in as_documents()
    ]

    if name in existing:
        await client.add_docs(name, docs, MutationOptions(upsert=True))
        print(f"  upserted {len(docs)} docs into existing index {name}")
    else:
        await client.create_index(name, docs, model_id) if model_id else await client.create_index(name, docs)
        print(f"  built index {name} with {len(docs)} docs (model_id={model_id or 'SDK default'})")

    await client.load_index(name)

    # Trust the server's count, not len(docs). If the index built from a stale corpus or
    # partially failed, the whole measurement is measuring something else.
    fresh = {ix.name: ix for ix in await client.list_indexes()}
    served = getattr(fresh.get(name), "doc_count", None)
    print(f"  index {name} loaded; server reports doc_count={served}")
    if served is not None and served != len(docs):
        raise SystemExit(
            f"BLOCKED: index {name} serves {served} docs but the corpus has {len(docs)}. "
            "Re-run with --recreate; measuring a mismatched index is meaningless."
        )
    return name


# ----------------------------------------------------------------- measurement


@dataclass
class QueryOutcome:
    query: str
    expect: str
    category: str
    critical: bool
    ranked_ids: list[str]
    rank: int | None  # 1-based rank of the expected doc; None if not in top-K
    top1_id: str
    top1_score: float
    wall_ms: list[float]
    moss_ms: list[int | None]


async def measure_alpha(
    client: MossClient, index: str, queries: list[Labelled], alpha: float, repeats: int
) -> list[QueryOutcome]:
    outcomes: list[QueryOutcome] = []
    options = QueryOptions(top_k=TOP_K, alpha=alpha)

    for q in queries:
        walls: list[float] = []
        mosses: list[int | None] = []
        ranked: list[str] = []

        for attempt in range(repeats):
            started = time.perf_counter()
            result = await client.query(index, q.text, options)
            walls.append((time.perf_counter() - started) * 1000)
            mosses.append(result.time_taken_ms)

            # TRAP 1: rank order is exactly as the SDK returns it. No sort, no max().
            ids = [d.id for d in result.docs]
            if attempt == 0:
                ranked = ids
                first_score = float(result.docs[0].score) if result.docs else float("nan")
            elif ids != ranked:
                # Nondeterministic ordering would invalidate a single-shot accuracy
                # number, so surface it rather than quietly averaging over it. Report the
                # DEPTH of the first divergence: observed instability in this index is
                # adjacent docs swapping deep in the tail, which cannot move top-1 or
                # recall@5/@8, and a message that only printed the top 3 would have made
                # a real tail-level effect look like a bug in this comparison.
                depth = next(
                    (i + 1 for i, (a, b) in enumerate(zip(ranked, ids)) if a != b), None
                )
                print(
                    f"    WARNING nondeterministic ranking at alpha={alpha} for "
                    f"{q.expect!r}: first divergence at rank {depth} "
                    f"(top-8 stable: {ranked[:8] == ids[:8]})"
                )

        outcomes.append(
            QueryOutcome(
                query=q.text,
                expect=q.expect,
                category=q.category,
                critical=q.critical,
                ranked_ids=ranked,
                rank=(ranked.index(q.expect) + 1) if q.expect in ranked else None,
                top1_id=ranked[0] if ranked else "",
                top1_score=first_score,
                wall_ms=walls,
                moss_ms=mosses,
            )
        )
    return outcomes


def recall_at(outcomes: list[QueryOutcome], k: int) -> int:
    return sum(1 for o in outcomes if o.rank is not None and o.rank <= k)


def summarise(outcomes: list[QueryOutcome]) -> dict[str, Any]:
    n = len(outcomes)
    crit = [o for o in outcomes if o.critical]
    walls = [w for o in outcomes for w in o.wall_ms]
    mosses = [m for o in outcomes for m in o.moss_ms if m is not None]
    return {
        "n": n,
        "top1": recall_at(outcomes, 1),
        "recall_5": recall_at(outcomes, 5),
        "recall_8": recall_at(outcomes, 8),
        "recall_20": recall_at(outcomes, TOP_K),
        "critical_n": len(crit),
        "critical_top1": recall_at(crit, 1),
        "p50_wall_ms": statistics.median(walls) if walls else None,
        "p100_wall_ms": max(walls) if walls else None,
        "p50_moss_ms": statistics.median(mosses) if mosses else None,
        "p100_moss_ms": max(mosses) if mosses else None,
    }


def pct(num: int, den: int) -> str:
    return f"{num}/{den} ({100.0 * num / den:.1f}%)" if den else "n/a"


# ------------------------------------------------------------------------ main


async def run(args: argparse.Namespace) -> int:
    creds = load_credentials()
    model_id = creds.get("MOSS_MODEL_ID", "").strip() or None
    queries = load_queries()
    alphas = [float(a) for a in args.alphas.split(",")]

    print("Spike 1 -- Moss retrieval accuracy vs alpha")
    print(f"  moss SDK        : {__import__('moss').__version__}")
    print(f"  corpus          : {len(PROTOCOLS)} protocols from aiscelapeus.protocols")
    print(f"  query set       : {len(queries)} labelled queries ({QUERY_SET.relative_to(REPO_ROOT)})")
    print(f"  critical queries: {sum(1 for q in queries if q.critical)}")
    print(f"  alphas          : {alphas}   top_k={TOP_K}   repeats={args.repeats}")
    print(f"  configured index: {creds.get('MOSS_PROTOCOLS_INDEX', '(unset)')}")
    print(f"  measuring index : {args.index}")
    print()

    client = MossClient(creds["MOSS_PROJECT_ID"], creds["MOSS_PROJECT_KEY"])
    index = await ensure_index(client, args.index, recreate=args.recreate, model_id=model_id)
    print()

    results: dict[float, list[QueryOutcome]] = {}
    for alpha in alphas:
        print(f"  querying at alpha={alpha} ...")
        results[alpha] = await measure_alpha(client, index, queries, alpha, args.repeats)

    # ------------------------------------------------------------ per-alpha table
    print()
    print("=" * 100)
    print("PER-ALPHA ACCURACY")
    print("=" * 100)
    header = f"{'alpha':>6} | {'top-1':>14} | {'recall@5':>14} | {'recall@8':>14} | {'critical top-1':>16} | RET-C02"
    print(header)
    print("-" * len(header))
    for alpha in alphas:
        s = summarise(results[alpha])
        c02 = "PASS" if s["critical_top1"] == s["critical_n"] else "FAIL"
        print(
            f"{alpha:>6} | {pct(s['top1'], s['n']):>14} | {pct(s['recall_5'], s['n']):>14} | "
            f"{pct(s['recall_8'], s['n']):>14} | {pct(s['critical_top1'], s['critical_n']):>16} | {c02}"
        )

    # --------------------------------------------------------------- latency table
    print()
    print("=" * 100)
    print("PER-ALPHA LATENCY  (wall = in-process client call; moss = SDK's own time_taken_ms)")
    print("=" * 100)
    lheader = f"{'alpha':>6} | {'p50 wall ms':>12} | {'p100 wall ms':>13} | {'p50 moss ms':>12} | {'p100 moss ms':>13} | samples"
    print(lheader)
    print("-" * len(lheader))
    for alpha in alphas:
        s = summarise(results[alpha])
        print(
            f"{alpha:>6} | {s['p50_wall_ms']:>12.3f} | {s['p100_wall_ms']:>13.3f} | "
            f"{str(s['p50_moss_ms']):>12} | {str(s['p100_moss_ms']):>13} | "
            f"{len(queries) * args.repeats}"
        )

    # ------------------------------------------------------- per-query rank detail
    print()
    print("=" * 100)
    print("PER-QUERY RANK OF THE CORRECT DOC  ('-' = not in top-%d)" % TOP_K)
    print("=" * 100)
    qhead = f"{'expected doc':<24} {'cat':<14} {'crit':<5}" + "".join(f"{'a=' + str(a):>8}" for a in alphas)
    print(qhead)
    print("-" * len(qhead))
    for i, q in enumerate(queries):
        ranks = "".join(
            f"{(str(results[a][i].rank) if results[a][i].rank is not None else '-'):>8}" for a in alphas
        )
        print(f"{q.expect:<24} {q.category:<14} {('YES' if q.critical else ''):<5}{ranks}")

    # -------------------------------------------------------------- failure detail
    print()
    print("=" * 100)
    print("TOP-1 MISSES (what was returned instead)")
    print("=" * 100)
    for alpha in alphas:
        misses = [o for o in results[alpha] if o.rank != 1]
        print(f"\nalpha={alpha}: {len(misses)} miss(es)")
        for o in misses:
            flag = "  [CRITICAL]" if o.critical else ""
            print(f"  expected {o.expect:<24} got {o.top1_id:<24} correct at rank {o.rank}{flag}")
            print(f"    query: {o.query[:90]}")

    if args.json:
        payload = {
            "moss_sdk": __import__("moss").__version__,
            "index": index,
            "model_id": model_id,
            "corpus_size": len(PROTOCOLS),
            "top_k": TOP_K,
            "repeats": args.repeats,
            "alphas": {
                str(a): {
                    "summary": summarise(results[a]),
                    "queries": [
                        {
                            "expect": o.expect,
                            "category": o.category,
                            "critical": o.critical,
                            "query": o.query,
                            "rank": o.rank,
                            "top1_id": o.top1_id,
                            "top1_rrf_score": o.top1_score,
                            "ranked_ids": o.ranked_ids[:8],
                        }
                        for o in results[a]
                    ],
                }
                for a in alphas
            },
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nraw results written to {args.json}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--index", default="aiscelapeus-protocols-measure")
    p.add_argument("--alphas", default="0.7,0.8,1.0")
    p.add_argument("--recreate", action="store_true")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--json", default=None)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
