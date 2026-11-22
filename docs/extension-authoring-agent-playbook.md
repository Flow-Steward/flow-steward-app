# Extension Authoring Agent Playbook

This playbook tells an AI agent what to study before writing a real Flow
Steward extension and how to build one by analogy with a published reference
extension from the extension catalog (https://github.com/Flow-Steward/extensions).
Flow Steward Core does not bundle any extension.

For portable developer-client guidance, use the
[Flow Steward Extension Authoring skill](agent-skills/flow-steward-developer/skills/flow-steward-extension-authoring/SKILL.md)
in the Flow Steward Developer package.
[Installation and usage](GUIDE.md#agent-skill-packages) cover
the packages and optional native standalone skills. After an authorized
extension installation, its operations are discovered through the existing
MCP catalog, for example with the Operator package's
[Automations skill](agent-skills/flow-steward-operator/skills/flow-steward-workflow-authoring/SKILL.md).

It is not a replacement for canonical standards. When rules conflict, follow
this order: system/tool rules, direct task instructions, `AGENTS.md`,
`docs/standards/*`, architecture ADRs, then this playbook.

## Required Reading

Before creating or changing an extension, read these files in this order:

1. `AGENTS.md`
   - Repository layout rules.
   - Repository context gathering and Docker execution requirements.
   - Quality gate requirements.
   - Extension authoring belongs under `extensions/`, not first-party app code.

2. `docs/standards/README.md`
   - Canonical layout, terminology, ADR policy, and enforcement expectations.

3. `docs/standards/agent-workflow-standard.md`
   - Required context gathering, consumer checks, verification, and dirty
     worktree discipline.

4. `docs/standards/extension-manifest-standard.yaml`
   - Manifest v2 reference.
   - YAML-only authoring files.
   - Allowed callback auth types.
   - `extension_id` package identity rules.
   - Runtime Contract v2 shape.
   - Read [the extension publication standard](standards/extension-publication-standard.md)
     for official artwork, bundled provenance, and publication review.

5. Documentation site extension docs, relative to the separate
   `flow-steward-doc` checkout (locate it; do not assume a machine-specific path):
   - `docs/integrations/build-an-extension.md`
   - `docs/developer/extensions.md`

6. Documentation site supporting docs:
   - `docs/developer/architecture.md`
   - `docs/developer/local-development.md`
   - `docs/reference/cli.md`
   - `docs/reference/configuration.md`
   - `docs/reference/release-readiness.md`
   - `docs/user/core-concepts.md`

   If the documentation checkout is unavailable, use the application repository's
   `docs/GUIDE.md` and ADRs 0013/0015; report the missing supporting docs instead
   of inventing flags or fetching a stale machine-local copy.

7. Reference implementation: pick a published extension from the extension
   catalog repository that exercises the features you need, and study its
   source folder (paths below are relative to that folder):
   - `README.md`
   - `extension.yaml`
   - `extension-descriptor.yaml`
   - `main.py`
   - `health.py`
   - `handlers/*.py`
   - `schemas/*.schema.yaml`
   - `ui/**/*.yaml`
   - `contracts/*.yaml`
   - `artifacts/**/*.yaml`
   - `scripts/smoke_check.py`
   - `tests/test_*.py`

Ignore the leftover Docusaurus template text in the documentation site's
`docs/intro.mdx`; it is not a Flow Steward extension rule.

## Extension Mental Model

A Flow Steward extension is an out-of-process provider bundle. The core server
discovers, validates, renders, and invokes extension contracts, but it must not
import provider runtime modules into the server process.

The local runtime contract is:

- Flow Steward starts the command from `extension.yaml`.
- The extension reads one JSON request from stdin.
- The extension writes one JSON response to stdout.
- The subprocess exits after the response.
- The platform normalizes invalid JSON, timeout, missing envelope, output cap,
  and command/event contract failures into extension errors.

Extensions are also where a user's custom code goes. Flow Steward has no inline
code step in workflows: when a user asks for "a code step", build an extension
action with `workflow_visible: true` and a same-id Contract v2 operation, so the
workflow calls it through the **Extension tools** step (`step_kind:
connector`). The code then has tests, passes validation and is versioned.

The Example Showcase (`flowsteward.example-showcase`,
https://github.com/Flow-Steward/ext-example-showcase) implements every extension
feature in one synthetic bundle; read the matching file there before writing a
feature you have not built before.

Use the chosen catalog extension as the living reference. Its source folder name
is its public id with dots and hyphens replaced by underscores (for example
`acme.sample-tools` lives in `acme_sample_tools/`).

## Identity And Layout Rules

Use a stable lowercase dot-separated `extension_id`:

```yaml
extension_id: com.acme.crm-sync
```

The folder name must be the normalized id:

```text
extensions/com_acme_crm_sync/
```

Do not treat the folder name as package identity. The canonical package identity
is `extension_id`.

Minimum real extension layout:

```text
extensions/com_acme_crm_sync/
  extension.yaml
  icon.png
  extension-descriptor.yaml
  main.py
  health.py
  handlers/
    __init__.py
    common.py
    router.py
    queries.py
    actions.py
  schemas/
    config.schema.yaml
    secrets.schema.yaml
    subscriptions.schema.yaml
  ui/
    ui_manifest.yaml
    pages/
    components/
    queries/queries.yaml
    actions/actions.yaml
  contracts/
    connection_types.yaml
    operation_manifest.yaml
    step_ui_manifest.yaml
    resource_query_manifest.yaml
    event_bindings.yaml
    artifact_policies.yaml
  artifacts/
  scripts/smoke_check.py
  tests/test_<extension>.py
```

Only include `contracts/` and `artifacts/` sections that the extension actually
needs. Do not add placeholder contracts that are not validated or used.

`icon.png` is required: it is the extension's logo, shown in the extension
list, on its page, in the catalog and on workflow steps from the extension.
The file name is the contract; the manifest has no icon field.

- PNG only, recognised by its file signature. No SVG.
- Square, 32-512 px on a side; 128x128 is the recommended size.
- At most 100 KB.

`extensions create` writes a default package glyph. For branded integrations it
is a development placeholder; follow the
[extension publication standard](standards/extension-publication-standard.md)
before the first release. `extensions validate`, `extensions
bundle` and install refuse a bundle without a valid `icon.png`
(`extension_icon_missing` / `extension_icon_invalid`, field `icon.png`).

## YAML-Only Contract Rule

Authoring files must be YAML. Do not create JSON equivalents for extension
contracts.

Allowed authoring files include:

- `extension.yaml`
- `schemas/*.schema.yaml`
- `ui/ui_manifest.yaml`
- `ui/actions/actions.yaml`
- `ui/queries/queries.yaml`
- `ui/pages/*.yaml`
- `ui/components/*.yaml`
- `contracts/*.yaml`
- `locales/*.yaml` (see Localization)

Forbidden authoring files include:

- `extension.json`
- `manifest.json`
- `schemas/*.schema.json`
- `ui/ui_manifest.json`
- `ui/actions/actions.json`
- `ui/queries/queries.json`
- `ui/pages/*.json`
- `ui/components/*.json`
- `contracts/*.json`
- `locales/*.json`

Runtime stdin/stdout payloads, REST payloads, database JSONB fields, CLI JSON
output, and fixtures may still be JSON.

## Manifest Checklist

`extension.yaml` is the installed runtime contract. It must declare:

- `manifest_version: 2`
- `extension_id`
- `kind`: `task_provider`, `notification_provider`, or `tool_provider`
- `version`
- `display_name`
- `description`
- `features`
- `required_scopes`
- schema refs
- subprocess `entrypoint`
- `health` command
- UI, query, and action manifest refs when UI operations exist

Use kind and feature combinations correctly:

- `task_provider` requires `task_ingress`
- `notification_provider` requires `notify`
- `tool_provider` requires `tool`
- `ingress_event` is valid only for `notification_provider`

`extensions create` generates a `tool_provider` scaffold with `tool` and
`action` features and an empty `event_subscriptions` list. Changing its kind
requires the corresponding operation contracts; changing the feature flags
alone is insufficient.

For `task_ingress`, declare these reserved operations in the action/query
manifests. Every listed result field and error code is required:

| Operation | Kind | `result_fields` | `error_codes` |
|---|---|---|---|
| `normalize_task_action` | Action | `result.normalized` | `not_configured`, `authentication_failed`, `invalid_payload`, `rate_limited`, `provider_unavailable`, `timeout`, `internal_error` |
| `fetch_attachments` | Action | `result.attachments` | `not_configured`, `authentication_failed`, `not_found`, `rate_limited`, `provider_unavailable`, `timeout`, `internal_error` |
| `sync_completion_status` | Action | `result.status` | `not_configured`, `authentication_failed`, `conflict`, `rate_limited`, `provider_unavailable`, `timeout`, `internal_error` |
| `provider_health` | Query | `result.status`, `result.configured` | `not_configured`, `authentication_failed`, `provider_unavailable`, `timeout`, `internal_error` |

All action/query error codes in that profile must belong to the canonical
`TASK_INGRESS_ERROR_CODES` catalog in
[`ingress_runtime.py`](../core/infrastructure/db/ingress_runtime.py).
The result/operation obligations are enforced by
[`extension_runtime_feature_validation.py`](../core/infrastructure/extension_runtime/extension_runtime_feature_validation.py).

An active `notification_provider` with `notify` must reserve exactly one
`deliver_notification` action, matching Contract V2 action operation, and
external effect:

- The action uses `handler.mode: extension`, `workflow_visible: false`, and
  exactly the required object parameters `event` and `route`.
- The V2 operation uses `operation_kind: action`, `workflow_visible: false`,
  exactly the required object inputs `event` and `route`, and outputs
  `delivered` (`boolean`) and `provider_message_id` (`text`).
- The effect names that operation, uses `effect_kind: message_delivery`, a
  nonempty `channel`, `idempotency: required`, and `test_mode_behavior: suppress`.

`release_stage: placeholder` exempts this delivery-contract check, but does not
exempt kind/feature validation and cannot be enabled. See the canonical
[manifest reference](standards/extension-manifest-standard.yaml) and
[`extension_runtime_compile.py`](../core/infrastructure/extension_runtime/extension_runtime_compile.py).

If callbacks are declared, each callback needs a stable `callback_id`, purpose,
methods, `action_id`, and supported auth. Current callback auth supports only
`none`, `shared_secret_header`, `hmac_sha256`, and `hub_signature` (Meta webhooks); follow the manifest reference
for their required fields and OAuth-authorization callback exceptions.

Declare event selectors in authored `event_subscriptions`. Compilation derives
`supported_events` from those selectors; adding `supported_events` directly to
`extension.yaml` fails with `unknown_manifest_field`. Emitted event types are
checked against the compiled projection.

For `ingress_event`, declare the `ingress_event` action with
`result_fields: [result.ingress_type]` and this required selector:

```yaml
event_subscriptions:
  - event_type: external.comment.received
    severity: any
    conditions: {}
```

`notification.interaction_received` is a recommended additional selector, not
a required one. These selectors do not replace the notification delivery
contract above.

If the extension contributes workflow trigger types, declare them under
`workflow_triggers` and keep ids stable.

## Descriptor Checklist

`extension-descriptor.yaml` is the distribution/import/marketplace descriptor.
It must match the runtime manifest id and release version.

Use it for:

- listing metadata
- vendor metadata
- distribution channel and execution modes
- runtime compatibility
- optional Contract v2 projections
- dependencies
- publishable artifacts
- provenance and commerce metadata

Descriptor models are strict. Avoid unknown fields unless the target block is
intentionally metadata-like.

For local/self-hosted extensions, follow the reference extension's `private_byo` + `local`
pattern. For remote HTTPS runtime descriptors, endpoint paths must be paths, not
full URLs, and must not contain path traversal.

## Host Contract `extension_host_v2`

`runtime.contract_version` must be `extension_host_v2`. The host refuses a
descriptor that names any other value, never invokes a release built for
another contract, and has no fallback to `extension_host_v1`. This token is not
the descriptor schema version, the workflow bundle version or the SDK package
version.

- Every invocation and event envelope carries `contract_version:
  extension_host_v2`. A runtime refuses an envelope that names another contract
  before doing any work (the SDK's `runtime_context_from_payload` raises
  `ValueError` for it).
- The envelope scope keeps account, project, workflow and job, and replaces the
  removed phase scope with four fields that are always present, string or null:
  `group_id` (the business id of the step's immediate Group), `for_each_id`
  (the business id of its immediate For each), `step_id` and
  `step_execution_id` (UUID: the step's logical execution; retries keep it,
  every For each item gets its own). At most one owner is set: a root step has
  both null, a For each step has `group_id: null` even when the For each sits
  in a Group. A Group- or For each-level event names only its container; a
  workflow-level envelope has all four null. A runtime refuses an envelope that
  misses a key or names both owners (the SDK raises `ValueError`); it never
  reads a missing owner as the root. There is no `phase_id`.
- A remote HTTPS runtime confirms the contract at handshake: the host's `POST`
  to the handshake endpoint carries `contract_version: extension_host_v2` and
  `execution_scope_fields`, and the runtime answers `{"ok": true, "result":
  {"contract_version": "extension_host_v2", "execution_scope_fields":
  ["group_id", "for_each_id", "step_id", "step_execution_id"], ...}}` (the SDK's
  `handshake_result()`). This confirms the one final contract; it is not a
  negotiation. A missing or different value marks the install incompatible
  before any operation or event is sent; the token alone or a passing health
  check does not count as confirmation.
- Artifact and dataset grants carry the same scope. An explicit
  `group_id: null`, `for_each_id: null` (or `step_execution_id: null`) in a
  grant binds it to exactly that value; it is not a wildcard. A step grant names
  both owners and the item's execution, so it never serves another owner or
  another item. Only a field a broader host-issued grant leaves out is unbound.
  The grant scope is part of the signed claims, so the extension cannot change,
  drop or widen it.

## Schemas And Secrets

Use schemas to define data shape, not UI layout:

- `schemas/config.schema.yaml` for non-secret project settings.
- `schemas/secrets.schema.yaml` for passwords, API tokens, webhook secrets, and
  OAuth access tokens.
- `schemas/subscriptions.schema.yaml` for operator-configurable event selectors.

Never echo secret values in query/action responses, logs, UI text, or tests.
Return redacted markers such as `<stored>` or boolean status fields.

Model separate dev/prod/customer credentials as separate project connections,
not as hardcoded runtime branches.

## UI Contract

`ui/ui_manifest.yaml` lists pages by ref. Each page composes components by ref.
Page ids and component ids must be unique.

Common component types:

- `form`
- `grid`
- `stats_row`
- `health_panel`
- `callback_setup`
- `markdown`
- `log_list`
- `section`
- `columns`
- `stack`
- `fieldset`
- `repeater`

Important binding rule: form action/query bindings belong under `data`, not as
top-level fields. The validator rejects top-level `save_action`,
`validate_action`, `test_action`, and `load_query`.

When adding a UI feature, update all related files together:

1. Component YAML under `ui/components/`.
2. Page YAML under `ui/pages/`.
3. Query/action manifest under `ui/queries/queries.yaml` or
   `ui/actions/actions.yaml`.
4. Handler in `handlers/queries.py` or `handlers/actions.py`.
5. Smoke check or pytest assertion.

## Localization

An extension translates its own text with flat YAML dictionaries, one per
language: `locales/en.yaml`, `locales/lt.yaml`, and so on. The file name is the
language code the platform uses for the user's interface language. Users get:

1. the string in their language, if it is there and not empty;
2. otherwise the `en` string, if it is there and not empty;
3. otherwise the text written in the declaration itself.

So a missing dictionary, a missing key or an empty value never breaks a page.
Ship `en.yaml` whenever you ship any dictionary; the validator warns without it.

```yaml
# locales/lt.yaml — one key: text per line, nothing nested
ext.display_name: Telegram pranešimai
page.setup.title: Nustatymai
component.config_form.title: Numatytieji žinučių nustatymai
component.config_form.field.message_prefix.label: Žinutės priešdėlis
component.config_form.field.message_prefix.help: Neprivalomas priešdėlis.
component.config_form.save_label: Išsaugoti
```

Rules:

- Keys are built from ids, never from English text. Keep `page_id`,
  `component_id`, `field_id` and `section_id` stable; renaming one orphans its
  translations.
- Only text is translated. Ids, `path`/`name`, option values, payloads and
  everything a form saves stay the declaration's own in every language.
- A dictionary that is not valid YAML, is not a flat mapping, or holds a
  nested/number/boolean value is an install error with its file path.
- A key no declaration uses is a `locale_orphan_key` warning (in `en.yaml`); a
  key missing from `en.yaml` is a `locale_dead_key` warning.
- Adding a language is adding a file. Text the platform itself draws (Save,
  Loading, form errors) comes from the platform's catalog, not from yours.
- Data your queries and actions return is shown as returned, never translated.

Keys:

| Text | Key |
|---|---|
| Extension name, description | `ext.display_name`, `ext.description` |
| Account scope text | `ext.account_scope_description` |
| Page title, description | `page.<page_id>.title`, `page.<page_id>.description` |
| Component title, description, markdown body | `component.<cid>.title`, `.description`, `.content` |
| Form section | `component.<cid>.section.<section_id>.title`, `.description` |
| Field label, help, placeholder | `component.<cid>.field.<fid>.label`, `.help`, `.placeholder` |
| Option label | `component.<cid>.field.<fid>.option.<value>.label` |
| Repeater item field | `component.<cid>.field.<fid>.field.<item path>.label`, `.help`, `.placeholder` |
| Save / test button | `component.<cid>.save_label`, `component.<cid>.test_label` |
| Form action button | `component.<cid>.action.<action key>.label` |
| Grid column | `component.<cid>.column.<column key>.label` |
| Grid row action | `component.<cid>.row_action.<action_id>.label` |
| Grid empty state | `component.<cid>.empty_state`, `component.<cid>.empty_state_description` |
| Connection form account noun | `component.<cid>.account_label` |
| Connection type | `connection.<type_id>.display_name`, `.description` |
| OAuth provider | `oauth.<pid>.display_name`, `.description`, `oauth.<pid>.credential.<fid>.label`, `.help_text`, `.placeholder` |
| Workflow operation | `op.<oid>.display_name`, `.description` |
| Operation input / output | `op.<oid>.input.<name>.display_name`, `.description`; `op.<oid>.output.<name>.…` |
| Step form | `step.<oid>.section`, `step.<oid>.field.<name>.label`, `.help_text`, `.placeholder` |
| Category, trigger, query, dataset connector | `category.<id>.display_name`, `trigger.<id>.display_name`, `query.<id>.display_name`, `dataset_connector.<id>.display_name`, `.description` |

`<cid>` is `component_id` (or `id`/`key`); a component without one is not
translated. `<fid>` is `field_id`, then `id`, then `path`, then — for old
declarations — the label itself. `<page_id>` falls back the same way to the
page title. Only text the declaration actually has is translated: a key does
not add a description the component never had. A secret OAuth field never
gets a placeholder, whatever a dictionary says, and OAuth copy keeps its
length limits.

## Queries And Actions

Queries are read operations. Actions are command operations. Declare them in
YAML, then implement matching handlers.

Query manifest requirements:

- stable `query_id`
- title and description
- required scopes
- result fields
- known error codes
- `handler.mode: extension` for provider runtime handling

Action manifest requirements:

- stable `action_id`
- title and description
- required scopes
- result fields
- known error codes
- `mutates_platform`
- `command_type` when `mutates_platform: true`
- `handler.mode: extension` for provider runtime handling

Treat action ids, query ids, trigger ids, operation ids, connection type ids,
resource query ids, and artifact ids as public contracts. Renaming them is a
breaking change unless the task explicitly approves a migration.

## Pages That Read Through A Connection

An extension whose data lives in its own remote service shows it on a page
with queries that run with a project connection (ADR 0067, host contract
1.7.0, SDK 0.5.0). Use this instead of a connection form with buttons whose
answers land in the action-result panel.

1. Declare the query's connection in `ui/queries/queries.yaml`:

   ```yaml
   queries:
     - query_id: runs.list
       title: Runs
       connection:
         connection_type_ids: [service_profile]   # this extension's own types
       handler:
         mode: extension
   ```

   The types are the extension's own `connection_types`; the list is plural
   even for one type. Such a query always runs with a connection; a query that
   also works without one is a separate ordinary query. It has
   `handler.mode: extension` and is never named in `external_effects`.

2. Set `runtime.compatibility.platform_min: 1.7.0`. An older host would run
   the query without its connection; the minimum makes it refuse the bundle.

3. Declare the same key on every page that shows such a query:

   ```yaml
   page_id: runs
   title: Runs
   connection:
     connection_type_ids: [service_profile]
   refresh_seconds: 30          # optional, 10..3600
   components:
     - ref: ../components/runs_grid.yaml
   ```

   Every connection query the page uses must accept every page type, the page
   is project-scoped, and `refresh_seconds` needs at least one query on the
   page. The host renders the connection selector beside the project selector,
   picks the only connection automatically, keeps a choice in the URL, and
   sends the selected connection with every connection query, query button,
   action and grid row action of the page. Keep the `connection_form` that
   creates and edits connections on its own page; the selector links to it
   when the project has none.

4. Read the connection in the handler with the SDK:

   ```python
   from flowsteward_extension_sdk import connection_from_payload

   connection = connection_from_payload(payload)   # None without a connection
   base_url = connection.config["base_url"]
   token = connection.secrets["service_token"]
   ```

   The same accessor reads an action's connection (`action.target.connection`)
   and a query's (root `target.connection`).

Rules the host enforces, so design for them:

- A connection query only reads. A response with `commands`, `events` or
  `project_state_updates` fails with `query_connection_effects_forbidden` and
  nothing is applied; writes to the remote service belong to actions, which
  carry the external-effect ledger.
- It runs at most 30 seconds. Page the remote data (`limit`, `cursor`) rather
  than returning everything.
- Its result passes the reflected-secret check: never echo a secret value.
- Report an unreachable or failing remote service as a domain failure (exit
  code 2, `ok: false`, `error_code`, `errors`), so the install's circuit breaker
  is not tripped by service outages.
- Only an operator or administrator loads the page data; a viewer sees a note.
  A connection whose stored secrets are not approved fails with
  `secret_configuration_unapproved`, and the page shows the approval control.
- Remote runtime installs cannot run connection queries.

Links to a detail page: query-string keys `p.<name>` of an extension page URL
are merged into the `params` of every query on that page (they override the
component's declared params; paging and filter state override them). A
markdown cell can link to
`/extensions/<extension_id>?project_id=<project>&extension_connection=<connection>&extension_page=run_detail&p.run_id=<id>`;
the handler receives `params.run_id`. Names are lowercase identifiers (at most
20 parameters, 1024 characters each) and the values are untrusted input the
handler validates. The parameters are dropped when the person switches pages.

### Grid links and filters

A grid links its cells, or whole rows, to another page of the extension or to
a page of the app, and filters its rows through page parameters. The host builds
every address: only same-app links, values URL-encoded, opened in the app
without a full page load. Declare it in the grid component:

```yaml
component_id: runs
type: grid
query_id: runs.list
columns:
  - key: name                          # the visible text
    label: Run
    type: link
    link:
      page: run_detail                 # another page of this extension
      params: {run_id: "{row.run_id}"} # p.run_id filled from the row
  - key: job_path                      # a type: link column without link holds an
    label: Job                         # in-app path itself, e.g. /jobs/42
    type: link
row_fields: [run_id]                   # fields the query returns beside the columns
row_link: {page: run_detail, params: {run_id: "{row.run_id}"}}   # optional
filters:
  - param: status                      # bound to p.status
    label: Status
    options: [{value: open, label: Open}, {value: done, label: Done}]
  - param: owner
    label: Owner
    options_query: {query_id: owners.list, value_field: id, label_field: name}
  - param: q
    label: Search
    type: text
```

- A link is either `{page, params}` (a page of this extension; each param is
  text with `{row.<field>}` placeholders) or `{path_field}` (a row field that
  holds an in-app path starting with `/`). A row that cannot fill it shows
  plain text; a value that is not a same-app path is never a link.
- `row_link` makes a click anywhere in the row (outside its links and buttons)
  open the same kind of target.
- Every row field a link reads is a column `key` or listed in `row_fields`.
- A filter is a select (static `options`, or `options_query` of an ordinary
  query without a connection) or a `type: text` box. Changing it sets
  `p.<param>` in the address, returns the grid to its first page and loads the
  page's queries again; the handler reads `params.<param>`. An empty choice
  removes the parameter.
- The compiler refuses an unknown page, an unknown row field, a malformed
  parameter name or template, an unknown or connection query for options and a
  repeated filter parameter (`grid_link_*`, `grid_filter_*`).

## Runtime Handler Pattern

Copy the reference extension's runtime shape unless there is a concrete reason not to:

- `main.py` validates stdin JSON and writes a canonical response envelope.
- `handlers/router.py` dispatches by `mode`, `query.query_id`, and
  `action.action_id`.
- `handlers/common.py` owns envelope helpers like `ok()`, `fail()`,
  `action_input()`, safe text/dict/list coercion, and secret redaction helpers.
- `handlers/queries.py` contains read-only query handlers.
- `handlers/actions.py` contains command handlers and provider integration code.

Canonical response envelope:

```json
{
  "ok": true,
  "result": {},
  "commands": [],
  "events": [],
  "reload": [],
  "errors": []
}
```

Return `reload` component ids after successful actions when the UI should
refresh specific panels.

## External API Safety

Live provider calls must be explicit and safe:

- Use mock mode by default in examples and tests.
- Require HTTPS for live requests.
- Reject URLs with embedded credentials.
- Restrict hosts through an `allowed_hosts` config field.
- Use platform URL safety helpers when available.
- Cap response body size.
- Keep credentials in headers, not URLs.
- Redact tokens from responses, exceptions, logs, and assertions.
- Prefer deterministic tests with mocked openers.

In the reference extension, study the action handler that performs its
outbound request (typically in `handlers/actions.py`).

## Network Destinations (egress)

Declare where the extension connects, in `extension.yaml`:

```yaml
egress:
  hosts:
  - api.telegram.org            # fixed destinations
  connection_fields:
  - custom_smtp.host            # the host a project enters on its connection
```

- `hosts` are bare host names (`api.example.com`, `*.example.com`) or IPv4
  addresses, without a scheme, port or path.
- `connection_fields` name a config field of one of the extension's connection
  types: `<connection_type_id>.<field>`, or just `<field>`. Use them for
  servers the user configures (SMTP host, WordPress site URL, API base URL).
- `egress: {}` says the extension connects nowhere.
- Leaving `egress` out still installs, with the `egress_undeclared` warning on
  the extension page and in the install result.

The declaration is information for the administrator; nothing enforces it in
this release, and it is no network isolation. An installed extension is
executable code its owner trusts and runs with the platform process's rights.

The install check also reviews the extension's own Python code. Dynamic code
(`exec`, `eval`, `compile`, computed imports), native code, shells and pickle
loading are refused; raw sockets, processes, absolute paths and permission
changes are listed for review.

## OAuth And Connections

OAuth2 belongs in Contract v2 `connection_types`, not callback auth.

For OAuth-backed connections:

- Use authorization-code grant.
- Require PKCE.
- Use HTTP(S) authorization and token endpoints.
- Declare required scopes.
- Store client credentials as platform secrets.
- Put access tokens in runtime secrets.
- Do not expose refresh tokens to extension handlers.

When the provider issues the refresh token in its own console (no redirect the
platform can take part in), declare the provider with
`flow: {type: refresh_token_import}` instead (ADR 0058). Such a provider declares
`display_name`, `description`, `setup_page_id`, `help_page_id`, `credential_fields`
and `token_endpoint`, optionally `error_help_anchors`, `post_connect_action_id` and
`revocation_endpoint`, `scope_separator`, and the token-response half of
`code_exchange` (`implicit_token_type`, `required_response_fields`), and nothing
about a redirect: `authorization_endpoint`, `callback_id`,
`authorization_intents`, `origin_binding`, `callback_verification`, `flow.pkce`
and the request half of `code_exchange` are refused, and
`unique_subject_per_configuration` must stay false. Its connection types may leave
`scopes.required` empty; Core then records no granted scopes. The operator pastes
the token into the setup page together with the connection type's `config_schema`
settings; Core exchanges it once with `grant_type=refresh_token`, stores it
encrypted, and refreshes it afterwards exactly like an authorization-code grant.
A page's `connection_form` for such a provider names `oauth_provider_id` and
`connection_type` and no `oauth_intent`.

- Never ask for the refresh token or the client secret as an ordinary connection
  secret, and never mint access tokens in extension code.
- Make the `post_connect_action_id` action verify the entered settings: its target
  carries `connection.config` (the settings plus the closed `oauth` fields) and
  `connection.secrets.access_token`, so it can confirm with the provider which
  account the token is for. The settings the operator typed are not identity.
- Keep provider names, endpoints and fields in the extension; Core stays neutral.

Which mechanism fits which provider (ADR 0058 keeps the full map): a redirect
with a code is `authorization_code`; a refresh token copied from the provider's
console is `refresh_token_import`; client credentials alone (no user), a service
account, a secret that signs every request, or a long-lived API key or token is a
`static_secret` or `api_key` connection, where the extension requests and caches
any token itself. None of them needs a Core change for a new provider.

Runtime handlers should read only the connection-scoped secret values they need
from `action.target.connection.secrets`.

Study:

- `contracts/connection_types.yaml`
- the action handler that reads the OAuth connection profile
- a published workflow that binds the OAuth connection

## Contract V2

Use Contract v2 when the extension contributes workflow-builder operations,
connection types, resource pickers, event bindings, or artifact policies.

Common files:

- `contracts/connection_types.yaml`
- `contracts/operation_manifest.yaml`
- `contracts/step_ui_manifest.yaml`
- `contracts/resource_query_manifest.yaml`
- `contracts/event_bindings.yaml`
- `contracts/artifact_policies.yaml`

Operation kinds include:

- `source`
- `action`
- `bulk_action`
- `report`
- `human_channel`

Value types include text, number, integer, boolean, json, array, object,
artifact handles, dataset handles, and connection refs.

Keep Contract v2 ids synchronized with runtime action/query handlers and UI
resource queries. For example, a `resource_picker` field referencing
`list_example_resources` needs that query in both `ui/queries/queries.yaml` and
the runtime query handler.

### Per-Action Audit Policy

Declare `audit` on an action in the file selected by `action_manifest` in
`extension.yaml`, normally `ui/actions/actions.yaml`; see the
[canonical manifest standard](standards/extension-manifest-standard.yaml).
It is not an `extension.yaml` root field, an `external_effects` declaration,
an operation-manifest field, or project-state configuration.

This is a fragment of an existing action declaration, not a complete installable
manifest: keep its required handler, parameters, and other action fields.

```yaml
actions:
  - action_id: fetch_record
    audit:
      redact_result_fields: [payload, note]
      failure_projection:
        keep_row_fields: [item_id]
```

`redact_result_fields` replaces matching fields in the audit payload with redacted
markers. For a non-succeeded invocation, `failure_projection` retains generic
protocol status, error codes, external-effect flags, and declared safe row
identifiers rather than unrestricted result payloads. A retained row identifier
must be a positive integer (not a boolean) or a code of at most 128 characters
containing only lowercase letters, digits, `.`, `_`, or `-`, not arbitrary free
text. This policy does not promise removal from every provider response, workflow
output, log, or storage path.

Only `redact_result_fields` and `failure_projection` are allowed in `audit`;
only `keep_row_fields` is allowed in `failure_projection`. Field-name lists must
contain valid unique names, with at most 64 names of at most 64 characters each.
`redact_result_fields` must be non-empty; `keep_row_fields` may be empty.

Omitting `audit` (or setting it to `null`) adds no per-action policy; the platform's
generic baseline redaction still applies. An empty, malformed, or unknown-key
declaration fails compiler validation with `invalid_action_audit_policy` instead
of silently dropping the declared protection. If an invalid declared policy
reaches stored runtime data, its audit projection fails closed to generic
protocol facts, not no-policy pass-through.

### Project-State Response Write-Back

Declare each writable `namespace` and `state_key` in `project_state.writes` in
`extension.yaml`; see the [canonical manifest standard](standards/extension-manifest-standard.yaml).
Return a `project_state_updates` array in the action response. Each row contains
`namespace`, `state_key`, and a non-secret `value_jsonb` object. For example:

```yaml
project_state:
  writes:
    - namespace: snapshots
      state_key: last_success
```

An action response updating that declared state after reading revision `7`:

```json
{
  "ok": true,
  "project_state_updates": [
    {
      "namespace": "snapshots",
      "state_key": "last_success",
      "value_jsonb": {"last_processed_id": "example-42"},
      "expected_revision": 7
    }
  ]
}
```

`expected_revision` is optional, but when updating previously read state, pass
its revision to avoid a blind overwrite. A stale revision produces
`project_state_revision_conflict` and marks the invocation failed. Do not blindly
retry or drop the revision: read the current state and reconcile the intended
change before retrying.

The host owns the account, project, extension-install, and connection identity.
Normally omit `connection_id`; if supplied, it may only echo the current runtime
connection id, never select or override it. Undeclared writes, non-object values,
and connection overrides fail validation. These updates do not imply atomicity
across the array or rollback of external effects if a later update fails.

## Artifacts

Use `extension-descriptor.yaml` to publish bundled Flow Steward objects:

- workflows
- agents
- skills
- tools
- data contracts
- guardrails
- nested extension artifacts

Each publishable artifact entry needs an id plus `ref` or `snapshot`.

In a typical reference extension, workflows, agents, skills, and tools are published explicitly
through the descriptor. Dataset, saved query, data connection, and seed files are
kept beside them as reference material for realistic demo/project seed support.

## Composite Actions

A composite action is one action that a workflow of your bundle implements
(ADR 0065). Use it to give people a single step for a job that takes several
steps of a base extension: your bundle depends on that extension, and your
workflow calls its actions, branches and loops for them.

A person sees an ordinary action of your extension. The workflow behind it is
never listed, linked to a project, edited, exported or launched; the action
cannot be called outside a workflow.

Build it in a Development environment:

1. Declare the base extension as a required dependency
   (`dependencies.extensions`, `required: true`, with a version range).
2. Build the workflow like any callable workflow and test it there. Set
   `settings.callable.enabled: true` and `trigger_channels: [parent_workflow]`
   only, and declare its inputs and outputs.
3. Mark it as the implementation and name the connections it needs as slots:

   ```yaml
   settings:
     action_implementation:
       action_id: orders.create_full
       connections:
         - slot: amazon
           extension_id: acme.amazon
           connection_type_ids: [seller_account]
   ```

4. In each connector step that needs a connection, name the slot. While you
   test in Development the step may also name a connection of yours; the
   packaged workflow must not:

   ```yaml
   connector_id: extension.acme.amazon      # package id, never an install id
   connection_binding_jsonb: {slot: amazon}
   override:
     connector: {extension_id: acme.amazon, operation_id: orders.create}
   ```

5. Declare the action with `handler: {mode: workflow, workflow_id: <local id>}`
   and `workflow_visible: true`, plus its same-ID Contract V2 operation. The
   operation's inputs and outputs must equal the workflow's `expected_inputs`
   and `expected_outputs`. Do not give the operation `connection_type_ids`.

Before packaging, strip what belongs to your environment from the workflow:
`connector_install_id` and every `connection_id`. Release validation refuses a
bundle that keeps them.

Rules the release validation enforces for the workflow:

- no hook, and no plain Invoke of another workflow;
- an Invoke is an action of this extension (`workflow_id` is its local id) or of
  a required dependency (the action alone, no `workflow_id`); pass your slots
  on with `connections: {<their slot>: {slot: <your slot>}}`;
- connector steps name this extension or a required dependency only.

What an update may change: a new release is refused while an enabled workflow
of the account calls the action and the release removes the action, removes a
mapped input or output, adds a required input, a connection slot or a
dependency. Add an optional input, or ship a new action, instead.

## Workflows That Use A Project Connection

A read-only workflow your bundle publishes (people run it from the UI, a
webhook, a schedule or an Invoke, and add their own steps only in its hooks)
gets the project's connection through connection slots (ADR 0068; host contract
1.8.0). Do not ship a copy the project edits just to set a connection.

1. Declare the connections the workflow needs, with the label people see:

   ```yaml
   settings:
     connection_slots:
       - slot: store
         extension_id: acme.store          # this extension or a required dependency
         connection_type_ids: [api_token]  # empty list = any type of that extension
         label: Store account
   ```

2. In each connector step that needs it, name the slot, exactly as in a
   composite action. An action Invoke may pass it on with
   `connections: {<their slot>: {slot: store}}`.

   ```yaml
   connector_id: extension.acme.store       # package id, never an install id
   connection_binding_jsonb: {slot: store}
   ```

3. Set `runtime.compatibility.platform_min: 1.8.0`.

While you build the workflow in a Development environment, a step may name the
slot and a connection of yours to test with; strip every `connection_id` and
`connector_install_id` before packaging. Release validation refuses a step that
reads an undeclared slot, a fixed connection next to a slot, a slot of an
extension that is neither yours nor a required dependency, and
`connection_slots` on an action implementation (it keeps its slots in
`action_implementation`).

What the person sees: the workflow's Overview says "This workflow needs: Store
account" with a connection selector for each linked project. With exactly one
usable connection of an accepted type, it is chosen automatically. Without one
the workflow is not ready in that project, and runs, webhooks and schedules are
refused before they start with the reason. The choice is pinned into each run;
a child workflow of your extension started by an Invoke gets the parent run's
connection for a slot of the same name when it fits.

What an update may change: a release that adds a slot, or changes a slot's
extension or connection types, activates only when every project that links the
workflow has a usable connection for it; otherwise the update is refused with
the workflow, the project and the reason, and the project chooses first
(`PUT /api/projects/{project_id}/workflows/{workflow_id}/connection-slots/{slot}`
with `release_id`). Changing a label never blocks. Removing a slot drops the
choices made for it. Uninstalling the extension or deleting the connection
removes the choice; export and import never carry connection ids.

## Naming Each Run Of A Workflow

When one workflow runs for several purposes (a scraper scheduled once per
supplier site, say), every job would carry the same title. Declare a run title
over the workflow's inputs so each job says what it was for:

```yaml
settings:
  run_title: "{{ inputs.run_name }} {{ inputs.site_url }}"
```

- Only `{{ inputs.<name> }}` placeholders are read; text around them is kept
  as written. Nothing is evaluated.
- An input that is secret (type `secret`, or a name such as `api_key`,
  `password`, `token`) can never be shown, and a secret reference, an object
  or a list renders as nothing. Release validation and readiness refuse a
  template that reads a secret input, an undeclared input (in fields mode) or
  anything other than `inputs.<name>`.
- The template is at most 300 characters, each value at most 120, and the
  rendered title at most 160; longer text is cut with "…". A title whose
  values are all empty is left out.
- A run started by a schedule is titled `<schedule name> · <rendered title>`;
  by a webhook, `<webhook description> · <rendered title>`. UI, MCP, API and
  Invoke runs use the rendered title alone.
- The title shows in Jobs, on the job page, on an Invoke step's link to its
  child run and in a schedule's run history. It is written once, when the job
  is created, so later edits of the template do not rename earlier runs.
- An optional free-text input (for example `run_name`, "Run name") lets a
  person name a run started from the UI.

`settings.run_title` needs no `platform_min`: a host before 0.9.18 ignores it.

## Callbacks And Ingress

Use callbacks for provider webhooks routed through Flow Steward into an action.

Checklist:

- Declare `callbacks` in `extension.yaml`.
- Use a stable lowercase callback id.
- Bind the callback to an action, usually `ingress_event`.
- Add a `callback_setup` component when operators need setup/status UI.
- Implement the ingress action to normalize provider payloads.
- Return fields such as `result.ingress_type` and `result.normalized`.
- Add tests for representative inbound payloads.

## Resuming Waiting Runs

A workflow can pause on a Wait step until your extension reports an event
(`override.wait.event`, [ADR 0071](architecture/adr/0071-wait-for-an-event.md)):
for example "start a crawl, wait for *this* crawl to finish, process its result".
The workflow names your extension as `source`, the event types it accepts, and a
correlation key, usually the id your start action returned. Your extension
resumes it by returning `resume_waits` next to `events` in a **successful**
response of any action: a callback's action, a workflow step's action, a page
action.

```json
{
  "ok": true,
  "result": {"accepted": true},
  "resume_waits": [
    {
      "correlation_key": "run-42",
      "event_type": "run_completed",
      "payload": {"rows": 120, "result_file": "crawl-42.csv"}
    }
  ]
}
```

A callback handler (`handlers/actions.py`) that turns the service's completion
POST into a resume:

```python
def crawl_finished(request: dict) -> dict:
    """Callback action: the crawler posts {run_id, status, rows} when a run ends."""
    body = request.get("request", {}).get("json") or {}
    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        return fail("invalid_payload", "run_id is required")
    event_type = "run_completed" if body.get("status") == "done" else "run_failed"
    return {
        "ok": True,
        "result": {"run_id": run_id},
        "events": [],
        "resume_waits": [
            {
                "correlation_key": run_id,
                "event_type": event_type,
                "payload": {"rows": int(body.get("rows") or 0)},
            }
        ],
    }
```

Rules the host enforces:

- The host takes your extension id, the account and the project from the
  invocation (the callback's registration, the step's run); anything you put in
  an item about them is ignored. Only a run of that project that waits for your
  extension as `source` can be resumed.
- `correlation_key`: non-empty text or integer, at most 256 characters,
  compared exactly. `event_type`: lowercase letters, digits, `_`, `.`, `:`, `-`,
  and one of the wait's `event_types`. `payload`: a JSON object of at most 64 KiB;
  it is redacted like action results, so never put secrets in it. At most 100
  items per response.
- Delivery is stored, never immediate: the control process matches stored events
  to waiting runs every second. An event that arrives before the run reaches its
  Wait is kept and taken when the run parks, so you do not have to wait for the
  workflow before reporting.
- Each project, extension, key and type is delivered once; a repeated item is
  reported as a duplicate and ignored, so a provider's retried callback is safe.
  Events are kept 7 days.
- A malformed item is refused without failing your action; the invocation's
  `wait_events` result lists `stored`, `duplicates` and `rejected` (with a code
  such as `resume_wait_payload_too_large`). If the host cannot store the events,
  the action fails with `wait_event_delivery_failed` and a callback answers 502,
  so the provider delivers again.
- A query that runs with a connection must not return `resume_waits`
  (`query_connection_effects_forbidden`).

Document the event types and the key your extension uses on its workflow page or
README, so workflow authors can fill `event_types` and `correlation_key`.

## Background Connection Polls

An extension can watch an external system and start workflows from what it finds
there, without webhooks: the worker calls one of its actions in the background,
once per interval and once per project connection. Any extension kind may do
this, `tool_provider` included. The example below polls "items" from a service
through two connection types: `items_user` (OAuth) and `items_app` (its own
secret).

### Declare the poll

```yaml
# contracts/connection_types.yaml (excerpt)
connection_types:
  - connection_type_id: items_user
    auth: {type: oauth2, provider_id: items_oauth, scopes: {required: [items.read]}}
    ingress_schema: &item_ingress
      type: object
      properties:
        sync_mode: {type: string, enum: ["off", polling], default: "off"}
        sync_scope: {type: string}
        poll_interval_seconds: {type: integer, minimum: 30, default: 60}
  - connection_type_id: items_app
    config_schema: {type: object, required: [account_ref], properties: {account_ref: {type: string}}}
    secret_schema: {type: object, required: [client_secret], properties: {client_secret: {type: string}}}
    ingress_schema: *item_ingress

# ui/actions/actions.yaml (excerpt)
- action_id: poll_items
  workflow_visible: false
  connection_type_ids: [items_user, items_app]   # never called for other types
  handler:
    mode: extension
    config:
      runtime_resources: {project_connection: true, provider_key: acme.items}
      host_mutations: [project_connection_upsert]
  contract:
    serialization: {resource: item_sync}          # one poll per connection at a time
    background:
      kind: connection_poll
      interval_field: poll_interval_seconds
      mode_field: sync_mode
      transport_policy: webhook_preferred
```

- **Types.** With `connection_type_ids`, the worker calls the action only for
  connections of those types. Without it the action runs for every connection of
  the install and keeps the older `required_provider_secrets` check.
- **Credentials per type.** Each call receives its connection resolved the way
  operations receive it, in `target.connection`. An OAuth type gets a valid,
  refreshed access token in `secrets`. Any other type gets its own connection
  secrets, and must have every key its `secret_schema` lists as `required`.
  - A connection that cannot be resolved is skipped before the call. A missing
    secret is shown as `ingress_config.background_poll_status`; a revoked grant
    stops polling until the connection is reconnected.
  - A temporary token-refresh failure skips that one interval.
- **Off by default.** A `default` in the type's `ingress_schema` is written into
  `ingress_config` when the connection is created. A missing mode also counts as
  that default. Declare `default: "off"` for the mode field, so a new connection
  polls only after someone switches it on. The values `polling` and `auto` mean
  on; anything else means off.

### What the action receives

`target.connection` has `connection_id`, `connection_type`, `config`, `secrets`,
`ingress_config` (with declared defaults filled in), `state_version` and
`config_generation`.

- `state_version` moves on every saved poll state.
- `config_generation` moves when what is polled changes:
  - the connection is disabled, enabled, reconnected, or its settings change;
  - a field written through `project_connection_ingress_patch` changes,
    except the background `interval_field`;
  - a `project_connection_upsert` outside a poll changes the `mode_field`.

Store your sync state in `ingress_config` and keep the generation it belongs to
inside it. When the generation you receive differs, start a new sync instead of
continuing the old one.

### What the action returns

```json
{
  "ok": true,
  "result": {"processed_updates": 2},
  "events": [
    {"event_type": "acme.item.created", "source_id": "acme:conn_1:item-17",
     "payload": {"connection_id": "conn_1", "item_id": "item-17"}}
  ],
  "host_mutations": [
    {"kind": "project_connection_upsert",
     "payload": {"connection_id": "conn_1",
                 "ingress_config": {"sync_mode": "polling", "item_sync": {"cursor": "c-42"}},
                 "expected_state_version": 7,
                 "expected_config_generation": 3}}
  ]
}
```

- `event_type` must be listed in the manifest's `supported_events`.
- `source_id` is the event's stable identity. Make it unique per connection and
  never reuse it, for example `<prefix>:<connection_id>:<immutable item id>`. An
  event whose `source_id` was already stored is ignored: it starts nothing and
  notifies nobody.
- Echo the `state_version` and `config_generation` you received as
  `expected_state_version` / `expected_config_generation`. The host never takes
  them from you as truth. It compares them with what the poll started from and
  refuses a state write that does not match.

### What the host guarantees

The result is committed in one transaction that locks the connection row: the
events, one dispatch record per matching workflow trigger, and the cursor, all
together.

- **Exactly once.** One job per event and matching workflow, through crashes of
  the worker or the host, repeated pages and two workers polling at once.
  - A crash before the commit loses the whole result, so the next poll reads the
    same page again.
  - Each job is created in one transaction with its delivery record. Unfinished
    records are retried by a sweep every 60 s.
- **Stale results.** A result is judged against the connection as it is now.
  - Another worker saved state meanwhile: the events are kept and the cursor is
    dropped.
  - The configuration changed, the connection was disabled or the mode switched
    off: the events and the cursor are both dropped.
- **Switch-off.** Once switching the poll off (or disabling the workflow trigger)
  has committed, no new job starts for events that were still pending. A job
  queued before it runs normally.

### Settings form

Let the user switch the poll on and off with the platform operation
`project_connection_ingress_patch`. Send the selected connection as
`target.connection_id`:

```yaml
- action_id: save_sync_settings
  workflow_visible: false
  connection_type_ids: [items_user, items_app]
  handler:
    mode: platform
    operation: project_connection_ingress_patch
    config:
      connection_type_ids: [items_user, items_app]   # other types: invalid_connection
      fields: [sync_mode, poll_interval_seconds, sync_scope]
```

The patch locks that connection, writes only the declared fields, validates them
against the connection type's `ingress_schema`, and moves `config_generation`
when a field other than the poll interval changes.

## Notification Providers

A `notification_provider` extension delivers the platform's notifications:
workflow events and approvals sent through a project's notification routes, and
optionally the platform's own system email. It reserves one action,
`deliver_notification(event, route)`, and declares it as one
`message_delivery` external effect (`idempotency: required`,
`test_mode_behavior: suppress`). The example below is a provider `example` that
emails people through `example_mail` and posts to a chat through
`example_chat` and `example_chat_bound`.

### Single-kind and mixed providers

The delivery effect's `channel` and `observability.target_kind` are the defaults
for every connection. A connection type may override them for deliveries through
that type with its own `notification` block:

```yaml
# contracts/connection_types.yaml (excerpt)
connection_types:
  - connection_type_id: example_mail
    notification: {target_kind: recipient, ledger_channel: email}
  - connection_type_id: example_chat
    notification: {target_kind: channel, ledger_channel: chat}
  - connection_type_id: example_chat_bound
    config_schema:
      type: object
      properties: {channel_label: {type: string}}   # display only
    notification:
      target_kind: channel
      ledger_channel: chat
      channel_source: connection                    # bound to one channel
  - connection_type_id: example_files               # no block: carries no routes
```

- `target_kind` is `recipient` or `channel`. It fixes the route's address fields:
  a `recipient` route has `to` (required), `cc`, `bcc` and `subject`; a `channel`
  route has `channel_id` (required; `channel` is accepted as an alias).
- `ledger_channel` is the external-effect ledger channel and rate-limit bucket for
  deliveries through the type (default: the effect's `channel`).
- `channel_source: connection` (channel types only; default `route`) means the
  connection itself is bound to one channel, for example a URL that posts to one
  place. Its routes carry no `channel_id`; the connection config's display-only
  `channel_label` is shown instead of a channel picker. A route that sends a
  channel to such a connection is refused.
- Once one type declares a block, only types that declare one can carry routes
  or send system email.
- A provider is *mixed* when its declared types address more than one target
  kind. Every route of a mixed provider, and of a provider with a
  `channel_source: connection` type, must name its connection
  (`route_config.connection_id`); the platform never falls back to the project's
  runtime connection for it.
- The build refuses an unknown `target_kind`, `channel_source: connection` on a
  recipient type, and a `notification` block in an extension that is not a
  `notification_provider`.
- A provider that declares no block behaves as it always did.

When a route is saved, and again before each delivery, the platform checks that
the route's connection exists, is enabled, belongs to the route's project and to
the provider's install, is of a type with a `notification` block, and that the
route's fields fit that type. A refusal is `notification_connection_required` or
`notification_connection_incompatible`: a validation error on save, and a failed
delivery that is not retried.

### The delivery request

Each delivery runs your action once with this request (a local provider: the
host runs your extension process and writes the request to its stdin):

```json
{
  "action": "deliver_notification",
  "idempotency_key": "<the claimed effect's key>",
  "external_effect_id": "<the claimed effect's id>",
  "action_payload": {
    "action_id": "deliver_notification",
    "target": {
      "connection": {
        "connection_id": "conn_1",
        "connection_type": "example_chat_bound",
        "display_name": "Operations chat",
        "config": {"channel_label": "Operations"},
        "secrets": {"<name>": "<value>"}
      }
    }
  },
  "event": {
    "event_id": "evt_1",
    "event_type": "job.failed",
    "severity": "error",
    "summary": "Invoice sync failed",
    "job_id": "job_7",
    "run_url": "https://flowsteward.example.com/jobs/job_7",
    "payload": {"summary": "Invoice sync failed", "error": "Timeout"}
  },
  "route": {
    "route_id": "route_1",
    "route_key": "ops",
    "provider_key": "example",
    "event_type": "*",
    "severity": "any",
    "route_config": {"connection_id": "conn_1"}
  },
  "provider": {"provider_key": "example", "integration_key": "example"},
  "contract_version": "extension_host_v2",
  "runtime_context": {"external_effect_id": "<the claimed effect's id>", "...": "..."}
}
```

- `idempotency_key` and `external_effect_id` are top-level. Key your own write
  with `idempotency_key`: a retry of the same delivery carries the same pair,
  and another event, route or connection carries another key. If it is missing,
  fail before any write.
- `route.route_config` holds `connection_id`, plus `to`/`cc`/`bcc`/`subject` for
  a recipient route, `channel_id` for a channel route, and neither for a
  `channel_source: connection` route. For a recipient route the platform fills `subject` and
  `event.payload.message` with its standard email text, unless the route set a
  subject; a channel route gets the event as it is and formats its own message.
- `event.run_url` is the platform page of the run the event is about (for an
  approval, the approval's own run). It is present only when the platform knows
  its public address and the event has a run, and it never carries a token. Link
  to it instead of building the address yourself.
- A repeat of a delivery that already succeeded is answered by the platform from
  its ledger (`provider_message_id` included); your extension is not called again.
  A failure that you mark `definitely_no_external_effect: true` and
  `retryable: true` is retried with the same key.
- Remote notification providers are not supported yet. A future remote branch
  will carry the same key in the remote action envelope's `idempotency_key`.

### Credentials on delivery

`target.connection` is resolved the way workflow operations resolve it:

- an OAuth connection type gets a valid `access_token` in `secrets`, refreshed if
  needed. A revoked grant fails the delivery with `authorization_required` before
  your extension is called, and it is not retried;
- any other type gets its own connection secrets (and any project provider
  secrets attached to the connection).

### Sensitive events

An event may carry `payload_sensitive: true` (absent means false). The
platform's own messages always do, because their body holds a single-use link.
For such an event the subject and body reach only your delivery request; every
copy the platform keeps (invocation log, extension audit, external-effect ledger,
runtime journal, delivery rows, error text) holds a placeholder with the value's
length and hash. Your extension must not log the subject, the body or any link
in them either, at any log level, and must not echo them in its response or
errors.

### System email sender

A platform administrator chooses one connection in Settings -> Email to send the
platform's own email (password reset, welcome, email change confirmation). A
connection is a candidate when it is enabled, its install is an enabled
`notification_provider`, and its type resolves to `target_kind: recipient`.

- The choice is confirmed against the connection's `sender_revision` and its
  OAuth configuration's `configuration_revision`. Disabling or re-enabling the
  connection, changing its settings, secrets, type, install or the account it is
  connected as, or editing the OAuth configuration, revokes the confirmation
  until an administrator confirms again. A token refresh does not.
- While a connection is the system sender, only a platform administrator may
  change those fields or delete it (`system_sender_locked` otherwise).
- Right before your extension is called the platform checks the setting and both
  revisions again; a change fails the send with `system_sender_changed` and no
  call. There is never a fallback to another connection.
- A system email arrives as a `system.*` event with `payload_sensitive: true`,
  `route_config.to`, `subject`, and the plain-text body in `event.payload.message`.

## Credentials From An OAuth Configuration

A connection type that authenticates as an application (your extension runs the
client-credentials exchange itself) does not have to repeat the same client
values on every connection. It can borrow them from one OAuth configuration of
the same install and project:

```yaml
# contracts/oauth_providers.yaml (excerpt)
oauth_providers:
  - provider_id: example_oauth
    credential_fields:
      - {field_id: client_id, value_type: text, required: true, secret: false, ...}
      - {field_id: client_secret, value_type: secret, required: true, secret: true, ...}
      - field_id: tenant_id
        value_type: text
        required: true
        secret: false
        usage: runtime        # stored and shown, never sent during sign-in
        ...

# contracts/connection_types.yaml (excerpt)
connection_types:
  - connection_type_id: example_app
    config_schema:
      type: object
      properties: {account_ref: {type: string}}   # the type's own fields only
    credentials_from:
      oauth_provider_id: example_oauth
      fields:                      # credential field id -> place in target.connection
        client_id: {into: config, as: client_id}
        client_secret: {into: secrets, as: client_secret}
        tenant_id: {into: config, as: tenant_id}
```

- `usage: runtime` marks a credential field the platform's own sign-in never sends
  to the authorization or token endpoint (default `exchange`). It does not block a
  configuration from being authorized. `client_id` and `client_secret` cannot be
  runtime-only.
- The build refuses an unknown provider, an unknown field, a secret field going
  `into: config`, a mapped name that is also one of the type's own config or
  secret fields, and `credentials_from` on an `auth.type: oauth2` type (which
  holds its own grant).
- The connection stores the chosen configuration. The connection form offers the
  install's configurations for that provider in the project and preselects the
  only one; it shows only the type's own fields. The configuration must be
  complete; whether it has been authorized does not matter.
- Every call receives the mapped values from the configuration's current revision
  in `target.connection.config` and `target.connection.secrets`: operations,
  resource queries, setup and test actions, background polls, connector steps and
  notification deliveries. A rotated secret reaches every linked connection on its
  next call, with no connection edit. Secret values never reach the browser or
  logs.
- Deleting the configuration lists its linked connections in the delete impact
  (`link: credentials`). They are kept, and until they are linked to another
  configuration they report `credentials_unavailable` and are not ready; calls
  through them fail with `credentials_unavailable`.
- For the system email sender, a change of the borrowed configuration (a secret
  rotation included) revokes the sender's confirmation until a platform
  administrator confirms again.

## Testing And Verification

Every real extension should have both a smoke script and pytest coverage.

Smoke script should verify:

- `health.py` returns healthy.
- Representative query succeeds.
- Representative action succeeds.
- Ingress/callback action succeeds when applicable.
- External request path stays mocked by default.

Pytest should verify:

- bundle manifest compiles
- descriptor validates
- Contract v2 validates
- forbidden JSON authoring files are absent
- health check passes
- representative queries return expected fields
- representative actions return expected fields
- event/ingress result shape
- OAuth access token injection behavior
- live external request safety and token redaction
- partial failure behavior for bulk actions

Run authoring commands through the public Docker-backed launcher from an
installed, healthy Development stack. Do not use a host `.venv`, host pytest,
or private CLI module paths. `extensions test` runs the extension's smoke script
and tests in the owning tool runtime; Production asks you to switch to Development
rather than creating test tooling implicitly.

Recommended commands:

```bash
./flow-steward extensions validate <extension_id> --root extensions --health --marketplace
./flow-steward extensions test <extension_id> --root extensions --non-interactive
./flow-steward extensions lock-deps <extension_id> --root extensions --non-interactive --confirm
./flow-steward extensions bundle <extension_id> --root extensions --with-wheels --non-interactive --confirm
```

Before an intermediate commit, run the fast gate:

```bash
./core/entrypoints/cli/scripts/lint.sh --fast
```

Before handoff or merge, run a clean full gate from the final committed state:

```bash
./core/entrypoints/cli/scripts/lint.sh --full
```

The authoring commands above run without a terminal: `--non-interactive` disables
prompts and `--confirm` explicitly accepts the reviewed lock/export operation.
`extensions test` executes immediately and does not accept `--confirm`.
Its normal path requires `requirements-dev.txt` and installs test dependencies
offline with `pip --no-index`, searching the extension's `dev-wheels/` and
`wheels/`. Inherited tooling packages can satisfy requirements; supply compatible
local wheels for development dependencies absent from that runtime. Wheel
directories are not required when those packages already satisfy the file.

A run without `tests/test_*.py` fails unless intentionally allowed with
`--allow-no-pytest`. Record the omitted pytest coverage; validation, smoke and
dependency failures still fail the run. Bundle success reports packaging rather
than full validation: run validate and test first and stop on any nonzero exit.
Inspect the extension and intended dependency pins first. Without confirmation,
lock/export commands refuse to run in an unattended agent session; do not bypass review
for installation or other unrelated destructive commands.



## Observed CLI status example

A historical isolated Compact product-runtime check at source SHA
`968f373fd879fee10aa9136d5930761be81bab0c` observed the following.
These are process exit codes for that fixture, not compiler issue codes or a
fresh installed-launcher acceptance result:

| Fixture and command | Exit and observed result |
| --- | --- |
| Generated valid scaffold: `extensions create`, then `extensions validate` | Both `0`; create used `--non-interactive --confirm`. |
| Temporary manifest `kind: invalid_kind`: `extensions validate` | `3`, outer error `validation_failed`; unsupported manifest kind. |
| Same invalid manifest: `extensions bundle --non-interactive --confirm` | `0`; packaging wrote the versioned ZIP despite the invalid manifest. |
| Extract that ZIP, then `extensions validate` against the extracted root | `3`, `validation_failed`; the invalid manifest remains invalid. |
| `extensions test --non-interactive` outside the dedicated test runtime | `3`, `validation_failed`; runtime guard stopped before pytest/smoke. |

The bundle row is a negative demonstration, not permission to continue after
failed validation. In the normal validate/test/bundle sequence, stop on any
nonzero exit. This historical check does not establish scaffold pytest,
offline dependency behavior, authenticated descriptor API or installed catalog
acceptance. Run tests through the installed Development launcher and retain the
actual exit/status evidence for the target release.

## Descriptor API and validation diagnostics

Use an authenticated client with the target release's existing authority checks.
The registered endpoints are:

| POST route | Content type and request fields |
| --- | --- |
| `/api/extensions/descriptors/validate` | `application/json`: `source_type` (`file` by default, or `url`, `builtin`, `payload`), `descriptor_path`, `descriptor_url`, `descriptor_payload` (object), `source_root`, `platform_version` (default `1.0.0`), `allow_insecure_remote` (default `false`). For inline validation use `source_type: "payload"` and the descriptor object in `descriptor_payload`. |
| `/api/extensions/descriptors/import/file` | `application/json`: required `descriptor_path`, optional `install_after_import` (default `false`), `runtime_mode`, `remote_base_url`. The path must be accessible to the server. |
| `/api/extensions/descriptors/import/url` | `application/json`: required `descriptor_url`, optional `install_after_import` (default `false`), `runtime_mode`, `remote_base_url`. The server fetches the URL. |
| `/api/extensions/install` | `multipart/form-data`: required `file` containing the ZIP; optional `approved_browser_authority_impact_hash` (empty by default, or 64 lowercase hexadecimal characters) binds the reviewed browser-authority impact. |

A descriptor file import is not a client-side file upload. JSON request wrappers
are compatible with YAML-only bundle contracts. Keep import and installation
within the authorized task; `install_after_import` explicitly requests installation.

`extensions validate --marketplace` adds manifest checks for identity/display
fields, `vendor.name` (or `vendor_name`), listing category/summary, nonempty
required scopes, machine-auth service-account key/scopes, and account-scope
metadata. These checks do not replace descriptor validation. Archive validation
separately checks that manifest identity/version match descriptor
`extension_id`/`release_version`; inspect the imported release and catalog after
installation to verify the resulting identity and metadata.

Preserve each emitted issue's code, field/path and message when reporting failures:

| Emitted issue code | Meaning |
| --- | --- |
| `extension.yaml_only_violation` | Forbidden JSON contract files; convert the contract and references to YAML. Runtime Python and JSON API wrappers are not prohibited by this issue. |
| `workflow_action_requires_contract_v2` | Workflow-visible actions require Contract V2. |
| `workflow_action_operation_mismatch` | A workflow-visible action lacks a same-ID visible V2 operation. |
| `workflow_operation_action_mismatch` | A visible V2 operation lacks a same-ID workflow-visible action. |
| `task_ingress_feature_required` | A manifest v2+ task provider lacks `task_ingress`. |
| `invalid_notification_provider_delivery_contract` | A notification provider's reserved delivery contract is missing or invalid. |
| `invalid_section_type` | A V2 section is neither an object, list nor reference string. |
| `extension_private_core_import` | Extension runtime code imports private Core instead of public SDK/runtime contracts. |

These compiler/validation issue codes are distinct from the public CLI error
envelope and process exit status. For example, request/service validation errors
can use the outer `validation_failed` code. Record the actual envelope and exit;
do not infer them from a compiler issue or invent an `extension.ui_*` family.

## Portable Python Dependencies And Export

Follow `docs/standards/extension-manifest-standard.yaml` and ADRs 0013/0015.
Treat packaging as part of the extension deliverable, not a task for its user.

1. Write direct dependencies to `<extension>/requirements.in`; omit this step
   only when the extension has no third-party Python runtime dependencies.
2. Run `extensions lock-deps` to write exact pins, the full transitive closure,
   and hashes into `extension.yaml` for **both** Linux AMD64 and ARM64.
3. Run `extensions bundle --with-wheels`. With no platform override, the existing
   packager automatically downloads hash-verified wheels for both default targets
   into `wheels/` before writing the ZIP. Bare `bundle` does not fetch missing
   wheels. Do not hand users an archive that needs runtime `pip install`.
4. Inspect the resulting archive: it must contain its manifest, dependency
   wheels and their declared hashes, but not authoring-only `dev-wheels/` or
   `.fs-python/` directories. Confirm that the complete lock is satisfiable on
   each supported runtime in Docker, not just the agent's current architecture.
5. Record the ZIP path, digest and the actual platform verification performed.
   A packaging success is not evidence of a live installation on both targets;
   report unavailable runtime checks explicitly.

```bash
./flow-steward extensions lock-deps com.acme.crm-sync --root extensions --platforms manylinux2014_x86_64,manylinux2014_aarch64 --non-interactive --confirm
./flow-steward extensions bundle com.acme.crm-sync --root extensions --with-wheels --non-interactive --confirm
```

The target mapping is:

| Deployment runtime | Default wheel target |
| --- | --- |
| `linux/amd64` (`linux-amd64`) | `manylinux2014_x86_64` |
| `linux/arm64` (`linux-arm64`) | `manylinux2014_aarch64` |

An architecture-independent `py3-none-any.whl` may serve both. Compiled Python
dependencies need matching wheels for each architecture and the runtime Python
version/ABI. Apple Silicon Docker and ARM Linux servers need Linux ARM64 wheels,
not `macosx_*` wheels. Resolve and bundle inside the Docker tool runtime so the
Python version is not inferred from a host interpreter.

If either target has no compatible prebuilt wheel, stop and choose a compatible
dependency/version or provide correctly built, pinned wheels through the
existing package path. The packager downloads wheels; it does not cross-compile
source distributions. Do not omit the failing architecture, loosen hashes, or
enable install-time fetching to disguise an incomplete release. Use
`--platforms active` only for explicitly local, active-runtime development output.

The server runtime is CPython 3.12 on Debian bookworm (glibc 2.36). Packages
that publish only `manylinux_2_28` wheels are not found under the
`manylinux2014` default; pass
`--platforms manylinux_2_28_x86_64,manylinux_2_28_aarch64` to both `lock-deps`
and `bundle --with-wheels`. Do not go above `manylinux_2_36`. Without the CLI,
the equivalent per-architecture download is
`pip download --only-binary=:all: --dest wheels --platform manylinux2014_x86_64
--implementation cp --python-version 3.12 --abi cp312 --abi abi3 --abi none -r
requirements.in` (and again with `manylinux2014_aarch64`), followed by listing
every wheel's SHA-256 in `python_requirements`.

What the tooling actually enforces about architectures (do not claim more):

- `lock-deps` and `bundle --with-wheels` fail when a requirement has no binary
  wheel (with a locked hash) for one of the platforms they are given; by
  default both.
- `extensions validate` checks that every file in `wheels/` is a declared wheel
  with a listed hash, that the closure is complete, and that each requirement
  has at least one bundled wheel. It does **not** check that both `_x86_64`
  and `_aarch64` wheels exist; a one-architecture `wheels/` passes.
- Install and update check only the server's own architecture and fail with
  `No compatible wheel for python requirement '<name>==<version>'`.

So before handing over an archive, list `wheels/` and confirm an `_x86_64` and
an `_aarch64` wheel (or one `any` wheel) per requirement. The packager and the
installer refuse archives over 50 MiB zipped or 150 MiB unpacked. When both
architectures do not fit, export per-architecture archives with
`bundle --runtime-target linux-amd64` and `--runtime-target linux-arm64`: each
keeps only its own wheels plus a `.fs-package.yaml`, and a server refuses the
other architecture's archive.

Native executables are a separate contract: prepare and checksum binaries for
both targets, then export **two** archives with `--runtime-target linux-amd64`
and `--runtime-target linux-arm64`. Do not pass Python tags such as `cp312` to
`--runtime-target`, or assume `--with-wheels` builds native executables. Keep
installer archive size limits; do not enlarge or bypass them to fit dependencies.

## Agent Workflow For Building A Real Extension

1. Read the canonical extension contracts and search the checked-out repository
   and Git history for every consumer before refactors, deletions, renames, or
   structural changes.

2. Identify extension type:
   - task provider
   - notification provider
   - tool provider

3. Define stable contracts before coding:
   - `extension_id`
   - folder name
   - kind and features
   - scopes
   - config schema
   - secrets schema
   - queries
   - actions
   - events
   - callbacks
   - Contract v2 operations
   - publishable artifacts

4. Scaffold or copy the reference extension's structure.

5. Implement the smallest working runtime:
   - health check
   - one query
   - one action
   - canonical envelope
   - smoke script

6. Add UI:
   - pages
   - components
   - query/action bindings
   - reload behavior after actions

7. Add provider integration:
   - mock mode first
   - HTTPS and allowlist checks
   - token redaction
   - body size cap
   - deterministic tests

8. Add Contract v2 only for builder-facing capabilities.

9. Add descriptor artifacts only when they should be imported or published.

10. Validate, test, lock dependencies for both Linux architectures, and export
    with `--with-wheels` as described above. Record archive/digest and actual
    verification evidence, plus durable architectural lessons in the relevant
    canonical standard, ADR, or maintained task document.

## Common Failure Modes

Check these first when validation or rendering fails:

- Folder name does not match normalized `extension_id`.
- A forbidden JSON authoring file exists.
- A ref is absolute, missing, points to JSON, or escapes the bundle.
- Page ids or component ids are duplicated.
- Action ids or query ids are duplicated.
- UI component references an undeclared action or query.
- Form bindings are top-level instead of under `data`.
- Mutating action lacks `command_type`.
- Runtime response omits `ok`.
- Runtime emits undeclared event types.
- Secrets are echoed in action/query results.
- Contract v2 resource picker references a missing query.
- OAuth handler expects refresh tokens instead of platform-injected access
  tokens.
- Live external request bypasses HTTPS or host allowlist.
- Dependency lock/wheels cover only the author's architecture, or include macOS
  wheels instead of the Linux runtime targets.
- Export omits `--with-wheels` and silently leaves required dependency files out.

## What Good Looks Like

A production-ready extension should be understandable from contracts alone:

- The manifest explains what the extension is allowed to do.
- Schemas separate config from secrets.
- UI pages are declarative and reference known queries/actions.
- Runtime handlers are small and route through a single envelope.
- Provider calls are explicit, bounded, allowlisted, and tested.
- Contract v2 projections match runtime operations.
- Descriptor artifacts are publishable and validated.
- Smoke and pytest checks prove the bundle works before packaging.
- The deliverable includes hash-locked wheels compatible with both Linux targets;
  native executable releases have the two explicit target archives.

## Conditional connection configuration

When a configuration update depends on a value still being absent or unchanged,
use `project_connection_config_compare_and_set` (extension API 1.9.0). Declare
the new kind in both `runtime_grants.mutations` and the action handler's
`host_mutations`; set the bundle's `platform_min` to `1.9.0`. An older host
rejects the unknown grant. Do not fall back to `project_connection_upsert`.

Return `expect` conditions and a `set` map of dotted configuration paths. For an
initial value that may be replayed, use `absent_or_equals` with that same value
in `set`. The host checks the currently persisted value under a row lock;
reading the invocation snapshot first is not an atomic precondition. A different
established value fails with `host_mutation_precondition_failed` and rolls back
the whole mutation batch.

The host preserves unrelated current configuration and columns. Do not include
`oauth` or its descendants in conditions or assignments, or send a copied OAuth
block back to the host. The final configuration must satisfy the native
connection schema, including any host-owned OAuth metadata it contains.

See the canonical `conditional_connection_config` manifest contract and
[ADR 0073](architecture/adr/0073-conditional-connection-config-writes.md) for
payload examples, limits, JSON equality, scope and rollback behavior.

## Notes in bundled workflows

Workflow artifacts use the same `settings.canvas_annotations` format as the
workflow builder and workflow YAML export. Authors can keep explanatory text
and canvas geometry in their workflow snapshot or referenced workflow YAML:

```yaml
workflow_id: stock_sync
settings:
  canvas_annotations:
    - id: stock_sync_note
      text: "## Inventory sync\nValidate the input before updating stock."
      x: 80
      y: 120
      width: 360
      height: 180
```

Notes are presentation metadata, not steps. They do not affect routing,
execution, inputs or outputs. The host validates this field with the same
contract as ordinary workflow imports and preserves it when installing or
updating the extension. IDs must be unique, use letters, digits, underscores
or hyphens, and have at most 100 characters. Text is limited to 20,000 characters;
positions must be finite numbers between -1,000,000 and 1,000,000; width is
160–4,000 and height is 100–4,000. A workflow can contain up to 500 notes.

Bundled notes contain text and geometry only. Installation-local artifact IDs,
image URLs and inline image payloads are not accepted. Builder exports omit
attached images; they must be attached on the destination installation where
the workflow is editable.

A protected extension workflow shows its canvas, including notes, read-only:
operators can read the structure and connect their workflows to its hooks.
Declaring notes does not enable editing, cloning or exporting the protected
definition.
