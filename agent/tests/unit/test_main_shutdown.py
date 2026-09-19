"""Shutdown ordering at the clinician-first/no-responder boundary."""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

pytest.importorskip(
    "livekit.agents",
    reason="aiscelapeus.main imports the LiveKit SDK; installed via .[dev] in CI",
)

import aiscelapeus.main as main  # noqa: E402
from aiscelapeus.config import Settings  # noqa: E402
from aiscelapeus.ports import ArchiveOutcome  # noqa: E402
from aiscelapeus.triage import TriageState  # noqa: E402


class FakeContext:
    def __init__(self, *, timeline_error: Exception | None = None) -> None:
        self.events: list[str] = []
        self.timeline_error = timeline_error

    async def archive(self) -> ArchiveOutcome:
        self.events.append("archive")
        return ArchiveOutcome(ok=True, doc_count=0)

    async def timeline(self) -> list[Any]:
        self.events.append("timeline")
        if self.timeline_error is not None:
            raise self.timeline_error
        return []

    async def aclose(self) -> None:
        self.events.append("close")


def test_shutdown_callback_precedes_every_external_context_await() -> None:
    # falsifier: a clinician-created job is cancelled while waiting for a responder and leaks Moss.
    source = inspect.getsource(main.entrypoint)
    registered = source.index("ctx.add_shutdown_callback(_shutdown)")
    moss_connect = source.index("await context.connect()")
    room_connect = source.index("await ctx.connect()")
    responder_wait = source.index('await wait_for_role(ctx.room, "responder")')

    assert registered < moss_connect < room_connect < responder_wait


@pytest.mark.asyncio
async def test_shutdown_archives_before_soap_and_always_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # falsifier: post-call model work delays the incident's only durable write.
    context = FakeContext()

    async def fake_soap(**_: Any) -> str:
        context.events.append("soap")
        return "note"

    async def publish(_: str, __: dict[str, Any]) -> None:
        context.events.append("publish")

    monkeypatch.setattr(main, "generate_soap_note", fake_soap)
    await main.finalize_session(
        context=cast(Any, context),
        settings=cast(Settings, object()),
        gemini_key="test-key",
        session_id="inc-test",
        state=TriageState(session_id="inc-test"),
        publish=publish,
    )

    assert context.events == ["archive", "timeline", "soap", "publish", "close"]


@pytest.mark.asyncio
async def test_shutdown_closes_when_no_responder_timeline_can_be_read() -> None:
    # falsifier: cancellation after Moss connect but before media start leaves the index loaded.
    context = FakeContext(timeline_error=RuntimeError("responder never arrived"))

    async def publish(_: str, __: dict[str, Any]) -> None:
        raise AssertionError("SOAP cannot publish without a readable timeline")

    await main.finalize_session(
        context=cast(Any, context),
        settings=cast(Settings, object()),
        gemini_key="test-key",
        session_id="inc-test",
        state=TriageState(session_id="inc-test"),
        publish=publish,
    )

    assert context.events == ["archive", "timeline", "close"]
