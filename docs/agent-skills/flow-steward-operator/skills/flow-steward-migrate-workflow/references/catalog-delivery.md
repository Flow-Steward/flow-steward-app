# Deliver converted workflows to the catalog

Use this path when a migration or a batch is intended for publication. Continue
the selected source list; do not substitute search children for full parents or
replace difficult sources with easier ones. A frozen source with incomplete
executable parameters stays a reported exclusion until it is recovered.

## Make the draft self-contained

- Give the parent a readable title and a description of its actual behavior,
  bounds, setup and intentional differences. An identifier or a description
  pointing only to a private `setup.json` is not a usable catalog card.
- Include the required agents, skills, data contracts and guardrails under
  `definitions`, using the target release's schema. A provisioning companion
  alone does not make a portable catalog file. Check categories against the
  target's current category list; `general` and `migration` are not assumed
  categories. Preserve agents' required structured output contracts.
- A template is one root plus the recursive children it invokes. A source with
  independent ingestion and chat entry points can require separate catalog
  templates. Report both roots and their shared dependencies; do not delete an
  entry point merely to pass the catalog's reachability check.
- Use the target authoring schema for state writes and Tabular Feed parameters.
  In particular, business email arrays need an application-owned state name,
  not the runtime-owned `state.messages`. A Tabular Feed rows selector is a
  `state.*` string where the current contract requires it; a guessed
  `{source: ...}` object is not interchangeable.

## Verify the actual installation

Read installed connectors as well as registry tools. Native tools, projected
extension operations and MCP operations may be exposed by different listings.
A search returning no matches does not prove that an operation is unavailable.
Check the current official extension and MCP catalogs too, with observation
time and revisions. Separate catalog presence, installation, connection setup
and operation-schema verification.

Use existing MCP/native/extension operations first. Leave a missing installed
connector as setup to map/install; do not call a catalog-listed Gmail, Sheets,
Airtable or WhatsApp operation a missing platform module. Named database query
helpers in an old draft are not automatically real installed registry tools:
qualify each query and its transaction/checkpoint semantics before binding it.

Run the target bundle import preview without executing the workflow. A
`requires_mapping` result is incomplete setup, not a successful import. Repair
definition/configuration errors separately from setup. When authorized to save,
save disabled objects and validate their saved definitions. A disabled child,
agent or skill remains an execution prerequisite, even when its definition is
included correctly.

An operation name alone is insufficient. Check request arguments and response
fields against the versioned selected operation, including additional fetches
when search returns IDs only. A named SQL query in a setup file is not an
implemented tool. Check its provisioning, parameter schema, read/write policy
and transaction behavior before treating it as available.

For authenticated services, verify the existing connection provider owns the
whole lifecycle required by the workflow. A manually supplied short-lived access
token does not implement OAuth consent, redirects, code exchange or renewal.
Leave that adapter blocked rather than presenting credentials as its only setup.
API-key HTTP adapters still need documented request/response contracts and
actual executor checks, including malformed responses, status failures and
secret handling. Reading provider documentation is not a successful execution.

Check the graph HTTP projection explicitly: an inline `response_target` receives
the response body itself, not the executor's diagnostic envelope. For example,
`response_target: state.search` puts provider JSON at `state.search`; selectors
such as `state.search.response_body` incorrectly assume another wrapper. Exercise
the authored output and provider-error selectors against that actual projection.
Use a provider-specific envelope guard before returning a response declared as
an object: an arbitrary JSON object is not a successful search response. Read
[executor verification](executor-verification.md) for the offline evidence boundary.

## Check the import/export round-trip

After an authorized save, export from the current target with children and
definitions. Inspect that actual export, rather than publishing an earlier
handwritten candidate. Compare the callable input/output contracts, child input
selectors, result mappings, required definitions and graph closure.

Pay particular attention to secret inputs passed to children. A sensitive
mapping key can be redacted even when its value was a state selector. Never
publish a required mapping whose exported value is `[redacted]`, blank or a
literal credential. Repair the authored workflow through supported controls,
then export again. A clearly named child reference input such as
`graph_auth_ref`, still typed `secret`, can preserve a canonical selector such
as `state.inputs.graph_credential` without embedding a key. Verify the runtime's
mapping contract; wrapping an Invoke selector in `{source: ...}` or Jinja is
not a valid repair unless that endpoint actually supports it. Do not change
Core or weaken redaction as part of a migration.

## Publish and report accurately

Use the publishing skill and current catalog README. Validate the final export
with the catalog's actual validator; preserve immutable published files and let
the catalog's index workflow generate URLs, byte counts and digests. Distinguish
a prepared file, a pull request, a merged submission and an entry visible in the
served index. Recheck the served index and target Catalog after publication;
account for its cache instead of claiming visibility from a local build.

Record separately: topology/configuration validation, dependency resolution,
import/save, saved-definition readiness, export round-trip, synthetic scenarios,
engine execution and live provider verification. With provider calls deferred,
state that limit in the template description and pull request; never label
synthetic replay as a successful live run. A catalog verification badge is not
evidence that the importing user's OAuth, model or provider was exercised.

The [unverified Outlook counterexample](../assets/unverified_examples/outlook_categorization/workflow.yaml)
was withdrawn after import-only verification missed its incomplete OAuth adapter.
Its [qualification notes](../assets/unverified_examples/outlook_categorization/SETUP.txt)
explain why a complete graph and preserved secret wiring do not establish a
working integration. Do not reuse or publish it as a qualified template.
