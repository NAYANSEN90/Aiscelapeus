"""The one place that reads `tests/data/utterances.yaml` and splits it.

Three test modules drove the corpus independently - `test_phrases`,
`test_escalation` and `test_main_transcript` - each re-deriving "which entries
must escalate" from the raw YAML. That was tolerable while the answer was
`expect is not None`, and stopped being tolerable the moment detection and
escalation became different questions: the `hard: false` field had to be
learned by three readers, and a fourth would have inherited the old meaning
silently.

One source of truth for a rule, per CLAUDE.md. The rule here is what each
subset MEANS, and it is stated once:

- `DETECTED`      - `find_markers` must report `expect`.
- `ESCALATING`    - ... and `hard_escalation_triggered` must return it too.
- `NOT_HARD`      - ... and `hard_escalation_triggered` must NOT.
- `NON_ESCALATING`- nothing may fire at all.

`ESCALATING` and `NOT_HARD` partition `DETECTED`, which `test_phrases` asserts,
so an entry cannot fall out of both and escape every sweep.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CORPUS_PATH = Path(__file__).parent / "data" / "utterances.yaml"


def load() -> list[dict]:
    with CORPUS_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


CORPUS: list[dict] = load()

#: Every entry whose marker must be DETECTED by `find_markers`.
DETECTED: list[dict] = [case for case in CORPUS if case["expect"] is not None]

#: Entries that must additionally force an escalation. The default is `true`:
#: an entry that says nothing escalates, as every entry did before `pallor`.
ESCALATING: list[dict] = [case for case in DETECTED if case.get("hard", True)]

#: Entries detected but deliberately NOT hard-escalating - see `MarkerSeverity`.
NOT_HARD: list[dict] = [case for case in DETECTED if not case.get("hard", True)]

#: Entries where nothing may fire at all.
NON_ESCALATING: list[dict] = [case for case in CORPUS if case["expect"] is None]

#: Entries reporting several findings in one breath.
MULTI_MARKER: list[dict] = [case for case in CORPUS if case.get("expect_also")]


def case_id(case: dict) -> str:
    """A readable pytest id for a corpus entry."""
    return case["text"][:60]
