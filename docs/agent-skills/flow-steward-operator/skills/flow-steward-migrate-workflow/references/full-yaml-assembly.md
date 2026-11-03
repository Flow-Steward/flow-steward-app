# Assemble a complete workflow, including deferred setup

Use after source inspection when the parser cannot translate supported behavior,
or when the user requests a portable draft before connections/secrets are ready.
This is agent-assisted composition, not a claim that the bundled CLI supports
new node types. Use the current authoring schema or version-matched platform
contracts; do not change core to make a migration work.

## Output states

| State | Deliverable and readiness |
| --- | --- |
| `bound_draft` | Full disabled YAML with discovered operation contracts and actual dependency mappings; validation and provider execution are still separate. |
| `unbound_draft` | Full disabled YAML with declared symbolic dependencies, setup requirements and any explicitly provisional argument mappings. Not ready to import/save/run. Use when the user defers setup. |
| `blocked` | Source mapping plus precise blockers when some required behavior cannot be represented. Do not emit a success-shaped partial parent. |

Connection/secret/field IDs can remain unset in an unbound draft. If an operation
is documented but its installed input schema is unavailable, mark the mapping
provisional in both artifact metadata and setup report, and list exactly what
must be confirmed. Do not treat structural validation as contract verification.
If even the required operation or target semantics is unknown, report that
uncertainty rather than inventing a functioning step. The user may request an adapted business-equivalent draft. Record that mode and
its behavioral differences; no claim of exact source execution is implied.
Adaptations must be explicit; unsupported behavior cannot be hidden as a setup requirement.

Read [runtime contracts](runtime-contracts.md) before graph construction, then
[native composition recipes](native-composition.md) for schedules + polling,
database-backed memory, approval/rework, typed data and native tool reuse. Resolve
source constructs by composition before declaring them unrepresentable.

## Assembly recipe

1. Account for every source node and edge in a source-to-target map. Record
   triggers, items/ordering, loops, parallel groups, joins, errors and retries.
   Notes need no execution step. Name any deliberately adapted behavior and its
   consequence; do not call an adaptation an exact translation.
2. Refresh native/extension/MCP/workflow discovery using
   [integration selection](integration-selection.md). Reuse matching operations
   first, verified simple HTTP next. Check each operation, not only the vendor.
3. Compose the full `platform.workflow_bundle.v2` graph. Keep `enabled: false`,
   `import_state: draft`, no unintended automatic triggers, complete outcome
   routing and terminal steps. Preserve a source automatic trigger or explicitly
   report its deliberate replacement; manual invocation is not equivalent.
4. For unresolved tool identities, declare a **template-local symbol** in
   `dependencies.tools` and reference the exact same symbol in `connector_id`.
   This is an import dependency to map later, never a made-up installation ID.
   Example dependency only (not a full bundle or real connector):

   ```yaml
   dependencies:
     tools:
       - tool_id: connector.company_record_create_template
         display_name: Bind the selected company record-create operation
         required: true
         sources: [create_company.connector_id]
         metadata:
           binding_required: true
           reference_kind: template_local_symbol_not_a_real_installation_id
   ```

   Resolve using supported `map_existing` to an actual discovered registry action.
   A server ID is not a projected action ID. Use the installed manifest's dispatch
   contract: a remote MCP tool name need not equal its projected function name.
   Do not populate a guessed `function_name`; a single-action tool may use its
   manifest default. Keep provisional argument mappings clearly marked.
5. Put credentials behind required secret inputs with no literal/default key.
   Keep MCP authorization on its connection. Do not copy source-instance base,
   connection or credential IDs. List table/field/project IDs as setup inputs or
   explicit replacement markers, including every location that must be replaced.
   Add a setup guard before provider steps where feasible. `enabled: false` alone
   is not a guarantee that manual invocation cannot execute a request.
6. Preserve types through the data mapping. Template steps produce **text**, not
   parsed objects. Connector mappings select state values; do not assume recursive
   interpolation of nested JSON. Build a typed record array with supported
   Transform/SetVariable primitives before passing it to a connector. Check
   nested state writes and mapping behavior against the current runtime contract.
   Do not put JSON text where `records` expects an array.
7. Supply the parent plus required children in the bundle. A reusable HTTP child
   is useful only with the parent wiring, inputs/outputs and error behavior.
   Preserve create versus upsert and duplicate behavior. Do not add automatic
   retries to writes unless their idempotency and source semantics support them.

## Verification and handoff

Use target draft validation or the authorized browser preview when available.
For offline-only requests, perform local bundle/graph checks using available
version-matched validators; exercise mappings and branches with synthetic data
and network disabled. Execute actual target nodes and known released operation
handlers against fixture provider transports; capture payloads and effects. Replace
only documented IO/host boundaries, keeping validators, mapping and business
handlers real. Unknown operation schemas stay unbound, not mocked into success. Do not execute source Code nodes to obtain a fixture.

Check success shape, wrong response shape, missing required fields, provider
failure routing and unbound setup. Verify that record arrays remain typed and
that no secret is included in normal outputs. Record the artifact hash, validator
version/source, performed checks and unperformed checks. A synthetic graph walk
is not a full engine run, installed MCP schema validation or provider test.

Deliver:

- Full YAML and source-to-target mapping, with `bound_draft`, `unbound_draft` or
  `blocked` status; `runtime_verified: false` until actually verified.
- Setup instructions and machine-readable requirements: dependency key, selected
  documented operation/evidence, schema certainty, unresolved connection/field
  inputs and whether import/save/run is blocked.
- Fresh inventory evidence: observation time, scope, versions, coverage/errors.
- A separate extension-candidate list (empty when none established). Each entry
  names a required behavior, checked alternatives, evidence and proposed contract.
  Unknown discovery, parser work and missing credentials go in their own lists.
- Structural/synthetic, dependency/contract, import and live execution results
  separately. Never publish or claim catalog readiness from offline checks alone.

For n8n #835, the reusable pattern is manual invocation → verified Brandfetch
HTTP → typed Name/Icon/Logo projection → Airtable MCP record creation. Missing
Airtable authorization is setup, not an extension gap. Modern Brandfetch response
projection and one-row-per-company selection must be disclosed if they replace
legacy item expansion. Do not hard-code this example's provider IDs or assumptions
into other migrations.
