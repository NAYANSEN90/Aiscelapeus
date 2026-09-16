"""Agent entrypoint. Run with:  python -m aiscelapeus.main dev"""

from __future__ import annotations

import json
import logging
from typing import Any

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    telemetry as lk_telemetry,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero
from opentelemetry import trace as otel_trace

from .agent import AiscelapeusAgent
from .config import Settings
from .moss_context import EmergencyContext
from .prompts import PROMPT_VERSION
from .soap import generate_soap_note
from .telemetry import setup_telemetry, span
from .triage import TriageState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aiscelapeus")

server = AgentServer()


@server.rtc_session(agent_name="aiscelapeus")
async def entrypoint(ctx: JobContext) -> None:
    settings = Settings.load()

    # --- observability ------------------------------------------------------
    # Our own tracer for Moss and triage spans, and the same provider handed to
    # LiveKit so its STT/LLM/TTS stages land in the same trace.
    setup_telemetry(settings.telemetry)
    lk_telemetry.set_tracer_provider(
        otel_trace.get_tracer_provider(),
        metadata={
            "aiscelapeus.prompt_version": PROMPT_VERSION,
            "aiscelapeus.room": ctx.room.name,
        },
        # Transcripts are patient data. The policy lives in TelemetryConfig,
        # where it is one named field a test can reach - not an expression
        # re-derived at each call site.
        allow_pii=settings.telemetry.allow_pii,
    )

    session_id = ctx.room.name
    logger.info("Starting triage session %s", session_id)

    # --- context ------------------------------------------------------------
    context = EmergencyContext(config=settings.moss, session_id=session_id)
    await context.connect()

    state = TriageState(session_id=session_id)

    async def publish(topic: str, payload: dict[str, Any]) -> None:
        """Push a structured event to every participant in the room.

        The responder UI and the doctor dashboard both render from this stream,
        so there is one source of truth for triage state.
        """
        try:
            await ctx.room.local_participant.publish_data(
                json.dumps({"topic": topic, "data": payload}).encode("utf-8"),
                reliable=True,
                topic=topic,
            )
        except Exception:  # noqa: BLE001 - never let UI plumbing kill a call
            logger.debug("publish_data failed for %s", topic, exc_info=True)

    agent = AiscelapeusAgent(
        settings=settings,
        context=context,
        state=state,
        publish=publish,
    )

    # --- pipeline -----------------------------------------------------------
    session = AgentSession(
        stt=deepgram.STT(
            model=settings.models.stt_model,
            language=settings.models.stt_language,
            # Diarization separates responder / casualty / clinician (PRD 5.2).
            enable_diarization=True,
            interim_results=True,
            smart_format=True,
            numerals=True,
            # Terms a responder will say under stress that generic models fumble.
            keyterms=[
                "tourniquet",
                "AED",
                "defibrillator",
                "adrenaline",
                "epinephrine",
                "anaphylaxis",
                "CPR",
                "compressions",
                "unresponsive",
                "haemorrhage",
            ],
        ),
        llm=openai.LLM(
            model=settings.models.llm_model,
            temperature=settings.models.llm_temperature,
        ),
        tts=elevenlabs.TTS(
            voice_id=settings.models.tts_voice_id,
            model=settings.models.tts_model,
        ),
        vad=silero.VAD.load(),
        # A responder will talk over the agent constantly. Let them.
        allow_interruptions=True,
        min_interruption_duration=0.3,
        # Tool chains here are short: retrieve, record, answer.
        max_tool_steps=4,
    )

    @session.on("user_input_transcribed")
    def _on_transcript(event: Any) -> None:  # noqa: ANN401 - SDK event type
        if not getattr(event, "is_final", False):
            return
        text = getattr(event, "transcript", "") or ""
        if text.strip():
            ctx.create_task(
                publish(
                    "triage.transcript",
                    {"speaker": "responder", "text": text, "final": True},
                )
            )

    async def _shutdown() -> None:
        """Close out the incident: SOAP note, then archive the Moss index."""
        with span("session.shutdown", **{"session.id": session_id}):
            try:
                timeline = await context.timeline()
                note = await generate_soap_note(
                    settings=settings,
                    session_id=session_id,
                    timeline=timeline,
                    state=state,
                )
                await publish("triage.soap", {"session_id": session_id, "note": note})
                logger.info("SOAP note generated for %s (%d chars)", session_id, len(note))
            except Exception:  # noqa: BLE001
                logger.exception("SOAP generation failed for %s", session_id)

            try:
                await context.archive()
            except Exception:  # noqa: BLE001
                logger.exception("Moss archive failed for %s", session_id)

            await context.aclose()

    ctx.add_shutdown_callback(_shutdown)

    await session.start(agent, room=ctx.room)

    await publish("triage.state", state.to_payload())
    await session.generate_reply(
        instructions=(
            "Open the call. In one short sentence, say you are listening and ask "
            "what has happened and whether the person is responsive and breathing. "
            "Do not give any instruction yet."
        )
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
