# Source formats and qualification boundaries

## n8n JSON

[n8n export documentation](https://docs.n8n.io/build/manage-workflows/export-and-import.md)
describes JSON workflow exports. Credential IDs/names and hardcoded HTTP auth
can be present; they are source-instance metadata, not portable connections.

The deterministic preview consumes one workflow object with `nodes` and
`connections`. Qualified node families are Manual Trigger v1, No Op v1, Set
v3–3.4, HTTP Request v3–4.3 and If v2–2.2. Exact supported options are deliberately
narrow: read the returned blockers. Version ranges here enumerate qualified
minor versions, not a promise about arbitrary future versions.

- Main edges are output-index to named target-input connections. A simple If
  uses index0=true and index1=false. Multiple targets on one output are fanout;
  auxiliary `ai_*` edges are not main-flow edges.
- Nodes process n8n items. HTTP JSON arrays can become multiple items; one
  Flow Steward state object is not an automatic equivalent. Named-node item
  linking, `$items`, `$input`, `.all()`, binary items and merge pairing require
  explicit adaptation.
- Set currently qualifies matching literal assignment types and blocks dynamic
  coercions. The preview translates whole `$json` field selectors and JSON scalar
  expressions only; it never evaluates JavaScript or arbitrary Jinja. Mixed
  strings, function calls and embedded target-template syntax are blockers.
- Source pinData/staticData, nondefault settings, execution flags and unknown
  versions/options are not silently ignored. Multi-trigger, joins, loops and AI dependencies currently need agent-assisted adaptation.
  Independent linear fan-out branches can become native Parallel Run Group lanes
  with explicit `--parallel-fanout`; it is an adaptation of source v1 ordering,
  not an assertion that Flow Steward lacks fan-out.
- Recognized authentication fields become required secret workflow inputs.
  Rebind credentials in the target; provider/OAuth connection records are never
  copied. Verify literals elsewhere before sharing any source export.
- Converted requests use the target's bounded inline HTTP response runtime;
  response size, redirects, retries and transport details still need parity
  review for the provider. Local conversion leaves `target_validation=required`; validate through the existing target authoring tools before import. It is not a live migration test.

## Make blueprints

[Make blueprints](https://help.make.com/blueprints) export scenario JSON.
Inspect module IDs, `module`/`version`, mapper expressions, filters, nested
routes, scheduling, iterator/aggregator semantics and connection references.
`{{module.field}}` names a source module output; it is not a Flow Steward state
selector. Router branches and aggregators need explicit routing and cardinality
mapping. A trigger's checkpoint/schedule cannot be inferred from a visual chain.
There is no qualified deterministic Make parser in this release.

## Activepieces exports

Use the actual exported flow/template and its versioned trigger/action tree.
Inspect piece name/version, action/trigger identifiers, `nextAction`, branch and
loop bodies, input mappings, connection references and error settings. Piece
availability in Activepieces does not establish a matching target operation.
The [template library](https://www.activepieces.com/docs/flows/template-library)
exposes used pieces and flow previews; a preview is not full execution proof.
There is no qualified deterministic Activepieces parser in this release.

## Catalog selection

Prioritize B2B lead intake/enrichment, CRM updates, email/Slack notifications,
document/invoice handling and operational reporting. Use authoritative catalog
exports with provenance, timestamp and source version. Call a template popular
only when the source exposes an actual popularity ordering or metric; search
position, an author claim and a large catalog are not such evidence. Preserve
attribution and review template licensing before publishing adaptations.

Classify target gaps by operation: native operation verified, HTTP replacement
verified, child-workflow replacement verified, connection/setup needed,
semantic adaptation needed, missing operation verified, or availability
unverified. This keeps the module backlog distinct from credential setup and
conversion-engine limitations.
