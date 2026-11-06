# ADR 0059: Workflow Report Documents

- Status: Proposed (checkpoint 2A of the component workflow reports epic; no
  production code may depend on it until the owner approves it)
- Date: 2026-11-06
- Related: [ADR 0033](0033-workflow-authoring-application-boundary.md),
  [ADR 0040](0040-attempt-output-contract-receipts.md),
  [ADR 0042](0042-bounded-state-selector-shape-proof.md)
- Contract: [Workflow Report Schema v1](../workflow-report-schema-v1.md)

## Context

Workflow authors want to return a finished report — key numbers, tables and
charts built from earlier steps — and have it shown on the run result page and,
later, inside compatible chat clients through MCP Apps. Today the only way to
produce presentable output is the Template step, which renders one Jinja text
in `text`, `markdown` or `html`. An HTML report would put author markup and
library options into run results, could not be validated as data, and could not
be rendered safely by a chat host.

The run boundary already exists. A workflow's Interface lives in
`settings.callable`; `expected_outputs` declares the outputs; at the success
stop the validation node checks them against the pinned run definition and the
terminal write stores the result in `job.meta.workflow_outputs`. Completed jobs
expose `outputs` through the public run projection. A report should use that
path rather than a new step kind, store or result channel.

## Decision

### 1. A report is a Template format

The Template step gains `format: report`. In that mode `override.template`
holds the existing `inputs` and `result_target`, a required `report` object
(ReportDefinition v1) and no `text`. The step compiles the definition against
its declared input values and writes the resulting object (ReportSnapshot v1)
to `result_target`. No Jinja runs in this mode. The `text`, `markdown` and
`html` modes are unchanged. No new step kind, output mode or storage is added.

### 2. Two documents: definition and snapshot

- **ReportDefinition v1** is authored layout: `schema_version`, `title`,
  `locale`, `time_zone`, optional `summary`, optional `datasets`, and
  `components`. Bindings name a declared Template input and an optional
  relative path in the canonical state path grammar; they never name workflow
  state directly.
- **ReportSnapshot v1** (`kind: "fs.report"`, `schema_version: 1`) is the
  resolved result: title, locale, time zone, `summary_text`, projected
  `datasets` and resolved `components`. Datasets keep only the fields that
  components read, in a deterministic order; nothing else from the inputs is
  stored.

The component catalog is closed: `text` (paragraph or heading), `metric`,
`table`, `chart` (`line` or `bar`) and `section`. Values carry explicit typed
formats: `text`, `boolean`, `number`, `currency` (ISO 4217 code required),
`percent` (fraction convention, 0.18 = 18 %), `date` and `datetime`. The schema
document fixes null, zero, false, missing, default and empty-array semantics,
limits, error codes and the JSON Schemas of both documents, and the frozen
fixtures in `tests/fixtures/workflow_reports/` are the reference input and
output for the compiler and the renderer.

### 3. Owner: `core/application/workflow_documents/`

A new application package owns the definition model, its validation, the
compiler and the snapshot validator with version dispatch. It is separate from
`core/application/reports/`, which owns operational reporting, dashboard
metrics and job detail and result bundles; nothing moves out of that package
for this feature.

Dependency direction:

- `orchestration` (Template runtime) calls the compiler;
- `registry` (Interface and boundary validation, authoring readiness) calls the
  definition and snapshot validators;
- jobs queries and entrypoints read stored snapshots through the jobs owner.

`workflow_documents` imports only leaf helpers: the canonical path parser
`core/application/orchestration/state_paths.py` (which depends only on
`transform_selectors`, itself depending only on `core.infrastructure.standard_json`) and
infrastructure time zone helpers. It does not import `registry`, `jobs`, other
`orchestration` modules, `entrypoints`, React or MCP code. The compiler is a
pure function: no I/O, clock, model call or randomness.

### 4. Interface selects the report: `settings.callable.report_output`

`report_output` is optional. When set it names exactly one declared
`expected_outputs` row that has `type: object` and `required: true`. A
selection on a disabled Interface is an error rather than silently ignored.
At the success boundary the selected output must be a native JSON object that
passes strict ReportSnapshot validation; failure fails the run with
`workflow.output.report_invalid` and nested `report.*` errors. The public
output schema describes the selected output with the exact ReportSnapshot v1
JSON Schema instead of a generic object.

The selected value is taken only from run state (`state.outputs` or
`state.workflow_data`), never from message `_state_patch` fallbacks, verified
direct artifacts or `workflow_terminal` payloads. Arbitrary message artifacts
therefore never become the published report. Any producer that writes a valid
snapshot to the selected output is accepted — a Template report step, or a
child workflow's report forwarded through Invoke.

### 5. Storage

The snapshot is stored where every validated output is stored:
`job.meta.workflow_outputs.outputs[<name>]`. The selected name is captured from
the pinned definition at terminal persistence as
`job.meta.workflow_outputs.interface.report_output`: the validation node
already validates against the pinned definition's interface and persists
`interface.dump()` in the same object, so the selection travels with the
validated result. Readers use that stored name, never the live workflow's
settings, never the layout of the current definition, and never data
reconstructed from logs or messages. Editing or deleting the live workflow does
not change a historical report.

### 6. Identity

A report is identified by the finalized job and the output name. There is no
signature, receipt or producer-provenance claim for report outputs beyond the
existing execution-attempt contracts of ADR 0040. Failed, cancelled, waiting
and pending jobs publish no report, because the public result exists only for
`completed` jobs and a failed boundary validation fails the job.

### 7. Strict validation and bounded size

Definitions are validated at authoring time (draft validate, draft save,
workflow save, apply changes, import) and again before compiling. Snapshots
are validated after compiling and at the workflow boundary. Every limit
(components, depth, datasets, rows, columns, series, text, summary, definition
and snapshot size) fails with an error and a field path. Nothing is truncated,
sampled or coerced to fit. Numbers must be finite; strings are never parsed as
numbers. The v1 limits are proposals until the owner approves them. The MCP
Apps spike (task 1) only checks that delivery is feasible; production delivery
budgets per host are measured and set by the delivery task (task 3), and may be
lower than these limits, never higher.

`summary_text` is what a client without a viewer receives. Without an authored
`summary` it is a deterministic, bounded fallback built from the title, metric
values and table and chart row counts (schema section 4.2); the only omission it
allows is an explicit `[K more items not shown]` line, and only in the summary.

### 8. What a report cannot contain

No raw HTML, Markdown-to-HTML, JavaScript, CSS, callbacks, chart or table
library options, external URLs, images fetched at render time or instructions
to refresh data. Unknown properties are rejected at every level. Renderers own
presentation; the snapshot owns data and typed formats.

### 9. Step messages and previews

A report Template step records a bounded summary in its message (kind, schema
version, component and row counts) and does not copy the snapshot into the
message `_state_patch` or into a string preview. No consumer may persist
`str(snapshot)` in place of the object.

### 10. Future boundary

Stable IDs, an integer schema version and datasets stored apart from components
leave room for renderer-local filters. A refresh is a future, separately
authorized operation that starts a new run and produces a new snapshot; stored
snapshots are immutable.

### 11. Privacy invariant: diagnostics never carry the report

The canonical `workflow_outputs` in run state and in
`job.meta.workflow_outputs` stays complete; it is the report's only store.
Every other projection of a run carries at most a bounded report summary:
`kind`, `schema_version`, `title`, `summary_text`, component count and the row
count of each dataset — never the snapshot, its datasets or rows.

This covers, on success and on failure alike:

- terminal events emitted by the validation node (`emit_event` details on the
  pass and fail paths) and the `proof_of_work` record, which today embed the
  whole `workflow_outputs` (`validation_node.py`, around lines 255, 271, 289
  and 309);
- step messages and string previews (decision 9);
- every diagnostic read that reaches a model: MCP `job_result`, `job_events`,
  `job_messages`, `job_logs`, `job_trace`, `job_runtime_journal`,
  `job_incident` and `job_errors`, and the HTTP routes behind them, including
  nested raw fields (`details`, `artifacts`, `payload`, `raw`, journal entries)
  that could rebuild the payload.

Only the dedicated report read (task 3) returns the snapshot, and only to a
report viewer, outside model-visible content. A failed boundary validation
reports error codes and paths for the report output, not its values.

## Verified against code

Checked in `claude/workflow-reports-20261106` at public main `5764b4ddb0`; the
branch now sits on `codex/release-batch-0.9.12` (`83eb76929a`), which adds only
the 0.9.12 version bump on top of that main:

- `core/application/orchestration/template_step_runtime.py`: formats come from
  `TEMPLATE_FORMATS = ("text", "markdown", "html")` in
  `core/infrastructure/security/jinja_sandbox.py:39`; `validate_template_config`
  (line 123) requires `text`; `execute_template_step` (272) returns `text`;
  `template_step_artifacts` (299) puts the state patch into the message only for
  texts up to 64,000 characters. Inputs resolve through
  `resolve_state_path_with_workflow_data`, so required-missing inputs already
  fail with `input_missing` and absent optional inputs stay undefined.
- `core/application/orchestration/graph/nodes_step_dispatch.py:764-783` writes
  `result["text"]` to `result_target`.
- `core/application/registry/builtin_step_kind_profiles.py:584-680` declares
  `format` as `text|markdown|html` and `required: ["text", "result_target"]`.
- `core/application/workflows/workflow_step_config_contracts.py:173` allows only
  `format`, `inputs`, `result_target`, `text` under `override.template`.
- `core/application/registry/io_resolver_outputs.py:547-555` and
  `io_resolver_preview.py:178-201` type the Template output as `text`.
- `core/application/registry/workflow_contract.py:401-560`: `WorkflowInterface`
  is rebuilt field by field in `from_settings` and `dump`, so an unknown
  `report_output` key would be dropped; `definition_issues` returns no issues
  when the Interface is disabled (line 564).
- `core/application/registry/workflow_contract_service.py`: `output_json_schema`
  (106) emits `{"type": "object", "additionalProperties": true}` for object
  outputs via `_json_schema_for_field` (788); `coerce_contract_value` accepts a
  JSON string for `object` (657); `collect_runtime_outputs` (712) falls back to
  message `_state_patch`, verified direct artifacts and `workflow_terminal`.
- `core/application/orchestration/validation_node.py:203-224` validates outputs
  against the pinned definition and returns `workflow_outputs =
  output_validation.dump()`, which includes `interface`.
- `core/application/jobs/terminal_persistence.py:113-140, 306-312` stores
  `workflow_outputs` in `job.meta` and already rejects NaN with
  `json.dumps(..., allow_nan=False)`; `task_execution_service.py:537-540` is the
  fallback merge.
- `core/application/jobs/workflow_run_projection.py:241-256` builds the public
  result only for `completed`; `workflow_run_models.py:89`
  `PublicWorkflowResultV1` forbids extra fields.
- Canonical selector grammar: `core/application/orchestration/state_paths.py`
  (`parse_state_path`, `resolve_state_path`), discovered by
  `workflow_state_selector_contract.py`, shape-proved by
  `core/application/registry/state_selector_shape.py` (ADR 0042).
- MCP `workflow_authoring_schema`, `workflow_draft_validate`,
  `workflow_draft_save` and `workflow_apply_changes`
  (`core/entrypoints/mcp/server_workflow_authoring_tools.py`) forward to
  `GET /api/workflow-authoring/schema`, `POST
  /api/projects/{id}/workflow-authoring/validate`, `.../drafts` and `POST
  /api/workflows/{id}/changes` (`server_api_client.py:48-57`), served by
  `core/application/workflows/authoring/` (`schema.py`, `drafts.py`).
- Child outputs: `core/application/orchestration/invoke_workflow_step_node_results.py`
  returns the child's final contract outputs when `ok` is true (73-77) and
  otherwise reconstructs values from child messages and many row containers
  (80-135).
- Portability: `workflow_portability_definitions.py:66-90, 131-170` exports
  settings and step overrides through `_redact_sensitive`, which replaces values
  under keys containing `password`, `secret`, `token`, `bearer`, `credential`,
  `api_key`, `access_key`, `private_key`, `client_secret`
  (`workflow_portability_redaction.py:13-27, 99-116`).

## Deviations from the brief

1. **There is no "Response" node or setting.** The run result boundary is the
   workflow Interface (`settings.callable`), edited in the product under
   Interface › Outputs and reached from the Stop card. `report_output` is
   therefore an Interface property; "Response" in the brief means this.
2. **Terminal persistence is not in `task_execution_service.py`.** The primary
   writer is `core/application/jobs/terminal_persistence.py`;
   `task_execution_service.py` only holds a fallback merge.
3. **The selected name needs no new persistence step.** It is captured by adding
   `report_output` to `WorkflowInterface`, because `workflow_outputs.interface`
   is already the pinned definition's interface dump.
4. **`core/application/reports/` is not only operational analytics.** It also
   owns `job_result_bundle.py` (`GET /api/jobs/{id}/result`, MCP `job_result`)
   and the job detail projections. That bundle returns workflow outputs and step
   messages, so a report snapshot would reach model-visible MCP output through
   `job_result` unless task 3 bounds it.
5. **`workflow_apply_changes` cannot set `report_output`.** `OPERATION_KINDS`
   (`workflow_definition_changes.py:45-64`) has step, group, for-each and graph
   operations only; `update_step_config` can write `override.template.report`,
   but the Interface is reachable only through a draft bundle or `PATCH
   /api/workflows/{id}` (which the product UI uses and which preserves unknown
   `callable` keys).
6. **`workflow_authoring_schema` does not describe `settings.callable` at all.**
   Its minimal bundle has empty settings and its guidance covers groups,
   for-each and hooks. Report authoring needs Interface guidance added there.
7. **Kept test outputs are capped at 256,000 bytes**
   (`workflow_kept_test_outputs.py:23`), below the proposed 1 MiB snapshot.
8. **Template inputs already validate types** (`text`, `number`, `boolean`,
   `object`, `array`, `json`), so a report binding can be checked against its
   input's declared type at authoring time. Those checks reject a top-level
   `null` for every type except `json`, and `resolve_state_path(value, "")`
   reports a `null` root as not found. A nullable whole input is therefore
   declared `json`, and a whole-input binding reads the alias from the Template
   context by presence (schema section 2.3); neither existing helper changes.
9. **The repository's `canonical_json_bytes` escapes non-ASCII**, so size limits
   are measured with `ensure_ascii=False` compact JSON instead.

## Consequences

- One report contract serves the result page, MCP Apps and the future editor;
  renderers need no knowledge of workflow state.
- Authors and agents must shape data in earlier steps (Transform, dataset
  queries); the report neither aggregates nor coerces.
- Strict rules make some real data fail loudly (numbers as strings, rows missing
  a key). That is intended; the error names the field.
- Snapshots up to 1 MiB live in `job.meta` JSONB and in run state; they are not
  duplicated into step messages. Run state is checkpointed, so a snapshot held
  in state may be written again by every later checkpoint that rewrites the
  same state channel. Checkpoint 2B measures checkpoint growth for a
  maximum-size snapshot followed by several steps and, if growth is material,
  places the report step last or lowers the limit before the limits leave
  proposal status.
- `jsonschema` (already a runtime dependency), Babel (already a runtime
  dependency) for locale and currency checks, and the existing time zone helpers
  are sufficient. No new dependency is added.

### Schema consumers that must change (checkpoint 2B unless marked)

| Area | File | Change |
| --- | --- | --- |
| Template config | `core/application/orchestration/template_step_runtime.py` | `report` format, conditional `text`/`report` validation, compile instead of render, bounded message summary, no snapshot in `_state_patch` |
| Template formats | `core/infrastructure/security/jinja_sandbox.py:39` | stays Jinja-only; the step keeps its own format list that adds `report` |
| Step dispatch | `core/application/orchestration/graph/nodes_step_dispatch.py:764-783` | write the snapshot object, not `text` |
| Allowed keys | `core/application/workflows/workflow_step_config_contracts.py:173` | add `report` |
| Step profile | `core/application/registry/builtin_step_kind_profiles.py:584-680` | `format` enum with `report`, `report` property referencing the definition schema, `text` required only for text formats, report example and use cases |
| Readiness | `core/application/registry/contract_validation_workflow.py:396-406`, `contract_validation_workflow_readiness.py:700-716` | definition validation against declared inputs through `validate_template_config` |
| Output types | `core/application/registry/io_resolver_outputs.py:547-555`, `io_resolver_preview.py:178-201` | Template output type `object` when `format` is `report` |
| Selectors | `core/application/orchestration/workflow_state_selector_contract.py:316-326` | keep discovering only `inputs[].source`; report bindings are not selectors |
| Interface model | `core/application/registry/workflow_contract.py:401-560` | `report_output` field in `from_settings`, `dump`, `definition_issues` (checked before the disabled early return) |
| Boundary | `core/application/registry/workflow_contract_service.py:106-125, 376-440, 657, 712-767, 788` | exact snapshot schema in `output_json_schema`; strict snapshot validation; native object only; state-only source for the selected output |
| Public result | `core/application/jobs/workflow_run_projection.py:241-256`, `workflow_run_models.py:89` | expose the stored `report_output` name with the completed result |
| Child workflows | `core/application/orchestration/invoke_workflow_step_node_results.py:73-135` | forward a child's report output only from an `ok` final contract, never from the message/container fallback |
| Group extraction | `core/application/workflows/workflow_group_extraction_bindings.py:390` | the extracted child gets a fresh Interface; the parent keeps its `report_output` and must still declare that output |
| Test runs | `core/application/workflows/platform_test_run_runtime.py:588-631, 972`, `test_run_result_values.py`, `test_run_query_service.py:76-183` | snapshot survives `redact_test_run_payload` unchanged |
| Kept outputs | `core/application/workflows/workflow_kept_test_outputs.py:23` | decide the cap for report steps (see open questions); a kept snapshot is still validated at the boundary |
| Export/import | `core/application/workflows/workflow_portability_definitions.py:66-170`, `workflow_portability_redaction.py` | round-trip test; report keys avoid redaction markers |
| Authoring schema | `core/application/workflows/authoring/schema.py:77-185` | report vocabulary from the one schema, Interface and `report_output` guidance, a report example bundle |
| Drafts | `core/application/workflows/authoring/drafts.py` | validate and save through the same validators |
| Change sets | `core/application/workflows/workflow_definition_changes.py:45-64` | `update_step_config` validates `report`; a canonical Interface operation that writes `settings.callable` in the same single write (recommendation 2) |
| Edit sessions | `core/application/workflows/workflow_edit_session_contract.py`, `workflow_definition_service.py:324-420` | carry the Interface operation; judge Interface issues with structural and readiness issues |
| Interface summaries | `core/application/workflows/workflow_contract_summary.py`, `knowledge_projection_builder.py:238-293`, `canvas_graph_service.py:222` | carry or show the report selection |
| Terminal events (2B) | `core/application/orchestration/validation_node.py` (`emit_event` details on pass and fail, `proof_of_work`) | replace the selected report output with its bounded summary (decision 11); the canonical `workflow_outputs` passed to terminal persistence stays complete |
| MCP read path (task 3) | `core/application/reports/job_result_bundle.py`, `core/entrypoints/mcp/server_job_tools.py:46` | keep the snapshot out of model-visible content |
| Diagnostic projections (task 3 verifies) | `core/entrypoints/mcp/server_job_tools.py` (`job_events`, `job_messages`, `job_logs`, `job_trace`, `job_runtime_journal`, `job_incident`, `job_errors`) and their `/api/jobs/{id}/…` routes | prove with a large fixture report that none of them, including nested raw fields, returns snapshot datasets or rows |
| Product UI (tasks 3–4) | `assets/frontend/src/pages/workflows/detail/detail-template-step.tsx:21-22`, `detail-workflow-settings-controller.ts:330-355` | format list, report editor, output selection; the controller already preserves unknown `callable` keys, so a renamed output leaves a stale selection that save must reject |
| User and agent docs | `docs/user/workflows.md`, `docs/agent-skills/flow-steward-operator/skills/flow-steward-migrate-workflow/references/full-yaml-assembly.md` | describe the report format |

## Alternatives rejected

- **A new `report` step kind.** Duplicates input declaration, selector
  discovery, readiness and preview paths that the Template step already owns.
- **HTML or Markdown report through the existing Template.** Not validatable as
  data, unsafe in chat hosts, and ties stored results to presentation.
- **Raw ECharts or table options in the definition.** Couples stored history to
  a library version and lets authors inject behaviour.
- **A report table or a separate report store.** A second source of truth for
  run results; retention, access and deletion already follow the job.
- **Reading layout from the current workflow at view time.** Edits would
  rewrite history.
- **Signing snapshots or recording producer receipts.** The job and output name
  already identify the result; provenance beyond ADR 0040 is not required for v1.
- **Putting the feature in `core/application/reports/`.** That package owns
  operational analytics and job detail projections with different lifecycles and
  callers.
- **Truncating rows or text to fit.** A report that silently omits data is worse
  than a failed run.

## Open questions and proposed recommendations

The recommendations below are **Proposed**, for one owner approval together
with this ADR. They are not accepted decisions, and they do not authorize
production code.

1. **Limits.** *Recommendation:* approve the v1 domain limits as written in the
   schema (section 6). Host delivery ceilings are not domain limits; task 3
   measures and sets them per host, lower than or equal to these.
2. **Setting `report_output` on an existing workflow.** *Recommendation:* add
   one canonical, atomic Interface operation to `workflow_apply_changes`, so the
   AI Builder sets `report_output` (and the outputs it names) through the same
   permissions, `expected_revision`, single revision and history as every other
   change. The operation name and payload are fixed in the change-set contract
   during 2B. No second authoring path is added, and agents are not pointed at a
   raw `PATCH /api/workflows/{id}`. Implementation note, verified in
   `workflow_definition_service.py`: `apply_workflow_changes` currently writes
   the locked workflow's own settings plus only `execution_graph`, and the
   change set's working definition (`_Definition` in
   `workflow_definition_changes.py`) carries no `settings.callable`. The
   operation therefore extends that working definition with the Interface,
   judges Interface issues (`WorkflowInterface.definition_issues`, including the
   `report_output` checks) together with the structural and readiness issues,
   and writes `settings.callable` in the same single write. The builder's edit
   session contract must carry the same operation.
3. **Kept test outputs.** *Recommendation:* keep `KEPT_OUTPUT_MAX_BYTES =
   256_000`. Keeping a larger report output is refused with an explicit error,
   with no partial or truncated keep; this matches the current
   `normalize_kept_outputs`, which already refuses an oversized value. The cap
   applies to Keep only: a successful run may still produce and store a report
   snapshot up to the 1 MiB domain limit.
4. **Row fields.** *Recommendation:* table columns and chart fields name exact,
   case-sensitive row keys. Nested data is shaped by a Transform step before the
   Template; no relative paths into rows in v1.
5. **Summary locale.** *Recommendation:* `summary_text` stays locale-independent
   plain text (schema sections 4.1 and 4.2). Rendered values in the product and
   in MCP Apps are formatted by the snapshot's `locale` and `time_zone`.
