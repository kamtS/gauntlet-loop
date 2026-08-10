---
name: gauntlet-loop
description: Turn a substantial build or refinement request into a ready-to-run prompt that divides work among independent workers, assigns separate adversarial critics, and repeats revision with explicit evidence and a bounded stopping rule. Use when a user asks for a gauntlet loop, parallel workers and reviewers, iterative self-critique, a prompt that loops until a quality bar is met, or a reusable orchestration prompt for Claude Code, Codex, tfcode, or another agent host with delegation support.
---

# Gauntlet Loop

Create a concise orchestration prompt for the user's task. Preserve three roles: an orchestrator, workers with non-overlapping ownership, and independent critics who try to disprove that the work meets its acceptance criteria.

## Build the prompt

1. Extract the deliverable, constraints, references, acceptance criteria, and available verification tools from the request.
2. Ask at most one focused question only when a missing answer would materially change the result. Otherwise state a reasonable assumption in the generated prompt.
3. Split the task into independent workstreams only where parallelism helps. Keep tightly coupled work sequential.
4. Assign each workstream to one worker with explicit ownership and a concrete output.
5. Pair each worker with a different critic. Prevent a worker from approving its own work.
6. Require critics to begin from a fail-until-proven position, cite concrete evidence, and return actionable findings rather than general impressions.
7. Require workers to revise against unresolved findings and critics to re-check the new artifact.
8. Set a finite pass limit and define escalation behavior for work that still fails.
9. Require the orchestrator to integrate the work, resolve conflicts, run end-to-end verification, and report residual risks.
10. Return the prompt in one copyable code block followed by only the assumptions or usage notes that matter.

## Adapt the evidence

Choose verification that fits the artifact:

- Code: tests, type checks, linting, security checks, runtime behavior, and diff review.
- Interfaces: screenshots at relevant viewports, interaction checks, accessibility checks, and comparison with supplied designs.
- Documents: render the final artifact, inspect every page, check facts and links, and compare with the brief or template.
- Data or research: validate calculations, source coverage, freshness, and traceability.
- Creative work: compare with the brief and references using a concrete rubric; do not substitute taste for acceptance criteria.

When no objective reference or acceptance criteria exist, say that the loop can improve consistency and polish but cannot prove that the chosen direction is correct.

## Use this prompt structure

```text
TASK
<Deliverable, constraints, references, and definition of done.>

ORCHESTRATION
Act as the primary orchestrator. Decompose the task into independent workstreams only where parallel execution is useful. Assign each workstream to one worker with non-overlapping ownership and a concrete output. Keep integration decisions with the orchestrator.

For every worker, assign a separate critic. The critic must try to refute the worker's claim of completion, assume failure until evidence proves otherwise, and evaluate against <acceptance criteria/reference> using <appropriate verification>. The worker must address every valid finding; the critic must re-check the revised artifact. A worker may not approve its own work.

Run at most <N> worker-critic passes per workstream. Stop early when the critic reports no material failures and provides supporting evidence. If the limit is reached, stop looping and surface the unresolved findings, evidence, and recommended next action.

INTEGRATION BAR
Do not declare completion until the orchestrator has reconciled the workstreams, run end-to-end verification, reviewed the integrated result against <definition of done>, and reported the evidence plus any residual risks. Preserve user work and stay within the authority granted by the task.
```

## Apply guardrails

- Default to three passes. Raise the limit only when the value justifies the added time and token cost.
- Prefer a strong initial implementation or reference. Do not use repeated critique to compensate for an undefined brief.
- Keep worker scopes independent and bounded. Avoid creating more agents than useful workstreams.
- Give critics access to the produced artifact and verification evidence, not only the worker's summary.
- Treat critic approval as input to the orchestrator, not as proof that integration succeeded.
- Preserve normal safety, approval, privacy, and repository boundaries in the generated prompt.
- Tell the user that the host must support delegation to execute the prompt as written. The skill itself does not transfer sessions, state, or tasks between Claude Code, Codex, and tfcode.
