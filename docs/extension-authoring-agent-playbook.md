# Extension Authoring Agent Playbook

This playbook tells an AI agent what to study before writing a real Flow
Steward extension and how to build one by analogy with a published reference
extension from the extension catalog (https://github.com/Flow-Steward/extensions).
Flow Steward Core does not bundle any extension.

For portable developer-client guidance, use the
[Flow Steward Extension Authoring skill](agent-skills/flow-steward/skills/flow-steward-extension-authoring/SKILL.md).
[Installation and usage](GUIDE.md#developer-toolkit-local-agent-skills) cover
the portable plugin and optional native standalone skills. After an authorized
extension installation, use the
[Workflow Authoring skill](agent-skills/flow-steward/skills/flow-steward-workflow-authoring/SKILL.md)
to discover its operations through the existing MCP catalog.

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

`extensions create` writes a default package glyph. Replace the file with the
extension's own logo, keeping the name. `extensions validate`, `extensions
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
