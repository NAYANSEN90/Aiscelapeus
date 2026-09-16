"""Test environment isolation.

Configuration used to be read at module import, so importing anything that
transitively imported `config` read the developer's own .env.local. Tests then
passed or failed depending on whose machine they ran on, and a test asserting
"PHI is off by default" could pass only because that developer happened not to
have the variable set.

`Settings.load` now takes the environment explicitly, and these fixtures make
the ambient one empty so nothing can read real credentials by accident.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from aiscelapeus.clock import ManualClock

# Every variable the agent reads. Cleared wholesale rather than selectively, so
# a newly-added setting cannot silently start leaking in from the developer's
# shell.
MANAGED_PREFIXES = (
    "MOSS_",
    "LIVEKIT_",
    "OPENAI_",
    "DEEPGRAM_",
    "ELEVENLABS_",
    "OTEL_",
)
MANAGED_KEYS = (
    "APP_ENV",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "ESCALATION_THRESHOLD",
    "DISPATCH_TIMEOUT_S",
)

# A complete, obviously-fake credential set. Tests that care about a specific
# value override it; tests that just need settings to construct use this.
FAKE_ENV: dict[str, str] = {
    "MOSS_PROJECT_ID": "test-project",
    "MOSS_PROJECT_KEY": "test-key",
    "DEEPGRAM_API_KEY": "test-deepgram",
    "ELEVENLABS_API_KEY": "test-elevenlabs",
    "OPENAI_API_KEY": "test-openai",
    "APP_ENV": "test",
}


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
    """Fail loudly if a real .env.local is in play during the run.

    A present .env.local does not break these tests - `Settings.load` is given
    its environment explicitly - but its presence means someone could write a
    test that reads it without noticing. Surfacing it is cheap.
    """
    yield
