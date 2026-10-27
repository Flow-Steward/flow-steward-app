# ADR 0010: FlowSteward Builder Test Run Rule

## Status

Accepted

## Context

The workflow builder accumulated multiple overlapping runtime surfaces and terms
for debugging and execution, including `Simulate`, `Preview run`, `Run Lab`,
step-level preview flows, and detached pages. This created product ambiguity,
hid variable-mapping diagnostics behind secondary paths, and made new step
kinds feel incomplete unless someone manually stitched together contract and
runtime checks.

For FlowSteward 1.0, the builder needs one builder-native execution story with
testability as a release gate for every shipping step kind.

## Decision

FlowSteward adopts `Test Run` as the single user-facing execution term inside
the builder.

Rules:

- Workflow-level execution inside the builder uses `Workflow Test Run`.
- Step-level execution inside the builder uses `Step Test Run`.
- `Test Run` means real execution, not validation-only, preview-only, or
  planning-only behavior.
- The builder must not add parallel user-facing runtime surfaces named
  `Simulate`, `Dry Run`, `Preview run`, `Run Lab`, `Prompt Preview`, or
  `Contract Preview`.
- Workflow `Test Run` must open inside the builder drawer from the main
  canvas/header control row.
- Step `Test Run` must live inside the step drawer.
- Every shipping step kind must provide builder editing, contract visibility,
  missing-input diagnostics, `Test Run`, structured result/error presentation,
  and secret-redacted execution details before it is considered complete.
- Step variable visibility must stay in the default builder UX through an
  always-visible summary that includes `Inputs`, `Mapped from`, `Missing`, and
  `Outputs`.
- Advanced-only controls such as `Optional step` remain editable only in
  advanced mode, with compact read-only visibility allowed when already
  configured. Parallel lanes are workflow groups, not a step control
  ([ADR 0035](0035-workflow-steps-without-phases.md)).

## Consequences

- Builder UX converges on one execution vocabulary and one default debug path.
- Contract visibility becomes part of the normal step-editing experience.
- New step kinds now carry a higher completeness bar but produce less product
  drift and fewer partial launches.
- Retired runtime routes and labels are not valid templates for new user-facing
  work; builder execution uses the `Test Run` surfaces above.
