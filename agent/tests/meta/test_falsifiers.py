"""Every unit test must name the failure it would catch.

The convention is established by tests/unit/test_triage.py and test_phrases.py:
the first comment inside the body is `# falsifier: <the real-world thing that
goes wrong if this assertion stops holding>`. It is not decoration. A test whose
author cannot state the failure mode usually does not have one, and a test with
no failure mode is a line of coverage rather than a piece of evidence.
"""

from __future__ import annotations

import pytest

from tests.meta.collect import FALSIFIER_PREFIX, CollectedTest, collect_test_functions

TESTS = collect_test_functions()

#: A falsifier shorter than this is a label, not a failure mode. The shortest
#: real one in the suite at the time of writing is ~60 characters.
MIN_FALSIFIER_CHARS = 25


def _id(function: CollectedTest) -> str:
    return f"{function.path.name}::{function.name}"


def test_the_collector_found_the_existing_suite() -> None:
    # falsifier: the collector silently matches nothing - a path typo, a changed
    # layout - and every rule below passes over an empty list, so the whole meta
    # gate goes green while enforcing nothing at all. This is the guard against
    # the meta-tests themselves becoming the vacuous thing they exist to ban.
    assert len(TESTS) >= 20, (
        f"expected the unit suite to contain many tests, collected {len(TESTS)}"
    )
    files = {function.path.name for function in TESTS}
    assert len(files) >= 2, f"expected several unit test modules, found {sorted(files)}"


@pytest.mark.parametrize("function", TESTS, ids=_id)
def test_every_unit_test_names_its_falsifier(function: CollectedTest) -> None:
    # falsifier: a test is added with no stated failure mode, so nobody
    # reviewing a green run can tell what it proves, and it can be deleted or
    # broken without anyone noticing the evidence went missing.
    assert function.falsifier is not None, (
        f"{function.location} has no falsifier comment.\n"
        f"Add `# {FALSIFIER_PREFIX} <what goes wrong in the real world if this "
        f"assertion stops holding>` as the first comment in the body.\n"
        f"See tests/unit/test_triage.py for the convention."
    )


@pytest.mark.parametrize("function", TESTS, ids=_id)
def test_falsifiers_describe_a_failure_not_just_a_label(
    function: CollectedTest,
) -> None:
    # falsifier: the rule above is satisfied by `# falsifier: n/a` or
    # `# falsifier: bug`, so the convention degrades into a checkbox and the
    # comment stops carrying information.
    falsifier = function.falsifier
    assert falsifier is not None, f"{function.location}: covered by the rule above"
    assert len(falsifier) >= MIN_FALSIFIER_CHARS, (
        f"{function.location}: falsifier {falsifier!r} is too short to name a "
        f"failure mode (needs >= {MIN_FALSIFIER_CHARS} chars)"
    )
    placeholders = {"n/a", "na", "none", "tbd", "todo", "obvious", "see above", "-"}
    assert falsifier.rstrip(".").lower() not in placeholders, (
        f"{function.location}: falsifier {falsifier!r} is a placeholder"
    )
