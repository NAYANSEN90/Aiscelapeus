"""Post-incident SOAP note generation (PRD 5.4).

Runs after the call ends, off the latency path, from the Moss timeline rather
than from the raw transcript -- the timeline is already structured, ordered and
timestamped, so the model has far less room to invent.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types

from .config import Settings
from .ports import FactRecord
from .prompts import PROMPT_VERSION, build_soap_instructions
from .telemetry import record_llm_usage, span
from .triage import TriageState

logger = logging.getLogger(__name__)


def _format_timeline(timeline: list[FactRecord]) -> str:
    """Render the timeline for the SOAP prompt.

    Takes `FactRecord`s, so `elapsed_s` is already a float and `kind` already a
    validated enum. The previous dict version re-parsed both at this use site -
    `float(fact.get("elapsed_s", 0) or 0)`, and a `.get("kind",
    "observation")` default that turned a fact with a missing classification
    into a plausible-looking observation right here, in the text a clinician
    signs off on. The adapter now raises on that instead.
    """
    if not timeline:
        return "(no facts recorded)"
    lines = []
    for fact in timeline:
        elapsed = int(fact.elapsed_s)
        stamp = f"T+{elapsed // 60:02d}:{elapsed % 60:02d}"
        lines.append(f"{stamp} [{fact.kind.value}] {fact.text}")
    return "\n".join(lines)


def _format_assessments(state: TriageState) -> str:
    if not state.history:
        return "(no criticality assessment recorded)"
    return "\n".join(
        f"{h['at']} level {h['level']} ({h['label']}), source {h['source']}: {h['rationale']}"
        for h in state.history
    )


async def generate_soap_note(
    *,
    settings: Settings,
    api_key: str,
    session_id: str,
    timeline: list[FactRecord],
    state: TriageState,
) -> str:
    """Produce the SOAP note. Returns markdown."""
    instructions = build_soap_instructions()

    user_content = "\n\n".join(
        [
            f"Session ID: {session_id}",
            f"Moss session index: {settings.moss.session_index_prefix}-{session_id}",
            f"Recorded facts: {len(timeline)}",
            "## Incident timeline\n" + _format_timeline(timeline),
            "## System criticality assessments\n" + _format_assessments(state),
            "## Escalation\n"
            + (
                f"Clinician bridged at {state.clinician_requested_at}: {state.escalation_reason}"
                if state.escalated
                else "No clinician was bridged."
            ),
        ]
    )

    # Use the same explicit Gemini credential as the live voice path. Relying on
    # SDK ambient discovery here made a correct Gemini-only deployment fail at
    # shutdown because the old implementation silently required OPENAI_API_KEY.
    client = genai.Client(api_key=api_key)

    try:
        with span(
            "soap.generate",
            **{
                "session.id": session_id,
                "triage.level": state.level,
                "triage.escalated": state.escalated,
                "prompt.version": PROMPT_VERSION,
                "soap.timeline_facts": len(timeline),
            },
        ) as current:
            response = await client.aio.models.generate_content(
                model=settings.models.llm_model,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=instructions,
                    temperature=0.0,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    # Milliseconds. SOAP is off the voice latency path but must
                    # remain bounded during shutdown.
                    http_options=types.HttpOptions(timeout=30_000),
                ),
            )
            usage = response.usage_metadata
            record_llm_usage(
                current,
                model=settings.models.llm_model,
                system_prompt=instructions,
                prompt_tokens=getattr(usage, "prompt_token_count", None),
                completion_tokens=getattr(usage, "candidates_token_count", None),
            )

        return (response.text or "").strip()
    finally:
        await client.aio.aclose()
