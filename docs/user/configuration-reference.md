# User Configuration Reference

This reference groups the configuration surfaces that users and operators are
most likely to need while working with Flow Steward features. The canonical env
contract lives in `core/infrastructure/config/flow_steward_env_manifest.json`.
For a generated inventory of documented env variables, grouped by scope and
owner, see `docs/configuration/flow-steward-env-catalog.md`.

## Configuration Scopes

| Scope | Use it for | Examples |
|---|---|---|
| Deployment env | Runtime defaults, infrastructure, safety limits, storage, logging. | `.env`, `.env.example`, `.env.example.ops`, `.env.example.prod`. |
| Account/project policy | Per-project security, provider, connection, notification, and data access behavior. | Project settings, security policy, project connections. |
| Workflow settings | Public interface, trigger channels, callable contract, workflow-level behavior. | Workflow detail page, import/export, external MCP authoring. |
| Group settings | Loop source and item name, sequence or lanes body, lane concurrency, error policy, boundary contract. | Workflow builder; `POST /api/workflows/{workflow_id}/changes`. |
| Step settings | Step type, mappings, connector binding, HTTP config, error policy, boundary contract. | Step drawer/editor. |
| Execution graph | Entry, outcome edges, rework edges and per-edge rework limits. | Workflow builder canvas; `POST /api/workflows/{workflow_id}/changes`. |
| Runtime evidence | Logs, events, runtime journal, security findings, notification deliveries. | Job detail, monitoring, MCP, CLI. |

The installer-managed `.env` or `.env.prod` file is the deployment
configuration and Compose-substitution source; it is not exported wholesale to
containers. Each process receives a closed role-specific environment:
`fs-app` gets application/auth/provider settings, `fs-worker` gets scalable
execution/queue/provider settings, `control-plane` gets singleton scheduler and
watchdog settings, and the development frontend gets only its backend target.
Changing the deployment file therefore does not expose unrelated database,
broker, storage, authentication, provider, or worker secrets to another role.

## Production Deployment Profiles

The canonical installer/config fields are independent choices:

| Field | Values | Rule |
|---|---|---|
| `deployment_profile` | `compact`, `scalable` | Compact is production-only and limited to one runtime replica. |
| `worker_processes` | integer `1..56336` | Compact only; default `2`; applied by recreate. Scalable capacity uses worker container replicas. |
| `file_storage` | `local_fs`, `bundled`, `external_s3` | New Compact installs default to `local_fs`; existing configurations retain their value during profile switches. |
| `ingress_mode` | `bundled_traefik`, `external_traefik`, `external_proxy`, `platform` | Platform ingress is Compact-only. External modes do not start bundled Traefik. |
| `tls_mode` | `local`, `self_signed`, `provided`, `acme`, `reverse_proxy`, `external_resolver`, `external_certificate`, `platform_managed` | The selected mode must belong to the selected ingress owner. |
| `runtime_http_port` | port | The single Compact HTTP port; provider environments may map their `PORT` to this value. |

Digest-pinned Compact distribution uses `FS_COMPACT_IMAGE_REGISTRY_REF`, `FS_COMPACT_IMAGE_INDEX_DIGEST`, `FS_COMPACT_IMAGE_MANIFEST_DIGEST`, `FS_COMPACT_IMAGE_CONFIG_DIGEST`, `FS_COMPACT_IMAGE_REVISION`, `FS_COMPACT_IMAGE_ROLE`, and `FS_COMPACT_IMAGE_PLATFORM`. These fields describe distinct OCI and build identities. A registry reference must contain `@sha256:`; the host installer resolves it to a real local image/config ID before Compose runs. See the [production deployment guide](../operations/compact-scalable-deployment.md) for the ingress-specific inputs and lifecycle sequence.

## Authentication Cookies

`FS_AUTH_COOKIE_SECURE` controls the authentication cookie's `Secure` attribute.
Compact Compose defaults to `1` and projects an explicit override only to the app
process. Use `0` only for controlled local HTTP testing; keep `1` for production
HTTPS. Without an explicit supervisor setting, the app retains its secure
default. This setting does not disable authentication or CSRF protection.

## API Documentation Surfaces

FastAPI's default `/openapi.json`, `/docs`, and `/redoc` routes are disabled.
Expose only the curated documentation surface you need:

| Variable | Purpose |
|---|---|
| `FS_OPENAPI_PUBLIC_ENABLED` | Enables `/openapi-public.json` for the external integration API allowlist. |
| `FS_DOCS_PUBLIC_ENABLED` | Enables `/docs/api` when the public schema is enabled. |
| `FS_OPENAPI_INTERNAL_ENABLED` | Enables `/openapi-internal.json` for authenticated app/operator `/api/*` routes. |
| `FS_DOCS_INTERNAL_ENABLED` | Enables `/docs/internal` when the internal schema is enabled. |
| `FS_OPENAPI_PUBLIC_REQUIRE_AUTH` | Requires auth for the public schema and Swagger UI; defaults to `1`. |
| `FS_OPENAPI_INTERNAL_REQUIRE_AUTH` | Requires auth for the internal schema and Swagger UI; defaults to `1`. |

## Uploads And File Types

General attachment policy is deployment-level:

| Variable | Purpose | Typical default |
|---|---|---|
| `FS_ATTACHMENT_MAX_SIZE_BYTES` | Maximum size per general attachment. | `20971520` |
| `FS_ATTACHMENT_MAX_FILES` | Maximum files per request. | `10` |
| `FS_ALLOWED_ATTACHMENT_MIMES` | Comma-separated MIME allowlist. | Images, PDF, Office, text, CSV, octet-stream. |
| `FS_ALLOWED_ATTACHMENT_EXTENSIONS` | Comma-separated extension allowlist. | `.png,.jpg,.jpeg,.webp,.gif,.pdf,.xlsx,.xls,.docx,.txt,.md,.csv` |

Incoming workflow webhooks also have per-entrypoint limits:

| Webhook setting | Purpose | Default |
|---|---|---|
| `allowed_content_types` | Request content types allowed for this webhook. | JSON, multipart, form, text. |
| `max_body_bytes` | Maximum request body. | `46137344` |
| `max_file_bytes` | Maximum uploaded file size. | `10485760` |
| `max_files` | Maximum uploaded file count. | `4` |
| `source_restrictions.exact_ips` | Exact IP allowlist. | empty |
| `source_restrictions.cidrs` | CIDR allowlist. | empty |

Zip uploads are inspected before being stored.

## PII And Security Policy

Security policy is available globally and as a project override.

Top-level settings:

| Setting | Values | Meaning |
|---|---|---|
| `enabled` | `true` / `false` | Turns policy enforcement on or off. |
| `level` | `minimal`, `standard`, `strict` | Applies preset severity and scanner behavior. |
| `surfaces.*.action` | `scan`, `skip` | Controls scanning on inbound, outbound, tool, and index surfaces. |

Scanner settings:

| Scanner | Configurable settings |
|---|---|
| Secrets | `enabled`, `severity_threshold`, `allowlist`. |
| Injection | `enabled`, `severity_threshold`, `allowlist`, `contexts`. |
| PII | `enabled`, `severity_threshold`, `allowlist`, `mode`, detector toggles. |

PII modes:

| Mode | Behavior |
|---|---|
| `pseudonymize` | Replaces detected PII with stable pseudonym tokens. |
| `hard_redact` | Replaces detected PII with redacted placeholders. |
| `allow` | Detects but leaves values visible. |

PII detector toggles:

- `person_name`;
- `email`;
- `phone`;
- `ssn`;
- `credit_card`;
- `ipv4`;
- `ipv6`.

Custom project rules:

| Field | Purpose |
|---|---|
| `rule_id` | Stable identifier. |
| `scanner` | `secrets`, `injection`, or `pii`. |
| `pattern` | Regex pattern. |
| `severity` | `low`, `medium`, `high`, or `critical`. |
| `description` | Operator-facing explanation. |

Deployment-level security variables:

| Variable | Purpose |
|---|---|
| `SECURITY_SCAN_MODE` | Policy runtime mode. |
| `SECURITY_FAIL_CLOSED_ON_POLICY_ERROR` | Blocks payloads when policy resolution fails. |
| `SECURITY_PII_SALT` | Salt used for deterministic PII pseudonymization. |
| `FS_TRACE_REDACT_MAX_LENGTH` | Max retained length for redacted traces. |
| `FS_EXTENSION_DEBUG_PAYLOADS` | Enables extension payload debug details. |
| `FS_EXTENSION_PAYLOAD_REDACT_KEYS` | Redaction key list. |
| `FS_EXTENSION_PAYLOAD_REDACT_EXACT_KEYS` | Exact redaction keys. |
| `FS_EXTENSION_PAYLOAD_REDACT_KEY_FRAGMENTS` | Redaction key fragments. |
| `FS_EXTENSION_PAYLOAD_REDACT_MAX_DEPTH` | Max nested redaction depth. |
| `FS_EXTENSION_PAYLOAD_STRING_TRUNCATE_LENGTH` | Max retained string length. |
| `FS_EXTENSION_DEPS_FETCH` | Extension dependency fetch mode: `off` (offline bundled wheels only) or `allowlist` (allow install-time fetch from the configured default index). |
| `FS_EXTENSION_DEPS_SITE_MAX_BYTES` | Maximum bytes allowed for a materialized per-extension Python dependency site directory. |
| `FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS` | Trusted local/dev override for extension artifact `file://` grants. Keep `0` for lower-trust extensions; use S3-compatible storage for enforced read/write capabilities. |

## Artifacts And Storage

Artifact storage is configured at deployment level.

| Variable | Purpose |
|---|---|
| `ARTIFACT_STORAGE_BACKEND` | `memory`, `s3`, or `local_fs`; unsupported names raise a configuration error instead of silently selecting memory. The runtime default is `local_fs`. Explicit `memory` is in-process only, loses bytes when that process is replaced, and does not produce extension subprocess grants. `local_fs` is trusted/dev-only for extension artifact grants unless `FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS=1`. |
| `ARTIFACT_KEY_PREFIX` / `ARTIFACT_S3_KEY_PREFIX` | Optional object key prefix. |
| `ARTIFACT_S3_ENDPOINT_URL` | S3-compatible endpoint. |
| `ARTIFACT_S3_PUBLIC_ENDPOINT_URL` | Optional browser-reachable S3-compatible endpoint used to sign direct artifact downloads. Leave empty when `ARTIFACT_S3_ENDPOINT_URL` is already browser reachable. |
| `ARTIFACT_S3_BUCKET` | Object bucket. |
| `ARTIFACT_S3_ACCESS_KEY` | Access key. |
| `ARTIFACT_S3_SECRET_KEY` | Secret key. |
| `ARTIFACT_S3_REGION` | Region. |
| `ARTIFACT_S3_USE_SSL` | SSL toggle. |
| `ARTIFACT_S3_PRESIGN_TTL_SECONDS` | Presigned URL TTL. |
| `ARTIFACT_LOCAL_ROOT_DIR` | Root path for local filesystem backend. |

User-facing artifact retention classes:

- `ephemeral`;
- `run_window`;
- `audit`;
- `persistent`.

## Workflows

Workflow-level configuration:

| Area | Settings |
|---|---|
| Public interface | `trigger_channels`, `input_mode`, `strict_inputs`, `expected_inputs`, `expected_outputs`. Returned files (file outputs) are derived from steps' `override.returned_files`; `output_mode` is derived. |
| Execution graph | `execution_graph`: `schema_version`, `entry`, `edges[]` (`source`, `outcome`, `target`, `rework`, `rework_limit`). Written only through change sets. |
| Group | `group_id`, `kind` (`loop` or `parallel`), `loop` (`over`, `item_name`), `body` (`sequence` with `entry`, or `lanes` with `lanes[]` and `concurrency_limit`), `error_policy`, `boundary_contract` (`entry`, `exit`). |
| Step | `step_id`, `step_kind`, `group_id`, `lane_id`, `input_mapping_jsonb`, `output_mapping_jsonb`, `connection_binding_jsonb`, `error_policy` (`on_error`, `max_retries`, `backoff_seconds`, `retry_on`), `boundary_contract` (`entry`, `exit`, `exit_on`), `optional`, `override`. |
| Approval step | `override.approval`: `message`, `show_outputs`, `output_mapping`, `response_mapping`. |
| Wait step | `override.wait`: `duration_ms` (0 to 31,536,000,000) or `input_mapping.duration_ms`. |
| Agent step | `agent_id`, `skill_id`, agent/skill contracts. |
| Connector step | `connector_id`, `connector_install_id`, operation schema, project connection. |
| HTTP step | Method, URL, headers, query params, body template, auth mode/ref, timeout, expected statuses. |
| Dataset query step | Query template, params, row limit, timeout, result target. |
| Nested workflow | Child workflow id, input mapping, output mapping, artifact mapping. |

Nested workflow deployment knobs (finite path of a child called from a parallel
lane; a serial parent waits durably as `awaiting_child` and ignores them):

- `FS_INVOKE_CHILD_TIMEOUT_SECONDS`;
- `FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS`;
- `FS_INVOKE_CHILD_POLL_SECONDS`;
- `FS_INVOKE_CHILD_CANCEL_ON_TIMEOUT`;
- `FS_INVOKE_INLINE_EXECUTION`.

## Search And Memory

| Variable | Purpose |
|---|---|
| `FS_MEMORY_PROFILE` | Memory backend profile. |
| `FS_MEMORY_TEXT_SEARCH_BACKEND` | Text search backend. |
| `FS_MEMORY_VECTOR_BACKEND` | Vector backend. |
| `FS_CONTEXT_GRAPH_BACKEND` | Context graph backend. |
| `FS_PGVECTOR_DSN` | Optional pgvector DSN. |

## Workflow Discovery

| Variable | Purpose |
|---|---|
| `FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED` | Enables lexical fallback. |

Provider defaults:

- `FS_DEFAULT_PROVIDER`;
- `FS_DEFAULT_MODEL`;
- `FS_DEFAULT_TEMPERATURE`;
- LLM provider credentials. Add and rotate these in **Providers Settings**; they are encrypted and account-scoped rather than environment variables.

## Notifications

Project notification route settings:

| Setting | Purpose |
|---|---|
| `route_key` | Stable route/channel key used by automation. |
| `provider_key` | Notification provider. |
| `event_type` | Event subscription; supports exact and wildcard matching. |
| `severity` | Minimum severity. |
| `workflow_id` | Optional workflow scope. |
| `route_config` | Provider-specific options such as channel id, suffix, timeout. |
| `enabled` | Route toggle. |

Notification evidence is visible through project notification deliveries, job
logs, and event dispatch records when dispatch tracking is available.

## Logs, Retention, And Trash

Logging variables:

| Variable | Purpose |
|---|---|
| `FS_LOG_LEVEL` | Base log level. |
| `FS_LOG_FILE_LEVEL` | File sink level. |
| `FS_LOG_STDERR_LEVEL` | Stderr level. |
| `FS_LOG_FORMAT` | Log format. |
| `FS_LOG_DIR` | Runtime log directory. |
| `FS_LOG_FILE_BASENAME` | Runtime log filename base. |
| `FS_LOG_FILE_ENABLED` | File logging toggle. |
| `FS_LOG_STDERR_ENABLED` | Stderr logging toggle. |
| `FS_LOG_CAPTURE_WARNINGS` | Python warnings capture. |
| `FS_LOG_RETENTION_DAYS` | File log retention. |
| `FS_LOG_UTC` | UTC timestamps. |

Retention variables:

| Variable | Purpose |
|---|---|
| `FS_RETENTION_LOGS_DAYS` | Default action-log retention in days, for accounts with no saved policy. |
| `FS_RETENTION_MESSAGES_DAYS` | Default job-log (agent message) retention in days, for accounts with no saved policy. |
| `FS_RETENTION_TRASH_DAYS` | Default trash retention in days, for accounts with no saved policy. |
| `FS_RETENTION_TERMINAL_JOB_CHECKPOINTS_DAYS` | Default retention in days of checkpoints of finished jobs, for accounts with no saved policy. |
| `FS_RETENTION_REVISIONS_KEEP` | Default number of revisions kept per item (1–100, default 5), for accounts with no saved policy. |
| `FS_RETENTION_JOBS_DAYS` | Job retention. |
| `FS_RETENTION_EVENTS_DAYS` | Event retention. |
| `FS_RETENTION_RETRY_QUEUE_DAYS` | Retry queue retention. |
| `FS_RETENTION_ENABLED` | `0` stops the control-plane retention loop (Scalable). |
| `FS_RETENTION_INTERVAL_SECONDS` | Cadence of the retention loop; default 86400, minimum 3600. |

Retention runs in the control plane once a day; see
[Scheduled maintenance](../operations/scheduled-maintenance.md).

Trash supports list, restore, purge plan, and purge execute operations for
supported entity types.

## Runtime Heartbeat Monitoring

Compact and Scalable publish runtime-owned observations over RabbitMQ into
PostgreSQL. The admin API reads stored status only; no Docker socket, provider
discovery or WebSocket is required. Configure these installation-level values
through the existing CLI/configuration owner, not an independent health catalog.

| Variable | Default | Purpose |
|---|---|---|
| `FS_INSTALLATION_ID` | unset | Persisted UUID for managed-platform installations without a Compose identity. Never generate a new value on every boot. |
| `FS_MONITORING_EXPECTATIONS` | `{}` | JSON object of service names to desired instance counts, or `null` when the deployment owner cannot know the count. Compose projects receive the deployment owner's projection. |
| `FS_MONITORING_DEPENDENCIES` | `[]` | JSON array of dependency names expected by this installation. Names are generic; missing checks remain unobserved. |
| `FS_MONITORING_INTERVAL_SECONDS` | `60` | Publication/retention-loop interval; must be below the stale threshold. |
| `FS_MONITORING_STALE_SECONDS` | `180` | Maximum heartbeat/source freshness window; transport does not reset original check age. |
| `FS_MONITORING_HISTORY_ENABLED` | `0` | Enables optional delta history; duplicate or unchanged facts do not fabricate new history. |
| `FS_MONITORING_CURRENT_RETENTION_DAYS` | `7` | Age-based cleanup of last-known current rows, independent of history. |
| `FS_MONITORING_HISTORY_RETENTION_DAYS` | `7` | Independent history retention. |
| `FS_MONITORING_CLEANUP_BATCH_SIZE` | `500` | Maximum rows removed per retention batch. |
| `FS_MONITORING_QUEUE_MAX_MESSAGES` | `10000` | Bounded per-scope heartbeat queue; overflow rejects new reports. |
| `FS_MONITORING_QUEUE_MAX_BYTES` | `16777216` | Heartbeat queue byte cap. |
| `FS_MONITORING_OPERATION_TIMEOUT_SECONDS` | `2` | Per-operation broker/database timeout budget. |

Compose uses the installed `FS_COMPOSE_PROJECT_NAME` identity, checked against
the installation journal when available. Explicit and installed identities must
agree. Clones use distinct scopes. Do not change the identity merely to refresh
the monitoring page.

Service roles, observed process incarnations and dependency probes are counted
separately. Readiness, lifecycle, source freshness and evidenced health are
different dimensions. An idle consumer can be ready; liveness is not a health
check. New reports preserve the original check timestamp and cached-check age.
During a broker outage the last committed report remains readable and becomes
stale normally; receiver channel evidence is independent of service evidence.

Replica completeness is `unknown` if any expected target count is unknown.
Process restarts produce new incarnation rows rather than authoritative
replacement of a stable replica slot. Old rows age out or are cleaned up after
retention; do not interpret observed incarnation count as an exact live-replica
inventory. The page polls every 20 seconds; no on-request broker probe is made.

## Watchdog And Runtime Control

Watchdog variables:

| Variable | Purpose |
|---|---|
| `FS_WATCHDOG_ENABLED` | Enables watchdog loop. |
| `FS_WATCHDOG_INTERVAL_SECONDS` | Scan interval. |
| `FS_WATCHDOG_AUTO_APPLY` | Applies recoveries automatically when safe. |
| `FS_WATCHDOG_STUCK_SECONDS` | Stuck-job threshold. |
| `FS_WATCHDOG_ORPHAN_GRACE_SECONDS` | Orphan grace period. |
| `FS_WATCHDOG_PICKUP_GRACE_SECONDS` | Pickup grace period. |
| `FS_WATCHDOG_MAX_JOURNAL_ENTRIES` | Runtime journal cap. |

Artifact cleanup control:

| Variable | Purpose |
|---|---|
| `FS_ARTIFACT_CLEANUP_RECONCILIATION_BUDGET` | Maximum cleanup journals reconciled per dispatch scan; clamped to `1..128` (default `16`). |

Workload pressure variables:

- `FS_WORKLOAD_THROTTLE_ACTIVE_JOBS`;
- `FS_WORKLOAD_THROTTLE_QUEUE_DEPTH`;
- `FS_WORKLOAD_SHED_ACTIVE_JOBS`;
- `FS_WORKLOAD_SHED_QUEUE_DEPTH`;
- `FS_WORKLOAD_PAUSE_ACTIVE_JOBS`;
- `FS_WORKLOAD_PAUSE_QUEUE_DEPTH`.

Use watchdog scan/recover APIs and the runtime journal when diagnosing stuck or
interrupted jobs.

## RabbitMQ transport

`FS_RABBITMQ_TRANSPORT_MODE` defaults to `tls`. Remote brokers must use `amqps`,
with certificate verification and hostname checking enabled. Component-built
broker URLs use TLS and port 5671 by default; an explicit `RABBITMQ_URL` preserves
its vhost, credentials, query options, and port but is validated before connecting.
Unsafe TLS query options are refused, including a mismatched TLS server name.

Canonical Compose and installer configuration explicitly select `compose-local`
for their owned `rabbitmq-fs:5672` service on the deployment project network.
Disposable audit configuration selects `audit-local` for `rabbitmq-audit:5672`.
These are deployment assertions, not trust inferred from a DNS name or private
IP address. Changing the URL to another plaintext broker is refused even when
one of these local modes is inherited. Verified TLS remains allowed in every mode.

For host development, `loopback-local` permits plaintext only at an explicit
numeric loopback address and port, such as `127.0.0.1:5673` or `[::1]:5673`.
`localhost`, other DNS names, and non-loopback private addresses do not qualify.
No mode disables certificate checks. Deployment doctor applies the same policy
before probing the broker socket. Refusal diagnostics never include URI credentials.

TLS uses the default trusted CA store unless the existing client `ssl_options`
query supplies a CA bundle. Mount that bundle at the same configured path in every
broker client runtime. The certificate must identify the URL host, including an
IP subject alternative name when the URL uses a numeric IP. An omitted client
server name is normalized to that host; an explicit different name is refused.
Do not disable certificate or hostname verification to accommodate a certificate.

See [ADR 0043](../architecture/adr/0043-rabbitmq-transport-trust-boundary.md) for the
trust decision and [the rollout procedure](../operations/compact-scalable-deployment.md#rabbitmq-transport-policy-rollout)
before upgrading an existing deployment with a plaintext broker URL.
