# Workflow steps compatibility matrix

Workflows without phases ([ADR 0035](../architecture/adr/0035-workflow-steps-without-phases.md))
change several contracts that components outside Flow Steward Core read or
write. This page lists those contracts and what each external component needs
before an installation can switch. The switch itself follows the
[rollout runbook](workflow-steps-rollout.md).

These are separate contracts with separate version numbers; do not read them as
one version:

| Contract | Version now | Was |
|---|---|---|
| Extension host contract (`runtime.contract_version`, wire envelopes, handshake) | `extension_host_v2` | `extension_host_v1` |
| Workflow bundle | `platform.workflow_bundle.v2` | `platform.workflow_bundle.v1` |
| Runtime checkpoint (`runtime_schema_version`) | `2` | phase-era |
| Extension descriptor schema, SDK package version | versioned independently; verify the required host and scope behavior | — |

## What changed

| Contract | Now | Effect on an old component |
|---|---|---|
| Host contract token | A release descriptor declares `runtime.contract_version: extension_host_v2`, and every invocation and event envelope carries `contract_version: extension_host_v2`. | The host refuses a descriptor with a missing, empty or different value and marks an installed release built for another contract incompatible; it never calls an old runtime "to try" and has no fallback to v1. |
| Invocation, event and artifact scope | Four keys, always present, string or null: `group_id` (the step's immediate Group), `for_each_id` (its immediate For each), `step_id` and `step_execution_id` (UUID; retries keep it, every For each item gets its own). At most one owner is set; a root step has both null, a Group- or For each-level event names only its container, a workflow-level envelope has all four null. Account, project, workflow and job scope are unchanged. There is no `phase_id`, not even empty. | A missing key or both owners set is refused before any extension effect; `group_id: null` alone never means root. A component that reads `phase_id` gets nothing. |
| Remote HTTPS runtime handshake | The host sends `contract_version: extension_host_v2` and `execution_scope_fields`; the runtime must answer with `result.contract_version: extension_host_v2` and exactly `result.execution_scope_fields: ["group_id", "for_each_id", "step_id", "step_execution_id"]`. | A missing or different answer marks the install incompatible before any operation or event; the token alone or a passing health check does not count. |
| Artifact and dataset grants | An explicit `group_id: null`, `for_each_id: null` (or `step_execution_id: null`) binds a grant to exactly that value; only a field a broader host-issued grant leaves out is unbound. Step grants carry both owners and the item's execution, so a root grant never serves a For each step, nor one item's grant another item. The scope is part of the signed claims. | An extension cannot widen a grant by sending null or dropping a signed key. |
| Approval notifications | Payloads, cards and outcomes carry `group_label` (only for a step directly in a Group) or `for_each_label` (a step in a For each) instead of `phase_label`. `allowed_actions` is `approve`/`reject`, or `approve`/`abort` for an exhausted rework budget, where reject is refused. An Approval step routes `approved`/`rejected`. | A card must show and send only the offered actions. |
| Child approvals | A child's `job.approval_waiting` is also delivered to each waiting ancestor with `propagated: true`, `approval_job_id`, `approval_id`, `child_job_id` and `origin_*` fields. | Links and decision callbacks must address `approval_job_id` + `approval_id`, never the parent's `job_id`. |
| Workflow reaction events | `job.group_started/completed/failed`, `job.approval_waiting` (`reason`: authored, recovery, autonomy) and `job.timer_waiting`; scopes `workflow`, `group`, `step`. | `job.phase_*`, `job.gate_waiting` and the `phase` scope are refused by API, MCP, import and validation, not aliased. |
| Job projection (REST, MCP, SSE) | `current_step_id`, `current_group_id`, `active_steps[]` (each with `group_id`, `for_each_id`, `lane_id`, `iteration`), `workflow_wait`; new statuses `awaiting_timer` and `awaiting_child`. | `current_phase` and a positional `current_step` are gone. |
| Message selectors | `message.step.<step_id>.<field>`, `message.group.<group_id>.<field>`. | `message.phase.*` is refused (`workflow.selector.phase_scope_removed`). |
| Workflow bundles | v2 carries `steps`, `groups`, `execution_graph`. | A v1 bundle is refused before any write with `required_schema_version`; there is no converter. |
| Workflow resume queue | One durable intent, `jobs.meta.workflow_resume`, published as `workflow_resume` (`reason`: approval, timer, child). | The old `approval_resume` message is not a valid intent and an unknown message type is dead-lettered, so finish or cancel old runs before switching. |
| Runtime checkpoint | Version 2, with the pinned definition reference. | A phase-era checkpoint is refused; an old run cannot continue on the new release. |

## What each external component needs

| Component | Needs before the switch |
|---|---|
| Every external extension (for example Shopify, Telegram, Outgoing Email/SMTP, LLM providers, and any other installed one) | A release whose descriptor declares `extension_host_v2`, that reads the four scope fields (`for_each_id` included) instead of `phase_id`, renders `group_label`/`for_each_label`, honors `allowed_actions`, and sends propagated approval decisions to `approval_job_id`. Each is a separate change in its own repository. |
| Extension SDK (`flowsteward_extension_sdk`) | A release built from the in-repository source (0.4.0): it speaks `extension_host_v2`, refuses an envelope with a missing, empty or different contract and a runtime context without the four scope keys or with both owners (`runtime_context_from_payload` raises `ValueError`), and returns `handshake_result()` for the handshake. Its package version follows the SDK's own release policy; it is not the host contract version. A previously published SDK release (0.3.0 and older) is not compatible. |
| Self-hosted and remote HTTPS runtimes | Confirm `extension_host_v2` and the exact `execution_scope_fields` in the handshake, and check `contract_version` and the four scope keys on every envelope before acting. |
| Workflow template catalog | Templates published as `platform.workflow_bundle.v2`, or the incompatible entries removed from what this release offers; a v1 template is refused at import. |
| Extension catalog | Entries pointing at `extension_host_v2` releases; a v1 release cannot be installed on this host. |
| API, MCP and SSE clients | Read the run position from `current_step_id`, `current_group_id` and `active_steps`, tell root, Group and For each apart by `group_id`/`for_each_id` (never `group_id: null` as root by itself), and treat `awaiting_timer` and `awaiting_child` as waiting, not stuck. |
| Automation rules and notification routes of existing installations | Recreated with group, step, approval or timer events during the manual recreation. |

The extension-side rules are in
[Host Contract `extension_host_v2`](../extension-authoring-agent-playbook.md#host-contract-extension_host_v2).
The SDK, extensions and catalogs have independent release processes. Verify
the exact package intended for installation against the host contract; a
package version or passing health check alone does not prove compatibility.

## Release gate

Before switching an installation, record in its release evidence the exact
package and runtime releases, their supported host contracts, and which
invocation, artifact, notification and approval contracts were verified.
Compatible SDK and extension releases must be ready and verified in staging.

An installation whose active workflows or enabled notification routes depend on
a component without a verified `extension_host_v2` release cannot switch. Do not
remove or disable an integration to pass this gate, and do not let approval
notifications silently stop.
