"""The triage agent: prompt, tools, and the escalation decision in one place."""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from livekit.agents import Agent, RunContext, function_tool

from .config import Settings
from .moss_context import EmergencyContext
from .prompts import PROMPT_VERSION, build_agent_instructions
from .telemetry import span
from .triage import Criticality, TriageState, hard_escalation_triggered

logger = logging.getLogger(__name__)

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

# Categories the protocol corpus is tagged with. Exposed to the model so it can
# narrow retrieval instead of searching the whole corpus every time.
PROTOCOL_CATEGORIES = (
    "cardiac",
    "haemorrhage",
    "airway",
    "drowning",
    "circulation",
    "burns",
    "trauma",
    "allergy",
    "neuro",
    "metabolic",
    "wounds",
    "environmental",
    "general",
)


class AiscelapeusAgent(Agent):
    """Field triage agent with Moss-backed protocol retrieval and live state."""

    def __init__(
        self,
        *,
        settings: Settings,
        context: EmergencyContext,
        state: TriageState,
        publish: Publisher,
    ) -> None:
        self.settings = settings
        self.context = context
        self.state = state
        self.publish = publish
        self._instructions = build_agent_instructions(
            session_id=context.session_id,
            escalation_threshold=settings.triage.escalation_threshold,
        )
        super().__init__(instructions=self._instructions)

    @property
    def system_prompt(self) -> str:
        return self._instructions

    # ------------------------------------------------------------------- tools

    @function_tool()
    async def lookup_protocol(
        self,
        context: RunContext,
        query: str,
        category: str = "",
    ) -> dict[str, Any]:
        """Search the verified first-aid protocol corpus. Call this before giving
        any clinical instruction.

        Args:
            query: What you need guidance on, in plain words. For example
                "adult CPR compression depth and rate" or "severe bleeding from
                the thigh".
            category: Optional narrowing filter. One of: cardiac, haemorrhage,
                airway, drowning, circulation, burns, trauma, allergy, neuro,
                metabolic, wounds, environmental, general.
        """
        categories = [category] if category in PROTOCOL_CATEGORIES else None
        result = await self.context.lookup_protocol(query, categories=categories)
        hits, trace = result.hits, result.trace

        await self.publish(
            "triage.retrieval",
            {
                "kind": "protocol",
                "query": query,
                "category": category or None,
                "hits": [
                    {"id": h.id, "title": h.title, "score": round(h.score, 3)} for h in hits
                ],
                "wall_ms": trace.wall_ms,
                "moss_ms": trace.moss_ms,
                "within_budget": trace.within_budget,
            },
        )

        if not hits:
            return {
                "found": False,
                "guidance": (
                    "No protocol matched. Tell the responder you do not have a "
                    "protocol for this and escalate to a clinician."
                ),
            }

        return {
            "found": True,
            "retrieval_ms": trace.wall_ms,
            "protocols": [
                {"title": h.title, "category": h.metadata.get("category"), "text": h.text}
                for h in hits
            ],
        }

    @function_tool()
    async def record_finding(
        self,
        context: RunContext,
        kind: str,
        detail: str,
    ) -> dict[str, Any]:
        """Record something that just happened, into the live incident state.
        Call this as soon as the responder reports a vital sign, an action they
        have taken, or a change in the casualty.

        Args:
            kind: One of "vital", "intervention", "observation", "dispatch".
            detail: What happened, in the responder's own words where possible.
                For example "tourniquet applied to left thigh, bleeding stopped".
        """
        record = await self.context.record_fact(kind, detail)
        await self.publish("triage.finding", record)

        # Deterministic safety net: certain phrases force Level 5 even if the
        # model has not yet called assess_criticality.
        marker = hard_escalation_triggered(detail)
        if marker and self.state.level < Criticality.CRITICAL:
            with span(
                "triage.hard_escalation",
                **{"triage.marker": marker, "session.id": self.context.session_id},
            ):
                self.state.set_level(
                    Criticality.CRITICAL,
                    f"Hard trigger on reported phrase: {marker!r}",
                    source="rule",
                )
                await self._broadcast_state()
                await self._do_escalate(f"Reported: {marker}")

        return {"recorded": True, "elapsed_s": record.get("elapsed_s")}

    @function_tool()
    async def recall_state(
        self,
        context: RunContext,
        query: str,
    ) -> dict[str, Any]:
        """Search what has already happened in this incident. Use this instead of
        asking the responder to repeat themselves, and to answer questions about
        elapsed time or what has already been given or done.

        Args:
            query: What you need to recall. For example "when was the tourniquet
                applied" or "has adrenaline been given".
        """
        result = await self.context.recall(query)
        hits, trace = result.hits, result.trace

        await self.publish(
            "triage.retrieval",
            {
                "kind": "state",
                "query": query,
                "hits": [{"id": h.id, "text": h.text} for h in hits],
                "wall_ms": trace.wall_ms,
                "moss_ms": trace.moss_ms,
                "within_budget": trace.within_budget,
            },
        )

        return {
            "elapsed_s": round(self.context.elapsed_seconds, 1),
            "retrieval_ms": trace.wall_ms,
            "facts": [h.text for h in hits] or ["Nothing recorded yet for that."],
        }

    @function_tool()
    async def assess_criticality(
        self,
        context: RunContext,
        level: int,
        rationale: str,
    ) -> dict[str, Any]:
        """Set the criticality of this incident on a 1 to 5 scale. Call this after
        the first exchange and whenever new information could change severity.

        Args:
            level: 1 Minor, 2 Low, 3 Moderate, 4 Severe, 5 Critical. Level 4 and
                above bridge a clinician onto the call.
            rationale: One short sentence naming the findings that drove this level.
        """
        changed = self.state.set_level(level, rationale)
        if changed:
            await self.context.record_fact(
                "observation",
                f"criticality set to {self.state.level} ({self.state.label}): {rationale}",
            )
        await self._broadcast_state()

        escalated = False
        if self.state.should_escalate(self.settings.triage.escalation_threshold):
            escalated = await self._do_escalate(rationale)

        return {
            "level": self.state.level,
            "label": self.state.label,
            "escalation_triggered": escalated,
            "already_escalated": self.state.escalated and not escalated,
        }

    @function_tool()
    async def escalate_to_clinician(
        self,
        context: RunContext,
        reason: str,
    ) -> dict[str, Any]:
        """Bring a human clinician onto the call. Call this when criticality is
        severe or critical, when the responder asks for a human, or when you are
        asked for a decision beyond field first aid.

        Args:
            reason: Why a clinician is needed, in one short sentence.
        """
        newly = await self._do_escalate(reason)
        return {
            "clinician_requested": True,
            "newly_requested": newly,
            "guidance": (
                "Keep giving the responder instructions while the clinician joins. "
                "Do not go silent."
            ),
        }

    # -------------------------------------------------------------- internals

    async def _do_escalate(self, reason: str) -> bool:
        if not self.state.mark_escalated(reason):
            return False

        with span(
            "triage.escalation",
            **{
                "triage.level": self.state.level,
                "triage.reason": reason,
                "session.id": self.context.session_id,
                "prompt.version": PROMPT_VERSION,
            },
        ):
            await self.context.record_fact(
                "escalation",
                f"clinician bridge requested at criticality {self.state.level}: {reason}",
            )
            await self.publish(
                "triage.escalation",
                {
                    "session_id": self.context.session_id,
                    "level": self.state.level,
                    "label": self.state.label,
                    "reason": reason,
                    "requested_at": self.state.clinician_requested_at,
                },
            )
            await self._broadcast_state()

        logger.warning(
            "ESCALATION session=%s level=%d reason=%s",
            self.context.session_id,
            self.state.level,
            reason,
        )
        return True

    async def _broadcast_state(self) -> None:
        await self.publish("triage.state", self.state.to_payload())


def encode(topic: str, payload: dict[str, Any]) -> bytes:
    return json.dumps({"topic": topic, "data": payload}).encode("utf-8")
