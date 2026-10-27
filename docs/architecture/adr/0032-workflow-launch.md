# ADR 0032: Explicit workflow launch

## Status

Accepted, 2026-10-01. The project owner approved explicit agent-driven workflow
launch and human approval through the shared runtime. Clarified 2026-10-12:
MCP launch is direct-only. Updated 2026-10-24 at the project owner's request to restore acknowledged ordinary Builder runs. Implementation and final
quality-gate evidence are tracked separately.

## Context

An agent needs to start a selected workflow without requiring a published
webhook. Direct workflow launch and published entrypoint invocation have
distinct input and channel contracts, while sharing durable queue admission.

## Decision

`workflow_trigger` exposes a workflow-as-a-tool surface. Direct launch requires
explicit project and workflow identity and uses JSON `POST /api/workflow-runs`.
Its application service reuses the existing durable queue admission owner and
enforces access, readiness, input-contract, MCP-channel, lifecycle-pause and
idempotency checks. Caller credentials remain authoritative. The server chooses
the MCP launch channel and internal queue correlation; callers cannot supply
arbitrary runtime state or provenance.

MCP discovery starts with a concrete project and its linked workflows. The agent
reads the public input contract, asks for missing required inputs, and launches
only the selected `workflow_id`; both `workflow_contract` and `workflow_trigger`
require `project_name`. Missing inputs fail before enqueue and do not create an
empty job. A generated job summary is not a workflow input.

Direct MCP launch accepts actual file bytes in a bounded JSON `files` array.
Each upload names its declared `file` input and supplies a filename, MIME type
and strict base64 content. Up to 10 uploads and 1 MiB decoded total are allowed,
within the existing configured attachment policy. Arbitrary artifact handles,
URLs and server paths are not an upload transport. Missing, malformed or fake
files fail before job creation. The existing attachment persistence owner stores
validated bytes before queue dispatch and binds the server-created artifact
reference to the named workflow input. Raw encoded content is not job metadata.
The agent asks for actual files before launching. MCP does not advertise
external webhook entrypoints; their REST listing and ingress remain unchanged.

External CRM/ERP, forms, and event providers invoke
`/webhook/workflows/{entry_key}` under the existing webhook transport, credential,
input-mapping, replay, and queue-admission contracts. MCP does not invoke that
webhook or delegate server-owned webhook credentials. Webhook creation remains a
separate action; direct launch does not require a published webhook.
The editor keeps workflow/step Test Run as the default testing flow and also
provides an explicitly acknowledged **Run workflow** action for ordinary execution.
It uses `POST /api/workflows/{workflow_id}/run`, requires an administrator browser
session, an enabled project-workflow link and a positive saved definition revision,
and chooses the UI channel and normal mode on the server. Token/MCP callers cannot
use this browser channel. Dirty/unapplied definitions, missing UI channel, disabled
workflows and unavailable dependencies block ordinary launch. The shared admission
owner enforces scope, readiness, lifecycle pause, declared inputs and idempotency;
the creating transaction pins the expected revision before dispatch.

Ordinary Builder runs accept the same bounded, named actual uploads as direct MCP
launch. Test samples and kept test outputs are never ordinary inputs. The live-effects
acknowledgement starts unchecked and resets on launch ownership changes and success.
Ordinary results follow their returned job identifier through shared Job Details,
separately from Test Run sessions and history. Test Run still supports saved disabled
drafts and retains its warning that HTTP and model calls can have real effects.
Generic `POST /api/tasks` remains retired; agent-driven launch uses `workflow_trigger`.

The run returns a durable job identifier. Existing `job_status`, `job_approval`
and `job_approval_decide` tools track the run and its human gates. An agent must
obtain the user's human approval decision; launch authority is distinct from
approval authority.

Internal workflow start, scheduler, follow-up jobs and shared queue mechanics
retain their owners. Schedule editing uses the project scheduler; creation and
enablement require explicit actions, and saved rows appear in the Workflow's
schedule list for that project.

## Consequences

Clients select an explicit workflow or a configured entrypoint. File-bearing
events use configured multipart webhook ingress. Queue, job history and approval
persistence share their existing storage model.

Verification covers direct MCP launch, webhook invocation, scope/channel
rejection, approval continuity and shared queue behavior. Final handoff requires
the canonical Docker gates for the final committed revision; this decision does
not claim those gates passed.
