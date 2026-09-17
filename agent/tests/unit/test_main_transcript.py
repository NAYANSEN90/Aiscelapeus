"""The transcript edge, which is where the deterministic net now actually runs.

`main.py` has never had a test. That is not a coverage gap, it is the reason two
defects lived in it undetected: `ctx.create_task(...)` - a method `JobContext`
does not have, so the *first* final transcript of every session raised
`AttributeError` and nothing ever reached the UI - and a hardcoded
`"responder"` speaker label, which filed a bystander's panic as a responder's
clinical report. Neither is subtle. Both survived because the module could not
be imported without the SDK and had no test to import it.

What is asserted here is B2's whole reason to exist. `test_escalation.py` proves
the *applier* escalates when called; nothing proved it was *called* on raw
speech. Before this edge existed, `hard_escalation_triggered` had exactly one
call site - inside the `record_finding` tool, fed the text the model chose to
pass - so the net designed to outrank the model was gated on the model. The
central assertion below is therefore a life threat escalating to CRITICAL with
no tool call anywhere in the test.

The handler is driven, not the entrypoint. `entrypoint` needs a live LiveKit
job, so `make_transcript_handler` takes its collaborators explicitly and is
module-level; that refactor is what makes the edge assertable. `spawn` is
injected too, so a test runs the handler's coroutine deterministically instead
of racing a real event loop - and `test_a_failing_handler_task_is_logged`
exercises the *production* spawn, because a done-callback that only exists in
the fake would prove nothing about a dropped escalation.

`aiscelapeus.main` imports the LiveKit SDK and the Deepgram/Google plugins at
module scope, so this module is skipped where they are absent. Declared with
`importorskip` rather than a silent try/except, for the reason test_agent_tools
gives: CI installs `.[dev]`, which pins the SDK, so these tests do run in the
blocking gate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable

import pytest
import yaml

pytest.importorskip(
    "livekit.agents",
    reason="aiscelapeus.main imports the LiveKit SDK; installed via .[dev] in CI",
)

from aiscelapeus.clock import ManualClock  # noqa: E402
from aiscelapeus.main import (  # noqa: E402
    TRANSCRIPT_TOPIC,
    make_transcript_handler,
    spawn_on_loop,
)
from aiscelapeus.config import ModelConfig  # noqa: E402
from aiscelapeus.transcript import BYSTANDER, RESPONDER, UNKNOWN_SPEAKER  # noqa: E402
from aiscelapeus.triage import Criticality, EscalationStatus, TriageState  # noqa: E402

CORPUS_PATH = Path(__file__).parent.parent / "data" / "utterances.yaml"


def _corpus() -> list[dict]:
    with CORPUS_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


CORPUS = _corpus()
ESCALATING = [case for case in CORPUS if case["expect"] is not None]
NON_ESCALATING = [case for case in CORPUS if case["expect"] is None]


def _id(case: dict) -> str:
    return case["text"][:60]


@dataclass
class FakeSttEvent:
    """An STT event with only the attributes `normalize_transcript` reads.

    A plain object rather than a mock: the edge reads this with `getattr`
    precisely because the vendor type has changed shape between SDK versions, so
    the stand-in has to be something whose shape the test controls exactly.
    """

    transcript: str
    is_final: bool = True
    speaker_id: int | None = 0


@dataclass
class Collaborators:
    """Everything the handler is given, with what it did recorded.

    Stands in for `main.entrypoint`'s `publish` and the agent's
    `_broadcast_state` / `_escalate_for_marker`. `pages` counts only what the
    real `_do_escalate` treats as a dispatch, keyed exactly as agent.py keys it,
    so "one emergency pages once" is asserted through the mechanism production
    actually has rather than a different one reimplemented here.
    """

    state: TriageState
    published: list[tuple[str, dict]] = field(default_factory=list)
    broadcasts: int = 0
    publish_error: Exception | None = None
    spawned: list[Awaitable[None]] = field(default_factory=list)

    async def publish(self, topic: str, payload: dict) -> None:
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((topic, dict(payload)))

    async def on_state_change(self) -> None:
        self.broadcasts += 1

    async def on_escalate(self, reason: str, category: str) -> None:
        """The agent's `_do_escalate`, in the part that matters: the key.

        Keyed exactly as agent.py keys it - `session_id:category` - because the
        claim under test is that *this existing key* is what makes the two paths
        idempotent.
        """
        key = f"{self.state.session_id}:{category}"
        self.state.request_escalation(reason, key=key)

    @property
    def pages(self) -> list[str]:
        """Which pages actually dispatched, read from `TriageState` itself.

        Deliberately NOT a list this class appends to under its own
        `before is NOT_NEEDED` check. An independent review caught that shape:
        the fake would have been re-implementing the dedup rule it exists to
        test, so breaking the real guard in `TriageState.request_escalation`
        would leave these assertions green while production double-paged. The
        rule has one source of truth and `escalation_log` is its output, so the
        test reads that instead of restating the rule.
        """
        return list(self.state.escalation_log)

    def spawn(self, coroutine: Awaitable[None]) -> None:
        """Collect the handler's coroutine instead of scheduling it.

        The handler is a sync pyee callback, so its async work is handed to
        `spawn`. Capturing it here lets the test await it directly: the
        assertion is about what the handler *does*, and a real task would make
        that a race. The production `spawn_on_loop` is exercised separately.
        """
        self.spawned.append(coroutine)

    async def drain(self) -> None:
        """Run everything the handler spawned, in order."""
        pending, self.spawned = self.spawned, []
        for coroutine in pending:
            await coroutine

    def transcripts(self) -> list[dict]:
        return [payload for topic, payload in self.published if topic == TRANSCRIPT_TOPIC]


@pytest.fixture
def state(clock: ManualClock) -> TriageState:
    return TriageState(session_id="inc-edge", clock=clock)


@pytest.fixture
def collaborators(state: TriageState) -> Collaborators:
    return Collaborators(state=state)


def _handler(collaborators: Collaborators) -> Any:
    return make_transcript_handler(
        state=collaborators.state,
        publish=collaborators.publish,
        on_state_change=collaborators.on_state_change,
        on_escalate=collaborators.on_escalate,
        spawn=collaborators.spawn,
    )


# --------------------------------------------------- B2: the net runs at the edge


async def test_a_life_threat_escalates_from_raw_speech_with_no_tool_call(
    collaborators: Collaborators,
) -> None:
    # falsifier: the deterministic net is reachable only through the
    # `record_finding` tool, so a responder saying "he's not breathing" escalates
    # only if the *model* decides to call that tool. The net designed to outrank
    # the model would be gated on the model, and a real arrest would get no
    # clinician whenever the model chose to narrate instead of recording. This is
    # B2's entire reason to exist: no tool, no model, no agent in this test - a
    # raw STT event, and a Level 5 with a clinician paged.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="he's not breathing"))
    await collaborators.drain()

    assert collaborators.state.level is Criticality.CRITICAL, (
        "raw speech reporting an arrest must reach Level 5 on its own"
    )
    assert collaborators.state.escalation is EscalationStatus.REQUESTED
    assert collaborators.pages == ["inc-edge:not_breathing"], (
        "a clinician must be paged, filed under the marker that fired"
    )
    assert collaborators.broadcasts == 1, "the UI must be told the level moved"


async def test_an_interim_result_does_nothing_at_all(
    collaborators: Collaborators,
) -> None:
    # falsifier: interim transcripts are acted on, so a half-heard "he's not..."
    # publishes a partial sentence to the doctor dashboard and a mid-word
    # fragment can trip the escalation net. Deepgram runs with
    # `interim_results=True`, so these arrive constantly on every call - the UI
    # would fill with retracted text and the pager would fire on speech the
    # speaker had not finished saying.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="he's not breathing", is_final=False))
    await collaborators.drain()

    assert collaborators.published == [], "an interim result must not be published"
    assert collaborators.state.level is Criticality.MINOR
    assert collaborators.pages == []


@pytest.mark.parametrize(
    "event",
    [None, FakeSttEvent(transcript=""), FakeSttEvent(transcript="   "), object()],
    ids=["none", "empty", "whitespace", "attribute-less"],
)
async def test_a_malformed_event_does_not_raise(
    collaborators: Collaborators, event: Any
) -> None:
    # falsifier: a missing or oddly-shaped STT event raises out of the handler.
    # This is a sync callback on the SDK's emitter, so the exception propagates
    # into LiveKit's event dispatch mid-call - taking out the transcript stream,
    # and with it the escalation edge, for the rest of the incident. The vendor
    # event type has already changed shape between SDK versions, so "it always
    # has a transcript attribute" is not something this edge may assume.
    handler = _handler(collaborators)

    handler(event)
    await collaborators.drain()

    assert collaborators.published == [], "nothing to record means nothing published"
    assert collaborators.state.level is Criticality.MINOR


# ------------------------------------------------------------- speaker attribution


def test_the_published_topic_is_the_one_the_dashboards_subscribe_to() -> None:
    # falsifier: the topic string is changed and every transcript test follows it,
    # because the tests filter on the same constant the code publishes with. The
    # responder UI and the doctor dashboard subscribe to a LITERAL string, so a
    # rename ships a green suite and two screens that have gone permanently
    # blank mid-incident. Verified by mutation: renaming the constant left all 61
    # tests passing, which is why the literal is pinned here exactly once.
    assert TRANSCRIPT_TOPIC == "triage.transcript"


async def test_the_payload_carries_the_timestamp_the_record_is_ordered_by(
    collaborators: Collaborators,
) -> None:
    # falsifier: the `at` field is dropped from the payload. Nothing else asserted
    # it, so deleting it left the suite green - verified by mutation. A clinical
    # record whose entries have no time cannot be ordered, and ordering is exactly
    # what an audit of a bad outcome turns on: whether the tourniquet went on
    # before or after the arrest is the question, and an untimed transcript cannot
    # answer it.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="tourniquet is on"))
    await collaborators.drain()

    payload = collaborators.transcripts()[0]
    assert "at" in payload, "every utterance must carry when it was said"
    # Parsed, not merely present: a value that is not a timestamp is no more
    # orderable than a missing one. Note `normalize_transcript` stamps from its
    # own clock (the real one by default), not from TriageState's - so the
    # assertion is that the field round-trips as an aware datetime, which is what
    # ordering and audit actually require.
    stamped = datetime.fromisoformat(payload["at"])
    assert stamped.tzinfo is not None, (
        "a naive timestamp cannot be ordered against records from another zone"
    )


async def test_the_payload_keys_are_exactly_the_wire_contract(
    collaborators: Collaborators,
) -> None:
    # falsifier: a key is added, removed or renamed - `speaker_index` to
    # `speakerIndex`, say - and the UI silently stops reading it. Individual tests
    # each read one or two keys, so none of them notices a renamed or vanished
    # field; only the key SET pins the shape the two front ends parse.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="he is breathing normally"))
    await collaborators.drain()

    assert set(collaborators.transcripts()[0]) == {
        "speaker",
        "speaker_index",
        "text",
        "final",
        "at",
    }


async def test_the_published_speaker_is_mapped_not_hardcoded(
    collaborators: Collaborators,
) -> None:
    # falsifier: the payload carries a literal "responder" for every utterance,
    # which is what the previous handler did. Diarization then buys nothing: the
    # doctor dashboard shows every voice on scene as the responder, so a
    # clinician reading the transcript cannot tell a trained responder's
    # observation from a panicking relative's guess - and attributes both to the
    # person they are advising.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="pulse is about forty", speaker_id=0))
    await collaborators.drain()

    published = collaborators.transcripts()
    assert len(published) == 1
    assert published[0]["speaker"] == RESPONDER
    assert published[0]["speaker_index"] == 0
    assert published[0]["text"] == "pulse is about forty"
    assert published[0]["final"] is True


async def test_a_bystander_is_attributed_to_bystander(
    collaborators: Collaborators,
) -> None:
    # falsifier: speaker index 1 is published as "responder", so a bystander's
    # words enter the incident record as a responder's clinical report. On a real
    # call the responder is frequently the one holding the phone and *not* the one
    # looking at the patient; mislabelling means the audit afterwards cannot tell
    # who actually saw what, and a clinician acts on a stranger's guess believing
    # it came from the person they briefed.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="he's turning blue", speaker_id=1))
    await collaborators.drain()

    published = collaborators.transcripts()
    assert published[0]["speaker"] == BYSTANDER, (
        "speaker 1 is not the responder and must not be labelled as one"
    )
    assert published[0]["speaker_index"] == 1
    # And RULE 7 still holds: a bystander who can see the patient may escalate.
    assert collaborators.state.level is Criticality.CRITICAL
    assert collaborators.pages == ["inc-edge:cyanosis"]


async def test_a_missing_diarization_index_is_not_claimed_as_the_responder(
    collaborators: Collaborators,
) -> None:
    # falsifier: an event with no speaker index defaults to "responder", so an
    # STT gap is recorded as positive evidence about who spoke. That is a
    # fabricated attribution in a clinical record: nobody downstream can
    # distinguish "the responder said this" from "we do not know who said this",
    # which is exactly the distinction an audit of a bad outcome turns on.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="pulse is weak", speaker_id=None))
    await collaborators.drain()

    assert collaborators.transcripts()[0]["speaker"] == UNKNOWN_SPEAKER
    assert collaborators.transcripts()[0]["speaker_index"] is None


# ------------------------------------------------------- publish vs. escalate


async def test_the_transcript_is_published_even_when_no_marker_fires(
    collaborators: Collaborators,
) -> None:
    # falsifier: publishing is moved inside the escalation branch, so only
    # life-threatening utterances reach the UI. The doctor dashboard then shows a
    # transcript consisting solely of emergencies with the whole clinical
    # conversation missing - no vitals, no interventions, no context for the one
    # line it does show.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript="small graze to the left knee"))
    await collaborators.drain()

    assert len(collaborators.transcripts()) == 1, "ordinary speech must still reach the UI"
    assert collaborators.state.level is Criticality.MINOR
    assert collaborators.pages == []
    assert collaborators.broadcasts == 0, "no state change means no broadcast"


async def test_a_failing_publish_does_not_cost_the_escalation(
    collaborators: Collaborators, caplog: pytest.LogCaptureFixture
) -> None:
    # falsifier: the publish and the escalation share one failure path, so a
    # broken data channel - a dropped WebRTC connection, a serialisation fault -
    # silently disables the safety net. A responder shouts "he's not breathing",
    # the data channel is down, and NO CLINICIAN IS PAGED: the arrest is lost to
    # UI plumbing. The UI going quiet is recoverable; a missed arrest is not.
    # This is the exact defect an independent review found in the first version
    # of `_handle_utterance`, which awaited an unguarded publish first - the
    # escalation below was unreachable - while its docstring claimed otherwise.
    collaborators.publish_error = RuntimeError("data channel closed")
    handler = _handler(collaborators)

    with caplog.at_level(logging.ERROR, logger="aiscelapeus"):
        handler(FakeSttEvent(transcript="he's not breathing"))
        await collaborators.drain()

    # The publish genuinely failed - the premise of the test.
    assert collaborators.published == [], "the fake publisher raised, as arranged"
    # And it was not swallowed. The guard's docstring claims the UI going dark is
    # itself an operational event someone must see; replacing the log with `pass`
    # left the suite green before this assertion existed.
    assert "data channel closed" in caplog.text, (
        "a failed publish must be logged, not silently absorbed by the guard"
    )
    # And the net still ran. These are the assertions that matter: they fail
    # against the unguarded ordering and pass only when the publish is guarded.
    assert collaborators.state.level is Criticality.CRITICAL, (
        "a dead data channel must not disable the escalation net"
    )
    assert collaborators.pages == ["inc-edge:not_breathing"], (
        "the clinician must be paged even with the UI unreachable"
    )


async def test_a_failing_escalation_propagates_rather_than_being_swallowed(
    collaborators: Collaborators,
) -> None:
    # falsifier: the escalation call is wrapped in a try/except that absorbs the
    # error, so a pager integration being down looks identical to a call with no
    # life threat in it. Afterwards nobody can tell whether the clinician was
    # never needed or never reached - the single worst ambiguity in the audit of a
    # bad outcome. The exception must reach the spawned task, where
    # `spawn_on_loop`'s done-callback logs it at ERROR. Named for what is actually
    # falsifiable here: an earlier draft claimed "a failing escalation does not
    # cost the transcript", which statement ordering guarantees on its own and no
    # mutation could break - an independent review flagged it as unfalsifiable.
    async def exploding_escalate(reason: str, category: str) -> None:
        raise RuntimeError("pager integration unavailable")

    handler = make_transcript_handler(
        state=collaborators.state,
        publish=collaborators.publish,
        on_state_change=collaborators.on_state_change,
        on_escalate=exploding_escalate,
        spawn=collaborators.spawn,
    )

    handler(FakeSttEvent(transcript="he's not breathing"))
    with pytest.raises(RuntimeError, match="pager integration unavailable"):
        await collaborators.drain()

    assert len(collaborators.transcripts()) == 1, (
        "the transcript was already published before the escalation failed"
    )
    # And the failure is not swallowed: it propagates to the spawned task, where
    # `spawn_on_loop`'s done-callback logs it. That path is asserted separately
    # by test_a_failing_handler_task_is_logged_not_silently_dropped.


# --------------------------------------------------- the spawned task's failure


async def test_a_failing_handler_task_is_logged_not_silently_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # falsifier: the handler's coroutine is handed to a bare
    # `asyncio.create_task` and nothing ever reads its exception, so a *failed
    # escalation* leaves no trace - asyncio reports an unretrieved exception only
    # at garbage-collection time, into whatever is running then. The one event
    # that must never vanish is a page that did not go out: afterwards nobody can
    # tell whether the clinician was never called or simply never answered.
    # Exercises the production `spawn_on_loop`, because a done-callback present
    # only in the test fake would prove nothing.
    async def explode() -> None:
        raise RuntimeError("escalation dispatch failed")

    with caplog.at_level(logging.ERROR, logger="aiscelapeus"):
        spawn_on_loop(explode(), loop=asyncio.get_running_loop())
        # Yield so the task runs and its done-callback fires.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors, "a failed handler task must reach the log at ERROR"
    assert "escalation dispatch failed" in caplog.text, (
        "the log must name the actual failure, not just that one happened"
    )


async def test_a_failing_spawn_is_logged_and_does_not_kill_the_emitter(
    collaborators: Collaborators, caplog: pytest.LogCaptureFixture
) -> None:
    # falsifier: `spawn` raises - no running loop on the dispatching thread, or a
    # loop already closed during shutdown - and the exception propagates into the
    # SDK's pyee emitter, taking down the transcript stream and with it the
    # escalation edge for the rest of the incident. The handler defends against a
    # malformed event but originally not against a failed schedule, which is the
    # asymmetry an independent review caught: a failed spawn is the failure that
    # loses a PAGE, not merely one line of transcript.
    def exploding_spawn(coroutine: Awaitable[None]) -> None:
        raise RuntimeError("no running event loop")

    handler = make_transcript_handler(
        state=collaborators.state,
        publish=collaborators.publish,
        on_state_change=collaborators.on_state_change,
        on_escalate=collaborators.on_escalate,
        spawn=exploding_spawn,
    )

    with caplog.at_level(logging.ERROR, logger="aiscelapeus"):
        # Must not raise: the emitter has to survive.
        handler(FakeSttEvent(transcript="he's not breathing"))

    assert "no running event loop" in caplog.text, (
        "a lost utterance - and the page inside it - must be logged, never dropped"
    )


async def test_the_default_loop_path_is_the_one_production_uses_and_it_works(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # falsifier: `spawn_on_loop` is only ever tested with an explicit `loop=`,
    # while `entrypoint` calls it with the default - so the branch that actually
    # runs in production has no coverage. That is precisely how `ctx.create_task`
    # survived: the production call path was the untested one. Called with no
    # loop here, from inside a running loop, which is how the pyee handler
    # reaches it. Also guards the `filterwarnings = ["error"]` interaction: if
    # this ever took asyncio's policy branch it would warn, and a warning fails
    # the suite.
    done = asyncio.Event()

    async def work() -> None:
        done.set()

    with caplog.at_level(logging.ERROR, logger="aiscelapeus"):
        spawn_on_loop(work())
        await asyncio.wait_for(done.wait(), timeout=1)
        await asyncio.sleep(0)

    assert done.is_set(), "the coroutine must actually run on the running loop"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == [], (
        "a successful task must not log an error"
    )


async def test_a_cancelled_task_is_not_reported_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # falsifier: shutdown cancels in-flight handler tasks, and each cancellation
    # is logged as an ERROR. Every normal end-of-call then produces spurious
    # errors, which is how a log stops being read - and the one genuine "the page
    # failed" line gets lost among them.
    async def forever() -> None:
        await asyncio.sleep(3600)

    coroutine = forever()
    with caplog.at_level(logging.ERROR, logger="aiscelapeus"):
        spawn_on_loop(coroutine, loop=asyncio.get_running_loop())
        await asyncio.sleep(0)
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == [], (
        "a cancelled task is shutdown, not a defect"
    )


# --------------------------------------------- one emergency, both paths, one page


async def test_the_edge_and_the_tool_path_page_once_for_one_emergency(
    collaborators: Collaborators,
) -> None:
    # falsifier: the edge adds its own deduplication, or none, so the transcript
    # hearing "he's not breathing" and the model then calling record_finding with
    # a paraphrase pages the on-call clinician twice for one arrest. The two texts
    # here are deliberately different strings with the same marker id, because
    # dedup on text equality would pass a weaker version of this and still
    # double-page in production. The existing `session_id:marker_id` key is the
    # only mechanism, and this asserts the edge inherits it rather than competing
    # with it.
    from aiscelapeus.escalation import SOURCE_TOOL, apply_hard_escalation

    handler = _handler(collaborators)
    handler(FakeSttEvent(transcript="he's not breathing"))
    await collaborators.drain()

    # The model's independent second trigger, exactly as agent.record_finding
    # invokes it.
    from_tool = await apply_hard_escalation(
        "casualty not breathing, starting compressions",
        state=collaborators.state,
        on_state_change=collaborators.on_state_change,
        on_escalate=collaborators.on_escalate,
        source=SOURCE_TOOL,
    )

    assert from_tool is not None, "the tool path must still attribute the finding"
    assert from_tool.escalated is False, "the edge dispatched first; this is a replay"
    assert len(collaborators.pages) == 1, "one arrest is one page"
    assert collaborators.state.escalation_log == ["inc-edge:not_breathing"]


async def test_two_different_markers_in_one_incident_still_page_once(
    collaborators: Collaborators,
) -> None:
    # falsifier: an arrest presents as several findings in sequence - not
    # breathing, then no pulse, then turning blue - each with a DIFFERENT marker
    # id and so a different dedup key, which the per-key check alone cannot
    # catch. One casualty would page the on-call clinician three times in twenty
    # seconds. What actually prevents it at this edge is the applier's
    # already-Critical early return, not the per-key set: verified by removing
    # `TriageState`'s cross-key guard, which this path does not even reach. So
    # this pins the edge's observable contract - three distinct life-threat
    # markers, one page - independently of which layer enforces it, which is the
    # assertion that survives a refactor moving that responsibility.
    handler = _handler(collaborators)

    for phrase in ("he's not breathing", "and I can't find a pulse", "he's turning blue"):
        handler(FakeSttEvent(transcript=phrase))
        await collaborators.drain()

    assert collaborators.pages == ["inc-edge:not_breathing"], (
        "one casualty is one page, under the first marker that fired"
    )
    assert collaborators.state.escalation_reason == "Reported: not breathing", (
        "the first reason stands; a later finding must not overwrite it"
    )
    assert len(collaborators.transcripts()) == 3, "every utterance still reaches the UI"


async def test_repeated_narration_of_an_arrest_does_not_storm_the_pager(
    collaborators: Collaborators,
) -> None:
    # falsifier: every final transcript re-enters the escalation path, and a
    # responder doing CPR narrates "still not breathing" continuously. The edge
    # fires on *every* utterance rather than only on findings, so this is the path
    # where a pager storm would actually originate - during the single busiest
    # minute of the incident.
    handler = _handler(collaborators)

    for phrase in (
        "he's not breathing",
        "still not breathing",
        "he is not breathing, continuing compressions",
    ):
        handler(FakeSttEvent(transcript=phrase))
        await collaborators.drain()

    assert len(collaborators.pages) == 1, "three reports of one arrest is one page"
    assert len(collaborators.transcripts()) == 3, "but every utterance still reaches the UI"
    assert collaborators.broadcasts == 1, "only the first changed the level"


# ------------------------------------------------------- the corpus, through the edge


@pytest.mark.parametrize("case", ESCALATING, ids=_id)
async def test_every_corpus_life_threat_escalates_through_the_edge(
    case: dict, collaborators: Collaborators
) -> None:
    # falsifier: this phrasing escalates when the applier is called directly but
    # not when it arrives as an STT event - a normalisation step strips it, the
    # smart_format curly apostrophe is mangled, or the edge drops it. The corpus
    # has been driven through the matcher and through the applier; neither proves
    # it survives the *edge*, which is the only path a responder's actual speech
    # travels. A green test_escalation.py with a broken edge is precisely the
    # "check that passes because it is not looking" this repo has on record.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript=case["text"]))
    await collaborators.drain()

    assert collaborators.state.level is Criticality.CRITICAL, (
        f"{case['text']!r} did not reach Level 5 through the transcript edge\n"
        f"this phrasing must escalate because: {case['why'].strip()}"
    )
    assert collaborators.pages == [f"inc-edge:{case['expect']}"], (
        f"{case['text']!r} must page under marker {case['expect']!r}"
    )
    assert len(collaborators.transcripts()) == 1, "and it must still reach the UI"


@pytest.mark.parametrize("case", NON_ESCALATING, ids=_id)
async def test_no_corpus_non_threat_escalates_through_the_edge(
    case: dict, collaborators: Collaborators
) -> None:
    # falsifier: this phrasing trips the net at the edge, pinning the incident at
    # Critical irreversibly - the ratchet never comes back down. The edge sees
    # *every* utterance on the call rather than only the model's curated findings,
    # so a false positive here is far likelier than on the tool path: one misheard
    # sentence of ordinary conversation ruins the triage for the rest of the call.
    handler = _handler(collaborators)

    handler(FakeSttEvent(transcript=case["text"]))
    await collaborators.drain()

    assert collaborators.state.level is Criticality.MINOR, (
        f"{case['text']!r} wrongly escalated through the edge\n"
        f"this phrasing must not escalate because: {case['why'].strip()}"
    )
    assert collaborators.pages == []
    assert len(collaborators.transcripts()) == 1, "it is still published, just not escalated"


# --------------------------------------------------------- P2: the provider swap


def test_the_llm_leg_wires_every_model_in_the_fallback_chain() -> None:
    # falsifier: `build_llm` reads `llm_chain.primary` and drops the fallback, so
    # the pair reads as configured resilience while providing none. Session 2
    # recorded a candidate model returning 503 and a retired model 404ing, which
    # is why the chain exists; a single-model build means the next 503 takes the
    # agent off the air mid-incident with a configured spare it never tries.
    from aiscelapeus.config import ModelFallbackChain
    from aiscelapeus.main import build_llm

    models = ModelConfig(
        llm_chain=ModelFallbackChain(primary="gemini-a", fallback="gemini-b"),
        llm_temperature=0.2,
    )

    adapter = build_llm(models, api_key="test-key")

    # The adapter must hold BOTH models, in chain order. Asserted against the
    # configured ids rather than a count, so a build that wired the primary
    # twice would fail. `model` and `provider` are public on the plugin's LLM.
    wired = [instance.model for instance in adapter._llm_instances]
    assert wired == ["gemini-a", "gemini-b"], (
        f"the whole chain must be wired, in order; got {wired}"
    )
    assert {instance.provider for instance in adapter._llm_instances} == {"Gemini"}, (
        "the migration's point: both legs must be Gemini, not OpenAI"
    )


def test_the_llm_leg_carries_the_configured_temperature_not_a_default() -> None:
    # falsifier: temperature is dropped or hardcoded, so a deliberately low
    # setting silently becomes the vendor's chattier default. On a triage call
    # that means the model improvising around a protocol instead of reciting it,
    # which is the one thing the prompt and the retrieval corpus exist to prevent.
    from aiscelapeus.config import ModelFallbackChain
    from aiscelapeus.main import build_llm

    models = ModelConfig(
        llm_chain=ModelFallbackChain(primary="gemini-a", fallback="gemini-b"),
        llm_temperature=0.07,
    )

    adapter = build_llm(models, api_key="test-key")

    for instance in adapter._llm_instances:
        assert instance._opts.temperature == 0.07


def test_the_stt_leg_enables_diarization_which_speaker_mapping_depends_on() -> None:
    # falsifier: diarization is switched off. Every speaker-attribution test above
    # drives the handler with a fake event whose `speaker_id` the test sets, so
    # none of them notices - verified by mutation: `enable_diarization=False` left
    # all 61 tests green. In production the STT then emits no speaker index at
    # all, `speaker_for(None)` labels every utterance "unknown", and the
    # responder/bystander distinction those tests exist to protect silently
    # disappears from the clinical record.
    from aiscelapeus.main import build_stt

    stt = build_stt(ModelConfig(), api_key="test-deepgram")

    assert stt._opts.enable_diarization is True, (
        "speaker attribution is impossible without diarization"
    )
    assert stt._opts.interim_results is True, (
        "the interim/final distinction the edge relies on requires interim results"
    )


def test_the_stt_leg_carries_the_configured_model_and_language() -> None:
    # falsifier: the STT model is hardcoded, so `DEEPGRAM_MODEL` advertises a knob
    # that does nothing. The default is a *medical* model chosen because generic
    # models mishear clinical terms; silently pinning or ignoring it means an
    # operator cannot move off a deprecated model, and cannot tell that they
    # haven't.
    from aiscelapeus.main import build_stt

    stt = build_stt(
        ModelConfig(stt_model="nova-3-medical", stt_language="en-GB"),
        api_key="test-deepgram",
    )

    assert stt._opts.model == "nova-3-medical"
    assert stt._opts.language == "en-GB"


def test_the_clinical_keyterms_reach_the_stt() -> None:
    # falsifier: the keyterm list is dropped or misspelled into a parameter the
    # plugin ignores - which is exactly what happened: this code passed the
    # deprecated `keyterms=`, and the installed plugin logs a deprecation and
    # aliases it. Without these terms the STT mishears the words the whole system
    # turns on: "tourniquet" and "adrenaline" are the difference between a
    # protocol lookup that hits and one that returns nothing mid-haemorrhage.
    from aiscelapeus.main import STT_KEYTERMS, build_stt

    stt = build_stt(ModelConfig(), api_key="test-deepgram")

    assert "tourniquet" in STT_KEYTERMS and "adrenaline" in STT_KEYTERMS
    for term in STT_KEYTERMS:
        assert term in stt._opts.keyterm, f"{term!r} never reached the STT"


def test_each_vendor_gets_its_own_key_not_the_other_vendors() -> None:
    # falsifier: the Deepgram and Gemini keys are cross-wired in `entrypoint`.
    # Both builders take an `api_key` of the same type, so swapping them is a
    # silent, type-correct mistake that produces an agent which can neither hear
    # nor think - and it fails only at the first real audio frame, with a
    # responder already on the line. This pins that each builder puts the key it
    # is given where that vendor reads it.
    from aiscelapeus.main import build_llm, build_stt

    stt = build_stt(ModelConfig(), api_key="deepgram-key")
    adapter = build_llm(ModelConfig(), api_key="gemini-key")

    assert stt._api_key == "deepgram-key"
    # The Gemini client holds its own key; assert it is not the Deepgram one.
    assert all(
        instance._client._api_client.api_key == "gemini-key"
        for instance in adapter._llm_instances
    ), "the LLM must hold the Gemini key, not Deepgram's"


def test_the_tts_leg_uses_the_configured_aura_model() -> None:
    # falsifier: the TTS model is hardcoded, so `DEEPGRAM_TTS_MODEL` advertises a
    # knob that does nothing and an operator switching voice - or moving off a
    # deprecated Aura model - changes nothing while believing they have. Aura-2
    # selects the speaker through the model id itself, so this id IS the voice.
    from aiscelapeus.main import build_tts

    models = ModelConfig(tts_model="aura-2-thalia-en")

    tts = build_tts(models, api_key="test-deepgram")

    assert tts._opts.model == "aura-2-thalia-en"


def test_a_blank_credential_raises_instead_of_reaching_the_vendor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # falsifier: a missing key is passed to the vendor as "" - the silent clamp
    # CLAUDE.md forbids. Running the plugins showed `deepgram.STT`, `deepgram.TTS`
    # and `google.LLM` all read `os.environ` directly and raise on a blank, while
    # `Settings.load` validates through `read_env`, which ALSO reads `.env.local`.
    # So a key living only in `.env.local` - what the README instructs - passes
    # validation and then fails inside a vendor constructor. This pins the guard
    # that turns that into a named ConfigError at the boundary.
    from aiscelapeus.config import ConfigError
    from aiscelapeus.main import credential

    # Point `read_env` at an empty directory. `repo_root` is patched rather than
    # the cwd changed, because `read_env` resolves .env.local from the repo root
    # by design - chdir leaves the developer's own real .env.local in play, and
    # the first draft of this test passed for exactly that reason.
    monkeypatch.setattr("aiscelapeus.config.repo_root", lambda: tmp_path)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="DEEPGRAM_API_KEY"):
        credential("DEEPGRAM_API_KEY")


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"], ids=["empty", "spaces", "tab", "newline"])
def test_a_whitespace_only_credential_is_refused_not_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blank: str
) -> None:
    # falsifier: the guard rejects an ABSENT key but passes a whitespace-only one
    # straight to the vendor - the silent clamp CLAUDE.md forbids, surviving in
    # the one form a real .env file actually produces. `KEY= ` and a trailing
    # newline are ordinary hand-editing faults (config.py strips for exactly this
    # reason), and a padded key reaches Deepgram or Gemini as a credential that
    # cannot authenticate: the agent goes silent on the first thing it tries to
    # say. A guard that only catches the absent case fails on the realistic input.
    from aiscelapeus.config import ConfigError
    from aiscelapeus.main import credential

    monkeypatch.setattr("aiscelapeus.config.repo_root", lambda: tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", blank)

    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        credential("GEMINI_API_KEY")


def test_a_padded_credential_reaches_the_vendor_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # falsifier: `.strip()` is dropped, so a key pasted with a trailing newline -
    # the single most common .env editing artefact - is handed to the vendor with
    # the whitespace attached and rejected as malformed. Config strips its own
    # reads for this reason; a boundary that does not strip reintroduces the fault
    # one layer further in, where the error surfaces as a vendor auth failure
    # rather than as a configuration problem anyone can diagnose.
    from aiscelapeus.main import credential

    monkeypatch.setattr("aiscelapeus.config.repo_root", lambda: tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "  real-key\n")

    assert credential("GEMINI_API_KEY") == "real-key"


def test_a_credential_in_a_dotenv_file_is_found_not_just_the_process_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # falsifier: the credential is read with `os.environ.get`, so a key set only
    # in `.env.local` - exactly what `.env.example` and the README tell an
    # operator to do - is invisible here. Config validation passes because
    # `read_env` reads that file, then the vendor constructor raises on a key it
    # cannot see: "configuration is complete" and a pipeline that cannot be
    # built, which is the divergence this function exists to close.
    from aiscelapeus.main import credential

    env_file = tmp_path / ".env.local"
    env_file.write_text("GEMINI_API_KEY=from-dotenv-file\n", encoding="utf-8")
    monkeypatch.setattr("aiscelapeus.config.repo_root", lambda: tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert credential("GEMINI_API_KEY") == "from-dotenv-file", (
        "a key in .env.local must reach the vendor, as config validation implies"
    )


def test_the_dropped_vendors_are_gone_from_the_pipeline_module() -> None:
    # falsifier: an `elevenlabs` or `openai` plugin import creeps back into
    # main.py. Both vendors are dropped and neither plugin is installed, so the
    # module stops importing at all - which is the state this subsystem began in,
    # and it is invisible to mypy because `livekit.*` is under
    # `ignore_missing_imports`. Asserted structurally over the real import graph
    # rather than by a comment, and it is what keeps the P2 swap from silently
    # regressing.
    import ast

    import aiscelapeus.main as module

    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    assert "elevenlabs" not in imported, "ElevenLabs was dropped; TTS is Deepgram Aura-2"
    assert "openai" not in imported, "OpenAI was dropped; the LLM is Gemini"
    assert "google" in imported, "the Gemini adapter must be the one wired"


def test_the_edge_corpus_sweep_covers_every_declared_marker() -> None:
    # falsifier: the parametrised sweeps above quietly cover only a subset of the
    # markers, so a marker could be unreachable through the edge - the one path
    # that matters - with no case noticing. This is the guard against the sweep
    # looking thorough while a marker slips through untested on the real path.
    from aiscelapeus.phrases import MARKERS

    declared = {marker.marker_id for marker in MARKERS}
    driven = {case["expect"] for case in ESCALATING}
    assert declared == driven, (
        f"markers never driven through the edge: {sorted(declared - driven)}"
    )
    assert len(ESCALATING) >= 26, (
        f"the corpus sweep must not silently shrink; got {len(ESCALATING)} cases"
    )
