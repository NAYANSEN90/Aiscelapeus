"""Vendor CLI environment adapter contracts."""

from __future__ import annotations

import pytest

pytest.importorskip(
    "livekit.agents",
    reason="aiscelapeus.main imports the LiveKit SDK; installed via .[dev] in CI",
)

from aiscelapeus.main import hydrate_livekit_environment  # noqa: E402


def test_root_dotenv_values_are_exposed_to_the_livekit_cli() -> None:
    # falsifier: the documented native launch fails before a job because LiveKit sees no URL.
    target: dict[str, str] = {}
    hydrate_livekit_environment(
        environ=target,
        resolved={
            "LIVEKIT_URL": "wss://example.invalid",
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": "secret",
            "MOSS_PROJECT_KEY": "must-not-be-copied-by-this-adapter",
        },
    )
    assert target == {
        "LIVEKIT_URL": "wss://example.invalid",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "secret",
    }


def test_process_environment_keeps_precedence_over_dotenv() -> None:
    # falsifier: a local file silently replaces deployment-injected LiveKit credentials.
    target = {"LIVEKIT_URL": "wss://injected.invalid"}
    hydrate_livekit_environment(
        environ=target,
        resolved={"LIVEKIT_URL": "wss://file.invalid", "LIVEKIT_API_KEY": "key"},
    )
    assert target["LIVEKIT_URL"] == "wss://injected.invalid"
    assert target["LIVEKIT_API_KEY"] == "key"


def test_blank_dotenv_values_are_not_promoted_to_vendor_configuration() -> None:
    # falsifier: a blank example value looks configured to the vendor CLI.
    target: dict[str, str] = {}
    hydrate_livekit_environment(
        environ=target,
        resolved={"LIVEKIT_URL": "  ", "LIVEKIT_API_KEY": ""},
    )
    assert target == {}
