"""Test environment isolation.

Configuration used to be read at module import, so importing anything that
transitively imported `config` read the developer's own .env.local. Tests then
passed or failed depending on whose machine they ran on, and a test asserting
"PHI is off by default" could pass only because that developer happened not to
have the variable set.

`Settings.load` now takes the environment explicitly, and these fixtures make
the ambient one empty so nothing can read real credentials by accident.

This module also enforces the `infra` marker. pyproject.toml declares it as
"requires real external infrastructure; not in the blocking gate", but nothing
acted on that declaration - no `addopts`, no hook - so an `infra` test ran in
the default suite and reached the network. `_skip_infra_by_default` below is
what makes the registered description true.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterator

import pytest

from aiscelapeus.clock import ManualClock
from aiscelapeus.config import repo_root


class _HermeticEnvironmentWarning(UserWarning):
    """A real dotenv is present. Not an error - see `_no_real_dotenv`.

    Its own class so pyproject.toml can exempt exactly this warning from
    `filterwarnings = ["error"]` without widening that rule for anything else.
    """


# Every variable the agent reads. Cleared wholesale rather than selectively, so
# a newly-added setting cannot silently start leaking in from the developer's
# shell.
MANAGED_PREFIXES = (
    "MOSS_",
    "LIVEKIT_",
    "DEEPGRAM_",
    "GEMINI_",
    "LLM_",
    "OTEL_",
    # Both vendors are dropped, but the prefixes stay listed. A developer who
    # worked on this repo before the migration still has these exported, and
    # clearing them is what stops a stale credential being present while a test
    # asserts the key is no longer required.
    "OPENAI_",
    "ELEVENLABS_",
)
# `LLM_` above already covers LLM_PROVIDER, LLM_MODEL, LLM_FALLBACK_MODEL and
# LLM_TEMPERATURE; listing them individually as well would be two sources of
# truth for one rule. What remains here is every managed variable with no
# shared prefix.
MANAGED_KEYS = (
    "APP_ENV",
    "ESCALATION_THRESHOLD",
    "DISPATCH_TIMEOUT_S",
    "DISTRESS_THRESHOLD",
)

# A complete, obviously-fake credential set: one key per surviving vendor,
# matching config.REQUIRED_KEYS. Tests that care about a specific value override
# it; tests that just need settings to construct use this.
FAKE_ENV: dict[str, str] = {
    "MOSS_PROJECT_ID": "test-project",
    "MOSS_PROJECT_KEY": "test-key",
    "DEEPGRAM_API_KEY": "test-deepgram",
    "GEMINI_API_KEY": "test-gemini",
    "APP_ENV": "test",
}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect `infra` tests unless they were asked for explicitly.

    The suite must stay hermetic: no credentials, no network. An `infra` test
    talks to real Moss, so leaving it in the default run makes `pytest -q` fail
    on a machine with no credentials and, worse, *pass by hitting the network*
    on a machine that has them - which is how a latency or accuracy number gets
    published from a run nobody realised was live.

    Asked for explicitly means either `-m infra` (or any `-m` expression
    naming it) or naming the test's node id on the command line. Anything else
    - a bare `pytest`, a directory, a whole file - skips it.
    """
    requested_marker = "infra" in (config.getoption("-m") or "")
    for item in items:
        if "infra" not in item.keywords:
            continue
        # A node id typed on the command line is an explicit request for that
        # test; a directory or file argument is not. `"::" in argument` gates
        # BOTH halves, and the parentheses are load-bearing: written without
        # them, `A or B and C` parses as `A or (B and C)`, so a bare
        # `-k test_real_moss_...` - the test's short name with no `::` - matched
        # the first disjunct and silently re-enabled a live-Moss run. That is
        # the precise failure this hook exists to prevent, reached by pasting a
        # test name out of CI output. Found by independent review.
        named = any(
            "::" in argument
            and (
                argument.split("::")[-1] == item.name
                or item.nodeid.startswith(argument)
            )
            for argument in config.invocation_params.args
        )
        if not (requested_marker or named):
            item.add_marker(
                pytest.mark.skip(
                    reason="needs real infrastructure; run with `-m infra`"
                )
            )


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every real credential from the process environment."""
    for key in list(os.environ):
        if key.startswith(MANAGED_PREFIXES) or key in MANAGED_KEYS:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    yield


@pytest.fixture
def fake_env() -> dict[str, str]:
    """A complete fake environment, safe to mutate per test."""
    return dict(FAKE_ENV)


@pytest.fixture
def clock() -> ManualClock:
    """A clock that only moves when the test moves it."""
    return ManualClock()


@pytest.fixture(scope="session", autouse=True)
def _no_real_dotenv() -> Iterator[None]:
    """Warn loudly when a real dotenv file is in play during the run.

    A present dotenv does not break these tests - `Settings.load` is given its
    environment explicitly - but its presence means someone can write a test
    that reads it without noticing, and that test then passes on this machine
    for a reason that does not exist on a runner.

    This fixture used to promise exactly that and contain only `yield`. It was
    `autouse=True` and session-scoped, so it read as an active guard in every
    run while surfacing nothing - a mechanism whose only implementation was its
    own docstring, which is the defect this repo keeps finding. It bit a real
    test: a credential case passed for the wrong reason because a dotenv exists
    at the repo root, and the guard meant to flag precisely that stayed silent.

    Both names are checked, in `read_env`'s own search order. The original only
    named `.env.local`, while `read_env` falls through to `.env` - so the file
    actually present here was the one not mentioned.

    A warning rather than a failure, deliberately: a developer keeping a dotenv
    at the repo root is doing nothing wrong, and failing the suite for it would
    make the hermetic check the reason to distrust the suite. CI has no dotenv,
    so this prints nothing there.
    """
    present = [name for name in (".env.local", ".env") if (repo_root() / name).exists()]
    if present:
        warnings.warn(
            f"{', '.join(present)} present at {repo_root()}; the suite is hermetic "
            "(Settings.load takes its environment explicitly) but a new test that "
            "reads the ambient environment would pass here and fail on a runner. "
            "Assert against an explicit mapping, never the real file.",
            _HermeticEnvironmentWarning,
            stacklevel=2,
        )
    yield
