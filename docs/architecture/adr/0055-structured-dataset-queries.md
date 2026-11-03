# ADR 0055: Structured Dataset Queries Alongside SQL

- Status: Accepted
- Date: 2026-11-02
- Extends: [ADR 0019](0019-dataset-connection-actions-in-application-layer.md)

## Context

Datasets already persist a query language and JSON query definition. The shared
saved-query validator and editor nevertheless accepted only SQL. Document,
key/value and search connectors need native structured reads, while existing
SQL queries and extension-owned Dataset projections must continue to work.

Provider drivers, wire protocols and query execution belong in separate
extensions. Suitable existing MCP servers remain a supported product route;
an MCP connection does not register a Dataset connector automatically.

## Decision

Keep the existing Dataset registry, saved query, preview and workflow execution
paths. Support two explicit saved-query languages:

- `sql`: the existing `{ "sql": "SELECT ..." }` definition and validation.
- `json`: an object with exactly `resource_id`, `operation` and `arguments`.

A structured read looks like this:

```json
{
  "resource_id": "orders",
  "operation": "find",
  "arguments": {
    "filter": { "customer_id": { "$param": "customer_id" } },
    "limit": 20
  }
}
```

`resource_id` names an exact, case-sensitive resource in the Dataset scope.
Core accepts the closed read-operation vocabulary `find`, `aggregate`, `get`,
`mget`, `hget`, `hgetall`, `lrange`, `search` and `query`. A connector implements
and accepts only the subset appropriate to its provider. An operation name
alone is insufficient to prove that its provider arguments are read-only.

Core validates JSON types and bounds: at most 64 KiB of UTF-8 serialized
query definition, depth 20 and 10,000 value nodes. Arguments are an object.
A parameter reference is exactly `{ "$param": "name" }`, recursively inside
arguments; the named parameter must be declared in the parameter schema.
Core forwards the original definition and resolved parameter values separately.
Extensions bind references as typed JSON values and revalidate the resulting
provider request, including field/resource permissions and size limits.

Persisted Dataset language and active installed connector language must agree
with the saved query. Where a connector catalog is available, missing,
ambiguous and mismatched bindings fail before persistence or dispatch. Existing
immutable extension Dataset bridges may retain a local connector ID: resolve
it through the bridge's canonical Dataset handle and exact install/account
ownership, rather than guessing a global alias. Existing consumers without a
catalog interface still validate the persisted Dataset language.

The editor obtains the language from the installed connector. SQL retains its
existing parameter syntax and schema builder. Structured queries use a JSON
editor and typed parameter markers. Editing and preview preserve definitions,
parameter defaults/constraints and output schemas without SQL conversion.

Dataset queries remain reads. Writes use guarded workflow operations with their
existing effect claims and test-mode behavior. No database triggers are added.

## Host contract and runtime-only services

Extension host contract 1.5.0 advertises native structured Dataset queries and
is separate from the application build version. Bundles requiring 1.5.0 are
accepted by the default host; explicit 1.3.0 hosts refuse them. Earlier 1.2.0
and 1.3.0 bundle contracts remain compatible.

A `service_extension` with a declared Extension Contract v2 runtime interface
may omit `artifacts` when it provides runtime operations or Dataset connectors
without authoring artifacts. Archive admission, install, update and scan import
use this same descriptor policy. They still validate native target packages,
identity, host compatibility, referenced runtime contracts and permissions.
Explicit empty, null or malformed artifact blocks remain invalid. Artifact
bundles must declare nonempty artifacts; no synthetic self-artifact is added.

Connector runtime context takes its install and connection identity from the
resolved installation and connection. Workflow state cannot select the identity
used for runtime serialization and advisory locks.

## Consequences

- Existing database columns support the contract; no schema migration is needed.
- Provider safety remains explicit: aggregation writes, scripts, cross-resource
  operations, forbidden fields and query budgets must be checked by adapters.
- New native Dataset support requires a compatible Core and provider extension;
  listing an extension or MCP server does not itself prove Dataset support.
- Each database remains separately packaged and versioned. Additional drivers
  do not enlarge unrelated extensions.
- SQL and structured-query regressions cover save, preview, workflow dispatch
  and install-owned projection materialization through their real contracts.

Public runtime 1.4.0 shipped with generic LLM settings before this integration.
Native JSON Dataset and artifact-free service bundles must declare a minimum of
1.5.0; earlier immutable provider releases are superseded by compatibility patch
releases, without rewriting their existing assets.
