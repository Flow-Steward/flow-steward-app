# ADR 0029: Compact and Scalable production deployment

## Status

Accepted, 2026-09-17. Compact runtime composition, persistent-volume bootstrap,
immutable extension invocation and profile-aware lifecycle services are
implemented. This record defines their architecture; release acceptance and
candidate verification remain separate requirements.

Amended 2026-09-22: Caddy was withdrawn from the supported `proxy_kind` set by
owner decision. The rendered fragment could not be proven in the same way as the
other two, and a Caddy site address written with an explicit `http://` scheme --
which is what the renderer emitted -- turns off the automatic certificate
handling that is the usual reason to pick Caddy at all. Nginx and Apache remain.
A `proxy_kind` of `caddy` already on disk is read as `nginx` rather than
rejected, so existing installations stay loadable.

Operator procedures and supported commands are documented in the
[deployment runbook](../../operations/compact-scalable-deployment.md).
Persistent paths and permissions are defined by the
[data registry](../../operations/compact-deployment-data-registry.md).
This ADR extends [runtime ownership](0002-runtime-ownership.md),
[atomic extension lifecycle](0015-atomic-extension-package-and-artifact-lifecycle.md)
and [isolated browser installation](0028-web-extension-installer.md).

## Context and observed constraints

Scalable runs separate application, worker, control, MCP and installer roles;
Compact composes those responsibilities inside one runtime. Separate source and
dependency slices preserve the slim Scalable images. The immutable PostgreSQL
image carries canonical first-install schema, and application bootstrap owns
seed and compatibility checks.

`DeploymentConfig.file_storage` supports `local_fs`, `bundled` and `external_s3`.
Installation state is durable rather than part of a runtime image's writable
layer. A runtime cannot assume the operator's checkout is available.

Workers retain per-incarnation identities, project lease slots, serial top-level
execution and RabbitMQ reconnection. Metrics liveness does not establish consumer
readiness. The queue drain loop receives the process stop event and checks it
before fetching another job.

Online extension updates retain previous immutable filesystem releases while
readers may still hold their paths. Invocation metadata, dependency resolution
and subprocess working directories must refer to the same selected release;
maintenance cleanup rechecks references before deleting retired releases.

## Decision

### Product and deployment boundaries

One release has two production deployment profiles:

- `compact`: one runtime replica containing the roles below, separate supported
  PostgreSQL with AGE/pgvector, separate RabbitMQ, one runtime volume at `/data`.
- `scalable`: existing separate role images and containers on one Docker host;
  one generic worker per worker container; capacity changes through replicas.

The development preset and audit/test images stay separate. Reject
`environment=development, deployment_profile=compact` before modifying anything.
Compact does not support multiple runtime replicas, overlapping replacement
replicas, mixed-profile consumers or simultaneous core versions. A local volume
lock prevents two Compact processes sharing that volume; provider replica and
rollout configuration must also enforce stop-before-start. A volume lock alone
cannot detect a second deployment using a different volume against the same DB.
Provider acceptance must test that policy explicitly. Downtime is supported.

Ingress selection is independent: `bundled_traefik`, `external_traefik`,
`external_proxy`, or `platform`. The first three support both profiles. `platform`
initially supports Compact only. Reject unsupported combinations without fallback.
Compact's base local_fs topology has three persistent services; optional bundled
Traefik adds an external TLS service. Existing bundled S3 retains SeaweedFS as an
explicit optional service and does not become a three-service topology by changing
the meaning of its storage selection.

### Fixed process inventory and ports

The ports below are composition defaults. Internal ports are fixed composition
constants; the ingress port and worker count are operator settings. Index `i`
starts at zero. Service roles run without root; supplementary data-access groups
are limited to the shared-tree policy below. The supervisor starts as root to
prepare mounted paths and drop service privileges.

| Process | UID:GID | Executable/module | Bind |
|---|---|---|---|
| Supervisor, PID 1 | 0:0 | `python -m core.entrypoints.cli.compact_provider_runtime` | health only, `127.0.0.1:9100` |
| HTTP router | 10006:10006 | `/usr/local/bin/traefik` with static CLI settings and `--providers.file.filename=/run/flow-steward-compact/router.yaml` for dynamic routing | `0.0.0.0:8080`, configurable |
| App | 10001:10001 | `uvicorn core.entrypoints.api.app:app` | `127.0.0.1:8099` |
| MCP | 10004:10004 | `python -m core.entrypoints.mcp.server --transport streamable-http --host 127.0.0.1 --port 8100` | `127.0.0.1:8100` |
| Installer | 10005:10005 | `uvicorn core.entrypoints.api.extension_installer:create_app --factory --host 127.0.0.1 --port 8101` | `127.0.0.1:8101` |
| Control | 10003:10003 | `python -m core.entrypoints.cli.control_process` | optional metrics `127.0.0.1:9102` |
| Worker `i` | 10002:10002 | `python -m core.entrypoints.cli.worker_process`, no project argument | optional metrics `127.0.0.1:(9200+i)` |
| Bootstrap/operator command, transient | 10007:10007 | `python -m core.entrypoints.cli.platform_runtime` | none |

The Python supervisor uses the existing pinned Python 3.12 base, Linux signals,
child reaping and process groups. No additional supervisor dependency or generic
role configuration language is introduced. Runtime logic belongs in
`core/infrastructure/runtime/compact/`; the CLI only dispatches to it. The router
binary comes from the exact existing production Traefik input
`traefik:v3.3@sha256:2cd5cc75530c8d07ae0587c743d23eb30cae2436d07017a5ff78498b1a43d09f`.
Build verification records its actual binary version and platform-specific source
digest; the tag is not interpreted as an exact patch version.

The supervisor owns process creation, UID/env/port assignment, readiness and
shutdown. It does not fetch jobs, allocate leases or schedule projects. The role
list is fixed. `APP_WORKERS` maps to app Uvicorn workers independently of the
workflow pool; `CONTROL_LOOPS_ENABLED=0` in every web process. Preserve current
worker-owned inbound polling, curation and knowledge sync locking/deduplication;
verify N>1 instead of moving their responsibilities speculatively.

`FS_WORKER_PROCESSES` defaults to 2 only when absent. Explicit empty, signed,
fractional, zero, negative or non-decimal input is invalid. Bound decimal parsing
before conversion. With this map N cannot exceed 56336; every configuration also
rejects ingress/role/metrics collisions and unavailable binds before child start.
This port bound is not a supported capacity claim. Explicitly setting this variable
for a Scalable worker is an error. Reserve the metrics range even when exporters
are disabled, so changing observability does not silently invalidate the topology.

### Supervision, readiness and shutdown budgets

The following runtime policy defaults are defined by canonical configuration,
the env manifest and generated env catalog. They are not performance
measurements and do not change queue, lease, retry or project-cap defaults.

| Canonical runtime setting / env | Default | Contract |
|---|---|---|
| `compact_startup_timeout_seconds` / `FS_COMPACT_STARTUP_TIMEOUT_SECONDS` | 300 | wall-clock deadline covering volume/bootstrap and all role readiness |
| `compact_restart_limit` / `FS_COMPACT_RESTART_LIMIT` | 3 | at most three automatic restarts per role's failure window; recovery can fall back to a group restart |
| `compact_restart_window_seconds` / `FS_COMPACT_RESTART_WINDOW_SECONDS` | 300 | monotonic sliding window, never reset by a brief ready state |
| `compact_restart_backoff_seconds` / `FS_COMPACT_RESTART_BACKOFF_SECONDS` | 2 | exponential 2, 4, 8 seconds; no jitter in deterministic tests |
| `compact_restart_backoff_max_seconds` / `FS_COMPACT_RESTART_BACKOFF_MAX_SECONDS` | 30 | cap when the restart limit is explicitly increased |
| `compact_shutdown_grace_seconds` / `FS_COMPACT_SHUTDOWN_GRACE_SECONDS` | 180 | one deadline from entry to shutdown, covering role drain, forced reap and adopted descendants |
| `compact_kill_reserve_seconds` / `FS_COMPACT_KILL_RESERVE_SECONDS` | 10 | shared forced-cleanup reserve within shutdown grace; standalone orphan cleanup uses one reserve |
| `compact_health_interval_seconds` / `FS_COMPACT_HEALTH_INTERVAL_SECONDS` | 2 | role/dependency observation cadence |
| `compact_health_stale_seconds` / `FS_COMPACT_HEALTH_STALE_SECONDS` | 10 | stale incarnation reports invalidate readiness |
| `compact_dependency_timeout_seconds` / `FS_COMPACT_DEPENDENCY_TIMEOUT_SECONDS` | 300 | after initial readiness, sustained dependency outage exits terminally |

Validate positive bounded integers and relationships (`kill_reserve < grace`,
`health_interval < health_stale`, `backoff <= backoff_max`) before any effects.
The supervisor computes one monotonic deadline on entry to `shutdown()`. Role
drain uses grace minus kill reserve; forced role reap and repeated adopted-chain
cleanup then share the remaining reserve. A late role reap or newly adopted
generation does not start another timer. Standalone orphan cleanup retains one
kill reserve and leaves live managed roles alone. If a managed role or visible
adopted descendant remains at shutdown's polling deadline, cleanup returns false
and group recovery cannot start an overlapping replacement.

Local Compose uses the same `FS_COMPACT_SHUTDOWN_GRACE_SECONDS` value, defaulting
to 180 seconds, for `stop_grace_period`; that budget now includes adopted cleanup.
The intended orchestrator allowance remains at least 190 seconds for the default
budget to leave an outer safety margin. Signal observation can delay entry to
shutdown, and signal dispatch and scheduler latency are not preempted by the
polling deadline. Thus this shared budget does not establish a hard end-to-end
190-second bound or prove installed/provider shutdown acceptance. Local Compose
still provides no separate allowance for the outer safety margin.

Initial local Compose restart policy is `on-failure:3`, not unbounded `always`.
Provider prerequisites include an equivalent finite restart policy or an explicit
operator intervention policy. Configuration/schema errors exit 78; exhausted
startup/recovery exits 1; completed operator shutdown exits 0.

For Compact on native Linux hosts using the system Docker daemon, the installer configures
a systemd oneshot boot trigger after successful installation. This is separate
from failure supervision: the unit has `Restart=no`, and Docker remains the only
container failure-retry owner. The trigger starts recorded, ownership-checked
container IDs after host boot or Docker activation; it cannot build, pull, recreate,
or migrate an installation. Infrastructure health checks precede application
startup. A missing recorded container requires the operator to rerun the installer.
The root-owned unit runs Docker operations as the deployment owner, and the normal
installer refreshes its record when containers change. Setup requires sudo;
explicitly disabled units stay disabled, while an explicit installer rerun adopts
its running containers for stop control. Shutdown drains consumers, then application
services, before stopping infrastructure. Failure cleanup also runs after an
unsuccessful startup. Platforms without host systemd keep their
own deployment lifecycle. Operator controls are documented in the
[deployment runbook](../../operations/compact-scalable-deployment.md).
Compact's bundled SeaweedFS follows the same `on-failure:3` policy so a disabled
installation stays stopped after Docker restarts. Scalable keeps its existing
Docker restart policies; switching to Scalable retires the checkout-owned Compact
unit rather than retaining stale container IDs.

Start sequence: validate complete configuration/build identity → acquire single
volume ownership → prepare/check paths → bootstrap once → create the fixed child
group in order: router, app, installer, MCP with direct app upstream, singleton
control, and N workers. Child creation does not await each role's readiness. The
router publishes its app/MCP routes statically; aggregate readiness is observed
separately and does not gate forwarding. Readiness-gated public admission is not
implemented by this sequence. Any partial pool creation failure keeps readiness
false and terminates the entire partial group. An unrecoverable bootstrap error
starts no consumers. Schema upgrade never runs once per role.

The implementation must expose actual consumer registration/disconnection/drain
state from the existing queue adapter through a small observation hook. It must
not infer it from a PID, metrics HTTP endpoint, a ready log line, existing project
count or successful TCP connect. Each role report is tied to a supervisor-created
incarnation channel, with no inherited write access in extension subprocesses.
The readiness observer has no queue mutation interface. An idle empty installation
is ready once one consumer has registered and mandatory role/dependency checks pass.
Reports cover busy and idle states; long jobs must not falsely expire readiness.

`GET /health/ready` on the public router proxies only the aggregate boolean/status
response, with no secrets, metrics or role control endpoints. Local health at
9100 includes a liveness/readiness distinction. Readiness requires completed
bootstrap, compatible schema, usable DB and broker, initialized app/installer/MCP,
healthy mandatory control loops, and at least one ready or actively executing
worker. Whether all N are is reported separately (`workers_complete` in the
loopback details), not folded into readiness: the supervisor terminates the
container once readiness stays false past its budget, and one worker restarting
under its backoff must not take every role with it.
The application's own readiness checks (the application's `/health/ready`: graph
catalogue identity, workflow-knowledge sync, queue lag and the rest) are not part of
the aggregate either, so they do not take a running installation out of the public
route: a degraded feature is not an outage. They are where an operator acts --
`./install.sh` evaluates them inside the runtime once the aggregate is ready and fails
by name on one that fails, and doctor reports them; `status` through the
one-shot CLI container reaches only the router's aggregate.
During maintenance report admission paused separately; the maintenance disable
health adapter checks dependencies and initialized roles without requiring already
unpaused consumers. This avoids a maintenance→readiness→maintenance deadlock.

Dependency loss sets readiness false and uses existing reconnection within the
outage budget, without restarting a still-live group repeatedly. Disabled optional
metrics do not cause a role restart. A crashed role is restarted alone, under its
own backoff, once its own process session is proven gone: the supervisor is the
subreaper, kills and reaps every process of that session, and resets only that
role's readiness. Where that cannot be proven -- no subreaper or /proc, a
descendant still alive at the kill deadline, or an adopted process that belongs
to no live role -- it falls back to stopping the group, and restarts it only
after every previous process and descendant is gone; failing that, it terminates
the container. The no-overlap guarantee is kept, narrowed to one role.

On SIGTERM, the supervisor first signals control and workers, keeping the router,
app, MCP and installer available during their drain. It then signals the remaining
services. There is no separate public-admission switch: static routes remain
available until the router is stopped. Public-admission gating during drain is an
unimplemented requirement, not a guarantee of the current supervisor. Preserve
completion dependencies through drain. Pass the process stop event through
the existing drain loop and check it before fetching another top-level job. This
does not introduce another execution policy. Keep leases/heartbeats with executing
workers; the supervisor never frees leases or ACKs jobs. At grace minus kill reserve,
SIGKILL surviving managed processes, then reap roles and repeatedly kill/reap
adopted descendants together within the original shutdown deadline.
Account for subprocesses that create their own sessions, not just the leader's
process group. Require a private PID namespace; if descendant cleanup cannot be
proved, no replacement role starts and PID 1 exits. Recovery then follows existing
lease expiry and delivery rules. No forced SUCCESS or synthetic ACK is allowed.

### HTTP composition and trust

Compact uses the pinned Traefik binary as an internal HTTP-only router. Its file
provider has no Docker socket/provider, dashboard, ACME, TLS or plugin loading.
The MCP route is ``Path(`/mcp`) || PathPrefix(`/mcp/`) || PathPrefix(`/.well-known/oauth-protected-resource`)``
with explicit priority 100. App is the fallback with priority 10, including
`/.well-known/oauth-authorization-server` issuer discovery. Keep
path/query/Authorization and streaming semantics; `/mcpx` reaches app. No
strip-prefix, request retries or response buffering.
MCP calls `FS_APP_INTERNAL_URL=http://127.0.0.1:8099` directly. The configurable
installer origin is `http://127.0.0.1:8101` and comes only from validated config.

Public origin comes from canonical config and must have no credentials, non-root
path, query or fragment. App trusts its immediate loopback router. The router trusts
only the configured external proxy peer addresses/CIDRs, strips untrusted forwarding
input and preserves the intended public scheme/host. MCP's current static allowed
hosts must include the validated public authority without permitting arbitrary hosts.
Regression coverage verifies both MCP transport security and auth; changing
allowed hosts is not permission to disable either. External Scalable app/MCP trust lists are derived from
their real immediate proxy, not from the Compact loopback default.

Use an unlimited response-write timeout on the internal router for supported
streaming; keep bounded connection and request-read timeouts. Provider timeouts are
separate acceptance inputs. Traefik documents distinct exact/prefix matchers and
trusted peer settings in its [routing rules](https://doc.traefik.io/traefik/v3.3/routing/routers/)
and [entrypoint options](https://doc.traefik.io/traefik/v3.3/routing/entrypoints/).

### Canonical configuration and migration

`DeploymentConfig` is the single durable non-secret source. Its closed
patch, command-specification and derivation surfaces share the following fields;
supported operator commands are listed in the deployment runbook.

| Field | Env projection / choice |
|---|---|
| `deployment_profile` | `FS_DEPLOYMENT_PROFILE=compact\|scalable`; absent legacy → scalable |
| `ingress_mode` | `FS_INGRESS_MODE=bundled_traefik\|external_traefik\|external_proxy\|platform` |
| `public_url` | existing `FS_PUBLIC_BASE_URL`; canonical external origin |
| `runtime_http_port` | `FS_RUNTIME_HTTP_PORT=8080`; deployment adapter maps provider `PORT`, rejects conflicting explicit values |
| `worker_processes` | `FS_WORKER_PROCESSES=2` for Compact only; never projected into Scalable workers |
| `file_storage` | `local_fs\|bundled\|external_s3`; new unspecified Compact → local_fs; all saved choices preserved |
| `artifact_local_root` | existing `ARTIFACT_LOCAL_ROOT_DIR`; Compact `/data/artifacts` |
| `tls_mode` | retain legacy values; add `acme\|external_resolver\|external_certificate\|platform_managed` |
| `proxy_placement` | `FS_PROXY_PLACEMENT=host\|docker`; external_proxy only |
| `proxy_kind` | `FS_PROXY_KIND=nginx\|apache`; selects operator fragment |
| `proxy_trusted_ips` | `FS_PROXY_TRUSTED_IPS`; validated addresses/CIDRs; no wildcard trust |
| `traefik_network` | `FS_TRAEFIK_NETWORK`; existing external Docker network |
| `traefik_https_entrypoint` | `FS_TRAEFIK_HTTPS_ENTRYPOINT`; explicit external Traefik value |
| `traefik_http_entrypoint` | `FS_TRAEFIK_HTTP_ENTRYPOINT`; optional HTTP redirect entrypoint |
| `traefik_cert_resolver` | `FS_TRAEFIK_CERT_RESOLVER`; required only for external_resolver |
| `proxy_network` | `FS_PROXY_NETWORK`; external_proxy Docker network |
| `reverse_proxy_backend_port` | retain current 18099 default for host app/Compact backend |
| `reverse_proxy_mcp_port` | `FS_REVERSE_PROXY_MCP_PORT=18100`; Scalable host MCP only |
| `acme_contact` | `FS_ACME_CONTACT`; bundled ACME only |
| `acme_directory_url` | `FS_ACME_DIRECTORY_URL`; explicit controlled/staging/production CA |
| `extension_installer_origin` | `FS_EXTENSION_INSTALLER_ORIGIN`; derived internal HTTP origin; validate against topology |

Allowed ingress/TLS pairs: bundled_traefik with local, self_signed, provided or
acme; external_traefik with external_resolver or external_certificate;
external_proxy with reverse_proxy; platform with platform_managed. New production
installation must select an ingress owner explicitly; there is no inferred provider
from the hostname. Legacy installations use the mapping below. Conditional fields
are required only for their selected owner, and contradictory explicit fields fail
before state publication.

Installation identity reuses persisted Compose identity for installed stacks;
platform persists an equivalent generated identity before resource derivation.
Names/aliases include that identity. Cloning an installation as a new instance
requires a new identity; recreating/upgrading the same instance preserves it.

Legacy conversion is lossless: `reverse_proxy` → external_proxy/host with existing
loopback ports; `provided` → bundled_traefik/provided with existing material;
`local|self_signed` → bundled_traefik and the same generated-certificate mode.
Conflicting old/new inputs fail validation. Existing bundled S3 credentials, bucket
and endpoint remain unchanged. Profile switching never copies S3 into local_fs.
Keep local/self-signed explicitly labelled private/development TLS options, never
fallbacks for failed production issuance.

Derive the whole ordered Compose file/profile/env set in one place and reuse it in
install, launcher, doctor, status, apply/recovery, recreate, upgrade and backup.
Use separate profile base manifests, then mutually exclusive ingress overlays and
storage overlays. Existing production manifest becomes the Scalable base through
a scoped extraction of its ingress wiring. Never overlay Compact onto the existing
all-role production file. Tests inspect rendered config, including removed bindings,
dependencies and labels. Initial compatibility floor remains Docker Compose plugin
2.0.0, matching the existing major-version check; fixtures must test that floor
as well as the current version. Prefer construction without merge-reset tags;
if implementation requires them, raise, pin and
test the required minimum before release. [Compose merge rules](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/)
explain why supplying another file does not by itself remove inherited entries.

### Persistence, extension permissions and invocation

Use the detailed path/UID ledger in the
[data registry](../../operations/compact-deployment-data-registry.md). `/data`
and all parents of extension code/active links are root-owned and not writable by public/runtime
roles. Only the installers write managed extension directories: the installer UID
owns them, and group 10103 lets the other profile's installer replace them (see the
shared data trees below). Runtime
source is root-owned. There is no extension install-policy file; grants come
from each manifest's `runtime_grants`, limited to its own namespace.
Bootstrap performs descriptor-relative,
symlink-safe preparation after the volume is mounted, then drops privileges for
services. It checks existing ownership/modes and rejects unsafe trees; it never
blindly recursively chowns a restored volume or replaces existing releases.

For each invocation, choose manifest metadata and its immutable release together
under the existing lifecycle synchronization boundary; pass that release path to
code and dependency resolution for the whole subprocess lifetime. Retain retired
filesystem releases and required metadata until confirmed quiescent maintenance.
Online update no longer deletes a retired release merely because active install
references moved. Cleanup runs explicitly under maintenance with writers/executors
stopped, verifies active references again and uses existing safe cleanup helpers.
Preflight free disk space before publishing. No new GC service, refcount database,
workflow-wide pinning or schema version manager. Regression coverage must include
uninstall/rollback and reader/update races, including metadata selection before
process launch.

One container's UID separation is weaker than separate containers. It does not
provide tenant sandboxing or distinct network/cgroup boundaries. Providers must
allow permission preparation, role UIDs, a private PID namespace and the required
termination budget. Running everything as root is unsupported.

### Shared data trees across the profiles (owner's decision B)

A switch between the profiles keeps the installation's data where it is. Artifacts
and extensions stay in Scalable's host directories or in the Compact data volume,
and the other profile mounts them there; nothing is copied, moved or re-owned. The record of where they are is `data_placement` in the installed
configuration: `host_directories` or `compact_volume`, following from the profile the
installation was created with, and written only where it differs from that profile's
own (earlier releases read the configuration as a closed shape). Compact on host
directories binds them over `/data` (`infra/docker/compact/data-host_directories.yaml`);
Scalable on the volume mounts it by subpath where each service bound a host directory
(`infra/docker/scalable/data-compact_volume.yaml`).

Both profiles reach the trees through fixed supplementary groups: 10100 artifacts,
10103 extension writers (the extension installer of either profile, and
nothing else; every other process reads extensions through the "other" bits). Every
process runs under umask 0002 and the trees' directories carry setgid, so what one
profile writes the other can change. This is the one exception to the rule that
permissions are never widened for shared data: supplementary groups on the shared data
trees, and nothing else. Secrets and installation state keep explicit owner-only modes
under that umask, and no tree is ever made world-writable or re-owned.

A Scalable installation's trees are given those groups and modes only by the switch
itself (`./install.sh --deployment-profile compact`), never by an update: on its own no
step changes an installation. The normalization runs as root in the target image over
the host trees alone, walks by descriptor without following links, leaves private
entries (nothing for group or others) and owners as they are, and sets each tree
root's mode last as its record of completion, so an interrupted run is finished by the
next. It first refuses, naming the group, a host group that already holds one of the
fixed gids and is not ours: its members would read the data. A group named
`flow-steward-artifacts` or `-extensions` is ours -- the optional group an
operator creates to give host users access to a tree. Only the host's group file is
consulted; a group known only to a network directory service is not.

The Compact volume check takes a host-bound tree's owner as it is (the host user) and
still requires its group and mode; the overlay that binds the trees says so
(`FS_COMPACT_DATA_PLACEMENT`). Bundled object storage runs beside either profile on
the same named volumes, on an internal network only the runtime joins.

The switch is the lifecycle's own journaled `config apply`, run in the Scalable CLI
container, with the installer preparing what apply does not: the target images, the
normalization, the worker credential that the
Compact runtime owns, a render of the target configuration that names anything missing
before any change, and the pending change with the placement kept. The installer lends
the lifecycle lock to the apply for its duration; its own lock and host record keep
every other command refused meanwhile. A switch interrupted anywhere continues on the
next `./install.sh`.

A provider deployment without Compose (Railway, for example) runs the Compact image on
one volume and has no switch: it has neither `./install.sh` nor a second profile to
switch to.

### Build, identity and lifecycle

The `compact-runtime` image uses a checked union of app/worker/control/MCP runtime source
and dependency inputs, prebuilt frontend, public extension SDK, worker toolchain,
router binary, and scoped operator tools. Scalable retains its slim targets. Source
slice tests reject whole-repository copies and development/audit dependencies.
The build emits root-owned immutable build metadata containing revision, role,
platform and input digests. Runtime env cannot overwrite that evidence.

Release metadata lists Compact, app, worker, control, MCP, required CLI artifacts,
custom PostgreSQL and the selected broker/proxy references. The PostgreSQL release
image packages canonical first-install schema; application source slices carry
the seeds and bootstrap code. Core images bundle no extensions. Operators install
packages from the extension catalog or supported archives under ADRs 0013/0015;
a runtime image never copies the developer's extension directory.

On the official artifact path, Docker-host installation resolves digest-pinned
registry artifacts, verifies role/platform/revision and passes the verified local
image/config ID to the existing exact-prebuilt guard. Preserve index digest,
platform manifest digest and config ID as separate fields. Platform startup
verifies embedded build identity against the
expected artifact metadata; external deployment evidence proves what digest the
provider actually ran. An env claim is not independent attestation. Compact worker
provenance identifies the aggregate image, never the separate worker image.

An ordinary fresh checkout installation without an installed or pending official
release identity or release handoff uses source builds under
[ADR 0030](0030-local-install-and-private-release-auth.md). Source provenance
records the full Git commit SHA and validates that selected and running
image/config identities prove that same revision. Existing official identities
and handoffs retain the complete verified role set and never silently fall back
to source builds.

Platform lifecycle uses a restricted thin command composition over existing
bootstrap, identity, maintenance, schema and backup services. It runs with the
same `/data` mount after attachment. Adapt the backup gateway to native PG client
execution while retaining backup encryption/verification policy. No Docker socket,
HTTP management API, new IAM, vendor client or alternate backup archive format.
Unsupported host-management commands return an actionable diagnostic before effects.
Supported commands and their limitations are in the
[deployment runbook](../../operations/compact-scalable-deployment.md#lifecycle-command-matrix).

Backup is an offline coordinated operation: pause admission, drain/stop all writers,
protect PostgreSQL, preserve broker data including messages, snapshot `/data` and
any selected bundled object storage, preserve secrets separately when provider-owned.
Keep external S3 objects consistent through the documented storage-owner procedure.
Restore the complete matching set at the same app/PG/broker versions before restart.
RabbitMQ definitions alone are insufficient. Schema-incompatible rollback needs
matching data restore, not just an older image reference.

TLS belongs to the selected ingress owner. Bundled ACME uses HTTP-01 on 80 and
public HTTPS on 443, persistent owner-only account/certificate state, controlled CA
renewal tests and an independent staging smoke. Provided fullchain/key undergo
existing PEM/key/SAN/expiry validation and atomic owned-proxy rotation. External
resolver/certificates remain externally managed. Never restart or modify an
unrelated proxy or copy its private keys into this installation.

## Alternatives considered

1. **Selected: HTTP-only Traefik plus a fixed Python process owner.** Reuses the
   current routing engine; an extra in-container process has an explicit health
   and dependency cost. Keeps MCP and installer boundaries visible.
2. **Embed MCP in the app ASGI process.** One fewer process, but couples lifespans,
   auth/transport handling and HTTP worker multiplicity; rejected for the
   separate-MCP contract and bounded composition scope.
3. **Use a general-purpose supervisor package.** Mature generic restart features,
   but still requires custom consumer readiness, bootstrap and drain coordination.
   Rejected to avoid a second configuration/state owner.
   If the fixed process owner cannot pass child-reaping/restart tests, revisit this
   choice explicitly before production implementation, not through a silent fallback.

## Validation and consequences

Required validation covers one-port app/MCP/auth/streaming/WS and direct MCP→API
behavior; empty-volume and interrupted/repeated bootstrap; N registered consumers;
immutable provenance without Docker; Linux UID denials; and concurrent immutable
release invocation/update/cleanup behavior. Implementation and regression-test
presence do not establish release acceptance.

Release completion also requires both profile regressions, ingress/TLS lifecycle,
coordinated backup/restore and profile switching, frozen-baseline benchmarks,
one real provider acceptance, and exact-candidate quality evidence. A green local
unit suite cannot substitute for those results. This ADR makes no minimum-RAM,
speedup or candidate-readiness claim.
