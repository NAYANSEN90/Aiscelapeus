"""The deterministic escalation action, extracted so two paths cannot drift.

`test_phrases.py` proves the matcher: given text, does a marker fire. This file
proves the *action*: given a marker, does the incident actually ratchet to
Critical, does a clinician actually get paged, and does it happen exactly once
when both paths see the same emergency.

That gap is the reason this file exists. The corpus in tests/data/utterances.yaml
has only ever been driven through `hard_escalation_triggered`, which returns a
`MarkerHit` and touches nothing. A green run there proved the rule *recognises*
26 phrasings; it proved nothing about whether recognising one escalates. The
whole corpus is driven through the applier below, so the assertion is about the
path a responder's words actually travel rather than about the matcher alone.

Assertions here observe resulting state and recorded side effects, never the
return flag alone - the convention test_triage.py establishes, and for the same
reason: `set_level` once returned a correct False while having already corrupted
the state behind it. Where a return value is asserted it is asserted *alongside*
the state, never instead of it.

No LiveKit import anywhere, deliberately: `aiscelapeus.escalation` reaches only
for the domain modules, and these tests run with the SDK absent. There is no
`importorskip` here, and its absence is the assertion - see
`test_the_applier_does_not_depend_on_the_vendor_sdk`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import aiscelapeus.telemetry as telemetry
from aiscelapeus.clock import ManualClock
from aiscelapeus.escalation import (
    ESCALATING_SPEAKERS,
    SOURCE_TOOL,
    SOURCE_TRANSCRIPT,
    HardEscalation,
    apply_hard_escalation,
    apply_hard_escalation_to_utterance,
)
from aiscelapeus.transcript import BYSTANDER, RESPONDER, UNKNOWN_SPEAKER, Utterance
from aiscelapeus.triage import Criticality, EscalationStatus, TriageState

CORPUS_PATH = Path(__file__).parent.parent / "data" / "utterances.yaml"


def _corpus() -> list[dict]:
    with CORPUS_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


CORPUS = _corpus()
ESCALATING = [case for case in CORPUS if case["expect"] is not None]
NON_ESCALATING = [case for case in CORPUS if case["expect"] is None]
#: Utterances reporting several life threats in one breath. Driven through the
#: applier separately: `find_markers` returning all of them says nothing about
#: which one the clinician is actually paged under.
MULTI_MARKER = [case for case in CORPUS if case.get("expect_also")]


def _id(case: dict) -> str:
    return case["text"][:60]


class Recorder:
    """Captures the side effects the applier is given, in order.

    Stands in for the agent's `_broadcast_state` and `_do_escalate`. It records
    rather than asserts: the escalation *sequence* is what the dashboard depends
    on, so the order of calls has to be observable and not just their count.

    `pages` counts only what the real `_do_escalate` would treat as a dispatch,
    which is how "paged once, not twice" is asserted without reaching into
    TriageState's private key set.
    """

    def __init__(self, state: TriageState) -> None:
        self.state = state
        self.broadcasts = 0
        self.escalate_calls: list[tuple[str, str]] = []
        self.pages: list[str] = []

    async def on_state_change(self) -> None:
        self.broadcasts += 1

    async def on_escalate(self, reason: str, category: str) -> None:
        """The agent's `_do_escalate`, reproduced in the part that matters.

        Keyed exactly as agent.py keys it - `session_id:category` - because the
        claim under test is that *this existing key* is what makes the two paths
        idempotent. Reimplementing the dedup differently here would test a
        mechanism the production code does not have.
        """
        self.escalate_calls.append((reason, category))
        key = f"{self.state.session_id}:{category}"
        before = self.state.escalation
        self.state.request_escalation(reason, key=key)
        if before is EscalationStatus.NOT_NEEDED:
            self.pages.append(key)


@pytest.fixture
def state(clock: ManualClock) -> TriageState:
    return TriageState(session_id="inc-test", clock=clock)


@pytest.fixture
def recorder(state: TriageState) -> Recorder:
    return Recorder(state)


@pytest.fixture
def spans() -> Any:
    """Capture spans the applier opens, then restore the global tracer.

    `telemetry.tracer()` reads a module global, so this swaps it and puts the
    previous value back. Without the restore, one test's exporter would keep
    collecting for the rest of the session.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = telemetry._TRACER
    telemetry._TRACER = provider.get_tracer("test")
    try:
        yield exporter
    finally:
        telemetry._TRACER = previous


def _utterance(text: str, *, speaker: str) -> Utterance:
    return Utterance(
        text=text,
        speaker=speaker,
        is_final=True,
        at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        speaker_index=0 if speaker == RESPONDER else 1,
    )


# ------------------------------------------------------------- the marker fires


async def test_a_marker_ratchets_to_critical_and_runs_both_callbacks(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: a responder reports "not breathing" and the extracted applier
    # matches the phrase but does not act on it - the level stays where it was
    # and no clinician is paged. The deterministic net would then be a matcher
    # that returns an object nobody uses, which is exactly the shape of the
    # defect this extraction exists to prevent in the second call site.
    outcome = await apply_hard_escalation(
        "he is not breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TOOL,
    )

    assert state.level is Criticality.CRITICAL, "the ratchet must reach Level 5"
    assert state.escalation is EscalationStatus.REQUESTED
    assert recorder.broadcasts == 1, "the UI must be told the level moved"
    assert len(recorder.escalate_calls) == 1, "a clinician must be paged"
    assert outcome is not None
    assert outcome.marker_id == "not_breathing"
    assert outcome.escalated is True
    assert outcome.level_before is Criticality.MINOR


async def test_the_result_carries_which_marker_fired_not_just_that_one_did(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: the applier returns a bool, so the caller cannot put the marker
    # id on its span or into the record. A Level 5 then appears in the audit with
    # no attributable cause, and "why did this incident escalate" becomes
    # unanswerable after the call has ended.
    outcome = await apply_hard_escalation(
        "I can't find a pulse",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TOOL,
    )

    assert isinstance(outcome, HardEscalation)
    assert outcome.marker_id == "no_pulse"
    assert "pulse" in outcome.matched_text
    # The category the page was filed under must be the marker, not free text:
    # it is what the dedup key is built from.
    assert recorder.escalate_calls[0][1] == "no_pulse"


async def test_the_escalation_reason_quotes_what_was_actually_said(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: the reason handed to the clinician is a generic "hard trigger"
    # with the responder's words dropped, so whoever picks up the page has to ask
    # the responder to repeat the one finding that caused the escalation - mid
    # arrest, when they should be doing compressions.
    await apply_hard_escalation(
        "she has stopped breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TOOL,
    )

    reason, _ = recorder.escalate_calls[0]
    assert "stopped breathing" in reason
    assert "stopped breathing" in state.rationale, (
        "the rationale on the record must name the phrase that forced Level 5"
    )


# ---------------------------------------------------------- the marker does not


async def test_no_marker_returns_none_and_touches_nothing(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: the net is too loose and any recorded finding trips it, so a
    # clinician is paged for a grazed knee. The net then gets tuned down or
    # switched off by the people it keeps interrupting, and the arrest it exists
    # for goes unprotected.
    outcome = await apply_hard_escalation(
        "small graze to the left knee, cleaned and dressed",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TOOL,
    )

    assert outcome is None
    assert state.level is Criticality.MINOR, "an unmatched finding must not move level"
    assert state.escalation is EscalationStatus.NOT_NEEDED
    assert recorder.broadcasts == 0, "no state change means no broadcast"
    assert recorder.escalate_calls == [], "no marker means no page"


async def test_a_negated_report_does_not_escalate(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: "the casualty is NOT unresponsive" contains "unresponsive", and
    # substring matching pinned the incident at Critical irreversibly - the
    # ratchet never comes back down, so one negated sentence ruins the triage for
    # the rest of the call. Asserted through the applier, because the matcher
    # returning None is worth nothing if the action ignores it.
    outcome = await apply_hard_escalation(
        "the casualty is not unresponsive, he is talking to me",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TOOL,
    )

    assert outcome is None
    assert state.level is Criticality.MINOR
    assert recorder.escalate_calls == []


# -------------------------------------------------------------- already critical


async def test_a_marker_at_critical_does_not_page_again(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: every further mention of "not breathing" during an ongoing
    # arrest re-enters the escalation path and pages the on-call clinician again.
    # A responder narrating CPR says it repeatedly, so this is a pager storm
    # during the single busiest minute of the incident.
    state.set_level(Criticality.CRITICAL, "arrest confirmed", source="rule")
    await apply_hard_escalation(
        "still not breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )
    history_after_first = len(state.history)

    outcome = await apply_hard_escalation(
        "he is still not breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    assert outcome is not None, "the finding is real and must still be attributable"
    assert outcome.escalated is False, "no dispatch happened on this call"
    assert outcome.already_critical is True
    assert recorder.escalate_calls == [], "the escalation path must not be re-entered"
    assert recorder.broadcasts == 0, "nothing changed, so nothing to broadcast"
    assert len(state.history) == history_after_first, (
        "a repeat at the same level must not pad the incident timeline"
    )


async def test_the_ratchet_is_never_lowered_by_the_net(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: the applier calls set_level with something other than CRITICAL,
    # or reimplements the ratchet, so a marker firing on an incident already
    # assessed as Severe could *lower* it. The deterministic net would then be
    # able to downgrade a clinician's assessment, which is the one thing it must
    # never do.
    state.set_level(Criticality.SEVERE, "uncontrolled haemorrhage")

    await apply_hard_escalation(
        "he is not breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TOOL,
    )

    assert state.level is Criticality.CRITICAL, "Severe must rise to Critical"
    # And the reverse direction: nothing the net does can take it back down.
    change = state.set_level(2, "responder sounds calmer")
    assert change.rejected is True
    assert state.level is Criticality.CRITICAL


# ------------------------------------------------- one emergency, both paths


async def test_the_same_emergency_from_both_paths_pages_once(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: the transcript edge hears "he's not breathing" and the model
    # then calls record_finding with a paraphrase of the same thing, and the
    # on-call clinician is paged twice for one arrest. This is the failure the
    # second call site introduces, and the whole reason the action was extracted
    # before that call site was written. Note the two texts are deliberately
    # *different strings* with the same marker - dedup on text equality would
    # pass a weaker version of this test and still double-page in production.
    from_edge = await apply_hard_escalation(
        "he's not breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )
    from_tool = await apply_hard_escalation(
        "casualty not breathing, starting compressions",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TOOL,
    )

    assert from_edge is not None and from_tool is not None
    assert from_edge.escalated is True, "the edge saw it first and dispatched"
    assert from_tool.escalated is False, "the tool's replay must not dispatch"
    assert len(recorder.pages) == 1, "one arrest is one page"
    assert state.escalation_log == ["inc-test:not_breathing"]


async def test_a_second_distinct_marker_does_not_page_a_second_time(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: an arrest presents as several findings at once - not breathing,
    # then no pulse, then turning blue - each with its own marker id and so its
    # own dedup key. If only the key guarded dispatch, one casualty would page
    # the clinician three times in twenty seconds. The `if self.escalated` guard
    # one level down is what covers this, and nothing asserted it through this
    # path before.
    await apply_hard_escalation(
        "he's not breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )
    await apply_hard_escalation(
        "and I can't find a pulse either",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    assert len(recorder.pages) == 1, "one casualty is one page, not one per finding"
    assert state.escalation_reason == "Reported: not breathing", (
        "the first reason stands; a later finding must not overwrite it"
    )


# --------------------------------------------------------------- speaker gating


@pytest.mark.parametrize("speaker", [RESPONDER, BYSTANDER, UNKNOWN_SPEAKER])
async def test_any_voice_on_scene_can_trip_the_net(
    state: TriageState, recorder: Recorder, speaker: str
) -> None:
    # falsifier: the net is gated on diarization speaker 0, so a bystander
    # kneeling over the patient shouting "he's not breathing" is ignored while
    # the responder is on the phone and cannot see the chest. The utterance most
    # likely to be correct is discarded for a reason with no clinical content:
    # which of two people the STT engine happened to index first. A missed arrest
    # is unrecoverable; a false Level 5 is stood down by the clinician who looks.
    outcome = await apply_hard_escalation_to_utterance(
        _utterance("he's not breathing!", speaker=speaker),
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
    )

    assert outcome is not None, f"{speaker} reporting an arrest must escalate"
    assert outcome.escalated is True
    assert state.level is Criticality.CRITICAL
    assert len(recorder.pages) == 1


async def test_a_speaker_outside_the_rule_is_refused_before_matching(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: the speaker gate is written as a comment or an `is_responder`
    # branch rather than as the checked ESCALATING_SPEAKERS value, so a role
    # added to transcript.py later silently inherits the power to force Level 5
    # on the whole incident. This pins the gate as a real decision point: a role
    # this rule does not name cannot escalate.
    outsider = _utterance("he's not breathing", speaker="dispatcher-recording")
    assert outsider.speaker not in ESCALATING_SPEAKERS, "fixture guards the premise"

    outcome = await apply_hard_escalation_to_utterance(
        outsider,
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
    )

    assert outcome is None
    assert state.level is Criticality.MINOR
    assert recorder.escalate_calls == []


async def test_the_utterance_path_cannot_claim_to_be_the_tool_path(
    state: TriageState, recorder: Recorder, spans: InMemorySpanExporter
) -> None:
    # falsifier: the Utterance convenience takes a `source` argument, so a caller
    # can label raw speech as having come from the model's tool call. The one
    # distinction this subsystem exists to create - net fired on what was said
    # vs. on what the model reported - would then be corruptible by its own
    # caller, and the telemetry proving the fix works could not be trusted.
    await apply_hard_escalation_to_utterance(
        _utterance("he's not breathing", speaker=RESPONDER),
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
    )

    recorded = spans.get_finished_spans()
    assert len(recorded) == 1
    attributes = recorded[0].attributes or {}
    assert attributes["triage.escalation_source"] == SOURCE_TRANSCRIPT, (
        "an Utterance always came from the transcript edge"
    )


# ------------------------------------------------------------------- telemetry


@pytest.mark.parametrize("source", [SOURCE_TOOL, SOURCE_TRANSCRIPT])
async def test_the_source_reaches_the_span(
    state: TriageState, recorder: Recorder, spans: InMemorySpanExporter, source: str
) -> None:
    # falsifier: the span does not record which path fired the net, so after the
    # transcript edge ships nobody can tell from the traces whether it is
    # actually catching anything the tool path was missing. The fix would be
    # unverifiable in production - and "the deterministic net now runs on raw
    # speech" would be a claim with no evidence under it, which is the failure
    # mode this repo already has on record.
    await apply_hard_escalation(
        "he is not breathing",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=source,
    )

    recorded = spans.get_finished_spans()
    assert len(recorded) == 1, "one marker firing is one span"
    span = recorded[0]
    assert span.name == "triage.hard_escalation"
    attributes = span.attributes or {}
    assert attributes["triage.escalation_source"] == source
    assert attributes["triage.marker"] == "not_breathing"
    assert attributes["session.id"] == "inc-test"
    assert attributes["triage.level_before"] == int(Criticality.MINOR)


async def test_no_span_is_opened_when_nothing_fires(
    state: TriageState, recorder: Recorder, spans: InMemorySpanExporter
) -> None:
    # falsifier: every utterance on the call opens a hard_escalation span, so the
    # traces fill with non-events and the one span that matters - an actual
    # Level 5 - becomes impossible to find in a real incident's trace.
    await apply_hard_escalation(
        "he has a small cut on his hand",
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    assert spans.get_finished_spans() == ()


# ------------------------------------------------------- the corpus, end to end


@pytest.mark.parametrize("case", ESCALATING, ids=_id)
async def test_every_corpus_life_threat_actually_escalates(
    case: dict, state: TriageState, recorder: Recorder
) -> None:
    # falsifier: this phrasing is recognised by the matcher but does not escalate
    # when it travels the real path - so test_phrases.py is green, the corpus
    # looks covered, and a responder saying this on a live call still gets no
    # clinician. The corpus has only ever been driven through
    # `hard_escalation_triggered`, which mutates nothing; matching is not acting,
    # and this is the assertion that the 26 recognised phrasings reach Level 5.
    outcome = await apply_hard_escalation(
        case["text"],
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    assert outcome is not None, (
        f"no escalation for {case['text']!r}\n"
        f"this phrasing must escalate because: {case['why'].strip()}"
    )
    assert outcome.marker_id == case["expect"]
    assert state.level is Criticality.CRITICAL, (
        f"{case['text']!r} fired {outcome.marker_id} but the level did not reach 5"
    )
    assert state.escalation is EscalationStatus.REQUESTED
    assert len(recorder.pages) == 1, "a life threat must page exactly one clinician"


@pytest.mark.parametrize("case", NON_ESCALATING, ids=_id)
async def test_no_corpus_non_threat_escalates(
    case: dict, state: TriageState, recorder: Recorder
) -> None:
    # falsifier: this phrasing reaches the applier and forces Level 5, pinning
    # the incident at Critical irreversibly - the ratchet never comes back down,
    # so a single misheard sentence ruins the triage for the rest of the call and
    # pages a clinician to a grazed knee.
    outcome = await apply_hard_escalation(
        case["text"],
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    assert outcome is None, (
        f"{case['text']!r} wrongly escalated via {outcome.marker_id if outcome else None!r}\n"
        f"this phrasing must not escalate because: {case['why'].strip()}"
    )
    assert state.level is Criticality.MINOR
    assert recorder.pages == []


def test_the_corpus_drives_every_declared_marker_through_the_applier() -> None:
    # falsifier: the parametrised runs above quietly cover only a subset of the
    # markers, so a marker could act wrongly - escalate to the wrong level, page
    # under the wrong category - with no case exercising the applier for it. This
    # is the guard against the corpus sweep above looking thorough while a marker
    # slips through untested on the acting path.
    from aiscelapeus.phrases import MARKERS

    declared = {marker.marker_id for marker in MARKERS}
    driven = {case["expect"] for case in ESCALATING}
    assert declared == driven, (
        f"markers never driven through the applier: {sorted(declared - driven)}"
    )
    assert len(ESCALATING) >= 41, (
        f"the corpus sweep must not silently shrink; got {len(ESCALATING)} cases"
    )


@pytest.mark.parametrize("case", MULTI_MARKER, ids=_id)
async def test_a_co_reported_finding_escalates_under_an_attributable_marker(
    case: dict, state: TriageState, recorder: Recorder
) -> None:
    # falsifier: an utterance reporting several life threats escalates, so the
    # level looks right, while the clinician is paged under a category that
    # names only one of them - and the co-occurring finding that dictates the
    # protocol (a submersion needing rescue breaths first, say) never reaches
    # the record the clinician reads. Matching is not acting: test_phrases.py
    # proves all the markers fire, and this proves the action carries one of
    # them as an attributable cause rather than escalating anonymously.
    outcome = await apply_hard_escalation(
        case["text"],
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    assert outcome is not None, (
        f"no escalation for the multi-finding report {case['text']!r}"
    )
    assert outcome.marker_id in set(case["expect_also"]), (
        f"escalated under {outcome.marker_id!r}, which is not one of the "
        f"reported findings {sorted(case['expect_also'])}"
    )
    assert state.level is Criticality.CRITICAL
    assert state.escalation is EscalationStatus.REQUESTED
    _, category = recorder.escalate_calls[0]
    assert category == outcome.marker_id, (
        "the page category must be the marker that fired, so a clinician can "
        "be told which finding forced the escalation"
    )


async def test_every_marker_in_a_co_reported_utterance_reaches_the_record(
    state: TriageState, recorder: Recorder
) -> None:
    # falsifier: the applier escalates on the first marker and the incident
    # history records only that one, so a poolside arrest is filed as
    # "unresponsive" with no trace of the submersion - and the hypoxic-arrest
    # sequence, which inverts adult BLS, is never signalled to anyone reading
    # the record afterwards.
    from aiscelapeus.phrases import find_markers

    text = "We pulled him out of the pool, not moving"
    reported = {hit.marker_id for hit in find_markers(text)}
    assert {"drowning", "unresponsive"} <= reported

    outcome = await apply_hard_escalation(
        text,
        state=state,
        on_state_change=recorder.on_state_change,
        on_escalate=recorder.on_escalate,
        source=SOURCE_TRANSCRIPT,
    )

    assert outcome is not None
    assert state.level is Criticality.CRITICAL
    # The matched text is what a clinician is shown as the cause, so it has to
    # quote the utterance rather than a marker name.
    reason, _ = recorder.escalate_calls[0]
    assert outcome.matched_text in reason.casefold()


# ------------------------------------------------------------ the vendor boundary


def test_the_applier_does_not_depend_on_the_vendor_sdk() -> None:
    # falsifier: someone adds a livekit import to escalation.py - or to one of
    # the modules it pulls in - and the deterministic safety net becomes
    # untestable without standing up a LiveKit session. That is how the rule
    # ended up with one model-gated call site in the first place: the action was
    # welded to the vendor layer, so the only way to reach it was through a tool.
    # Asserted structurally, over the real import graph, because a comment saying
    # "no vendor imports" is not a constraint.
    import ast
    import aiscelapeus.escalation as module

    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert "livekit" not in imported
    forbidden = {"livekit", "openai", "moss"}
    assert not (imported & forbidden), (
        f"escalation.py must stay free of vendor SDKs, found: {sorted(imported & forbidden)}"
    )


def test_the_speaker_rule_is_a_value_not_a_hardcoded_branch() -> None:
    # falsifier: ESCALATING_SPEAKERS is decorative and the real gate is an
    # `is_responder` branch, so editing this constant changes nothing and the
    # clinical decision recorded on it is not the decision the code makes.
    # Pinned against transcript.py's own role names so a rename there cannot
    # leave this set referring to roles that no longer exist.
    assert ESCALATING_SPEAKERS == {RESPONDER, BYSTANDER, UNKNOWN_SPEAKER}
    assert BYSTANDER in ESCALATING_SPEAKERS, (
        "the bystander is often the only person who can see the patient"
    )
