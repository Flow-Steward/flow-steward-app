# Compose equivalent business behavior with existing Flow Steward capabilities

Read the current product README/user workflow guide and tool contracts before
concluding a source feature is absent. The product has step kinds for Agent,
Connector (system/extension/MCP), HTTP, Dataset Query, Transform, Tabular Feed,
File from base64, Template, If, Switch, Approval, Wait, Set Variable, Collect,
Continue, Break, Batch and explicit Stop outcomes; Group/Parallel lanes and
For each are containers. Use the current version's schemas and placement rules.
Do not add core code during a migration.

## Native replacements are compositions

| Source need | Target composition to try |
| --- | --- |
| Conditions, routers, joins | If/Switch plus graph edges; branches can share a continuation. Independent work uses Group lanes and joins at group exit. |
| Item mapping, batching, aggregation | Transform/Set Variable, For each, Collect, Batch; move a nested item processor into a callable child when containers cannot nest. |
| JSON reshaping and typed outputs | Transform typed actions, structured agent/skill outputs, connector mappings; Tabular Feed for formats supported by the selected producer/reader pair. A missing generic JSON-text parser does not mean JSON processing is absent. |
| CSV/XLSX/full datasets | Tabular Feed read/transform/concat/write/read_rows/merge_rows. Process bounded pages without silently treating preview rows as the full dataset. Use a guard and explicit bound when the draft's supported input envelope is bounded. |
| HTML/Markdown/email body | Template Jinja variables, conditions, loops and filters over prepared typed data. It emits text; choosing format does not parse arbitrary source HTML or convert Markdown to HTML. |
| Review and resume | Native Approval with approved/rejected edges, feedback/response mappings and explicit rework; notifications choose the review channel. Email-specific reply ingestion is an adapter, not absence of approval. |
| Gmail/attachments/labels | Official Gmail MCP and complementary Workspace extension operations, including attachment download. Discover exact operations before HTTP. |
| New-message/folder polling | Schedule + provider search/history/list + persisted cursor/seen IDs through a DB connector + per-item processing. Declare cursor schema/initial position and no-overlap/dedup policy; store progress after accepted effects. Do not require a dedicated source-named trigger node. |
| Conversation memory | Read ordered last-N messages by project/session through DB connector, map into agent inputs, append the new exchange. Supply saved query/operation contracts and schema requirements. No invented generic memory service. Curated internal project context is separate. |
| Dates and timezones | Existing standard DateTime tools (`std.date` in published contracts): parse, add, format, timezone, day boundaries and interval operations. Select a supported time source; never invent `std.date.now` if absent in that version. |
| Child workflow | Invoke workflow + typed inputs/outputs/files. For parallel children use Group lanes. Document source fire-and-forget→joined behavior changes; ordinary child support is not missing. |
| Provider operation with no ready action | Verified bounded HTTP, optionally as an included child. Google Maps calls do not inherently require a new extension. |

## Authoring a full adapted unbound draft

The default goal is the business process, not byte-for-byte reproduction of
incidental legacy bugs, implementation timing, source node names or unused
notes. Include `migration_mode: business_equivalent_adaptation`, a concrete list
of behavioral differences, input bounds and source-defect decisions in artifact
metadata and setup instructions. Keep it disabled and `runtime_verified: false`.
Do not change write intent, invent data, truncate records silently, omit a source
branch, or claim an adapted result is an exact translation.

Credentials, registry IDs, project connection selection and exact installed
argument names can remain explicitly unbound/provisional. They block execution,
not creation of the draft. Supply full agent/skill definitions and prompts, not just symbols for unspecified
future agents. Use top-level `definitions` only if the target version accepts it.
A strict six-section v2 importer refuses that extra section: then provide
`definitions.yaml` for explicit registry provisioning (or embed its review copy
in `metadata.authoring_definitions`), declare agent/skill dependencies, and make
that setup step explicit. Parsing a bundle does not provision those companion
definitions. A connector symbol
must denote an existing documented operation, with real behavior described; it
cannot be a new catch-all action such as “execute the source JavaScript”.

Keep source schedules/webhooks as declared channels and executable polling or
event processing. List the schedule/subscription setup separately; a schedule
record is not created merely by writing metadata in the bundle. A manual-only
replacement must be disclosed as an adaptation, never called trigger parity.

For a supported draft input bound, add a fail-closed guard and describe the bound
(e.g. maximum materialized rows). A guard may narrow an adapted draft; it must not
silently process only the first page and report complete success. Distinguish
full structure within its declared envelope from unbounded source parity.

Simple Code operations can be translated to native steps/Jinja. For regex,
recheck the installed Standard Tools catalog and use `connector.std.text` with
`regex_find`, `regex_find_all`, `regex_replace`, or `regex_replace_all` when
available. Match input is `value` + `pattern`, optional `flags` (`i/m/s/x/a`, each at most once, maximum 5 characters).
`result` is the full first match or null for find, and an ordered array of full
match strings for find all; capturing groups never change the result shape.
Replace takes `replacement` (Python backreferences), replacing the first or all
matches. Translate JS `/pattern/g` into a bare Python-compatible pattern and the
all operation; `g` is not an accepted flag. Verify source character classes,
flags, zero-width matches and replacement syntax instead of claiming JS parity.

These actions reject limits rather than truncate (1 Mi characters input/output,
4096 pattern/replacement characters, 1000 matches, 100ms matching timeout).
Compilation also has a conservative counted-repeat expansion budget of 65,536;
reverse/inverted spans and ambiguous counted syntax are rejected.
Dependency `tool_id` must be the actual registry ID (`std.text` here); the
step uses `connector_id: connector.std.text`. Check dependency resolution too:
a valid step schema and a successful step test do not establish that the
importer can resolve a connector-prefixed dependency ID.
Add a workflow guard for a narrower supported input envelope when needed. Do not
fall back to a model on regex errors. Older installations without these actions
remain a precise pending dependency; an explicitly requested extractive-agent
alternative is nondeterministic and must be declared as such.

A model/embedding integration in progress is a declared pending dependency.
Provide the workflow structure if the intended operation is known, and list the
required model, dimensions and vector-store compatibility for later activation.
A step that only stops for missing setup does not implement the underlying
behavior: the rest of the full graph still has to exist.

## Verification is part of assembly

Apply the [runtime-contract checks](runtime-contracts.md) before emitting the full
graph. In particular, qualify a dataset producer and reader together; a returned
handle alone does not establish readable serialization or complete row coverage.

Check the bundle graph and exact configuration keys against the version-matched
validators. Resolve every selector through success and failure fixtures: typed
arrays, per-item scopes, branch outputs, checkpoint writes, review feedback and
child input/output mappings. Capture connector/HTTP payloads using synthetic
responses; never dispatch providers during an offline request. Unknown live
schemas remain explicit, not a fabricated validation pass. Reject no-op graphs,
missing child implementations and unimplemented parser/script placeholders.


## Version and data-shape checks that change the emitted graph

A standard tool action and a Transform action are separate registrations. If the
target has `std.date` but its Transform registry lacks `date.*`, call the actual
standard tool through a Connector; do not add an unsupported Transform action or
patch core during migration. Use the published parameter/format-token schema.

Keep error routes in their scope. A loop body cannot jump directly to a root
error handler: propagate failure to the container's error policy, or route to a
body-owned failure step. Route native `error`, not an invented `failure` outcome.

Check HTTP dispatch, not only the transport result envelope: in the current
runtime an inline `response_target` receives the body itself, and an artifact
`response_target` receives the scalar artifact handle. Do not append
`.response_body`/`.artifact_handle` to those variables. HTTP interpolation accepts
state selectors/defaults, not Jinja filters: use typed body objects with source
references, or render JSON in an upstream Template and reference that text.

Set Variable resolves source references at assignment level, not recursively
inside a literal array/object. Construct typed nested payloads with separate
assignments and Transform actions. For example: scalar cell object → json.values
row array → wrapper object → json.values matrix for Sheets inline_rows.

Optional provider fields can be omitted or null. Map the whole documented result
and normalize optional fields before iteration; declaring a Template input
optional does not make null valid for type text. Use type json for nullable
values and test omitted-field cases as well as populated responses.


## Complete adapted examples

For a newly forward-tested B2B example, read
[monthly finance №6294](../assets/offline_examples/monthly_finance_6294/workflow.yaml),
its [setup](../assets/offline_examples/monthly_finance_6294/SETUP.txt) and
[qualification](../assets/offline_examples/monthly_finance_6294/qualification.json).
It includes full definitions, source JSON/mapping and19 synthetic scenarios. The
real executor and released Workspace0.3.3/SMTP0.3.1 handlers were exercised with
fixture transports. Read the declared UTC, row/money conventions,100-row envelope,
SMTP delivery and aggregate-report adaptations before reuse. Connections, model
choice, scheduler and live OAuth/permissions remain setup, not tested facts.


For a full scheduled B2B email digest, inspect
[`assets/adapted_examples/gmail_digest/workflow.yaml`](../assets/adapted_examples/gmail_digest/workflow.yaml)
and its [agent definitions](../assets/adapted_examples/gmail_digest/definitions.yaml)
and [setup requirements](../assets/adapted_examples/gmail_digest/SETUP.txt).
It composes run-clock metadata, standard DateTime connectors, paginated Gmail
search/get, typed aggregation, an agent and HTML delivery. It is a disabled
unbound adaptation, not an installed integration inventory or a live-tested
provider example. Keep its bounded input and time/filter differences explicit.

Full disabled drafts using native regex are included as
[Maps email №2567](../assets/adapted_examples/maps_email_2567/workflow.yaml) and
[Maps email №5385](../assets/adapted_examples/maps_email_5385/workflow.yaml).
Their adjacent `SETUP.txt` files describe connection setup and source adaptations;
they are not live-tested or published catalog entries.
