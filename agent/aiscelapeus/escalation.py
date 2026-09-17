"""The deterministic escalation *action*, as one function both paths share.

`phrases.py` decides whether an utterance reports a life threat. This module
decides what the system then *does* about it: ratchet the level to Critical,
broadcast, and page a clinician. Those are two different responsibilities and
they were previously split the wrong way - the matcher had a module, the action
was eleven lines inlined in a `@function_tool` body.

That inlining is the defect this module exists to remove. The rule had exactly
one call site: `record_finding`, fed the text the *model* chose to pass. So the
deterministic net designed to outrank the model was gated on the model - a
responder saying "he's not breathing" escalated only if the model decided to
call that tool. The fix runs the same rule on every final transcript at the
edge, which means a second caller. Two callers of an inlined block is two
implementations of one rule, and the rule is the system's central safety claim.
So the action is extracted here first, before the second caller exists.

Deliberately free of any vendor import: this module reaches only for `.triage`,
`.phrases`, `.transcript` and `.telemetry`, so the safety net is importable and
testable with the LiveKit SDK absent. The two callers inject their side effects
as callbacks rather than being reached into, which is what lets the whole action
- ratchet, broadcast and page - be asserted without a session.

Deduplication is deliberately NOT implemented here; see `apply_hard_escalation`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .phrases import MarkerHit, hard_escalation_triggered
from .telemetry import span
from .transcript import BYSTANDER, RESPONDER, UNKNOWN_SPEAKER, Utterance
from .triage import Criticality, TriageState

__all__ = [
    "ESCALATING_SPEAKERS",
    "HardEscalation",
    "SOURCE_TOOL",
    "SOURCE_TRANSCRIPT",
    "apply_hard_escalation",
    "apply_hard_escalation_to_utterance",
]

logger = logging.getLogger(__name__)

#: Where the text came from. These reach telemetry as `triage.escalation_source`,
#: which is the whole point of the fix: a span must show whether the net fired
#: on what was actually said or only on what the model chose to report. Named
#: constants rather than bare strings at the call sites, so the two values the
#: dashboards group by have one definition.
SOURCE_TOOL = "tool"
SOURCE_TRANSCRIPT = "transcript"

#: Which diarization roles can trip the deterministic net.
#:
#: RULE 7, and it is a clinical decision rather than a plumbing one. A bystander
#: IS allowed to escalate, and the reasoning is the asymmetry of the two failure
#: modes.
#:
#: On a real first-aid call the responder is frequently the one holding the
#: phone, which means they are frequently NOT the one looking at the patient. The
#: person who can see that the chest has stopped moving is the bystander kneeling
#: over them. Gating the net on speaker 0 would discard the single most
#: informative utterance on the call - "he's not breathing!" shouted by the
#: person actually watching - and would do so for a reason with no clinical
#: content: which of two people the STT engine happened to index first.
#:
#: Weigh the two errors. A false negative is a missed arrest: compressions do not
#: start, no clinician is bridged, and survival falls by roughly ten percent per
#: minute. It is unrecoverable. A false positive is a Level 5 on a patient who
#: turns out to be breathing - a clinician is paged, looks, and stands the
#: incident down. It costs a clinician's attention and it is fully recoverable by
#: a human in the loop. Those are not comparable magnitudes, so the net is
#: deliberately biased toward firing.
#:
#: UNKNOWN_SPEAKER is included for the same reason and a sharper one: a missing
#: diarization index is an STT gap, not evidence about who spoke. Excluding it
#: would silently disable the safety net exactly when the audio is hardest to
#: process - a noisy scene - which is when an arrest is most likely to be the
#: thing being reported. Refusing to escalate on "I don't know who said it" is
#: choosing the unrecoverable error to avoid the recoverable one.
#:
#: Encoded as a frozenset checked in `apply_hard_escalation_to_utterance` rather
#: than as a comment or an `is_responder` branch, so the decision is a value the
#: tests can read and a future change has to edit deliberately.
#:
#: Stated plainly so this is not over-trusted: this set currently contains every
#: role `transcript.py` defines, so the check admits everything and the gate is a
#: no-op *today*. That is the intended answer to rule 7 rather than an oversight
#: - the decision was "any voice on scene counts" - but it means the gate's
#: present value is entirely forward-looking. It is the place a role added later
#: (a dispatcher feed, a recorded line, a second patched-in caller) has to be
#: considered rather than silently inheriting the power to force Level 5 on the
#: whole incident. Noted by independent review.
ESCALATING_SPEAKERS: frozenset[str] = frozenset(
    {RESPONDER, BYSTANDER, UNKNOWN_SPEAKER}
)


@dataclass(frozen=True)
class HardEscalation:
    """What the deterministic net did about one utterance.

    A result object, not a bool, for two reasons. The caller needs to know
    *which* marker fired so it can put that on its span - a bool cannot carry
    the attribution, and an escalation nobody can attribute afterwards is one an
    audit cannot review. And `escalated` alone conflates "this call paged a
    clinician" with "a marker fired but the incident was already Critical", which
    are different events: the first is a dispatch, the second is a confirmation.
    Callers that branch on a single flag get the second one wrong.

    `level_before` is recorded rather than recomputed because by the time the
    caller reads this, the ratchet has already moved `state.level`.
    """

    marker_id: str
    matched_text: str
    level_before: Criticality
    #: True only when THIS call was the one that paged a clinician. A replay of
    #: the same marker from the other path reports False: the marker fired, the
    #: finding is real, and no second page was sent.
    escalated: bool
    source: str

    @property
    def already_critical(self) -> bool:
        """Whether the incident was at Critical before this marker fired."""
        return self.level_before is Criticality.CRITICAL


async def apply_hard_escalation(
    text: str,
    *,
    state: TriageState,
    on_state_change: Callable[[], Awaitable[None]],
    on_escalate: Callable[[str, str], Awaitable[None]],
    source: str,
) -> HardEscalation | None:
    """Run the deterministic net over `text` and act on it.

    Returns None when no marker fired - the ordinary case for nearly every
    utterance. Returns a `HardEscalation` describing what happened when one did.

    `on_escalate` receives `(reason, category)` and is the caller's existing
    escalation path; `on_state_change` broadcasts the mutated state. Injected
    rather than imported so this module stays free of the vendor layer and so a
    test can observe the whole action.

    DEDUPLICATION. The same utterance now reaches this function twice: the
    transcript edge hears the raw speech, and moments later the model may call
    `record_finding` with a paraphrase of it. Escalating twice must not page a
    clinician twice - and nothing here does anything about that, on purpose.

    The caller's `_do_escalate` already keys on `f"{session_id}:{category}"`, and
    `category` is the marker id. A marker id is stable across paraphrase in a way
    the matched text is not: "he is not breathing" and "casualty not breathing,
    starting CPR" yield different `matched_text` and the same `not_breathing`.
    So both paths compute the same key, `TriageState.request_escalation`
    recognises it in `_seen_keys`, and the second call does not dispatch. A
    co-occurring *different* marker is covered too, one guard further down: the
    `if self.escalated` branch refuses to dispatch again under any new key once
    an incident has escalated at all.

    Adding a second dedup mechanism here - a set of seen texts, a hash, a
    per-utterance guard - would put a second answer to one question in a second
    place, which is the DRY defect this extraction exists to prevent, one level
    down. The existing key is sufficient and it is the only one.

    The ratchet is likewise not reimplemented: `state.set_level` already refuses
    downgrades and returns a `LevelChange`. This function only ever asks for
    CRITICAL, and only when the level is below it.
    """
    marker = hard_escalation_triggered(text)
    if marker is None:
        return None

    level_before = state.level

    # The ratchet. `set_level` would refuse a downgrade anyway, but there is
    # nothing to do at all once an incident is Critical: the level cannot rise,
    # and re-paging is the failure mode the key above exists to prevent. Reported
    # rather than silently dropped, so a caller can still attribute the finding.
    if level_before >= Criticality.CRITICAL:
        logger.info(
            "hard marker %s fired on %s text for session=%s; already Critical, "
            "not re-escalating",
            marker.marker_id,
            source,
            state.session_id,
        )
        return HardEscalation(
            marker_id=marker.marker_id,
            matched_text=marker.matched_text,
            level_before=level_before,
            escalated=False,
            source=source,
        )

    with span(
        "triage.hard_escalation",
        **{
            "triage.marker": marker.marker_id,
            "triage.marker_text": marker.matched_text,
            # Rule 5. Without this a span cannot say whether the net fired on
            # what the responder said or only on what the model reported, which
            # is the exact distinction this subsystem exists to create.
            "triage.escalation_source": source,
            "triage.level_before": int(level_before),
            "session.id": state.session_id,
        },
    ):
        state.set_level(
            Criticality.CRITICAL,
            f"Hard trigger on reported phrase: {marker.matched_text!r}",
            source="rule",
        )
        await on_state_change()
        await on_escalate(
            f"Reported: {marker.matched_text}",
            marker.marker_id,
        )

    return HardEscalation(
        marker_id=marker.marker_id,
        matched_text=marker.matched_text,
        level_before=level_before,
        escalated=True,
        source=source,
    )


async def apply_hard_escalation_to_utterance(
    utterance: Utterance,
    *,
    state: TriageState,
    on_state_change: Callable[[], Awaitable[None]],
    on_escalate: Callable[[str, str], Awaitable[None]],
) -> HardEscalation | None:
    """Apply the net to a transcript `Utterance`, honouring the speaker rule.

    The convenience the edge path calls. It adds exactly one thing over
    `apply_hard_escalation`: the `ESCALATING_SPEAKERS` gate, whose clinical
    reasoning is recorded on that constant. Returns None for a speaker outside
    that set without consulting the matcher at all.

    `source` is not a parameter here. An `Utterance` only ever comes from the
    transcript edge, so letting a caller pass "tool" alongside one would make an
    illegal combination expressible.
    """
    if utterance.speaker not in ESCALATING_SPEAKERS:
        logger.info(
            "not applying the hard escalation net to speaker %r for session=%s",
            utterance.speaker,
            state.session_id,
        )
        return None

    return await apply_hard_escalation(
        utterance.text,
        state=state,
        on_state_change=on_state_change,
        on_escalate=on_escalate,
        source=SOURCE_TRANSCRIPT,
    )
