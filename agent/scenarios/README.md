# Headless scenarios (B5)

```bash
cd agent && python -m aiscelapeus.harness --headless scenarios/
```

**These files are the specification. `aiscelapeus/harness.py` is an implementation of
them** — the framing `tests/data/utterances.yaml` already uses, and the reason `expect` is
written in clinical vocabulary rather than in the harness's own. A clinician must be able
to judge an oracle here wrong without reading any Python.

Every scenario carries `traces_to`, naming the review finding it pins. A scenario nobody
can trace is a scenario nobody can judge wrong, and every one of these exists because a
reviewer already paid for the defect it guards.

## The turn format

| key | meaning |
|---|---|
| `responder` | what the responder said, fed verbatim to L1's deterministic net |
| `findings` | typed clinical findings established *this* turn; cumulative across the scenario |
| `assumed` | which of those findings were ASSUMED_WORST rather than reported (ASM-14) |
| `speech` | what the agent would say, if this turn exercises B4's output gate |
| `retrieved` | the protocol documents the model was shown, which is what a citation can cite |
| `expect` | the oracle — see below |
| `why` | why this turn is here. Prose, for the reader. |

## The oracle

| key | asserts |
|---|---|
| `step` | L2's `AssessmentStep` |
| `criticality` | the **incident's** level after this turn, from `TriageState` — so the ratchet is visible |
| `letter` | which ABCDE letter the branch treats |
| `provenance` | `evidence` or `assumption` — whether a clinician can still correct the level |
| `assumed_inputs` | exactly which inputs the path took on trust |
| `escalated` | whether a clinician has been asked for |
| `escalation_status` | the `EscalationStatus` member |
| `marker` | the L1 marker id, or `null` for "nothing should fire" |
| `gate` | `approved` or `rejected` |
| `gate_reasons` | which `RejectionReason`s fired |

An unknown key in `expect` is a **load error**, not a warning. An expectation the runner
does not read is an expectation the author believes they wrote, and that is this repo's
signature defect (see `harness.py`'s module docstring).

## What these do not assert

`harness.py:unreached_requirements()` lists the DESIGN.md §8 rows whose evidence column
says `harness` and which need infrastructure a headless run does not touch. It is printed
on every run. Nothing here asserts FR-001, FR-002, FR-003, FR-008, FR-011, NFR-001,
NFR-002, NFR-003 or OBS-002.
