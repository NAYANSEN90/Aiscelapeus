"""Fetch the ambient scene beds from Freesound, once.

Every bed is CC0 (public domain). That is not a preference - it is what makes
the beds committable and redistributable without an attribution obligation
travelling with the repository. The fetcher *refuses* anything that is not CC0
even if Freesound's search returns it, so a licence cannot creep in by accident.

This is the only script in the corpus pipeline that touches the network, and it
runs once. The fetched beds are committed, so nobody else ever needs a Freesound
key: `build_corpus.py` reads `beds/` off disk and is fully offline.

    python scripts/fetch_beds.py --live          # fetch everything missing
    python scripts/fetch_beds.py --live --force  # refetch even if present
    python scripts/fetch_beds.py --list          # show what is needed, no network

Requires FREESOUND_API_KEY (free, from https://freesound.org/apiv2/apply/).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = AGENT_ROOT.parent
BEDS_DIR = AGENT_ROOT / "tests" / "audio" / "beds"
LICENSES = BEDS_DIR / "LICENSES.md"

API = "https://freesound.org/apiv2"

# Freesound's licence field for CC0. Anything else is refused.
CC0 = "Creative Commons 0"

# Minimum usable length. A bed shorter than this has to loop so often that the
# repetition becomes an audible periodic artefact, which an STT model can latch
# onto in a way real ambience never allows.
MIN_DURATION_S = 20.0
MAX_DURATION_S = 240.0


@dataclass(frozen=True)
class BedSpec:
    """One ambient bed: what to search for, and what it has to sound like."""

    key: str
    query: str
    # Words that must appear in the result's tags or name. Freesound's relevance
    # ranking alone returns music and foley for these queries often enough that
    # an unfiltered "take the top hit" produced a guitar loop for "hospital".
    require_any: tuple[str, ...] = ()
    # Words that disqualify a result outright.
    exclude: tuple[str, ...] = field(
        default=("music", "song", "melody", "guitar", "piano", "beat", "loop pack")
    )
    note: str = ""


BED_SPECS: tuple[BedSpec, ...] = (
    BedSpec(
        key="kitchen_domestic",
        query="domestic kitchen room tone tap water dishwasher",
        require_any=("kitchen", "tap", "sink", "domestic", "room tone"),
        note="s01 - quiet home kitchen, running tap, appliance hum",
    ),
    BedSpec(
        key="playground_wind",
        query="playground children playing outdoor wind",
        require_any=("playground", "children", "kids", "park"),
        note="s02 - children at play, wind across the mic",
    ),
    BedSpec(
        key="office_openplan",
        query="open plan office ambience air conditioning keyboards",
        require_any=("office", "ambience", "air conditioning", "hvac"),
        note="s03 - HVAC hum, distant phones and chatter",
    ),
    BedSpec(
        key="kitchen_commercial",
        query="commercial restaurant kitchen extractor fan pans busy",
        require_any=("kitchen", "restaurant", "cafe", "extractor"),
        note="s04 - loud service kitchen, fans and metal",
    ),
    BedSpec(
        key="roadside_rain_night",
        query="road traffic rain night passing cars wet",
        require_any=("traffic", "road", "rain", "cars"),
        note="s05 - passing traffic on a wet road at night",
    ),
    BedSpec(
        key="livingroom_tv",
        query="living room television background clock ticking",
        require_any=("living room", "television", "tv", "room tone", "clock"),
        note="s06 - domestic room tone with a TV playing",
    ),
    BedSpec(
        key="crowd_garden_party",
        query="outdoor party crowd chatter glasses garden",
        require_any=("crowd", "party", "chatter", "people", "garden"),
        note="s07 - garden party, crowd babble",
    ),
    BedSpec(
        key="construction_site",
        query="construction site machinery reversing alarm building",
        require_any=("construction", "site", "machinery", "building"),
        note="s08 - heavy plant, reversing alarms, metal impacts",
    ),
    BedSpec(
        key="bedroom_night",
        query="quiet bedroom room tone night distant traffic",
        require_any=("room tone", "bedroom", "quiet", "night", "ambience"),
        note="s09 - near-silent 3am bedroom; highest SNR in the corpus",
    ),
    BedSpec(
        key="poolside_chaos",
        query="swimming pool splashing children indoor echo",
        require_any=("pool", "swimming", "splash", "water"),
        note="s10 - pool, splashing, reverberant shouting",
    ),
    BedSpec(
        key="train_platform",
        query="train station platform announcement tannoy brakes",
        require_any=("train", "station", "platform", "railway"),
        note="s11 - platform announcements over the caller",
    ),
    BedSpec(
        key="hiking_trail_helicopter",
        query="helicopter approaching rotor outdoor wind",
        require_any=("helicopter", "rotor", "wind"),
        note="s12 - wind giving way to an approaching helicopter",
    ),
)


class FetchError(RuntimeError):
    """The fetch failed in a way the operator has to resolve."""


def _get(path: str, token: str, **params: Any) -> dict[str, Any]:
    params["token"] = token
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "aiscelapeus-tests"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
        raise FetchError(f"GET {path} failed: {exc}") from exc


def _acceptable(result: dict[str, Any], spec: BedSpec) -> str | None:
    """Why this result is unusable, or None if it is fine."""
    # An absent or empty licence field is a REFUSAL, not a pass. The previous
    # form - `if result.get("license", "") and CC0 not in ...` - skipped the
    # check entirely when the field was falsy, so a result with no licence
    # information would be accepted as though it had been verified, and the
    # docstring's claim that this fetcher refuses non-CC0 audio rested entirely
    # on Freesound's server-side filter. Unknown provenance is exactly the case
    # this gate exists for.
    licence = result.get("license") or ""
    if CC0 not in licence:
        return f"licence {licence!r} is not CC0" if licence else "no licence field"

    duration = float(result.get("duration", 0.0))
    if duration < MIN_DURATION_S:
        return f"{duration:.0f}s is shorter than the {MIN_DURATION_S:.0f}s minimum"
    if duration > MAX_DURATION_S:
        return f"{duration:.0f}s is longer than the {MAX_DURATION_S:.0f}s maximum"

    haystack = " ".join(
        [result.get("name", ""), " ".join(result.get("tags", []))]
    ).lower()

    # Word-boundary matching, not substring. Plain `in` rejected "birdsong" for
    # containing "song", and would equally reject "crossing" for "cross" or
    # "seasons" for "season" - throwing away usable beds for a spelling
    # coincidence. Multi-word terms ("loop pack") still work, because the
    # boundary is applied to the phrase rather than to each word.
    def mentions(term: str) -> bool:
        return re.search(rf"\b{re.escape(term)}\b", haystack) is not None

    for word in spec.exclude:
        if mentions(word):
            return f"excluded by {word!r}"

    if spec.require_any and not any(mentions(w) for w in spec.require_any):
        return f"none of {spec.require_any} present"

    return None


def find_bed(spec: BedSpec, token: str) -> dict[str, Any]:
    """The best CC0 candidate for a bed, or raise with what was rejected."""
    payload = _get(
        "/search/text/",
        token,
        query=spec.query,
        # Filter at the API so the CC0 constraint is applied server-side too,
        # rather than relying only on the local check.
        filter=f'license:"Creative Commons 0" duration:[{MIN_DURATION_S} TO {MAX_DURATION_S}]',
        fields="id,name,license,duration,tags,username,url,previews",
        sort="score",
        page_size=30,
    )

    rejected: list[str] = []
    for result in payload.get("results", []):
        reason = _acceptable(result, spec)
        if reason is None:
            return result
        rejected.append(f"  - {result.get('name', '?')[:50]!r}: {reason}")

    detail = "\n".join(rejected[:8]) or "  (no results at all)"
    raise FetchError(
        f"no usable CC0 bed for {spec.key!r}\n"
        f"query: {spec.query!r}\n"
        f"rejected:\n{detail}"
    )


def download(result: dict[str, Any], destination: Path) -> int:
    """Fetch the preview MP3. Full-quality download needs OAuth2; the preview is
    plenty, because the bed is mixed underneath speech and then band-limited by
    the phone-channel simulation anyway."""
    previews = result.get("previews") or {}
    url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
    if not url:
        raise FetchError(f"{result.get('name')!r} has no preview URL")

    request = urllib.request.Request(url, headers={"User-Agent": "aiscelapeus-tests"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return len(data)


def write_licenses(records: list[dict[str, Any]]) -> None:
    """Record provenance for every bed.

    CC0 imposes no attribution requirement. This file exists anyway: a
    safety-critical test corpus should be able to answer "where did this audio
    come from" years later without a network call.
    """
    lines = [
        "# Ambient bed provenance",
        "",
        "Every file in this directory is **CC0 (public domain)**. Attribution is",
        "not legally required; it is recorded so the corpus can account for its",
        "own inputs.",
        "",
        "Fetched by `agent/scripts/fetch_beds.py`. Do not add a file here by hand",
        "without adding its row.",
        "",
        "| Bed | Source | Freesound ID | Author | Duration | Licence |",
        "|---|---|---|---|---|---|",
    ]
    for record in sorted(records, key=lambda r: r["key"]):
        lines.append(
            f"| `{record['key']}` | [{record['name'][:44]}]({record['url']}) "
            f"| {record['id']} | {record['username']} "
            f"| {record['duration']:.0f}s | CC0 |"
        )
    lines += [
        "",
        "## What each bed is for",
        "",
        "| Bed | Scenario role |",
        "|---|---|",
    ]
    for spec in BED_SPECS:
        lines.append(f"| `{spec.key}` | {spec.note} |")

    LICENSES.parent.mkdir(parents=True, exist_ok=True)
    LICENSES.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually call Freesound. Without it, nothing touches the network.",
    )
    parser.add_argument(
        "--force", action="store_true", help="refetch beds that already exist"
    )
    parser.add_argument(
        "--list", action="store_true", help="show the bed inventory and exit"
    )
    parser.add_argument("--only", default="", help="fetch just this one bed key")
    args = parser.parse_args()

    if args.list or not args.live:
        print(f"{len(BED_SPECS)} beds required\n")
        for spec in BED_SPECS:
            path = BEDS_DIR / f"{spec.key}.mp3"
            print(f"  [{'x' if path.exists() else ' '}] {spec.key:26} {spec.note}")
        if not args.live:
            print("\nNothing fetched. Re-run with --live (needs FREESOUND_API_KEY).")
        return 0

    token = os.environ.get("FREESOUND_API_KEY", "").strip()
    if not token:
        print(
            "FREESOUND_API_KEY is not set.\n\n"
            "  1. Sign in at https://freesound.org\n"
            "  2. Get a key at https://freesound.org/apiv2/apply/ (free, instant)\n"
            "  3. set FREESOUND_API_KEY=... (PowerShell: $env:FREESOUND_API_KEY='...')\n",
            file=sys.stderr,
        )
        return 2

    specs = [s for s in BED_SPECS if not args.only or s.key == args.only]
    if not specs:
        print(f"no bed named {args.only!r}", file=sys.stderr)
        return 2

    records: list[dict[str, Any]] = []
    failures: list[str] = []

    for spec in specs:
        destination = BEDS_DIR / f"{spec.key}.mp3"
        if destination.exists() and not args.force:
            print(f"  [skip] {spec.key} (already present)")
            continue

        try:
            result = find_bed(spec, token)
            size = download(result, destination)
        except FetchError as exc:
            failures.append(f"{spec.key}: {exc}")
            print(f"  [FAIL] {spec.key}", file=sys.stderr)
            continue

        records.append(
            {
                "key": spec.key,
                "id": result["id"],
                "name": result["name"],
                "url": result["url"],
                "username": result["username"],
                "duration": float(result["duration"]),
            }
        )
        print(
            f"  [ok]   {spec.key:26} {result['name'][:38]:40} "
            f"{result['duration']:.0f}s  {size / 1024:.0f}KB"
        )

    if records:
        # Merge with anything already recorded, so a partial run does not erase
        # provenance for beds fetched earlier.
        existing: list[dict[str, Any]] = []
        index = BEDS_DIR / "_provenance.json"
        if index.exists():
            existing = json.loads(index.read_text(encoding="utf-8"))
        merged = {r["key"]: r for r in existing}
        merged.update({r["key"]: r for r in records})
        index.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")
        write_licenses(list(merged.values()))
        print(f"\nprovenance written for {len(merged)} bed(s)")

    if failures:
        print(f"\n{len(failures)} bed(s) failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nRefine that bed's query in BED_SPECS and re-run with --only <key>.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
