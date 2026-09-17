---
description: Multi-persona project readiness review - 18 expert personas judge the project against demo and production bars
argument-hint: "[persona,persona|roles|principles] (optional; default all 18)"
---

# Project readiness review

Run a multi-persona readiness review of this project. `$ARGUMENTS`, if present,
is a comma-separated subset of persona keys; otherwise run all eighteen.
The shorthand `roles` selects the eleven role personas, `principles` the seven
principle personas.

**Role personas** (what part of the system): `pm`, `qa`, `architect`,
`system-design`, `ux`, `systems`, `marketing`, `field`, `security`, `devops`,
`devsecops`. `system-design` is dual-mode: in a review it judges layer placement
and separation of concerns; in a design-council session it decomposes a
requirement into layer-assigned tasks.

**Principle personas** (what class of mistake): `proof`, `test-behavior`,
`domain-model`, `boundary`, `type-discipline`, `shared-state`, `idempotency`.
Adapted from the `principle-*` skills in `cursor/plugins` (pstack); each applies
one rule as a lens.

Agent names are `readiness-<key>` for both groups.

## Procedure

### 1. Dispatch — all personas in parallel

Launch every selected persona in a **single message with multiple Agent tool
calls** so they run concurrently. Each gets this prompt:

> Review the Aiscelapeus project at the repository root from your persona's lens.
> Read `.claude/agents/_readiness-rubric.md` and follow its output format exactly.
> Inspect the actual code, not just the README. Cite `file:line` for every
> blocker or mark it `ABSENT:` with the paths you searched. Return only the
> structured block the rubric specifies.

Do not review the project yourself while they run, and do not pre-empt or
predict their findings.

### 2. Aggregate

When all have returned, build the report. The aggregation is where the value is
— not in concatenating seven opinions:

- **Score matrix.** Table of every persona's DEMO and PROD score. Report the
  role-persona mean and the principle-persona mean **separately**, then the
  overall. The two groups measure different things and a blended number hides
  the disagreement between them.
- **Convergence.** Issues raised independently by three or more personas. This is
  the highest-signal section — lead with it. Name which personas converged, and
  mark any convergence that spans **both** groups as `CROSS-GROUP` — a defect
  found from both a role lens and a principle lens is the strongest signal in
  the report. Convergence among principle personas alone is weaker than it looks,
  since they share a source and may restate one defect in seven vocabularies;
  collapse those into a single finding naming the root cause.
- **Contested.** Where personas disagree, or where one persona's blocker is
  another's strength. State both sides; do not resolve it silently. A principle
  persona demanding a structure that a role persona calls unnecessary ceremony is
  a real tradeoff — present it as one, do not average it away.
- **Unverified claims.** Anything a persona marked `UNVERIFIED CONCERN`, kept
  separate from evidenced findings.
- **Per-persona detail.** Each block verbatim.
- **Prioritized actions.** One ranked list across all personas, each item tagged
  `[DEMO]` / `[PROD]` / `[BOTH]` with the personas backing it.
- **Delta.** If earlier reports exist in `docs/reviews/`, read the most recent
  and report what moved: scores up or down, blockers closed, blockers that
  survived. A blocker present in two consecutive reviews gets flagged as
  **stale** — it is being deferred, not addressed.

### 3. Write and publish

- Write to `docs/reviews/YYYY-MM-DD-readiness.md` (today's date). If one exists
  for today, suffix `-2`, `-3`.
- Publish the report as an Artifact for sharing. Load the `artifact-design`
  skill before writing the page.
- Print a short verdict in chat: the two mean scores, the top three actions, and
  the single thing most likely to sink the project. Do not paste the whole
  report into chat — it is in the file.

### 4. Do not fix anything

This command reviews only. Report findings and stop. Fixes are a separate,
explicitly requested task.
