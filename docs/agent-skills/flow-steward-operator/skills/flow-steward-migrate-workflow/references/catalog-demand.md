# Catalog discovery and integration demand evidence

Use for a multi-platform migration campaign or a missing-integration backlog.
Refresh official catalogs and export documentation at the start of the campaign.
Preserve source attribution and inspect reuse terms before publishing adaptations.
Catalog availability, access to an executable export and conversion support are
three separate facts.

## Select sources and qualify support

Start with n8n, Make and Activepieces, then inspect other relevant B2B catalogs.
Use official evidence for each platform: catalog URL, export documentation URL,
observed date, source/export version, authentication/access restrictions and reuse
terms. When there is only a recipe or screenshot, report feasibility and request
the actual export through the authorized source; do not invent omitted logic.

The bundled deterministic converter accepts only its qualified n8n JSON subset.
Make blueprints and Activepieces flow exports require agent-assisted mapping of
their actual format. Other source platforms require the same inspection plus a
format-specific mapping recipe; listing a platform below is not parser support.
Keep parser gaps separate from Flow Steward operation gaps.

Discovery starting points below were reviewed on **2026-11-03**. Reopen the
official pages before use: formats, access tiers, galleries and service notices
can change. Priority is a B2B research choice, not a market-share ranking.

| Research order | Source and catalog evidence | Export evidence and boundary |
| --- | --- | --- |
| Existing | [n8n catalog](https://n8n.io/workflows/) | [Workflow JSON](https://docs.n8n.io/workflows/export-import/); inspect template-specific download and reuse terms. Existing narrow parser plus qualified agent-assisted adaptations. |
| Next | [Make templates](https://www.make.com/templates) | [Blueprint JSON](https://help.make.com/blueprints); a public card need not expose anonymous JSON. Instantiation/export can require an account. |
| Next | [Activepieces template library](https://www.activepieces.com/docs/flows/template-library) | Inspect the authorized Explore/import/export interface: actual versioned single-flow JSON or multi-flow package is required; the preview is not the export. |
| Later B2B | [Zapier templates](https://zapier.com/templates) | [Zap JSON export](https://help.zapier.com/hc/en-us/articles/8496308481933-Import-and-export-Zaps-in-your-Team-or-Enterprise-account) is documented for Team/Enterprise. A template card does not contain the configured flow. |
| Later B2B | [Power Automate templates](https://learn.microsoft.com/en-us/power-automate/get-started-logic-template) | [Non-solution ZIP](https://learn.microsoft.com/en-us/power-automate/export-import-flow-non-solution) and [solution export](https://learn.microsoft.com/en-us/power-automate/export-flow-solution) differ. Verify environment roles, connector licenses and dependencies. |
| AI/knowledge | [Dify Marketplace](https://marketplace.dify.ai/) | [App export](https://docs.dify.ai/en/cloud/use-dify/workspace/export-import-apps): version-dependent DSL `.yml` or `.ifpkg` ZIP containing `app.yaml` and files. Inspect version and contents, exclude plaintext secret environment values, map tools/plugins/knowledge dependencies. |
| Inspectable graphs | [Node-RED Flow Library](https://flows.nodered.org/) | [Flow JSON export](https://nodered.org/docs/user-guide/editor/workspace/import-export). Distinguish nodes from complete flows and account for third-party packages, context and message semantics. |
| Scenario research | [Albato Solutions](https://albato.com/en/wiki/articles/solutions) | Sharing/installing a Solution is not proof of a downloadable general export. No general JSON/YAML export established by this survey; complete conversion requires an actual artifact. |
| User exports | [Pipedream export docs](https://pipedream.com/docs/workflows/export-workflows) | Project ZIP with manifest and versioned workflow JSON. Check current service/migration notices and skipped/truncated manifest entries; former templates URL did not establish a current workflow gallery in this survey. |
| User exports | [Sim workflow sync](https://docs.sim.ai/cli/workflow-sync) | Portable JSON; [current official comparison](https://www.sim.ai/comparisons/make) says template gallery/APIs were removed. Recheck availability, inspect literal secrets and workspace reference bindings; do not advertise a gallery from old links. |

None of these later sources has a qualified deterministic converter in this
package. For Make and Activepieces, qualify representative real exports next;
for other formats, provide researched mappings and honest unverified status until
their actual conversions have passed the required executor checks.
The reviewed Dify Marketplace exposed both plugins and workflow-template sections;
plugin install counts are not workflow-run counts. Inspect each selected artifact
and the metric's scope instead of classifying the whole marketplace as one format.

For a new format, inspect a real export before assembling YAML. Account for its
typed values, item/bundle cardinality, branch/merge and iterator semantics,
triggers/checkpoints, authentication, error/retry behavior and external effects.
Retain the raw export hash, but remove credentials before including source data
in a public package. Do not execute source code as a shortcut to translation.

## Freeze a useful B2B sample

Record selection filters and date, business use case and the actual selection
method. Prefer lead intake/enrichment, CRM synchronization, support routing,
invoice/document processing, notifications and operational reporting. Do not
filter away hard cases after seeing conversion failures.

If the source supplies usage, installation counts or a popularity sort, record
the metric name, value or rank, scope, evidence URL and observation time. With
no such evidence, label the sample `curated_b2b`, not `most_popular`. Search
position, template count and promotional copy are not usage measurements.
Random checks use a frozen relevant pool, seed, exclusions and sampling without
replacement. Never combine different vendors' usage metrics as if comparable.

## Machine-readable campaign ledger

Emit `campaign.json` with these collections. Use null/empty values with an
explanation for unknown facts; the field list is a reporting contract, not a
requirement to manufacture evidence.

| Collection | Required fields and meaning |
| --- | --- |
| `sources` | `platform`, `catalog_url`, `export_docs_url`, `observed_at`, `access`, `export_format_version`, `reuse_terms_url`, `coverage` (complete/partial/unknown), `discovery_errors`. |
| `workflows` | `source_key` (`platform:id`), `source_url`, `source_version`, `export_sha256`, `use_case`, `canonical_workflow_key`, `duplicate_of`, `selection_method`, `popularity` (metric, value/rank, scope, evidence URL, observed time, confidence), `conversion_status`, `artifact_sha256`, and separate structural/contract/fixture/live results. |
| `operation_occurrences` | `source_key`, source node IDs, normalized `provider`, `resource`, `operation`, required auth/cardinality/pagination/effects, `status`, selected target route, exact native/MCP/extension operation/version/schema evidence, inventory observation time/scope, installed/authorized connection state, and remaining blockers. |
| `integration_candidates` | Normalized provider/operation, missing behavior, alternatives checked, evidence/coverage, distinct workflow and source-platform counts, blocked workflow keys, `unlock_workflow_keys`, `unlock_count`, and priority rationale/confidence. |

`popularity.confidence` refers to the evidence, not guessed popularity. Keep
direct customer requests in a separate `customer_demand` record with provenance
and consent/privacy limits; catalog occurrence alone is not customer demand.

Classify each occurrence as one of:

- `native_verified`, `mcp_verified`, `existing_child_verified` or
  `simple_http_verified`: the exact known contract covers the operation; record
  project setup separately.
- `setup_needed`: a usable route is known but install/connection/permissions or
  resource identity is not configured.
- `contract_needed` or `availability_unverified`: incomplete discovery or missing
  schema; this is not verified absence.
- `adapter_needed`: source expressions/control-flow/paging need translation using
  existing primitives.
- `capability_gap`: fresh, sufficiently complete target discovery established that
  the required behavior has no suitable native/extension/MCP/child/simple-HTTP
  route. Include the concrete extension contract proposal separately.

Use [integration selection](integration-selection.md) for discovery and route
ordering. Record the underlying route when setup is needed, so an unconfigured
Google Workspace connection is not counted as a missing Google integration.

## Deduplicate and prioritize operations

Normalize vendor aliases and resource/operation semantics, not just node names.
Create and upsert, send and reply, polling and webhook intake are different
operations. Count a provider-operation at most once per canonical workflow even
if many nodes use it. Record raw node occurrence counts separately.

Deduplicate source versions/reposts/forks and obvious cross-platform copies by
business behavior, normalized operation graph and mappings. Retain all source
provenance and platform sightings. Record uncertain duplicate decisions rather
than merging merely because titles match. Publish both raw template counts and
deduplicated workflow counts with the deduplication method and sample size.

Rank genuine missing operations by distinct B2B workflows affected, distinct
source-platform sightings and the additional complete workflows they unlock.
For an operation's standalone `unlock_count`, count only workflows whose other
capability gaps are already covered; a workflow blocked by two missing
integrations is not unlocked by either alone. Report combinations separately.
Setup, undiscovered schemas, parser work and live verification stay separate.

Supply source workflow keys and evidence beside every count. Popularity metrics
can support prioritization within their own comparable scope, but do not turn a
curated sample's frequency into a market-share or customer-demand claim. Report
what improving an existing extension action would unlock before proposing a new
provider extension.

## Deliverables

Return the frozen source selection, full YAML/mapping/setup artifacts when
representable, operation ledger, verified capability candidates and a concise
coverage table. Keep `source_parser`, `agent_assisted`, `structural`, `contract`,
`fixture_engine`, `live_provider` and publication states distinct. A catalog page
or a clean parser result does not establish a complete executable conversion.

For each source format newly claimed as supported, qualify a representative real
export through the real target executor and known operation handlers using
explicit transport fixtures before calling that conversion path fixture-verified.
