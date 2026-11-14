# Flow Steward Extension SDK

Public Python SDK surface for Flow Steward extension subprocesses.

Extensions may import `flowsteward_extension_sdk` for stable platform
primitives such as artifact grants, runtime context helpers, SSRF-safe HTTP, and
portable extension test-kit utilities. Extension runtime code must not import
private `core.*` modules.

## Host contract

Runtime releases must explicitly declare `runtime.contract_version: extension_host_v2`.
The host refuses missing, empty and older declarations before invoking the
extension. Remote runtimes must also confirm the same version and its execution
scope during handshake: return `handshake_result()` as the handshake `result`
(`contract_version` plus `execution_scope_fields` =
`["group_id", "for_each_id", "step_id", "step_execution_id"]`).
The SDK's `runtime_context_from_payload` rejects envelopes without that explicit
version, and runtime contexts that miss one of the four scope keys or name both
a Group and a For each. `group_id` and `for_each_id` name the step's immediate
container; both are `None` for a root step. A version declaration alone is
insufficient: the release must implement this execution scope and its grants.
Published extension and SDK artifacts require their own compatible releases;
upgrading the platform does not modify installed extension bundles.

## Reading the connection

An action, and a page query that declares `connection` (host contract 1.7.0,
ADR 0067), runs with the project connection the host resolved for it. The host
puts it at `action.target.connection` for an action and at the envelope's root
`target.connection` for a query; `connection_from_payload` reads either:

```python
from flowsteward_extension_sdk import connection_from_payload

connection = connection_from_payload(payload)
if connection is None:
    ...  # no connection for this invocation
base_url = connection.config["base_url"]
token = connection.secrets["service_token"]
```

`RuntimeConnection` carries `connection_id`, `connection_type`, `display_name`,
`environment`, `credential_revision`, `config` and `secrets` (secret values or
the extension-owned OAuth access token; `repr` never shows them). A query that
holds a connection only reads: the host refuses commands, events and
project-state updates from it, checks its result for echoed secret values, and
stops it after 30 seconds. Report an unreachable service as a domain failure
(exit code 2, `ok: false`) so the install's circuit breaker is not tripped.

## Reporting progress

A long operation can tell the person watching the run what it is doing.
`report_progress` shows the message, and `done` of `total` when you give them,
on the running step in Job Details:

```python
from flowsteward_extension_sdk import report_progress

for page in pages:
    export(page)
    report_progress("Exporting products", done=exported, total=product_count)
```

Call it as often as you like; the host keeps the newest update. Outside a run,
or on a Flow Steward older than this SDK's 0.3.0, it does nothing, and it never
raises. An extension that must also run on older hosts imports it defensively.

## Versioned data migrations

Bundles declare `lifecycle.data_schema_version` and an ordered
`lifecycle.data_migrations` chain. A migration action accepts a
`MigrationEnvelope` and returns `migration_result(value)`. The only supported
targets are install configuration, project connection configuration, workspace
data connection configuration, and extension project state.

The host executes the action from the staged candidate bundle before publishing
it. The payload contains only the stable record ID, namespace when applicable,
schema versions, target type, and non-secret JSON value. It never contains a
database handle, Core import path, runtime token, credential, secret value,
secret ref, artifact grant, or host mutation grant. Migration actions cannot
declare external effects.

The host snapshots revisions before invocation, validates every result against
the candidate contract, locks and rechecks the source rows, and applies all
changed values plus the new applied schema version in one lifecycle transaction.
Any timeout, invalid output, concurrent update, or persistence failure leaves the
previous package and data active.
