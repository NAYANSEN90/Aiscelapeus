"""Falsifiers for the post-incident record's model boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiscelapeus.soap import generate_soap_note
from aiscelapeus.triage import TriageState


@pytest.mark.asyncio
async def test_soap_uses_the_supplied_gemini_key_and_closes_its_client(monkeypatch) -> None:
    # falsifier: SOAP still constructs AsyncOpenAI(), or lets the Gemini SDK
    # discover a credential from ambient process state. The voice call works,
    # but shutdown loses its clinical record on an otherwise correctly
    # configured installation.
    observed: dict[str, object] = {}

    class FakeModels:
        async def generate_content(self, **kwargs):
            observed["request"] = kwargs
            return SimpleNamespace(
                text="## S\nResponder report.",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=11,
                    candidates_token_count=7,
                ),
            )

    class FakeAsyncClient:
        models = FakeModels()

        async def aclose(self) -> None:
            observed["closed"] = True

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            observed["api_key"] = api_key
            self.aio = FakeAsyncClient()

    monkeypatch.setattr("aiscelapeus.soap.genai.Client", FakeClient)
    settings = SimpleNamespace(
        models=SimpleNamespace(llm_model="gemini-test"),
        moss=SimpleNamespace(session_index_prefix="incident"),
    )

    note = await generate_soap_note(
        settings=settings,
        api_key="explicit-gemini-key",
        session_id="inc-1",
        timeline=[],
        state=TriageState(session_id="inc-1"),
    )

    assert note == "## S\nResponder report."
    assert observed["api_key"] == "explicit-gemini-key"
    assert observed["closed"] is True
    request = observed["request"]
    assert request["model"] == "gemini-test"
    assert request["config"].temperature == 0.0
    assert request["config"].automatic_function_calling.disable is True
    assert "Session ID: inc-1" in request["contents"]


@pytest.mark.asyncio
async def test_soap_closes_gemini_client_when_generation_fails(monkeypatch) -> None:
    # falsifier: the provider raises and its async HTTP client leaks, so repeated
    # incident shutdowns consume sockets until later live calls cannot connect.
    observed: dict[str, object] = {}

    class FakeModels:
        async def generate_content(self, **kwargs):
            raise RuntimeError("provider unavailable")

    class FakeAsyncClient:
        models = FakeModels()

        async def aclose(self) -> None:
            observed["closed"] = True

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.aio = FakeAsyncClient()

    monkeypatch.setattr("aiscelapeus.soap.genai.Client", FakeClient)
    settings = SimpleNamespace(
        models=SimpleNamespace(llm_model="gemini-test"),
        moss=SimpleNamespace(session_index_prefix="incident"),
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await generate_soap_note(
            settings=settings,
            api_key="explicit-gemini-key",
            session_id="inc-1",
            timeline=[],
            state=TriageState(session_id="inc-1"),
        )

    assert observed["closed"] is True
