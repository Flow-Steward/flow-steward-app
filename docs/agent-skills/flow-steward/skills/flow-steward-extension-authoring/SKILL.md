---
name: flow-steward-extension-authoring
description: Use when a developer or integrator wants to create or change a Flow Steward extension bundle using the platform's existing manifest and CLI lifecycle.
---

# Flow Steward Extension Authoring

Build against the target release's contracts and existing lifecycle. Keep
provider logic in the extension's out-of-process runtime.

## Locate the contracts

Find the target Flow Steward application checkout. Read `AGENTS.md`, applicable
`docs/standards/*`, `docs/standards/extension-manifest-standard.yaml`, and
`docs/extension-authoring-agent-playbook.md`. Locate its version-matched
documentation checkout for `docs/integrations/build-an-extension.md`,
`docs/developer/extensions.md` and `docs/reference/cli.md`; the playbook identifies
fallbacks when supporting docs are unavailable. Report gaps instead of inventing
flags. Do not assume an absolute machine path or repository-relative links from
this copied skill folder.

Choose a compatible reference exercising the required features; Core bundles no
reference extension. <https://github.com/Flow-Steward/ext-example-showcase>
implements every extension feature and is the default reference. Inspect its manifest, contracts and representative runtime
handlers. Keep schemas in their canonical owners rather than copying them here.

## Choose the authored contracts

`extensions create` writes a minimal `tool_provider` at version `1.0.0`, with
features `[tool, action]`, empty `event_subscriptions`, and a sample
`save_settings` action with `workflow_visible: false`. It does not generate a
workflow-visible tool action or a finished task/notification provider. Choose
`tool_provider` + `tool`, `task_provider` + `task_ingress`, or
`notification_provider` + `notify`, then replace the sample contracts and
handlers together; changing kind/features alone is insufficient.

For workflow use, explicitly set the action's `workflow_visible: true` and
supply a same-ID visible Contract V2 operation. Visible operations need the
same-ID visible action in the other direction too. Active workflow-visible
actions require Contract V2; merely exposing the settings action in UI does
not make it callable from a workflow. Keep configuration/delivery actions
hidden where their contracts require `workflow_visible: false`.

Flow Steward deliberately has no inline code step. When a user asks for "a
code step", build an action with `workflow_visible: true` plus a same-ID
Contract V2 operation, and call it from the workflow's Extension tools step.

### Host contract and logo

`runtime.contract_version` must be `extension_host_v2`, the one host contract
Flow Steward 1.0 runs; the scaffold writes it. Build runtime code on the public
`flowsteward-extension-sdk` (0.4.0 or later), never on private Core modules.
The bundle's logo is a file named `icon.png` next to `extension.yaml`: a square
PNG of 32–512 px (128×128 recommended), at most 100 KB. There is no manifest
field for it. The scaffold writes a neutral default icon; replace it before
publishing. The archive installer validates the icon.

Declare the extension's outbound network access under `egress` (an extension
that makes no outbound calls declares `egress: {}`); without it, installation
warns `egress_undeclared`.

### Task ingress

Declare these reserved action/query contracts and implement their runtime
responses. Every listed `result_fields` value and `error_codes` entry is required:

| Operation | Kind | Result fields | Required errors |
| --- | --- | --- | --- |
| `normalize_task_action` | Action | `result.normalized` | `not_configured`, `authentication_failed`, `invalid_payload`, `rate_limited`, `provider_unavailable`, `timeout`, `internal_error` |
| `fetch_attachments` | Action | `result.attachments` | `not_configured`, `authentication_failed`, `not_found`, `rate_limited`, `provider_unavailable`, `timeout`, `internal_error` |
| `sync_completion_status` | Action | `result.status` | `not_configured`, `authentication_failed`, `conflict`, `rate_limited`, `provider_unavailable`, `timeout`, `internal_error` |
| `provider_health` | Query | `result.status`, `result.configured` | `not_configured`, `authentication_failed`, `provider_unavailable`, `timeout`, `internal_error` |

All declared action/query errors for this profile must come from the target
release's `TASK_INGRESS_ERROR_CODES` catalog in
`core/infrastructure/db/ingress_runtime.py`; additional invented errors fail
validation. The generated health query's smaller error set is insufficient for
this profile. Do not invent a polling/ack operation to replace the reserved IDs.

### Notification delivery and ingress events

An active notification provider reserves exactly one `deliver_notification`
action, same-ID V2 operation and external effect:

- Action: `handler.mode: extension`, `workflow_visible: false`, exactly required
  object parameters `event` and `route`.
- V2 operation: `operation_kind: action`, `workflow_visible: false`, exactly
  required object inputs `event` and `route`; outputs include `delivered`
  (`boolean`) and `provider_message_id` (`text`).
- Effect: `operation_id: deliver_notification`, `effect_kind: message_delivery`,
  nonempty `channel`, `idempotency: required`, `test_mode_behavior: suppress`.

`release_stage: placeholder` exempts delivery validation, but does not exempt
kind/features and cannot be enabled. It is not a working notification provider.

Author `event_subscriptions`; compilation derives `supported_events`. Writing
`supported_events` directly in the manifest fails with `unknown_manifest_field`.
The `ingress_event` feature is notification-provider-only and requires an
`ingress_event` action with `result_fields: [result.ingress_type]` and selector:

```yaml
event_subscriptions:
  - event_type: external.comment.received
    severity: any
    conditions: {}
```

`notification.interaction_received` is recommended, not required. Subscriptions
and ingress handling do not replace the reserved delivery contract.

Phase events no longer exist. Subscribe to the current job events, such as
`job.group_*`, `job.approval_waiting` and `job.timer_waiting`.

### Own namespace grants and distribution metadata

Declare only needed `runtime_grants.resources` / `runtime_grants.mutations`
from the manifest standard. Host grants cover the owning extension's data:
`scope` may only be `owning_extension`, and a `provider_key` may only equal the
extension's own ID; relevant kinds default to those values. Foreign namespaces,
unknown kinds or malformed `secret_keys` / `connection_type_ids` lists fail
with `runtime_grant_forbidden`. Vendor identity or catalog verification grants
no extra rights; use the public SDK rather than private Core imports.

Runtime manifest vendor metadata uses `vendor.name`; the strict
`extension-descriptor.yaml` vendor model requires `vendor.vendor_name` and
rejects unknown fields. The descriptor uses
`schema_version: extension_descriptor_v1` and `release_version`, not manifest
`version`. Its release version defaults to `1.0.0`, so set it explicitly when
changing the manifest version. Archive manifest `extension_id` and `version`
must match descriptor `extension_id` and `release_version`. Descriptor
`platform_version` in a validation request selects host compatibility, not the
extension release. Marketplace manifest checks do not validate this descriptor
shape. Validate both surfaces before packaging/importing.

## Author and verify

Work from an installed, healthy Development source environment. Use the public
Docker-backed `./flow-steward` launcher and current `extensions --help` plus
subcommand help. The public module is `python -m core.entrypoints.cli` inside
the supported runtime; private CLI modules are not executable alternatives.
Do not use host pytest or a host virtual environment for platform verification.

1. Define the extension ID, behavior and operation input/output contracts.
   Scaffold through `extensions create` or adapt the chosen compatible reference.
   Implement only the requested features, using the canonical YAML manifest,
   bundle-local references and public extension SDK/runtime contracts.
2. Run `extensions validate` and focused `extensions test`. Repair actual
   contract/runtime failures before packaging. Health checks can execute the
   extension's health command; choose fixtures appropriate to the requested task.
   Testing requires `requirements-dev.txt`; dependencies install offline with
   `pip --no-index`, searching `dev-wheels/` and `wheels/`. Inherited tooling
   packages can satisfy requirements; supply compatible local wheels for missing
   development dependencies. Without `tests/test_*.py`, the run fails unless
   intentionally allowed with `--allow-no-pytest`; report the omitted coverage.
   Validation, smoke and dependency failures still fail the run.
3. For third-party Python dependencies, follow the target release's wheel guide
   and playbook: `extensions lock-deps` must lock the full transitive closure and
   wheel hashes, then `extensions bundle --with-wheels` must include portable
   wheels for both Linux AMD64 and ARM64. The default platforms are
   `manylinux2014_x86_64,manylinux2014_aarch64`, for the tooling interpreter's
   Python version/implementation/ABI, not every Python version. A compatible
   universal wheel may cover both; compiled dependencies require matching wheels
   and hashes for each. Use `--platforms active` only for an explicitly requested
   local-only bundle. If either target lacks compatible wheels, stop portable
   packaging and report the gap; do not silently omit a target or rely on runtime
   downloads.
   - The runtime is CPython 3.12 on glibc 2.36 (`python:3.12-slim`, Debian
     bookworm). For packages that publish only `manylinux_2_28` wheels, pass
     `--platforms manylinux_2_28_x86_64,manylinux_2_28_aarch64` to both
     `lock-deps` and `bundle`.
   - Plain fallback without the CLI: `pip download --only-binary=:all:
     --platform manylinux2014_x86_64` (and again with `manylinux2014_aarch64`)
     `--implementation cp --python-version 3.12 --abi cp312 --abi abi3 --abi none`,
     with each wheel's hash listed in `python_requirements`.
   - `extensions validate` does **not** check both architectures. Before
     handoff, list `wheels/` and confirm, for every requirement, an `_x86_64`
     and an `_aarch64` wheel, or one `any` wheel.
   - Archives are limited to 50 MiB zipped and 150 MiB unpacked. A pure-Python
     bundle over the limit can be split with `--runtime-target linux-amd64` and
     `--runtime-target linux-arm64`, one archive per architecture.

Native executables require two separately built and targeted archives using
`--runtime-target linux-amd64` and `--runtime-target linux-arm64`. Wheel tooling
is not a cross-compiler. Packaging and wheel coverage are not actual installation
verification on both architectures; report the environments and checks actually
run, including missing architecture coverage. Keep the full procedure in the
located `docs/integrations/build-an-extension.md` wheel/distribution guide.

The bare bundle example below is for zero third-party Python dependencies, from
the application checkout with a task-authorized new ID:

```bash
./flow-steward extensions create com.acme.example --non-interactive --confirm
./flow-steward extensions validate com.acme.example --root extensions --health
./flow-steward extensions test com.acme.example --root extensions --non-interactive
./flow-steward extensions bundle com.acme.example --root extensions --output-dir dist/extensions --non-interactive --confirm
```

The default scaffold version is `1.0.0`, so this example writes
`dist/extensions/com.acme.example-1.0.0.zip`. Use the reported archive path when
the manifest version changes. Bundle success reports packaging; retain the
separate validate/test results as evidence of those checks.

An extension kept in its own repository (outside the application checkout) is
checked by the application's quality gate, from the application checkout, with
an absolute path to the extension checkout:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root /abs/path/to/extension-checkout
```

`extension-static` runs compile, ruff, ruff format, mypy and bandit over the
bundle's modules; `extension-tests` is the `extensions test` path (validation,
then pytest in an offline venv). The checkout is copied into the gate
container, so commit first and cite that commit. To publish to the public
extension catalog, use the `flow-steward-publish-extension` skill.

`--root` is the parent containing the normalized bundle folder, not that folder.
The leaf `--confirm` accepts the reviewed create/package operation; it is not
permission for installation or publication. `extensions test` has no `--confirm`.
It requires the Development launcher's dedicated `fs-test-runner`; a generic
MCP/QA/admin client can refuse. Report that limitation and use the supported
launcher, without setting `FS_EXTENSION_TEST_RUNTIME` to bypass it. Production
does not provision test tooling implicitly. Nonzero or review exits are not
successful validation.



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

## Install only within the request

When installation is requested and authorized, inspect the archive and run the
existing `extensions install --archive <path>` lifecycle, following its review.
That command works on Development and Scalable installations; on Compact,
install only through the UI's **Install extension from file** or
`POST /api/extensions/install`. An update is the installation of a higher
version; the same or a lower version is refused.
Then use `extensions list`, `extensions show <extension_id>` and
`extensions health <extension_id>` to inspect the installed result. Report what
ran, actual results, archive identity and remaining connection/readiness needs.
Do not install, enable or publish automatically. After authorized installation,
use the workflow-authoring skill's live discovery through the existing knowledge
synchronization path; do not create another index or execution interface.
