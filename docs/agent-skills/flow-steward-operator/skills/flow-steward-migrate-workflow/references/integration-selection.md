# Choose existing capabilities before integrations

For each source operation, inspect the requested project's available native
actions, installed extensions, MCP connectors and reusable workflows. Describe
promising results to get the exact operation, input/output schema, permitted
connection and readiness. Inspect accessible catalogs when local search has no
match; distinguish catalog availability from installed/authorized project tools.
Do not install a connector or extension merely to make a migration report green.

If authoring MCP tools are unavailable in the chat, inspect the existing authorized
Flow Steward browser interface: MCP Catalog, Connected servers, Tools, operation
details and workflow schemas. Record the workspace/project and evidence source.
Catalog presence, connection authorization, published operation schema and workflow
validation are separate facts. An empty Connected list does not erase a catalog
entry. Do not change the in-progress MCP implementation or create credentials to
work around unavailable discovery.
A public Tools reference proves only what it documents; a tool name/description
does not replace the installed version's argument schema and permitted connection.

## Freshness and coverage

At the start of every conversion, refresh the target inventory in the intended
project/workspace. Historical reports, bundled examples and previous migrations
are search hints, never a current availability list. Search both provider names
and the required operations; inspect installed **extensions and their actions**,
current extension catalogs/releases, native actions, MCP catalogs/connected tools,
and reusable workflow contracts. Follow available pagination or explicitly record
partial coverage. Use actual tool discovery for available inventory interfaces;
do not invent a catalog-search tool. If unavailable, use the authorized browser.
Read failures are unknown coverage, not empty inventories.

Record observed time, scope, evidence source, extension/tool version, operation
coverage, installed/authorized state and schema readiness. Describe promising
operations; a newly shipped extension can cover only some of a provider's needs.
Before finalizing extension candidates, recheck the relevant current inventories
and any user-announced new releases. Revalidate selected contracts if versions or
connections changed. Do not refresh credentials, install or upgrade software as
part of this read-only discovery.

## Selection order

1. Use an existing native/extension action or MCP connector operation that covers the
   required behavior. A supplied inventory fact, such as an available Airtable
   connector, is a discovery lead that must be checked. Do not skip it because
   writing a POST request would be easy. Inspect the actual MCP tool rather than
   guessing a function name from the vendor name or public API.
2. Reuse a ready workflow that wraps those capabilities when its contract matches
   the required behavior. Preserve its input/output and error semantics.
3. When no suitable ready operation exists, a simple verified HTTP request or
   reusable HTTP child can cover a bounded provider operation. Verify auth,
   request/response shape, item count, pagination and errors. Existing generic
   HTTP capability does not by itself prove provider compatibility.
4. If the remaining operation needs facilities that existing native/MCP/child
   solutions and simple HTTP cannot provide, describe an extension candidate.
   Keep extension implementation as a separate task.

Simple HTTP means understood, bounded behavior supported by the current HTTP
runtime. A short URL or one GET is not sufficient evidence: custom signing,
OAuth renewal, asynchronous polling, checkpoints, binary transfer, pagination
or event delivery may require additional existing capabilities or an extension.
Do not silently remove those behaviors to call an integration simple.

## Keep different blockers separate

| Situation | Classification and next action |
| --- | --- |
| Parser rejects a native n8n node/version/expression | Adapter work: translate it using target contracts; not proof of a missing provider module. |
| Connector exists, but its exact action/schema is unavailable | Contract needed: obtain discovery or a versioned contract; do not invent IDs or choose HTTP to bypass missing information. |
| Connector is catalog-listed but not installed/authorized in the project | Setup needed: report the available route and required binding. Do not treat setup as a platform capability gap. |
| Connection, base, table or field identity is not yet bound | Setup needed: retain a dependency blocker without copying source-instance credentials or fabricating local IDs. |
| Suitable MCP/native action is verified and ready | Use it and map its typed inputs/outputs; preserve per-item calls and failure behavior. |
| No ready action exists; documented operation fits simple HTTP | HTTP adaptation, optionally published later as a child workflow. No provider extension is inherently required. |
| Required behavior cannot be covered by existing capabilities or simple HTTP | Genuine capability gap: document an extension candidate with evidence. |
| Search/catalog tools are unavailable or the inventory is incomplete | Availability unverified: obtain missing information. Do not label the operation absent. |

An extension candidate records the source node/operation, required behavior,
inputs/outputs, authentication, item cardinality, pagination/event/error behavior,
capabilities and catalogs checked, and why current alternatives do not cover it.

## Complete the requested migration

The bundled n8n parser is a mechanical helper. A parser blocker should lead to
agent-assisted mapping rather than abandoning a solvable complete workflow.
For example, legacy Set fields and qualified named-node references can map to
state assignments while Airtable append maps to an available MCP record-create
operation. Obtain its actual schema for a verified mapping; when setup is
explicitly deferred, use the [unbound draft contract](full-yaml-assembly.md).
Preserve append as record creation; do not silently change it to an upsert.

Account for every source behavior in the full draft, then use existing target
validation. If the user deferred setup, retain explicit unbound dependencies and
contract assumptions in the YAML and setup report. They prevent readiness, not
assembly of the requested draft. Truly unrepresentable behavior still blocks a
complete conversion; do not replace it with a success/no-op step. A separately useful HTTP
child is a scoped intermediate artifact; do not report the parent migrated.
Keep structural validation, dependency readiness and live execution distinct.

Use the [native composition recipes](native-composition.md) before a gap verdict.
A missing dedicated source-named trigger, memory node or approval node is not a
missing feature when a supported native/connector composition implements the
business behavior. Draft adaptations and their bounds must be explicit.
