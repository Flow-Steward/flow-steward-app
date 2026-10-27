# Flow Steward Feature Catalog

This catalog describes user-facing Flow Steward capabilities for first-release
documentation. Each feature should be documented with both behavior and
configuration: what it does, when to use it, where it is configured, defaults,
limits, safety notes, and troubleshooting.

## Documentation Pattern

Use this shape for feature pages:

| Section | Purpose |
|---|---|
| What it does | Plain-language capability summary. |
| When to use it | Common workflows and operator decisions. |
| How it works | Runtime model, data sources, and lifecycle. |
| How to configure it | UI, project settings, workflow settings, env vars, limits, and defaults. |
| Security and retention | PII, secrets, access, logging, artifact lifetime, and cleanup. |
| Troubleshooting | Common validation errors, failure states, and where to inspect evidence. |

Configuration should be grouped by scope:

- Deployment-level environment variables.
- Account or project policy.
- Workflow, group, step, and execution graph configuration.
- Runtime limits, allowlists, and retention settings.
- UI/API/CLI/MCP entrypoints.

## Core Capabilities

| Capability | What users need to understand | Main configuration surfaces |
|---|---|---|
| Projects | Projects group linked workflows, data connections, providers, rules, and integrations. | Project settings, project run config, provider credentials, workflow links. |
| Jobs | A job is an asynchronous workflow run with status, logs, runtime journal, messages, artifacts, approvals, and cost data. It runs, and Job Details shows, the definition snapshot it pinned before its first step. | UI submit form, REST/CLI/MCP task creation, workflow selection, queue policy, `GET /api/jobs/{job_id}/definition`. |
| External Workflow Authoring | Connected agents discover live local contracts, validate `platform.workflow_bundle.v2` bundles, save disabled drafts, and change existing workflows with atomic change sets (`workflow_apply_changes`) without enabling or executing them. | Shared MCP/API authoring services, project scope, administrator authority. |
| Workflow Builder | Visual authoring for reusable workflows made from steps, real loop/parallel groups and one execution graph. Structural edits stay local until Apply saves them as one revision; Discard drops them. | Workflow settings, group settings, step settings, edges and entry, Apply/Discard, Test Run. |
| Workflow Change Sets | One atomic writer of steps, groups and the graph for builder, API and MCP: one revision per set, `409` on a stale revision, per-operation reference authorization, enabled workflows refuse blocking issues. | `POST /api/workflows/{workflow_id}/changes`, `workflow_apply_changes`. |
| Workflow Test Runs | Durable worker-executed workflow-level and step-level tests for debugging mappings and contracts. Blocked while builder changes are unapplied or unsaved; refused with `409` before any job when the revision moved on. | Test Run request payload, `expected_revision`, task envelope, project binding, queued job status, dry-run mode. |
| Incoming Webhooks | Public workflow-owned HTTP entrypoints that can trigger a workflow. | Auth mode, content types, source restrictions, idempotency, body/file limits, input mapping. |
| Scheduled Jobs | Cron-like project schedules that run configured workflows. | Schedule enablement, cron expression, timezone, workflow input values, automatic per-schedule no-overlap protection. |
| Notifications | Project notification routes deliver normalized events through enabled providers. Workflow Automation reacts to job, step, group, approval and timer events at workflow, group or step scope. The project's Notifications tab is a read-only overview of both (Automatic notifications), with links to where each is configured. | Notification routes, provider extension, event type, severity, workflow scope, route key, Automation rules. |
| Approval Steps | An `approval` step pauses the run without a worker until a person approves or rejects; the graph routes `approved` and `rejected`. Recovery approvals pause a failed step execution or an exhausted rework budget. A child's approval also notifies waiting parents. | Approval `message`, `show_outputs`, `output_mapping`, `response_mapping`; error policy `await_approval`; reactions on `job.approval_waiting`; notification route. |
| Wait Steps | A `wait` step postpones the run for up to 365 days without holding a worker (`awaiting_timer`); the control process wakes due timers every second. | `duration_ms` or `input_mapping.duration_ms`; reactions on `job.timer_waiting`. |
| Agents and Skills | Agent steps execute a selected agent/skill pair and use declared input/output contracts. | Agent registry, skill registry, expected inputs/outputs, step mappings, project providers. |
| Connector Steps | Workflow steps can call extension or outbound MCP connector operations. Outbound MCP compatibility is bounded to remote Streamable HTTP `tools/list` and `tools/call` with complete tool results, including workflow-scoped sessions for the supported legacy protocol family and stateless MCP `2026-07-28`. Stdio, standalone legacy SSE, MRTR/`input_required`, Tasks, resources, and prompts are not supported. | Connector catalog, install binding, operation schema, connection binding, secret refs. |
| HTTP Request Steps | Built-in outbound HTTP/HTTPS API calls and bounded downloads from a workflow. Inline responses are capped at 5 MiB; artifact downloads default to 100 MiB and cap at 200 MiB using decoded-byte counting, no redirects, and static HTTPS origins for secret auth. FTP/FTPS/SFTP transfers need a file-transfer extension installed from the extension catalog. | Method, URL, headers, query, body template, auth mode/ref, timeout, expected statuses, response mode, max response bytes, artifact filename. |
| Dataset Query Steps | Workflow steps can execute saved dataset query templates through a connector runtime. | Dataset, connection, saved query template, params, row limit, timeout, result target. |
| Nested Workflows | A parent workflow can invoke a callable child workflow and map outputs/artifacts back. A serial invocation waits durably (`awaiting_child`) however long the child waits; a parent cancel cancels unfinished children durably; a lane invocation keeps the finite path. | Child workflow contract, input mapping, output mapping, artifact mapping, finite-path child timeout env. |
| Artifacts | Durable workflow files and uploaded attachments stored through the artifact plane. | Storage backend, retention class, presign TTL, upload limits, cleanup. |
| Global Search | Search across jobs, events, runtime messages, and workflows. | Search category, PostgreSQL backend. |
| Project Context | Runtime-only project context and curated task lessons support workflow execution without a public KB transport. | Canonical memory backend, curation, retention policy. |
| PII and Security Policy | Payloads can be scanned for secrets, prompt injection, PII, and custom rules. | Global/project policy, scanners, PII mode, detector toggles, custom regex rules. |
| Upload Policy | Attachments and webhook files are constrained by size, count, type, and safe archive checks. | Attachment env vars, webhook entrypoint limits, MIME/extension allowlists. |
| Logs and Runtime Journal | Operators can inspect job logs, agent messages, events, runtime journal, and diagnostics. | Log env vars, retention env vars, job detail UI, MCP job tools. |
| Trash and Retention | Deleted entities can be listed/restored/purged and operational data can be cleaned up. | Retention env vars, trash API/UI, ops schedule cleanup. |
| Watchdog | Control-loop scan for stuck, orphaned, interrupted, or recoverable runtime work. | Watchdog env vars, scan/recover API, runtime journal. |
| Queue and DLQ | RabbitMQ-backed per-project queues and dead-letter behavior protect delivery. | Queue names, retry policy, workload throttle/shed/pause env vars. |
| Extensions | External bundles can provide task ingress, notifications, tools, skills, datasets, and connectors. | Extension install, project connection, secrets, routes, manifests, callbacks. |
| MCP Server | Agents and IDEs can access Flow Steward project, job, workflow, report, and notification tools over stateless MCP `2026-07-28` Streamable HTTP or compatible legacy initialization. Authenticated `tools/list` is filtered by token scope and `tools/call` enforces authorization again. Inbound transports are `/mcp` Streamable HTTP and `stdio`; SSE is not supported. | Client MCP configuration, bearer token scopes, project/workflow allowlists. |
| CLI | Operators can manage projects, workflows, jobs, reports, release tasks, and tooling from the shell. | CLI command flags, env overlays, Docker auth state. |

## Priority Docs For First Release

1. Workflow Builder and runtime concepts (steps, groups, execution graph).
2. Workflow configuration reference.
3. Incoming webhooks.
4. Artifacts and uploads.
5. Security, PII, and custom rules.
6. Search and internal project context.
7. Notifications, Approval and Wait steps.
8. Watchdog, logs, retention, and troubleshooting.
