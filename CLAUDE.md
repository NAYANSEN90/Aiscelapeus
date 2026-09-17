# Aiscelapeus — engineering rules

Real-time first-aid triage voice agent for first responders. `docs/DESIGN.md` is the
engineering design; `docs/reviews/` holds the multi-persona readiness reviews.

## Mandatory build discipline

These are not preferences. They apply to every change, in every session.

### 1. Foundations first

Code is grounded in **design patterns, DRY, and SOLID**. Concretely, in this repo:

- **Dependency inversion is the load-bearing one.** `agent/aiscelapeus/ports.py` defines
  `MossPort` and `PublisherPort` as `Protocol`s. Nothing in the domain or the agent layer
  imports a vendor SDK directly. New external dependencies get a port before they get a
  caller.
- **Single responsibility at the edges.** The STT event, the config, and the data channel
  are parsed once, at the boundary, into a frozen domain type (`Utterance`, `Settings`,
  `FactRecord`). Business logic downstream receives parsed values, never raw vendor objects.
- **Make illegal states unrepresentable.** Prefer an enum over a bool
  (`EscalationStatus`, not `escalated: bool`), a result object over a flag
  (`LevelChange`, not `-> bool`), and a raise over a silent clamp. A model emitting
  criticality `7` is a malfunction to surface, not a value to coerce to `5`.
- **DRY means one source of truth for a rule**, not merely no duplicated text. The
  escalation markers live in `phrases.py` and everything else — prompt, tests, docs —
  derives from that constant.

### 2. Correct first, fast second

Write correct code. Test it for correctness. **Only then** consider optimisation.

A performance number with no correctness evidence under it is not an achievement — it is
the failure mode this project already has on record (a sub-10ms retrieval claim that had
never been executed). Latency budgets are asserted by tests that run, or they are not
asserted at all.

### 3. Independent review after every code change — no self-review

After **every** code change, spawn an **independent reviewer subagent** before committing.

- Use a fresh `Agent` call — `feature-dev:code-reviewer` for implementation diffs, or the
  project's `readiness-*` personas for design-level passes.
- **Reviewing your own diff does not satisfy this rule.** The author's model of the code is
  exactly what missed the defect; a second pass that shares it adds nothing. Two confirmed
  bugs in this repo (`set_level`'s no-op mutation, the `last_trace` cross-task race) survived
  self-review and were caught only by an independent lens.
- Report the reviewer's findings honestly, including the ones not acted on and why.

## Per-subsystem workflow

Build one subsystem at a time, in this order:

```
fakes → tests → implementation → wire → integration test → independent review → commit
```

Do not start the next subsystem until the current one's suite is green and its review is
addressed. Integration comes after the subsystem is proven in isolation, never before.

## Evidence discipline

A requirement is `BUILT` only when something that runs asserts it. Do not mark a row BUILT
whose evidence column names a mechanism another row marks NEW — that is how
`docs/reviews/2026-09-15-readiness-3.md` found five unproven BUILT rows.

## Commands

```bash
cd agent && python -m pytest -q
```

The suite must stay hermetic — no `.env.local`, no credentials, no network. Anything needing
real infrastructure goes behind an explicit flag and is not part of the default run.
