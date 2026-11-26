# ADR 0079 Classify a task only when nobody chose its workflow

Status: Accepted

Date: 2026-11-21

## Context

Before every run the routing node called the task classifier: a separate
request to the project's model that returns five labels — `effort_hint`,
`risk_level`, `automation_recommendation`, `confidence` and a reason. The
classifier was introduced when it chose the workflow for a task.

The workflow is now always named by whoever starts the run: the operator form,
the API, a schedule, a webhook, a provider trigger, a rerun or a parent
workflow. A run without a `workflow_id` is refused before orchestration. What
remains of the classifier's answer:

- `confidence` describes the workflow choice; the autonomy gate already ignores
  it for a named workflow;
- `automation_recommendation` sets the default autonomy mode when the project
  sets none. `supervised` never pauses a named workflow; only `human_only` puts
  every step on manual approval — a model deciding, non-deterministically, to
  stop a run;
- `effort_hint` goes into the agent prompt's "Execution Mode" block, and all
  labels are available to the workflow as variables.

The price is 3–4 seconds and model tokens on every run, including workflows
without a single AI step. On a receipt-reading workflow it was a sixth of the
run time.

## Decision

1. The model classifies a task only when the caller did not choose the
   workflow. Sources in `EXPLICIT_WORKFLOW_SELECTION_SOURCES` get a
   deterministic classification without a model request: `effort_hint=standard`,
   `risk_level=medium`, `automation_recommendation=supervised`,
   `confidence=1.0`, reason `explicit_workflow_selection`.
2. A person's decision is explicit: an Approval step the author places where it
   is needed, and the project's or step's autonomy policy. A model can no longer
   put a run on manual approval by itself.
3. `provider_trigger` and `rerun` are explicit selections: a provider trigger is
   bound to one workflow and a rerun starts its job's workflow. They join
   `EXPLICIT_WORKFLOW_SELECTION_SOURCES` and keep their own source label (the
   routing node used to rewrite it to `explicit_request`).
4. Where the classifier still runs, it asks for `low` reasoning depth: five
   labels need no deliberation. A project can set the classifier's own depth in
   `agents_config.agents.config.classifier`.

## Consequences

- A run of a named workflow makes no extra model request: 3–4 seconds faster
  and cheaper, including workflows without AI steps.
- A project without an autonomy mode that used to get pauses from `human_only`
  no longer gets them. A needed pause is an Approval step or the `manual` mode
  of the project or step.
- The labels in the prompt and the variables of a named workflow are constant
  (`standard`/`medium`); a workflow that branched on them always sees the same
  values.
- A resumed run with labels already recorded (`resume`, redelivery) uses them as
  before.
