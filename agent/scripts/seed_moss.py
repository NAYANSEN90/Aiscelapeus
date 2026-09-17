"""Build (or refresh) the Moss protocol index.

    python scripts/seed_moss.py            # upsert into the existing index
    python scripts/seed_moss.py --recreate # delete and rebuild from scratch
    python scripts/seed_moss.py --check    # load the index and run sample queries

Run this once before starting the agent for the first time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiscelapeus.config import MossConfig, _Env, read_env  # noqa: E402
from aiscelapeus.moss_context import seed_protocol_index  # noqa: E402
from aiscelapeus.protocols import PROTOCOLS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("seed")

SAMPLE_QUERIES = [
    "he's not breathing, what do I do",
    "blood won't stop coming out of his leg",
    "she's choking and can't speak",
    "kid pulled out of the pool, not moving",
    "small cut on the hand",
    "he's confused and one side of his face is drooping",
]


async def check(config: MossConfig) -> None:
    from moss import MossClient, QueryOptions

    client = MossClient(config.project_id, config.project_key)
    started = time.perf_counter()
    await client.load_index(config.protocols_index)
    logger.info("Index loaded in %.0fms", (time.perf_counter() - started) * 1000)

    worst = 0.0
    for query in SAMPLE_QUERIES:
        started = time.perf_counter()
        result = await client.query(config.protocols_index, query, QueryOptions(top_k=2, alpha=0.8))
        wall_ms = (time.perf_counter() - started) * 1000
        worst = max(worst, wall_ms)
        top = result.docs[0] if result.docs else None
        logger.info(
            "%6.2fms (moss %sms)  %-45s -> %s",
            wall_ms,
            result.time_taken_ms,
            query[:45],
            (top.metadata or {}).get("title", top.id) if top else "NO MATCH",
        )

    verdict = "PASS" if worst <= config.latency_budget_ms else "OVER BUDGET"
    logger.info("Worst-case retrieval %.2fms against a %.0fms budget: %s",
                worst, config.latency_budget_ms, verdict)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Aiscelapeus protocol index")
    parser.add_argument("--recreate", action="store_true", help="delete and rebuild the index")
    parser.add_argument("--check", action="store_true", help="only run latency checks")
    args = parser.parse_args()

    # Reading the environment is an explicit argument now (see config.py). This
    # script still called the old no-argument `MossConfig.from_env()` and had
    # been broken since that refactor -- which is why the configured index had
    # never been created. A script outside the test suite's import graph, with no
    # type checker running, had nothing to catch it.
    #
    # Only the Moss settings are resolved, deliberately. `Settings.load()` also
    # requires OPENAI_API_KEY and ELEVENLABS_API_KEY, and both vendors were
    # dropped in docs/WORKLOG.md Session 2 (Gemini for the LLM, Deepgram Aura-2
    # for TTS) -- a migration that reached .env but never reached config.py.
    # Building a search index should not require TTS credentials in any case.
    config = MossConfig.from_env(_Env(read_env()))

    if not args.check:
        logger.info("Seeding %d protocols into %s", len(PROTOCOLS), config.protocols_index)
        await seed_protocol_index(config, recreate=args.recreate)

    await check(config)


if __name__ == "__main__":
    asyncio.run(main())
