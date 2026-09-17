"""Agent entrypoint. Run with:  python -m aiscelapeus.main dev

Two things here are worth reading before changing anything.

**The transcript edge is where the deterministic safety net actually runs.**
Before this file was rewritten, `_on_transcript` read the STT event inline with
`getattr`, published the text under a hardcoded `"responder"` label, and threw
it away. That left `hard_escalation_triggered` with exactly one call site -
inside the `record_finding` tool, fed the text the *model* chose to pass. A
responder saying "he's not breathing" escalated only if the model decided to
call that tool, so the net designed to *outrank* the model was gated on it.
`make_transcript_handler` below runs the shared applier on every final
utterance, and the model's tool path stays as an independent second trigger.

**The handler is built by a factory, not closed over the entrypoint.** The
entrypoint needs a live LiveKit job to run, so a handler defined inside it
cannot be tested. The collaborators are therefore passed in explicitly and the
factory is module-level: `tests/unit/test_main_transcript.py` drives it with a
fake event and a fake publisher, which is what makes the edge assertable at
all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    llm as lk_llm,
    telemetry as lk_telemetry,
    tts as lk_tts,
)
from livekit.plugins import deepgram, google, silero
from opentelemetry import trace as otel_trace

from .agent import AiscelapeusAgent
from .config import ConfigError, ModelConfig, Settings, read_env
from .escalation import apply_hard_escalation_to_utterance
from .moss_context import EmergencyContext
from .prompts import PROMPT_VERSION
from .soap import generate_soap_note
from .telemetry import setup_telemetry, span
from .transcript import Utterance, normalize_transcript
from .triage import TriageState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aiscelapeus")

server = AgentServer()

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]
Escalator = Callable[[str, str], Awaitable[None]]

#: The topic the responder UI and the doctor dashboard both read transcripts from.
TRANSCRIPT_TOPIC = "triage.transcript"

# The Gemini plugin reads `GOOGLE_API_KEY` from the environment; this project
# standardises on `GEMINI_API_KEY`, which is what `config.REQUIRED_KEYS`
# validates at startup and what `.env.example` documents. Left to the plugin's
# own default, a correctly-configured environment would pass config validation
# and then raise inside `google.LLM(...)` on a key it could not find - config
# saying "complete" while the pipeline cannot be built. So the key is read here,
# at the boundary, under the name this repo actually uses, and passed
# explicitly. Named rather than inlined so the mismatch is one documented fact.
GEMINI_API_KEY_VAR = "GEMINI_API_KEY"

#: Deepgram's key, covering STT *and* TTS. Both plugin classes read it straight
#: from `os.environ` and raise on a blank, so it is subject to exactly the same
#: resolution divergence as the Gemini key below and is passed explicitly for
#: the same reason.
DEEPGRAM_API_KEY_VAR = "DEEPGRAM_API_KEY"


def credential(name: str) -> str:
    """One required credential, resolved exactly as `Settings.load` resolved it.

    Read through `config.read_env` rather than `os.environ`, and that is the
    whole point of this function. `read_env` merges `.env.local` / `.env` with
    the process environment; `os.environ` alone sees only the latter. Every
    vendor plugin here reads `os.environ` directly, so a developer whose keys
    live only in `.env.local` - which is precisely what `.env.example` and the
    README instruct - passes `REQUIRED_KEYS` validation and then hits a vendor
    constructor that cannot find the key. An independent review caught this on
    the Gemini path; running it showed `deepgram.STT` and `deepgram.TTS` raise
    the same way, so the fix is one function both vendors use rather than two
    spellings of it.

    Raises rather than returning a blank. CLAUDE.md: "a raise over a silent
    clamp". An empty key is not a configuration, and handing one to a vendor
    defers the failure to the first thing the agent tries to hear or say -
    mid-incident, as silence on the line. `Settings.load` has already validated
    these keys are present, so this is the guard on that invariant still
    holding at the point of use, not a second copy of the rule: the names come
    from `config.REQUIRED_KEYS` and the error type is config's own.
    """
    value = read_env().get(name, "").strip()
    if not value:
        raise ConfigError([f"missing required environment variable {name}"])
    return value


#: Terms a responder will say under stress that generic models fumble. A
#: module-level constant rather than a literal buried in the constructor, so a
#: test can assert the clinical vocabulary actually reaches the STT - and so
#: adding a term is one edit in one place.
STT_KEYTERMS: tuple[str, ...] = (
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
)


def build_stt(models: ModelConfig, *, api_key: str) -> deepgram.STT:
    """The STT leg: Deepgram, with diarization on.

    Extracted from the `AgentSession(...)` literal so it can be asserted.
    `enable_diarization=True` is the load-bearing option here and it had no test:
    every speaker-attribution test drives the handler with a fake event whose
    speaker index the test sets by hand, so diarization could be switched off in
    production with all of them still green. An independent review pointed that
    out and a mutation confirmed it - `enable_diarization=False` left the whole
    suite passing while the responder/bystander distinction silently died.

    `keyterm` is the current spelling; the plugin deprecates `keyterms`.
    """
    return deepgram.STT(
        api_key=api_key,
        model=models.stt_model,
        language=models.stt_language,
        # Diarization separates responder / casualty / clinician (PRD 5.2) and is
        # what makes `speaker_for` able to map an index onto a role at all.
        enable_diarization=True,
        interim_results=True,
        smart_format=True,
        numerals=True,
        keyterm=list(STT_KEYTERMS),
    )


def build_llm(models: ModelConfig, *, api_key: str) -> lk_llm.LLM:
    """The LLM leg, with `models.llm_chain` honoured as a real fallback.

    `llm_chain` carries a primary *and* a fallback because a candidate model
    returned 503 during Session 2. `livekit.agents.llm.FallbackAdapter` takes a
    list of LLMs and moves to the next on failure, so the chain is wired through
    it rather than being reduced to its primary - a pair that reads as
    configured resilience while providing none is the failure mode
    `ModelFallbackChain` exists to prevent, and silently dropping the fallback
    here would reintroduce it one layer down.

    `attempts` is iterated rather than `primary`/`fallback` being read as
    fields, so adding a third model to the chain needs no change here.
    """
    return lk_llm.FallbackAdapter(
        [
            google.LLM(
                model=model,
                temperature=models.llm_temperature,
                api_key=api_key,
            )
            for model in models.llm_chain.attempts
        ]
    )


def build_tts(models: ModelConfig, *, api_key: str) -> lk_tts.TTS:
    """The TTS leg: Deepgram Aura-2, which rides the same key as STT.

    Aura-2 selects the speaker through the model id itself
    (`aura-2-<voice>-en`), so `deepgram.TTS` has no `voice_id` parameter at all
    and `models.tts_voice_id` is not passed. That field is an ElevenLabs-shaped
    remnant; it is now unread by this file, which is the condition config.py
    documented for its removal.

    `api_key` is explicit because `deepgram.TTS` raises when it cannot find
    `DEEPGRAM_API_KEY` in `os.environ` - see `credential`.
    """
    return deepgram.TTS(model=models.tts_model, api_key=api_key)


def make_transcript_handler(
    *,
    state: TriageState,
    publish: Publisher,
    on_state_change: Callable[[], Awaitable[None]],
    on_escalate: Escalator,
    spawn: Callable[[Awaitable[None]], None],
) -> Callable[[Any], None]:
    """Build the `user_input_transcribed` handler.

    A factory taking its collaborators explicitly, for two reasons.

    The first is testability. The entrypoint needs a live LiveKit job, so a
    handler closed over its locals could only be exercised by standing up a
    session - which is how the previous version's `ctx.create_task` call sat
    broken and unnoticed. Here the handler is a plain function over four
    injected callables and can be driven with a fake event.

    The second is the vendor boundary. The event is normalised *once*, at this
    edge, into an `Utterance`; everything downstream receives that frozen domain
    type rather than an SDK object read with `getattr`.

    The returned handler is **synchronous**, because pyee's emitter calls it
    synchronously and it cannot await. Its async work is handed to `spawn`,
    which is injected so the entrypoint can supply the real event loop and a
    test can run the coroutine deterministically.
    """

    def _on_transcript(event: Any) -> None:  # noqa: ANN401 - SDK event type
        # Parse defensively and *before* anything else. A malformed or missing
        # event is an STT gap, not a reason to kill the handler: an exception
        # escaping here propagates into the SDK's emitter mid-call.
        try:
            utterance = normalize_transcript(event)
        except Exception:  # noqa: BLE001 - a vendor event of unknown shape
            logger.exception("could not normalise an STT event; dropping it")
            return

        # None covers interim results and empty text. `normalize_transcript`
        # owns that decision; re-checking `is_final` here would be a second
        # answer to one question.
        if utterance is None:
            return

        work = _handle_utterance(
            utterance,
            state=state,
            publish=publish,
            on_state_change=on_state_change,
            on_escalate=on_escalate,
        )
        # Guarded for the same reason the parse above is, and independent review
        # flagged the asymmetry of defending one and not the other: `spawn` can
        # fail too - no running loop on this thread, or a loop already closed
        # during shutdown - and that is the failure that loses a PAGE rather than
        # one transcript. Logged at ERROR and the coroutine closed explicitly,
        # because an un-awaited coroutine would otherwise raise
        # "never awaited" as an error under `filterwarnings = ["error"]`, turning
        # a logged failure into a second, noisier one.
        try:
            spawn(work)
        except Exception:  # noqa: BLE001 - the emitter must survive this
            logger.exception(
                "could not schedule transcript handling for session=%s; "
                "the utterance and any escalation in it are lost",
                state.session_id,
            )
            work.close()

    return _on_transcript


async def _handle_utterance(
    utterance: Utterance,
    *,
    state: TriageState,
    publish: Publisher,
    on_state_change: Callable[[], Awaitable[None]],
    on_escalate: Escalator,
) -> None:
    """Publish one final utterance, then run the deterministic net over it.

    Publishing happens first and unconditionally: the transcript reaching the UI
    must not depend on whether a marker fired.

    A publish failure must not cost the escalation, so the publish is *guarded*
    rather than merely ordered first. An independent review caught the first
    version of this function promising exactly that in its docstring while
    awaiting an unguarded `publish` - so a raising publisher meant the applier
    below was never reached and a life threat silently went unpaged.

    The asymmetry is the project's own (see `escalation.ESCALATING_SPEAKERS`): a
    missed arrest is unrecoverable, while a quiet dashboard is a recoverable
    inconvenience. Tying the safety net to the health of the UI data channel
    trades the unrecoverable error for the recoverable one, which is backwards.

    The exception is logged rather than swallowed: the responder UI having gone
    dark mid-incident is itself an operational event someone must see.
    """
    try:
        await publish(
            TRANSCRIPT_TOPIC,
            {
                # The mapped diarization role, not a hardcoded "responder".
                # Speaker 0 is whoever initiated the call; anyone else on scene
                # is a bystander whose panic must not be filed as a responder's
                # clinical report. `speaker_for` owns the mapping.
                "speaker": utterance.speaker,
                "speaker_index": utterance.speaker_index,
                "text": utterance.text,
                "final": True,
                "at": utterance.at.isoformat(),
            },
        )
    except Exception:  # noqa: BLE001 - the net must outlive the data channel
        logger.exception(
            "publishing a transcript failed for session=%s; continuing to the "
            "escalation net",
            state.session_id,
        )

    # The second trigger for the same rule the `record_finding` tool runs, by
    # way of the same shared applier - so a life threat escalates on what was
    # actually said, not only on what the model chose to report. Idempotency is
    # NOT re-implemented here: the applier's page is keyed on
    # `session_id:marker_id`, so the same emergency arriving from both paths
    # dispatches once. A second dedup mechanism would be a second answer to one
    # question, which is the DRY defect the extraction exists to prevent.
    await apply_hard_escalation_to_utterance(
        utterance,
        state=state,
        on_state_change=on_state_change,
        on_escalate=on_escalate,
    )


def spawn_on_loop(
    coroutine: Awaitable[None], *, loop: asyncio.AbstractEventLoop | None = None
) -> None:
    """Run `coroutine` as a task, and make sure a failure is never swallowed.

    Replaces `ctx.create_task(...)`, which does not exist: `JobContext` has no
    task-spawning method at all, so the previous handler raised `AttributeError`
    on the *first* final transcript of every session and no transcript ever
    reached the UI. It sat hidden behind `ModuleNotFoundError: livekit` (see
    docs/evidence/2026-09-17-spike0b-livekit-installed.md).

    A bare `asyncio.create_task` is not enough on its own. A task whose
    exception is never retrieved logs only at garbage-collection time, through
    asyncio's own "exception was never retrieved" path - and under
    `filterwarnings = ["error"]` that is a warning surfacing in whichever test
    happens to trigger the collection. More importantly, in production it means
    a failed *escalation* could go unrecorded. So a done-callback reads the
    exception and logs it at ERROR, which is the level a paging failure warrants.

    Returns None rather than the task: the handler is fire-and-forget by
    necessity, and handing back a task nobody awaits would invite a caller to
    believe otherwise.

    `get_running_loop`, not `get_event_loop`. Both work today - the handler is
    dispatched from inside the session's loop, and `get_event_loop` returns a
    running loop without consulting the policy - but they differ exactly when
    the assumption breaks. If a future SDK dispatches the emitter from a thread
    with no running loop, `get_event_loop` takes the policy branch, which warns
    on 3.12+ and is slated to raise; under `filterwarnings = ["error"]` that
    becomes an exception inside the transcript handler. `get_running_loop` is
    the expression that states the invariant this function actually relies on,
    and it fails loudly instead of fabricating a loop nothing is driving.
    """
    running = loop if loop is not None else asyncio.get_running_loop()
    task = running.create_task(_awaitable_to_coroutine(coroutine))
    task.add_done_callback(_log_task_failure)


async def _awaitable_to_coroutine(awaitable: Awaitable[None]) -> None:
    """Adapt an arbitrary awaitable to the coroutine `create_task` requires."""
    await awaitable


def _log_task_failure(task: "asyncio.Task[None]") -> None:
    """Surface a handler task's failure instead of letting it vanish.

    A cancelled task is not a defect - the job shutting down cancels in-flight
    work - so it is not reported as one. Anything else is: an escalation that
    failed must be in the log, never dropped.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "transcript handler task failed: %s", error, exc_info=error
        )


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

    # Credentials resolved BEFORE any external side effect. Independent review
    # caught these being read further down, after `context.connect()` had already
    # provisioned a Moss session index - and before `add_shutdown_callback`, so a
    # raise there leaked an index no `_shutdown` would ever archive, and the next
    # attempt on the same room then tripped the `resumed_existing_session`
    # warning that exists to catch two incidents merging into one audit trail.
    # `Settings.load` has validated presence already; these resolve the values
    # the vendor plugins actually need (see `credential`).
    deepgram_key = credential(DEEPGRAM_API_KEY_VAR)
    gemini_key = credential(GEMINI_API_KEY_VAR)

    # --- context ------------------------------------------------------------
    context = EmergencyContext(config=settings.moss, session_id=session_id)
    # `connect` reports rather than raises, so the failure is handled here
    # instead of killing the job before the responder is greeted. Retrieval
    # will fail closed - the tier gate raises TierViolation on a Class-0 lookup
    # while the corpus is not resident - so the agent cannot answer from an
    # unloaded index and pass it off as a cited protocol.
    outcome = await context.connect()
    if not outcome.ok:
        logger.error(
            "Moss unavailable for %s (%s); starting without protocol retrieval",
            session_id,
            outcome.error,
        )
    elif outcome.resumed_existing_session:
        # Documents already under this index name mean a previous run's records
        # are still there, which is the condition that silently merges two
        # incidents into one audit trail. Previously the count was discarded.
        logger.warning(
            "Session index %s already holds %d documents - resuming, not starting clean",
            context.session_index_name,
            outcome.loaded_doc_count,
        )

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
    # Annotated because `AgentSession` is generic over its userdata type. Left
    # bare, mypy reports "Need type annotation for session" - this session
    # carries no userdata, so the parameter is None.
    session: AgentSession[None] = AgentSession(
        stt=build_stt(settings.models, api_key=deepgram_key),
        llm=build_llm(settings.models, api_key=gemini_key),
        tts=build_tts(settings.models, api_key=deepgram_key),
        vad=silero.VAD.load(),
        # A responder will talk over the agent constantly. Let them.
        allow_interruptions=True,
        min_interruption_duration=0.3,
        # Tool chains here are short: retrieve, record, answer.
        max_tool_steps=4,
        # OFF, and this is a correctness requirement of the output gate rather
        # than a tuning choice. The SDK defaults it ON
        # (`voice.turn._PREEMPTIVE_GENERATION_DEFAULTS` is `enabled: True`),
        # which starts a speculative reply - tool calls included - from an
        # INTERIM transcript, before end-of-turn is confirmed.
        #
        # That breaks the citation record's lifetime, which the gate depends on.
        # `AiscelapeusAgent.on_user_turn_completed` clears the record when the
        # real turn is confirmed; a speculative `lookup_protocol` has by then
        # already written into it, and the speculative speech is gated only
        # afterwards - so a turn that genuinely retrieved and correctly cited a
        # protocol is refused as MISSING_CITATION and the responder gets the safe
        # line instead of the depth they need. Worse, an ABANDONED speculation's
        # write is not forcibly cancelled, so a document retrieved for a
        # discarded guess at the utterance could ground a number in the turn that
        # actually ran - a citation for text the model was shown for a different
        # question, which is the permissive direction.
        #
        # Found by independent review, which traced it through
        # `agent_activity.on_preemptive_generation`. Disabled rather than worked
        # around inside the gate: making the record survive speculation would
        # mean tracking which speech handle each retrieval belonged to, i.e.
        # reimplementing the SDK's own speculation bookkeeping to defend a safety
        # property. Turning the speculation off is the one-line answer, and the
        # latency it buys back is not worth a gate that misfires on correct
        # guidance - `output_gate`'s docstring is explicit that such a gate is
        # one somebody switches off.
        preemptive_generation=False,
    )

    # The agent already owns the escalation action and the state broadcast, so
    # the edge injects *its* methods rather than growing a second copy. That is
    # what makes "one emergency pages once" hold across both paths: both call
    # `_do_escalate`, which keys on `session_id:marker_id`.
    session.on(
        "user_input_transcribed",
        make_transcript_handler(
            state=state,
            publish=publish,
            on_state_change=agent._broadcast_state,
            on_escalate=agent._escalate_for_marker,
            spawn=spawn_on_loop,
        ),
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

            # `archive` reports rather than raises: it is the only durable
            # write in the system, so "did the incident record reach storage"
            # must be a value the caller inspects, not an exception that may or
            # may not have been thrown.
            archived = await context.archive()
            if not archived.ok:
                logger.error(
                    "Moss archive failed for %s: %s", session_id, archived.error
                )

            await context.aclose()

    ctx.add_shutdown_callback(_shutdown)

    await session.start(agent, room=ctx.room)

    await publish("triage.state", state.to_payload())
    # THE OPENING TURN MUST BE A QUESTION AND NOTHING ELSE, and that is now a
    # requirement rather than a style preference.
    #
    # This turn goes through the output gate like any other (the node overrides
    # on `AiscelapeusAgent` apply to `generate_reply` too), and it runs before
    # any user turn - so `on_user_turn_completed` has not fired, no
    # `lookup_protocol` has happened, and the citation record is empty by
    # construction. A turn `turn_gate.classify` reads as a clinical instruction
    # therefore CANNOT be cited and is refused.
    #
    # The previous wording asked for a statement AND a question in one sentence
    # ("say you are listening and ask what has happened"), which a model renders
    # as "I'm listening - what happened, and is the person responsive?" - a
    # mixed declarative that classifies as a clinical instruction. The first
    # thing a responder heard would have been the safe line. Found by
    # independent review, and confirmed by running `classify` over the three
    # plausible renderings.
    #
    # Fixed here rather than by exempting the greeting in the gate: an exemption
    # keyed on "this is the first turn" is a hole a malfunctioning model could
    # be in when it speaks, and the requirement costs nothing - `prompts.py`
    # instruction 1 already says "establish in ONE QUESTION what happened".
    # This aligns the entrypoint with the prompt it was contradicting.
    await session.generate_reply(
        instructions=(
            "Open the call. Ask one short question and say nothing else: what has "
            "happened, and whether the person is responsive and breathing. Phrase "
            "it as a question only - do not add a statement about yourself, and do "
            "not give any instruction yet."
        )
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
