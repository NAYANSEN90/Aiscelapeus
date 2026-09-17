"""The triage agent: prompt, tools, and the escalation decision in one place."""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from livekit.agents import Agent, RunContext, function_tool

from .config import Settings
from .escalation import SOURCE_TOOL, apply_hard_escalation
from .moss_context import EmergencyContext
from .ports import FactKind, TierViolation
from .prompts import PROMPT_VERSION, build_agent_instructions
from .retrieval import RetrievalRequest
from .telemetry import span
from .triage import (
    EscalationStatus,
    TriageState,
)

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
        # Two changes here, and the measured truth is that EITHER ONE alone
        # fixes the typecheck - neither is individually necessary. `Agent.__init__`
        # does `self._instructions = instructions` from a `str | Instructions`
        # parameter, so reusing that inherited name means the base declaration
        # wins over the `-> str` return of `build_agent_instructions`, and
        # `system_prompt` - which promises `-> str` - cannot narrow it.
        #
        # Isolated by execution, one variable at a time:
        #
        #     name                annotation   mypy
        #     _rendered_prompt    : str        clean
        #     _instructions       : str        clean
        #     _rendered_prompt    inferred     clean
        #     _instructions       inferred     ERROR   <- the original
        #
        # So the error needs the conjunction: the inherited name AND no
        # annotation. Both are kept deliberately rather than picking the minimal
        # one - the explicit `: str` states the type this attribute actually
        # holds, and the distinct name stops a subclass borrowing a base class's
        # private field to hold a narrower type, which is how this arose.
        #
        # Recorded because a single-variable mutation gives the wrong answer
        # here, twice over: reverting only the name looks like "the annotation
        # fixed it" and reverting only the annotation looks like "the rename
        # fixed it". Both readings are wrong.
        self._rendered_prompt: str = build_agent_instructions(
            session_id=state.session_id,
            escalation_threshold=settings.triage.escalation_threshold,
        )
        super().__init__(instructions=self._rendered_prompt)

    @property
    def system_prompt(self) -> str:
        return self._rendered_prompt

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
        # The request is built here, at the tool boundary, from a
        # model-supplied string: `RetrievalRequest` validates it (blank query,
        # blank category, out-of-range alpha) before anything reaches the
        # store. A rejected request is reported to the model rather than
        # crashing the tool call - the model can retry with a better query,
        # whereas an exception out of a function_tool ends the turn.
        categories = (category,) if category in PROTOCOL_CATEGORIES else ()
        try:
            request = RetrievalRequest.for_protocol(
                query, self.settings.moss, categories=categories
            )
        except ValueError as exc:
            return {"found": False, "error": str(exc)}

        # TierViolation is caught here and turned into the same "no protocol,
        # escalate" answer as a zero-hit search. It is a RuntimeError, not a
        # ValueError, so the clause above does not cover it - and it is
        # reachable in production: `main.entrypoint` deliberately continues past
        # a failed `connect`, which leaves the corpus non-resident for the whole
        # incident, so every lookup would raise. An exception out of a
        # `function_tool` ends the turn, so that would have been a repeated
        # mid-emergency turn kill on the one tool that matters most.
        #
        # This does not weaken the gate. The raise still happens at the call
        # site, the violation is still what stops the network query, and the
        # responder is told there is no protocol rather than hearing silence.
        try:
            result = await self.context.lookup_protocol(request)
        except TierViolation:
            logger.error(
                "protocol lookup refused on the voice path for session=%s: "
                "the corpus is not resident",
                self.state.session_id,
                exc_info=True,
            )
            return {
                "found": False,
                "guidance": (
                    "Protocol retrieval is unavailable. Tell the responder you "
                    "cannot confirm a protocol right now and escalate to a "
                    "clinician. Do not give a clinical instruction you cannot cite."
                ),
            }
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
            kind: One of "vital", "intervention", "observation", "symptom".
            detail: What happened, in the responder's own words where possible.
                For example "tourniquet applied to left thigh, bleeding stopped".
        """
        # The model supplies this as free text, so it is validated here at the
        # boundary rather than deeper in. Everything downstream then holds a
        # checked domain type instead of a string that might mean anything.
        try:
            fact_kind = FactKind(kind)
        except ValueError:
            return {
                "recorded": False,
                "error": (
                    f"unknown kind {kind!r}; use one of "
                    f"{', '.join(k.value for k in FactKind if k is not FactKind.ESCALATION)}"
                ),
            }

        record = await self.context.record_fact(fact_kind, detail)
        # One wire shape, defined by FactRecord.to_payload. The former
        # `hasattr(record, "to_payload")` bridge is gone: the adapter now
        # returns the FactRecord the port declares, so there is no second
        # branch to type, test or keep in sync.
        payload = record.to_payload()
        await self.publish("triage.finding", payload)

        # Deterministic safety net: certain phrases force Level 5 even if the
        # model has not yet called assess_criticality.
        #
        # The action lives in `escalation.apply_hard_escalation`, not here. This
        # is one of two call sites - the transcript edge runs the same rule on
        # every final transcript, so that a responder saying "he's not breathing"
        # escalates whether or not the model chooses to call this tool. Inlined,
        # the rule would have two implementations, and it is the system's central
        # safety claim.
        await apply_hard_escalation(
            detail,
            state=self.state,
            on_state_change=self._broadcast_state,
            on_escalate=self._escalate_for_marker,
            source=SOURCE_TOOL,
        )

        return {"recorded": True, "elapsed_s": payload.get("elapsed_s")}

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
        try:
            request = RetrievalRequest.for_session_state(query, self.settings.moss)
        except ValueError as exc:
            return {"error": str(exc), "facts": []}

        try:
            result = await self.context.recall(request)
        except TierViolation:
            logger.error(
                "state recall refused on the voice path for session=%s",
                self.state.session_id,
                exc_info=True,
            )
            return {
                "elapsed_s": round(self.context.elapsed_seconds, 1),
                "error": "state recall is unavailable",
                "facts": ["Cannot search the incident record right now."],
            }
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
        try:
            change = self.state.set_level(level, rationale)
        except ValueError as exc:
            # The model emitted a level outside the scale. Say so rather than
            # clamping it into range and carrying on as though it were fine.
            return {"recorded": False, "error": str(exc)}

        # Persist whenever there is something new, not only when the number
        # moved. A re-assessment at the same level with new reasoning is a
        # genuine clinical update; branching on "did the level change" dropped
        # it, leaving the UI showing reasoning the record never received.
        if change.should_persist:
            await self.context.record_fact(
                FactKind.OBSERVATION,
                f"criticality set to {int(self.state.level)} ({self.state.label}): {rationale}",
            )
        await self._broadcast_state()

        escalated = False
        if self.state.should_escalate(self.settings.triage.escalation_threshold):
            escalated = await self._do_escalate(rationale, category="criticality_threshold")

        return {
            "level": int(self.state.level),
            "label": self.state.label,
            "downgrade_refused": change.rejected,
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
        newly = await self._do_escalate(reason, category="model_requested")
        return {
            "clinician_requested": True,
            "newly_requested": newly,
            "guidance": (
                "Tell the responder you are requesting a clinician - not that one "
                "is connecting. Keep giving instructions while the request is out. "
                "Do not go silent."
            ),
        }

    # -------------------------------------------------------------- internals

    async def _escalate_for_marker(self, reason: str, category: str) -> None:
        """Adapt `_do_escalate` to the callback shape the applier injects.

        `_do_escalate` takes `category` keyword-only and returns whether it
        dispatched; the applier's port is a two-positional-argument coroutine
        returning None. Bridged here rather than by loosening either side:
        `category` stays keyword-only at the call sites that spell it out, and
        the applier stays free of this class's method signature.

        The return value is dropped deliberately. Whether a page went out is
        already reported by the `HardEscalation` the applier returns, derived
        from the level it observed before acting - so consuming it here would be
        a second answer to that question.
        """
        await self._do_escalate(reason, category=category)

    async def _do_escalate(self, reason: str, *, category: str) -> bool:
        """Request a clinician. Returns whether this call was the one that did it.

        `category` is a stable label for why, kept separate from the free-text
        reason so telemetry can record the cause without carrying clinical
        detail into spans.
        """
        # Keyed on the incident and the cause, not on a flag held by this
        # object: an agent restarted mid-incident reconstructs state, and a
        # boolean would come back False and page the clinician a second time.
        key = f"{self.state.session_id}:{category}"
        before = self.state.escalation
        status = self.state.request_escalation(reason, key=key)
        if before is not EscalationStatus.NOT_NEEDED:
            return False

        with span(
            "triage.escalation",
            **{
                "triage.level": int(self.state.level),
                # The category, not the reason: the free-text reason is
                # clinical detail and spans are not a PHI-carrying surface.
                "triage.escalation_category": category,
                "triage.escalation_status": status.value,
                "session.id": self.state.session_id,
                "prompt.version": PROMPT_VERSION,
            },
        ):
            await self.context.record_fact(
                FactKind.ESCALATION,
                f"clinician bridge requested at criticality {int(self.state.level)}: {reason}",
            )
            await self.publish(
                "triage.escalation",
                {
                    "session_id": self.state.session_id,
                    "level": int(self.state.level),
                    "label": self.state.label,
                    "status": status.value,
                    "reason": reason,
                    "requested_at": self.state.clinician_requested_at,
                },
            )
            await self._broadcast_state()

        logger.warning(
            "ESCALATION session=%s level=%d category=%s",
            self.state.session_id,
            int(self.state.level),
            category,
        )
        return True

    async def _broadcast_state(self) -> None:
        await self.publish("triage.state", self.state.to_payload())


def encode(topic: str, payload: dict[str, Any]) -> bytes:
    return json.dumps({"topic": topic, "data": payload}).encode("utf-8")
