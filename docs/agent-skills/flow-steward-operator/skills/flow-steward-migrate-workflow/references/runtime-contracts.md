# Runtime contracts that determine the graph

Read before full YAML assembly. These checks come from executed migrations, not
from provider names. Reinspect the selected target and released operations on
every migration: a newer version may close a gap or change a recipe. Record the
source commit, extension version/hash, operation/schema and observed response.
The examples below were qualified against core 0.9.9 and the released Workspace
0.3.1/0.3.3, SMTP0.3.1 and PostgreSQL1.4.0 contracts; they are not a claim about every
installation. Inspect current sources or describe the installed operation before
using a different version. Do not patch core to fit generated YAML.

## Typed values and graph placement

- A Template emits text. JSON-looking output is still text. Build connector
  arrays/objects through supported typed Transform/SetVariable/Collect steps.
- A SetVariable assignment `{source: state.some.path}` copies that value. An
  assignment `{value: {}}` creates the literal object with key `value`, not an
  empty-object initializer. Initialize with `{}` itself. A source reference
  inside a literal object/list is not recursively resolved; assign nested fields
  separately or use a supported Transform operation.
- Transform `input.source` must be an actual state path such as
  `state.row_wrapper`; bare `state` is invalid. Make a wrapper with SetVariable
  when the transform needs multiple pieces of state.
- Transform destinations must satisfy the selected operation's identifier schema;
  a spreadsheet heading with spaces is not necessarily a valid `to` name. Use
  quoted/bracket source selectors for headings containing spaces or punctuation.
  Confirm operation result types before chaining: audited `array.at` yields JSON,
  which needs validation before it is treated as text.
- For fixed-width sheet rows, supply explicit defaults for missing/null fields
  before projecting objects through `json.values`. Audited Transform omits such
  fields without defaults, compacting the row and shifting later columns. Assert
  column order and width, including a blank middle field followed by nonblank
  cells, then verify that a second update preserves the first update and tail.
- Connector output mapping and state input selection are different mechanisms.
  An output selector such as `rows.0.status` can fail even when
  `state.saved_rows.0.status` is a valid state input. Capture the whole array,
  check cardinality, then project through SetVariable. Do not rewrite every
  provider's wrapper to a universal shape.
- One graph source/outcome has one target. Enter/leave groups and loops through
  their container nodes/exits. A Continue has no outgoing edge. Put loop-local
  error handlers in the loop or propagate through the container's error route;
  a body step cannot jump to a root handler.
- An error edge requires its source step's own supported
  `error_policy.on_error: route_to_step`; an edge alone does not enable routing.
  Use the exact container-exit schema, including explicit `lane_id: null` for
  the audited `for_each_exit` contract when no lane applies.
- Count the maximum nested child invocation depth, including helpers introduced
  for paging. Inline a helper container when necessary; ordinary child support
  does not mean arbitrarily deep invocation is allowed.
- Keep portable agent/skill declarations within the importer's allowed fields.
  In the audited bundle format, `expected_inputs[].source` is rejected. Put state
  selections in the agent step's input mapping, not in a skill declaration.
- Use actual callable channel values: the audited UI channel is `ui`, not
  `manual`. Public callable inputs are under `state.inputs.*`, not implicitly
  promoted to root state. Test the same admission channel used by the real caller;
  a fixture launcher's wrong channel must be fixed in the launcher.
- Inspect PromptAgent's normalized output before mapping it. In the audited
  version, a declared `executive_summary` output is selected directly as
  `executive_summary`, not through an invented `outputs.executive_summary` wrapper.

A minimal array-projection recipe (fragments, not a complete bundle):

```yaml
# Connector output_mapping_jsonb:
rows: state.claim_rows
# After checking state.claim_rows has exactly the expected result count,
# native SetVariable override.set_variable.assignments:
state.claim_status:
  source: state.claim_rows.0.status
```

## Operation arguments and result shapes

Use the concrete operation contract and execute its real normalization path.
An extension manifest, its handler response and the connector-visible outputs
can have different envelopes.

### Portable integration identity is not a fixture binding

A fixture registry's arbitrary `connector.foo` ToolManifest ID can execute on
that fixture host without identifying any portable native extension. Do not
publish those symbols as though the real importer can discover an installation.
Inspect the version-matched exporter/importer and canonical export dependencies.
In the audited core0.9.9, a native logical reference can use
`connector_id: extension.<discovered-extension-id>` with the actual operation in
`override.connector.operation_id`. For example, the discovered native packages
used `extension.flowsteward.google-workspace` and
`extension.flowsteward.outgoing-email`; these are examples, not a permanent
integration inventory. The exporter records the extension dependency and import
resolves the selected installation. MCP projected tool identities follow their
own discovered contract; do not convert every tool ID to this native form.

Before publication, obtain a canonical target export including required children
and `definitions`; inspect extension dependencies and connection requirements,
not just the executable steps. A provisioning companion alone is insufficient
for a self-contained catalog bundle. If portable routing identity, definitions,
graph closure or mapping changes, rerun real validation and fixture execution on
the final exported bytes and record new hashes. Prior fixture success does not
qualify a different published artifact. If export/round-trip cannot be checked,
report that gap rather than calling a fixture binding portable.

- Audited SMTP `send_email` exposes flat `delivered` and `provider_message_id`.
  `result.delivered` failed after the email had already been sent. Test mappings
  after the actual send handler; a fake `{result: {delivered: true}}` conceals
  the bug. Omit an optional empty `cc` instead of supplying `cc: ''`.
- Workspace operation results often use `result.*`; preserve their real wrapper.
  Gmail `search_messages` returns IDs, not full email evidence. Hydrate with
  `get_message` before classification/summarization, and use the documented
  `message_id`, `internal_date`, `rfc_message_id` and body fields. Parse mailbox
  addresses before operations that require a bare email address.
- Audited native Gmail search requires `label_id`, always sending a label
  constraint. It cannot reproduce all-mailbox queries merely by using INBOX.
  Typed `filters.to` with SENT covers sent-history, but a raw `filters.query`
  and negative RFC Message-ID matching are not supported by that release.
  `set_message_flags` changes seen/flagged, not arbitrary labels. A verified MCP
  operation may cover the rest; without its schema, retain a precise blocker.
- An HTTP `response_target` holds the inline body directly or a scalar artifact
  handle in artifact mode. Read it accordingly, not through invented
  `.response_body` or `.artifact_handle` suffixes.
- OAuth includes renewal, consent, scopes and permitted resource access. A
  Graph GET/PATCH/POST sequence using a supplied token verifies routing only.
  It does not implement a managed Outlook integration. Do not call it ready.

## SQL, memory, polling and deduplication

Use the published connector's restricted SQL language, not arbitrary PostgreSQL
syntax. Validate through its real parser and execute against a disposable local
DB when fixture qualification is requested.

- Saved query records use `params_schema_jsonb`; the core adapter translates to
  operation input `param_schema_jsonb`. Direct `execute_write` uses its documented
  `sql`, `params`, `param_schema`. Do not confuse persistence and operation fields.
- Audited write operations accept one parameterized INSERT/UPDATE/DELETE using
  named `:parameters`; do not assume top-level WITH, positional `$1`, transaction
  scripts or invented query-name execution are supported.
- The read-only parser restricts functions and FROM shapes. JSON aggregation
  functions and a derived FROM subquery were rejected. For last-N memory, use
  a scoped direct-table SELECT, a supported correlated COUNT of newer rows and
  deterministic chronological ordering; include DDL and saved-query records.
  Preserve paired exchanges versus individual messages explicitly.
- Map whole write `rows` before indexing. Require the intended affected/returned
  count. Zero-row lease/claim/update results are not successful external work.
- Scope memory/claims/checkpoints by the actual tenant/project/mailbox/session
  keys. Test fresh claim, concurrent claim, replay after done and recovery after
  uncertain effects. Save progress only after accepted effects. Do not auto-retry
  an uncertain email/write without an idempotency or reconciliation contract.
- Provider page tokens are pagination state, not automatically durable polling
  watermarks. Preserve the source envelope and pagination semantics; use a
  DB-owned bounded time window and exact per-message filtering when adapting
  day-resolution searches. Establish initial position, overlap and lease owner.
- A test launcher without a persisted job ID needs explicit fixture ownership;
  keep production `state.job_id` references intact in the original YAML. A
  caller-supplied timestamp must not silently become a trusted production clock.
- SQL datetime serialization may differ from RFC3339. Normalize at a documented
  boundary before passing it to Drive or another strict timestamp contract.
- Native date operations need an explicit timezone policy. Audited `date.diff`
  requires a common timezone for offset-aware timestamps; pass the supported
  timezone argument (UTC only when that is the chosen business policy) and test
  offset/date boundaries rather than stripping timezone information.

## Sheets and datasets: prove full coverage before writing

Qualify the producer/reader pair using real stored bytes and metadata. In the
audited version, Workspace finalizes NDJSON, but Tabular Feed `read_rows` treats
that non-Parquet object as a whole JSON document. A fixture that rewrites it to
JSON would hide the incompatibility. A handle is not a guarantee of readability.

For a bounded workflow-only adaptation:

1. Use native preview only after proving `len(preview) == row_count`, the row
   bound, schema/column order and byte budget. Preview can be byte-truncated even
   below its row limit. An incomplete preview fails before effects.
2. If the declared envelope exceeds preview capacity, use serial bounded A1
   ranges, preserve first-page headers, rename subsequent `column_N` fields
   through typed steps and reconcile the aggregate count with the full count.
   Preserve the original declared bound; do not silently reduce1000 to100.
3. Reject page gaps/trimmed physical rows or other conditions that would shift
   update coordinates. State the supported table shape and input envelope.
4. For `write_google_sheet` overwrite, inspect the actual clearing rectangle.
   The audited handler clears below/right of `start_cell`, beyond the supplied
   rows. Updating a middle row therefore requires writing its replacement plus
   every following row and required columns, usually via a real native dataset.
   Reread before each sequential update so stale snapshots do not undo earlier
   writes. Do not simulate a row-only clear in a fixture.
5. Verify both append and update, including a distinct untouched row after the
   target. Restrict an adaptation to dedicated ordinary-value tables with one
   writer when formulas/types/formatting and concurrency are not preserved.
   Describe these limits instead of claiming unrestricted Sheets parity.
6. Count every submitted inline row, including a header. A 100-row write limit
   does not accept 100 data rows plus a header. For an existing table, a data-only
   replacement starting at A2 can preserve the header, subject to the same real
   clearing semantics. Recheck limits in the selected release; 100 is not a
   permanent platform-wide limit.

## Agents, empty values, embeddings and approval

- Required text/collection contract fields can reject empty strings or arrays.
  Empty first-turn history, absent secondary classification and no citations
  can be legitimate: declare optional fields where appropriate, preserve types
  and branch explicitly when business evidence is absent. Do not add fake
  history, matches, URLs or sentinel objects to satisfy a validator.
- Agent input names can collide with runtime state. In the audited version,
  resolving required `messages` can read the empty runtime collection before
  a mapped business value. Use a distinct business name such as `mail_records`
  and test its materialization in an actual PromptAgent, not a fake agent node.
- A RAG no-evidence branch should produce a supported refusal/clarification or
  request review; it must not reuse a canned factual answer from a success
  fixture. Test empty and populated retrieval separately. Keep citation claims
  grounded in supplied text, with empty URLs valid only when none are claimed.
- A model/embedding replacement is an adaptation. Equal vector dimensions do
  not imply the same vector space. Switch both ingestion and query paths and
  require full reindex if changing embedding provider/model. Reinspect current
  native model operations; a stale checkout is not proof a released one is absent.
- Test native Approval both before decision and after real resume. Parking at
  `awaiting_approval` does not exercise SMTP/write mappings after it. Approve
  should permit the intended single effect; reject should not send. Use the
  real resume helper/checkpoint route with a fixture decision. Record any
  synchronous child-launcher limitation instead of treating a paused child as
  unsupported product functionality.

## Qualification evidence

Run `python scripts/check_bundle.py candidate.yaml` first; fix its definite
errors and investigate warnings. This preflight is deliberately incomplete and
never claims runtime or provider validation. Numeric output selectors are warnings:
a numeric dictionary key can be valid; confirm field type before changing it.

Then record these independently, using exact artifact hashes:

| Layer | Required evidence |
| --- | --- |
| Structure | Real version-matched portability and graph/config validation. |
| Contracts | Exact installed/published operation and handler shapes, allowed inputs, connection/permissions status. Catalog metadata alone is insufficient. |
| Fixture engine | Real executor, PromptAgent and known released handlers. Substitute explicit provider/model transports and host IO boundaries; verify intermediate payloads, final effects and failure routing. |
| SQL | Actual restricted parser plus execution/ordering/claims on an isolated DB; no canned query-name rows. |
| Live provider | Separately authorized real connection/OAuth/scopes/provider test. Always unperformed for an offline-only request. |

Fixtures must not manufacture success for an unknown MCP tool or replace the
business handler being qualified. Keep the real artifact serialization, Sheets
clear semantics and output envelopes. Record registry/connection, process host,
authorization, effect ledger, storage, scheduler and queue substitutions. For
fake vectors/models, do not claim retrieval quality or financial reasoning.

Choose test cases from the operations used: source date boundaries and timezone,
no data, incomplete/oversize pages, malformed responses, optional fields, update
in the middle, provider rejection and uncertain effect, duplicate/retry, and
approval/rework. Source notes and JavaScript are data for translation, never
fixture generators to execute. A stopped or failed dependency remains visible.

Deliver full disabled YAML plus definitions/children, source mapping, setup and
adaptation decisions, current inventory evidence, separate blockers and a scoped
verification matrix. Use `fixture_engine_verified` for offline success; preserve
`live_provider_verified: false` and publication/readiness blockers. For legacy
converter reports keep `runtime_verified: false`; do not overload it with a live
integration claim. A fixture pass does not itself authorize publication or prove
live readiness. If the user explicitly requests publication of an offline-qualified
template, follow the catalog's actual publication rules and disclose the verified
layer, unbound dependencies, trigger adaptations and unperformed live checks.
