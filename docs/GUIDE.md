# Flow Steward — User Guide

Flow Steward is a self-hosted AI workflow automation platform. You build workflows from steps, connected by one execution graph, grouped where needed into Groups that run once (in sequence or as parallel lanes) and For each blocks that run their steps once per item of a list — in the workflow builder or through MCP with an external coding agent — link them to projects, and run them as tasks from the web UI, the REST API, webhooks, MCP, or schedules. Runs can call agents, extensions, HTTP endpoints, datasets, and other workflows, pause for a human decision at an Approval step or for a set time at a Wait step, and produce artifacts and notifications.

For production topology, ingress/TLS, provider lifecycle, backup/restore, and profile switching, see [Compact and Scalable Production Deployment](operations/compact-scalable-deployment.md). Destructive same-profile installation replacement is documented in the [Installation Clone runbook](operations/installation-clone.md). Persistent paths and permissions are in the [Compact Deployment Data and Permission Registry](operations/compact-deployment-data-registry.md). Moving an existing installation to workflows without phases is a manual recreation: follow the [workflow steps rollout runbook](operations/workflow-steps-rollout.md) and check the [workflow steps compatibility matrix](operations/workflow-steps-compatibility.md) first.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Prerequisites](#prerequisites)
3. [Step 1 — Initial Setup](#step-1--initial-setup)
4. [Step 2 — Create a Project in the UI](#step-2--create-a-project-in-the-ui)
5. [Step 3 — Run a Workflow](#step-3--run-a-workflow)
6. [Step 4 — Workflow Runtime](#step-4--workflow-runtime)
7. [Step 5 — Human Approval](#step-5--human-approval)
8. [Step 6 — Optional Local/Audit UI Testing](#step-6--optional-localaudit-ui-testing)
9. [Step 7 — Internal Memory](#step-7--internal-memory)
10. [Step 8 — Flow Steward MCP Server](#step-8--flow-steward-mcp-server)
11. [Step 9 — Monitoring & Logs](#step-9--monitoring--logs)
12. [Step 10 — Multi-Model](#step-10--multi-model)
13. [Extension Python Dependencies](#extension-python-dependencies)
14. [Step 11 — Extension Notifications](#step-11--extension-notifications)
15. [Feature And Configuration Docs](#feature-and-configuration-docs)
16. [Environment Variables Reference](#environment-variables-reference)
17. [Troubleshooting](#troubleshooting)
18. [Installation Clone](#installation-clone)

---

## How It Works

```
Task (manual or provider webhook)
        │
        ▼
  ┌─────────────┐     RabbitMQ queue
  │  Web Server  │ ──────────────────► Worker process
  │  (port 8099) │                         │
  └─────────────┘                          ▼
                                   LangGraph workflow runtime
                                           │
                                           │
                                           ▼
                                    PostgreSQL
                         (job state, logs, memory,
                          pgvector search, AGE graph)
                                           │
                                           ▼
                         approvals → outputs, artifacts → notifications
```

Tasks run asynchronously. The web process creates a job and publishes it to RabbitMQ; a worker process picks it up and runs the LangGraph workflow to completion. You watch progress in real time on the job log page.

---

## Prerequisites

- A running **Docker** engine using Linux containers, and **Docker Compose v2**,
  for both Compact and Scalable on a server or desktop
- AI provider credentials are optional during installation and configured after login

Use Docker's official [Engine](https://docs.docker.com/engine/install/),
[Ubuntu](https://docs.docker.com/engine/install/ubuntu/), or
[Desktop](https://docs.docker.com/desktop/) installation documentation. Flow
Steward does not install Docker automatically.
Docker Desktop includes Compose; Linux Engine users also need the official
[Compose plugin](https://docs.docker.com/compose/install/linux/).
On Windows, run the shell entrypoints from Git Bash or WSL with Linux containers.
On Linux, follow Docker's [post-install instructions](https://docs.docker.com/engine/install/linux-postinstall/)
to give the deployment user access; do not run the installer as root.

Check before running `./install.sh`, from the same deployment account:

```bash
docker --version
docker compose version
docker info
```

Missing CLI/Compose means the prerequisite needs installation. A failing
`docker info` means the daemon must be started or socket access corrected.

---

## Step 1 — Initial Setup

```bash
# 1. Clone Flow Steward
git clone <flow-steward-repo-url> flow-steward
cd flow-steward

# 2. Install or safely resume core, then validate shared readiness
./install.sh
./flow-steward status
./flow-steward doctor

# 3. Inspect schema state; the installer already owns initial schema and seed setup
./flow-steward database status

# 4. Production runtime uses direct API provider keys. CLI provider auth is
# local/optional only and requires a CLI-enabled image.

# 5. Open the web UI
open https://localhost:8099
```

`./install.sh` checks the canonical schema and applies idempotent seed data on
both Compact and Scalable. Compact also performs schema/seed bootstrap before
starting its role processes. Its host launcher does not support
`database bootstrap-seed`; if the installer reports a seed failure, correct the
reported problem and rerun `./install.sh`. The Scalable command remains available
for an explicit operator seed repair.

At 1.0, `db/migrations/` is empty, so a fresh installation needs no separate
migration command. Later SQL migrations use `database upgrade` after enabling
maintenance and verifying a backup, with `--maintenance-confirmed --confirm`;
see the [deployment lifecycle procedure](operations/compact-scalable-deployment.md#update-an-installation-to-new-sources).

A fresh production install of a clean checkout exactly at its published
`v<VERSION>` tag (`git checkout v<version>` before `./install.sh`) installs that
stable release's official images: the installer reads the release from
`FS_RELEASE_REPOSITORY`, requires its tag to name the checked-out commit, and
pulls the complete digest-pinned image set from the first configured registry
that has all of it. From 0.9.2 that set includes the PostgreSQL image
(`flow-steward-postgres`), so nothing is built; 0.9.0 and 0.9.1 published no
PostgreSQL image, and installing them still builds it locally. The result is an
official installed identity. When that release is not published as stable, its images
are incomplete, or GitHub or the registry is unreachable, the installer stops
with `release_images_unavailable` and the cause; it never falls back to a
source build. Wait for publication, or check out a non-release commit.

Any other checkout (no tag at `HEAD`, the tag on another commit, or tracked
changes) builds the current checkout in Docker, without requiring a release tag
or rediscovering it through GitHub. Use a full Git clone for the Scalable
build's release bookkeeping. Docker still needs base/dependency image access.
This creates a source installation, not an inferred official identity. The
installer prints which of the two it chose. Official release transition/upgrade
and Clone activation verify their complete image sets separately; existing
installations keep their identity.

Scalable `./flow-steward release upgrade` uses a separate CLI runner so it can
finish after replacing the retained CLI. For a private registry, explicitly
select a container-compatible Docker credential store with `DOCKER_CONFIG`
in the host shell before running it. The runner mounts only that directory's
readable `config.json`, read-only at the same absolute path, and keeps Buildx
metadata in its own writable `/tmp`. It uses the mounted Docker socket's default
context rather than a host-only context named in that file. Credentials entered
only inside the retained CLI are not inherited by the separate runner.
Host keychain/credential-helper state is not exported; supply a store usable by
the Linux CLI. Keep it under the selected installation's deployment-local
`secret-input/` directory, or outside captured installation state, so Clone does
not transfer registry credentials. Keep the directory owner-only and
`config.json` mode `600`, outside committed files; never put registry tokens in
environment variables or arguments.

The installer directs loopback installations to the existing web first-admin
form. For a non-loopback hostname it writes a 60-minute setup token to an
owner-only file and prints that path. Open the complete browser link from the
framed `NEXT STEP: CREATE THE FIRST ADMINISTRATOR` block; it is printed on stderr
outside JSON output and has its own line for copying. Do not share the link.
If recovering the token from the owner-only file, use it only in
`#setup-token=<token>` on the `/bootstrap` URL; the frontend removes the
fragment immediately and submits it as `x-platform-setup-token`. If it expires
while zero users still exist, rerun `./install.sh`: an already-installed run
issues a replacement token and prints a working link. `./flow-steward users
issue-bootstrap-token` does the same on the Scalable profile, but a Compact
launcher supports `users create` and refuses the installer-owned
`users issue-bootstrap-token` command, so the installer is the portable answer.

A database created by a pre-release (pre-1.0) build cannot be upgraded: 1.0
starts from a clean `db/schema/` baseline with no historical migrations, and
`database upgrade` and the installer refuse such a database unchanged. Recreate
it with a new installation from a clean checkout and a distinct Compose project
identity, then run the Step 1 installer and status/doctor checks above. Initial
schema and seed setup remain installer-owned. Pre-1.0 data is not carried over;
see [ADR 0012](architecture/adr/0012-post-1.0-sql-migrations.md) for the baseline
policy.

`install.sh` bootstraps its Python installer inside Docker, so a host Python
runtime is not required for the normal setup path. It is a one-time
install/resume entrypoint and has no public subcommands. Set
`FS_INSTALLER_USE_HOST_PYTHON=1` only for local development fallback.
Recoverable non-signal failures retain verified journal checkpoints. If an
initial install receives `SIGINT` or `SIGTERM`, the installer removes only that
operation's Compose runtime and volumes and clears its journal. The next
`./install.sh` starts a new operation from the beginning; cached images may be
reused, while external S3 data and user-provided TLS material are preserved.

Production uses PostgreSQL with Apache AGE, pgvector, and RabbitMQ. The guided
installer supports either bundled self-hosted storage or external
S3-compatible storage. Production does not include the test runner, audit
tools, Sonar, backend reload, or Vite HMR.

The Docker-hosted guided UI can read S3 credentials and provided TLS files
placed under the repository, such as `.flow-steward/import/`. For an input file
elsewhere on the host, supply its absolute path on the original `./install.sh`
command line so the launcher can mount that one file read-only. Relative paths
may not escape the repository, and generated TLS outputs must remain inside it.

A pre-Phase3 stack with configuration or labeled Docker resources but no
`.flow-steward/install-state.json` is preserved and refused as ambiguous;
Phase3 never guesses its environment or storage selection. Continue managing
it with `./flow-steward`. For a new installation use a clean checkout and a
distinct Compose project identity. Never remove existing volumes as a recovery
shortcut.

With `--tls-mode reverse_proxy`, the installer leaves TLS termination to the
existing host proxy, skips bundled Traefik, and binds `fs-app` only to
`127.0.0.1:18099` by default. Configure the proxy to use that private HTTP
backend; change it with `--reverse-proxy-backend-port` when needed. The
installer rejects non-loopback backend hosts.

For local development, run `./install.sh --environment development`.
Development is a complete preset: core plus frontend and continuously ready
test/audit runtimes; Mailpit and SonarQube/scanner are available on demand.
Choose bundled or external S3 storage independently of the environment.

Core database initialization does not install extension-specific records.
Tools contributed by an installed extension are registered in the normal Tools
registry. Open such a tool and choose the
**Actions** tab to see every function, accepted input parameter, and returned
field. The workflow builder uses those same action contracts for connector Smart
Setup: mapped parameters and returned fields are selectable rows, duplicate
choices are disabled, and removing a row makes it available again through
`+ Add`. For connector input sources, use **State path** for values such as
`state.catalog_rows` and **Literal value** for fixed strings or numbers.

An installed extension is not necessarily connected to an external account.
Actions that require a connection remain visible as **Connection required** until
the selected project has a compatible account. In the action editor, choose
**Set up connection** to open that extension in a new tab, follow its setup and
connection pages, then return and choose **Refresh connections**. Your workflow
draft stays open. Core does not create provider-specific connections; each
extension owns its setup and authorization. Actions that require no connection
remain usable without this step.

In the Data Connections registry, saving metadata such as a description preserves
the enabled state of the connection opened in the editor. It does not toggle,
test or introspect the connection. Closing that editor and choosing **Add
connection** starts a new connection with the existing enabled-by-default
setting, independent of the previously edited connection.

For low-level maintenance only, define these helpers once in your shell so every
command targets the canonical `flow-steward` compose project instead of creating
a second default `docker` stack. Installation/resume uses `./install.sh`;
post-install readiness uses `./flow-steward status` and `./flow-steward doctor`.

```bash
fs-compose() { docker compose -p flow-steward -f infra/docker/docker-compose.yaml "$@"; }
fs-compose-dev() { docker compose -p flow-steward -f infra/docker/docker-compose.yaml -f infra/docker/docker-compose.dev.yaml "$@"; }
```

### Memory Backend Profiles

Postgres is the source of truth for canonical memory, vector search, and graph
search. The base Compose stack builds the canonical Postgres image with Apache
AGE + pgvector by default; an official install or release upgrade pulls the same
image, published as `flow-steward-postgres`, by digest instead.

Use the guided `./install.sh` flow to select the supported first-release stack.
Production uses the unified Postgres profile with Apache AGE, pgvector,
pg_trgm, and unaccent.

Examples live in `.env.example.memory`. Use only the `FS_*` memory
variables from that file. Supported release values are `postgres_unified`,
Postgres text search (`postgres_fts` or `postgres_trgm`), `postgres_pgvector`,
and Postgres AGE graph/context edges. Unsupported values or retired pre-FS
memory env names fail startup.

### Frontend Runtime Mode

Flow Steward serves product routes through the React shell by default (SPA mode):

- public product routes are React-owned
- `/app/*` remains a valid React alias namespace
- removed routes such as `/events`, `/account`, `/account-settings`,
  `/users`, `/processes`, `/workflows`, `/datasets`, and `/system/monitoring`
  are not supported release routes
- project, job, registry, settings, security, and monitoring pages use their
  canonical React URLs only; mutations stay under canonical `/api/*` routes
- `/registry/changelog` and `/bootstrap` are React-owned routes

Viewers cannot read registry entity changelog history. Existing account/project
scope checks and machine-token scopes still apply; restore remains admin-only.
Changelog list, history, revision, diff and restored snapshot responses redact
credential fields and credential-shaped text without changing stored snapshots.
Workflow history additionally retains its specialized secret-authority and
protected-internal projection. Each workflow entry records the stored workflow
after the write with its actual `revision`; a workflow revision number is
looked up by that stored number, never by list position. Both workflow restore
routes (`/api/workflows/{id}/restore` and `/api/changelog/workflow/{id}/restore`)
use the workflow definition service and the Apply blocking policy; see
[Workflow History](user/workflows.md#workflow-history).
Reading a selected Workflow revision checks the current Workflow's access scope
first. Deleted content history retains its scope-checked fallback when its
Workflow identity matches the requested Workflow; an identity-free lifecycle
marker cannot authorize a revision read on its own.
Guardrail Trash and Restore events retain the authenticated account and actor
metadata, so they remain visible in that account's history. Lifecycle markers
are audit events, not restorable content revisions.
History responses label these events separately from content revisions. A diff
that includes a lifecycle event is not a content comparison: it preserves the
selected entry IDs without reporting content removals or zero-change counts.
Changelog screens display the event and do not offer revision restore for it.
Delete events also use lifecycle presentation, even when their audit record
retains the full pre-delete snapshot. Their presence in either side of a diff
does not report content changes. This display rule does not change existing
backend replay eligibility for a full Delete snapshot or its access checks;
compact lifecycle markers remain non-restorable.
Guardrail history access uses the current account-owned entity and its project
scope, never the historical snapshot. Missing or still-trashed entities remain
unavailable through the generic changelog routes.
Restoring a selected Workflow root through a lifecycle plan records its Restore
event in the same transaction. An audit write failure rolls back the restoration.
Dataset assignment changes made through a lifecycle plan record the acting
user as their history author.

Frontend workspace verification:

```bash
cd assets/frontend
npm ci
npm run lint
npm run test
npm run build
```

Build React assets from `assets/frontend/` with `npm run build`. This does not
enable backend reload or Vite HMR.

Hot reload is opt-in only. When the local-development env explicitly sets
`FS_AUTO_RELOAD=1`, `fs-app` uses `uvicorn --reload` for backend code changes.
The installer still starts the selected runtime services only. Start Vite HMR
explicitly when you need browser-side hot reload:

```bash
fs-compose up -d fs-frontend
```

Use `fs-compose logs -f fs-app fs-frontend` to watch both development processes together.

Services started by Docker Compose:

| Service | Port | UI | Purpose |
|---|---|---|---|
| `fs-app` | 8099 | http://localhost:8099 | Web UI + REST API + webhook receiver |
| `fs-mcp` | — (TLS `/mcp` via Traefik) | — | Authenticated MCP server for external agents |
| `pg-fs` | 55432 -> 5432 | — | PostgreSQL — job state, logs, LangGraph checkpoints |
| `rabbitmq-fs` | 5673 -> 5672 (AMQP), 15673 -> 15672 (Management) | http://localhost:15673 | Task queue |
| `fs-worker` | — | — | Worker supervisor that runs one dedicated child worker per project |

Long-running core containers use Docker's `always` restart policy. After a host
reboot or Docker Engine/Desktop restart, an installed stack therefore recovers
without a manual `compose up`. On-demand migration, audit, test, and scanner
containers retain `restart: "no"` and run only when explicitly requested.

RabbitMQ management remains available when exposed by the local stack.

### Feature And Configuration Docs

The release documentation is split into user-facing feature pages and generated
configuration references:

- [Feature Catalog](user/feature-catalog.md) explains the main capabilities that
  users can enable, configure, or operate.
- [Workflows](user/workflows.md) covers steps, real groups, the execution graph,
  Approval and Wait steps, editing with change sets, contracts, test runs, the
  run inspector, incoming webhooks, artifacts, and notifications.
- [Workflow steps rollout](operations/workflow-steps-rollout.md) is the manual
  recreation runbook for installations with phase-based workflows, and the
  [compatibility matrix](operations/workflow-steps-compatibility.md) lists what
  extensions, the SDK, remote runtimes and catalogs need for this release.
- [Configuration Reference](user/configuration-reference.md) groups user/operator
  settings by feature area, including uploads, PII, artifacts,
  notifications, logs, retention, and watchdog controls.
- [Environment Catalog](configuration/flow-steward-env-catalog.md) is the
  generated inventory of documented env variables from `.env.example*` overlays
  and the canonical env manifest.

### Documentation Freshness

Use these as the current source of truth for day-to-day work:

- `README.md`
- `docs/GUIDE.md`
- `docs/user/*`
- `core/infrastructure/config/flow_steward_env_manifest.json`

If a dated architecture or testing note conflicts with the docs above or with current code, treat it as stale and do not rely on it for release setup or runtime behavior.

### Curated API/OpenAPI Documentation

FastAPI's default `/openapi.json`, `/docs`, and `/redoc` endpoints are
intentionally disabled. Enable only the curated schema/docs slice you need:

**All four surfaces below are disabled by default** — every one of the flags
defaults to `0`, so on a stock deployment `/openapi-public.json`, `/docs/api`,
`/openapi-internal.json` and `/docs/internal` all return `404`. Set the flags
explicitly to get a machine-readable API contract.

| Surface | Schema | Swagger UI | Main flags (default `0`) |
|---|---|---|---|
| Public integration API | `/openapi-public.json` | `/docs/api` | `FS_OPENAPI_PUBLIC_ENABLED=1`, `FS_DOCS_PUBLIC_ENABLED=1` |
| Internal app API | `/openapi-internal.json` | `/docs/internal` | `FS_OPENAPI_INTERNAL_ENABLED=1`, `FS_DOCS_INTERNAL_ENABLED=1` |

Both surfaces require authentication by default through
`FS_OPENAPI_PUBLIC_REQUIRE_AUTH=1` and `FS_OPENAPI_INTERNAL_REQUIRE_AUTH=1`.
Set either requirement to `0` only for trusted local documentation review.

The public schema is the curated external integration contract with this allowlist:

- `POST /api/workflow-runs`
- `GET /api/jobs`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/approval`
- `GET /api/events/stream?job_id={job_id}`
- `POST /api/jobs/{job_id}/cancel`
- `POST /api/approvals/{thread_id}`
- `GET /api/projects/{project_id}/workflow-catalog`
- `GET /api/projects/{project_id}/workflow-entrypoints`
- `GET /api/projects/{project_id}/workflows/{workflow_id}/contract`

The internal schema covers authenticated `/api/*` JSON routes for the app UI and
operator tooling. It excludes health, metrics, public auth routes, and raw
webhook/callback ingress.

### Public workflow integration contract

External callers start a concrete workflow with JSON `POST /api/workflow-runs`.
Both `project_id` and `workflow_id` are required; workflow values belong in
`inputs`. Inputs and execution-only `client_context` are each bounded to 32
nesting levels and 256 KiB of serialized JSON. Send an `Idempotency-Key` on
every retryable launch. The first accepted response has `deduplicated=false`;
a replay returns the same `job_id` with `deduplicated=true`. The caller must
have workflow-write authority and the workflow must allow its MCP launch channel.

A linked durable job keeps the accepted replay available even beyond the initial
result window. If that replay is no longer available while its bounded tombstone
is retained, the same retry returns `410` with code `idempotency_result_expired`
rather than silently starting a replacement run. This is not a permanent
reservation of the key.

The endpoint does not accept generic task summaries, arbitrary runtime state,
caller-selected provenance, or multipart files. Existing configured workflow
webhooks retain their separate file-ingress contract and upload safeguards.

The job detail response contains the durable `workflow_run` projection. It
names the run position with `current_step_id` (null while a parallel group
runs), `current_group_id` and `active_steps` (`step_id`, `step_execution_id`,
`attempt`, `group_id`, `for_each_id`, `lane_id`, `iteration`, `status`); there is no
`current_phase` and no positional `current_step`. For
realtime delivery, connect to `/api/events/stream?job_id=...`; the stream emits
versioned `job.state` snapshots, SSE keepalive comments, and retry metadata.
Use a snapshot's marker id as `Last-Event-ID`. For the initial connection, omit
both `cursor` and `Last-Event-ID` to receive the current snapshot. After an
ordinary disconnect while the job is nonterminal, reconnect with the newest
received marker id.

If a supplied cursor is unknown or expired, the stream returns `410`. Reconcile
once with `GET /api/jobs/{job_id}` and inspect `workflow_run.terminal`. If it is
true, stop reconnecting. Otherwise, reopen the job stream without the stale
`cursor` or `Last-Event-ID`, then retain the new marker for later reconnects.

When a received `job.state` snapshot has `terminal: true`, close the client
stream and stop reconnecting. A reconnect with a valid cursor to an already
terminal job still receives its final snapshot once, then the server closes the
stream; do not treat that close as a reason to reconnect. Projection or
multi-approval conflicts are returned as JSON before SSE headers are sent.

After SSE headers have been sent, a projection or read failure instead emits a
safe `stream.error` control event with code `stream_unavailable` and
`retryable: true`, then closes the stream. This event is not `job.state`, carries
no replacement state marker, and does not mean the workflow failed or completed.
The durable job detail and `job.state` snapshots remain the authority for the
workflow's outcome.

`FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_PRINCIPAL` (default `10`) and
`FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_ACCOUNT` (default `100`) bound concurrent
public job streams in each application process. They are local guards, not
cluster-wide quotas: deploy a shared connection limiter or use load-balancer
policy when a global account limit is required.

Machine/service-token approval calls use JSON only and must echo the current
`approval_id`, include an `Idempotency-Key`, and provide an explicit approve or
reject action (or `abort` where `allowed_actions` offers it). Rejects require
a non-empty comment. Optional `external_actor`
is delegated audit context only; the authenticated token remains the authority
for account, project, workflow, and write-scope checks. A `202` means the
decision and recoverable resume intent are durable, not that execution has
finished.

The opt-in four-stage live contract test is
`tests/e2e/test_public_workflow_integration.py`. Run it from Docker only after
provisioning a workflow that reaches one approval gate and setting
`FS_PUBLIC_WORKFLOW_INTEGRATION_E2E=1`, `E2E_MACHINE_TOKEN`,
`E2E_PUBLIC_WORKFLOW_PROJECT_ID`, `E2E_PUBLIC_WORKFLOW_ID`, and
`E2E_PUBLIC_WORKFLOW_STEP_ID`. The final value is the configured workflow step
ID asserted in the public SSE projection.
The configured workflow must allow the MCP trigger channel and accept declared
JSON `title` and `description` inputs; this fixture no longer uploads a task
attachment. It must reach its configured human approval gate and publish the
expected output artifact after approval.

Release evidence is produced by the local Docker gates, with audit approval
before SonarQube and Tier 3. GitHub Actions are disabled. When this live fixture
is configured, the candidate deployment must expose its immutable image revision
as `FS_BUILD_SHA`, matching the final tested commit SHA, so a stale deployment
cannot satisfy the gate.
`/health` must also return a non-empty image digest; blank `build_sha` or
`image_digest` values mean the deployment is not attributable and must not be
approved as a release candidate.

## Step 2 — Create a Project in the UI

The platform interface supports English and Lithuanian. Choose the language in
**Settings → Account → Profile → Interface language** and save the profile.
Unsupported profile or language-cookie values are skipped when resolving the
interface language. The next supported preference is used, with English as the
default when none is available.

Project registration is UI-first. A project groups the workflows you link to it, its data connections, and its model defaults.

Project names must contain a non-whitespace character. Creation and explicit name
updates reject empty or whitespace-only names with HTTP 422 before any Project
data, linked resources, or revision history is changed. Omitting `name` from a
partial API update preserves the existing name; valid names are trimmed.

1. Open http://localhost:8099
2. Click **Projects → New Project**
3. Enter **Name** and choose **Workspace**. A single available workspace is shown as context.
4. Create the project. It starts without linked workflows or data connections.

Configure the remaining settings inside the project after creation. The project
page has one row of tabs and every section is one click away: **Overview**
(name, description, workspace and the Project summary), **Workflows**, **Data
Connections**, **Policy**, **Models**, **Notifications**, **Runtime
Connections**, **Datasets** and **History** (settings revisions with their diffs
and restore). A link can open a tab directly with `?tab=`, for example
`/projects/<id>?tab=history`. Roles without the right to change projects see the
same tabs read-only, with no **Save Project**; the server enforces the same rule.
**Runtime Connections** is a read-only list: an extension creates its own
connections, so each row links to the extension's page, where connecting,
changing and removing happen.
**Notifications** is a read-only overview of the project's automatic
notifications; see [Automatic notifications](#automatic-notifications).


| Field | Description | Example |
|---|---|---|
| **Name** | Display name | `Customer Support` |
| **Description** | Free-text summary | `Ticket triage and weekly reports` |
| **Workspace** | Workspace the project belongs to | `Default` |
| **Linked workflows** | Workflows the project can run; one can be the operator default | `invoice-import` |
| **Data connections** | Project-scoped connections to external data sources | A PostgreSQL connection |
| **Project Rules** | Free-text rules injected into every agent's prompt | See below |
| **Default Provider** | LLM provider for all agents | `openai`, `anthropic`, `google` |
| **Default Model** | Model name for all agents | `gpt-4o`, `claude-sonnet-4-20250514`, `gemini-2.0-flash` |
| **Agent overrides** | Override provider/model for specific agents | One agent on Claude, the rest on GPT-4o-mini |

**Project Rules** example:
```
- Answer customers in the language they wrote in
- Never promise a refund; route refund requests to the approval gate
- Summaries must list the ticket IDs they cover
```

These rules are automatically included in the context block sent to every agent step the project runs.

4. Click **Save**. The project now appears on the Projects list.

Project job counts and the `has_jobs` filter include only Jobs outside Trash,
matching the default Jobs list and project detail. Trashed Jobs do not make an
otherwise empty project match `has_jobs=yes`.

## Step 3 — Run a Workflow

For a natural-language request, call `project_list` when the project is unknown,
then `workflow_list(project_name="Customer Support")` for that concrete project.
Compare the request with the accessible workflows' names and descriptions. Ask
the user to choose when several workflows match; ask for clarification when none
matches. The server does not perform semantic routing.

Read `workflow_contract(workflow_id="weekly-ticket-summary", project_name="Customer Support")`
and use `workflow_validate` in the same project to inspect readiness. Collect every
required input from the user before calling
`workflow_trigger(project_name="Customer Support", workflow_id="weekly-ticket-summary", payload={...})`.
Do not substitute a generated task summary for a missing workflow input or create
an empty job while waiting for the user's answer. The server validates inputs
before queue admission. Direct launch requires the workflow's MCP trigger channel.

If the contract requires a `file`, ask for the actual file before launching.
Pass `files=[{"input_name": "source_file", "filename": "inventory.csv",
"mime_type": "text/csv", "content_base64": "c2t1LHF1YW50aXR5CkExLDcK"}]`
to `workflow_trigger`; keep file fields out of `payload`. The example carries
actual CSV bytes (`sku,quantity` and `A1,7`), not a server path or an artifact ID.
The transport accepts up to 10 files and 1 MiB of decoded bytes in total;
configured attachment type/size limits also apply. Base64 must be strict, with
no whitespace, and each declared file input has exactly one upload. The server
validates every required input before creating a job and persists attachments
before dispatch. The workflow receives server-created artifact descriptors in
its named inputs and `files`; raw base64 is not retained in job metadata.
Filenames, URLs, local paths and fabricated artifact objects in `payload` do
not satisfy a file input and are rejected before enqueue.

External CRM/ERP systems, forms, and event providers call the public
`/webhook/workflows/{entry_key}` with the configured webhook credentials and
transport. MCP agents launch workflows directly by project/workflow identity.
A published webhook is not needed for direct launch.

An operator can start the same run from the deployment host:

```bash
./flow-steward workflows run --project-id 7d01e946-88e4-4ce0-aac6-35b36d906bd8 \
  weekly-ticket-summary --inputs '{"week": "2026-W40"}' \
  --idempotency-key weekly-ticket-summary-001
```

The direct workflow launch REST contract is JSON:

```http
POST /api/workflow-runs
Authorization: Bearer <FLOW_STEWARD_TOKEN>
Idempotency-Key: weekly-ticket-summary-001
Content-Type: application/json

{
  "project_id": "7d01e946-88e4-4ce0-aac6-35b36d906bd8",
  "workflow_id": "weekly-ticket-summary",
  "inputs": {"week": "2026-W40"}
}
```

Both identifiers are required. The caller needs `workflows.write` (or an
accepted broader workflow scope), access to the account/project/workflow, and
valid declared inputs. The endpoint does not accept a free-form task summary,
caller-selected launch channel, arbitrary runtime state, or multipart files.
For file inputs, supply the same `files` array described above in the JSON body.
External providers retain the existing configured webhook ingress for file-bearing events.

The returned `job_id` identifies the asynchronous run. Poll `job_status`;
when approval is required, fetch `job_approval`, ask the user for a decision,
and submit that decision with `job_approval_decide`. Starting a run is not
permission to approve a human gate automatically.

To configure scheduled launches as an administrator, open the workflow page
and its **Smart setup** tab. Set **Status** to **enabled**, enable **Workflow
interface**, select **Schedule** under **Trigger channels**, and click **Save
workflow settings**. The **Schedules** tab appears next to **Smart setup** as
soon as **Schedule** is ticked (the **Webhook** tab does the same for
**Incoming Webhook**). These launch settings must be saved before the schedule
editor can create or update a schedule.

The workflow page's tabs are **Overview**, **Projects**, **Hooks** (only when the
workflow has hooks), **Smart setup**, **Webhook**, **Schedules**, **Automation**
and **History**; `?tab=` in the address selects one.

As an administrator, open **Schedules** on the workflow page or in the Workflow
builder's Workflow settings to create and edit schedules. Choose a named linked project when a
workflow has multiple links; a sole linked project is selected automatically.
Saved rows, paused ones included, appear in the Workflow's schedule list for that
project.
Enable and save the Workflow's Schedule trigger before creating schedules.
Selecting that trigger or saving ordinary workflow settings does not create or
enable a schedule; new schedules are paused unless explicitly enabled.

Schedules provide values for the workflow's saved input contract; define those
fields in Workflow settings. Fixed defaults are applied automatically and are
not caller-supplied schedule values. A schedule cannot start another run while
its previous run is active; other schedules remain independent. Missed intervals
are not replayed as a backlog. There are no schedule-specific instructions or
configurable concurrency/catchup controls.

See [ADR 0032](architecture/adr/0032-workflow-launch.md) for the shared launch boundary.

**What happens behind the scenes:**
1. A job row is created in PostgreSQL with status `pending`
2. In the same transaction, an encrypted dispatch envelope is written to the
   durable `job_dispatch_outbox` table — this is the hand-off point between the
   API and the messaging layer, and the first place to look when a submission
   returns `500 Failed to enqueue task`
3. A lightweight ready signal goes to the shared RabbitMQ ready queue
   (`platform.projects.ready`), and the per-project queue
   (`platform.project.{project_id}`) carries the job itself
4. The `fs-worker` supervisor consumes the ready queue and routes the job to the
   dedicated worker for that project
5. The workflow runtime runs to completion, or parks the run without a worker
   while it waits for an approval, a Wait timer or a child workflow
6. Log entries stream to the web UI in real time

---

## Step 4 — Workflow Runtime

A workflow is a set of **steps** connected by one **execution graph**: an entry
and edges from a step's outcome (`success`, `true`/`false`, a switch `case`,
`default`, `approved`/`rejected`, `error`) to the next step. Loops and parallel
lanes are real **groups**; there are no phases. The workflow you pick decides
what a run does — there is no fixed pipeline. Step kinds:

| Step kind | What it does |
|---|---|
| `agent` | Runs an LLM agent with the project's rules, model defaults, and permitted tools |
| `connector` | Calls an extension or built-in connector action with mapped inputs and outputs |
| `http_request` | Calls an HTTP endpoint directly |
| `dataset_query` | Reads rows from a dataset or data connection |
| `transform` | Reshapes workflow state deterministically |
| `tabular_feed` | Processes CSV, TSV, JSON, NDJSON, Parquet, or XLSX files without loading full rows into state |
| `template` | Builds one text from declared inputs |
| `invoke_workflow` | Runs another workflow as a child |
| `if` / `switch` | Chooses the outcome edge to follow based on workflow state |
| `approval` | Waits for a person to approve or reject |
| `wait` | Postpones the run for a duration |
| `batch` | Splits an array into batches |
| `collect` / `break` / `continue` | Adds a value to an existing variable, ends the For each, or ends the current item (For each body only) |
| `set_variable` | Writes a value into workflow state |
| `stop_success` / `stop_fail` | Ends the run with an explicit outcome |

A Group runs its body once, as a sequence or as parallel lanes that then join;
a For each runs its body of ordinary steps once per item of a ready collection,
one item at a time. An Invoke can be a hook that each project connects to its
own workflow. Every run executes
the definition revision it pinned before its first step, so editing the
workflow never changes a run in progress. While a run waits for an approval
(`awaiting_approval`), a Wait timer (`awaiting_timer`) or a child workflow
called serially (`awaiting_child`), it holds no worker; the control process
wakes due timers and parents of finished children every second.

Around the steps, the runtime loads related project memory before agents run,
records every step in the job's runtime journal, and sends configured
notifications when the run finishes. Use **Test Run** in the workflow builder to
exercise a workflow before linking it to a project. Builder edits to steps,
groups and edges stay local until **Apply**, which saves them as one revision
through `POST /api/workflows/{workflow_id}/changes`; see
[Workflows](user/workflows.md) for the definition rules, Approval and Wait
steps, error policies and the run inspector.

- **UI/browser testing:** first-release production runtime does not run automatic browser-flow checks. Browser flows can be run explicitly from local/audit tooling when Playwright is installed.

### Workflow Automation

Administrators can open the workflow page's **Automation** tab.
For a group or step, select it in the builder and open its **Automation** tab.
Check the editor's **Scope** before choosing **+ Add Rule**, a trigger source,
an event, and an **Action**; enable the rule and click **Save rules**.

| Editor scope | Actions offered |
|---|---|
| Workflow | Resume job, Approve job, Reject job, Cancel job, Send notification |
| Group | Send notification |
| Step | Retry step, Send notification |

To ask a person after a failure, connect the step's existing **On error** port
to an ordinary **Approval** step. Its **Approved** and **Rejected** connections
decide what runs next, including a valid return path to the original step. This
uses the existing canvas and Approval editor, not another Automation setting.
See [Unknown operation outcomes](#when-an-external-operations-outcome-is-unknown)
for the exact retry boundary.

**Send notification → Human decision** selects a notification channel; it does
not create an Approval wait. Workflow Approve/Reject reactions decide an existing
gate where their event path permits it. **Save rules** saves those reaction
rules; it does not create the authored error/Approval connections.

These are the editor's reaction choices; execution also requires an event path
that supports the action. Built-in job completion, failure, and gate-waiting
events execute **Send notification** only. Saving **Approve job** for a built-in
gate-waiting event does not automatically approve that gate. Incoming polling
events support approval/resume actions; Cancel job, Retry step, and Send
notification are unavailable on that event path.

An approval/resume reaction needs the event's current `approval_id`, an
authorized scope, and a decision allowed by the current gate. A missing or stale
identity is not permission to resume. **Retry step**, where its event path
supports it, requires a saved retryable interrupted-step snapshot and an
eligible job state; selecting a step in the editor does not create that snapshot.

---

## Step 5 — Human Approval

A run pauses for a human decision at an **Approval step**, a **recovery
approval** under its applicable error handling, or an existing rework-budget or
autonomy pause. Host transport failures, including unknown outcomes, do not force an Approval after
technical retries stop: the effective error policy decides. Other validation,
guardrail and rework behavior is unchanged. When a gate opens, the job status
changes to `awaiting_approval` and the web UI shows an approval panel.

**In the web UI:**
- The panel shows the step and, for a step in a Group or a For each, that
  container, the content the step asked to show for review, and the actions the
  approval allows.
- At an Approval step, **Approve** follows the step's `approved` edge and
  **Reject** its `rejected` edge — usually back to rework with your feedback, or
  to `stop_fail`. **Reject is not terminal** unless the workflow routes it so;
  use **Cancel Job** (or the job cancel API/MCP action) to stop a job
  permanently. Rework alone does not repeat an unchanged logical external side
  effect; the directly bound Approval return described below can authorize one
  further attempt of an uncertain operation.
- At a recovery approval for a failed step, approve continues past the error
  on the step's success edge, and reject runs the same step again with your
  comment as feedback.
- When the run has used up its rework budget, the approval offers only
  **approve** (continue with additional budget) and **abort** (cancel the run).

When a child workflow waits for approval, the parent job stays
`awaiting_child`, links to the child job, and decisions are made on the child's
approval with the child's permissions. Parent workflow reactions to
`job.approval_waiting` also fire for it (see
[Notifications And Automation](user/workflows.md#notifications-and-automation)).

Review the current job before each decision. The UI submits the approval identity
recorded for that gate; if it is missing, refresh before deciding. A gate from an
earlier attempt cannot authorize the next gate. If a decision conflicts with the
current gate, reload the job and review its current gate before submitting a
new decision.

**Via the CLI** (on the deployment host):
```bash
# Job detail, including the pending approval's thread_id and approval_id
./flow-steward jobs show {job_id} --include-details

# Approve
./flow-steward jobs approve {thread_id} --approval-id {approval_id} --comment "Looks good"

# Reject (with a reason)
./flow-steward jobs reject {thread_id} --approval-id {approval_id} \
  --comment "The summary is missing last week's tickets"
```

**Via MCP:** read the gate with `job_approval(job_id)` and decide with
`job_approval_decide(thread_id, approval_id, approved, idempotency_key, comment, action)`.

**Via REST API:**
```http
# Current interrupt payload (what needs approval)
GET /api/jobs/{job_id}/approval
Authorization: Bearer {token}

# Durable job projection (includes workflow_run and approval when pending)
GET /api/jobs/{job_id}
Authorization: Bearer {token}

# Realtime durable state (send the last `id:` as Last-Event-ID when reconnecting)
GET /api/events/stream?job_id={job_id}
Authorization: Bearer {token}
Last-Event-ID: {last_marker_id}

# Approve
POST /api/approvals/{thread_id}
Authorization: Bearer {token}
Content-Type: application/json
Idempotency-Key: approval-{approval_id}

{"approval_id": "{approval_id}", "approved": true, "comment": "Looks good"}

# Reject (with a reason): same request with this body
{"approval_id": "{approval_id}", "approved": false, "comment": "The summary is missing last week's tickets"}
```

The `thread_id` is the LangGraph checkpoint ID, returned in the job's API response at `GET /api/jobs/{job_id}`.

Read the current approval's `allowed_actions` and always send the current
`approval_id` plus an explicit decision. An Approval step and a recovery
approval allow `approve` and `reject`; a pause for an exhausted rework budget
allows `approve` and `abort`, and a reject there is refused. A recognized action
can supply the decision by itself (`approve`/`resume` resolve true;
`reject`/`rework` resolve false). For ordinary approve/reject decisions, an
explicit `approved` value determines the outcome; the existing `override`
action still counts as approval. Retry authorization uses the same canonical
effective outcome as graph resume.
The authored Approval used by an error handler retains existing comment rules:
Approve may omit a comment; ordinary browser Reject requires feedback. Token
rejection retains its required reason, except for an allowed `abort`. Retry permission follows the chosen
graph outcome; it is not inferred from a comment or a factual no-effect assertion.
For a gate that allows `abort`, that action cancels the run
and needs an explicit `approved: false`; unlike a reject, it does not require a
comment from a machine token. For example:

```bash
./flow-steward jobs reject {thread_id} --approval-id {approval_id} --action abort \
  --comment "Stop here; the loop is not converging"
```

The REST equivalent is `POST /api/approvals/{thread_id}` with this body:

```json
{"approval_id": "{approval_id}", "approved": false, "action": "abort", "comment": "Stop here; the loop is not converging"}
```

For a child workflow's approval, use the child job's `thread_id` and
`approval_id` (`approval_job_id` and `approval_id` in a propagated
`job.approval_waiting` event), never the parent's.

### When an external operation's outcome is unknown

An unknown outcome does not force an Approval. The host keeps duplicate
protection and follows the configured error handling. Admissible technical
retries keep their existing count and deadline; an unknown mutation is never
automatically resent. Unhandled errors stop, an authored error connection runs
its handler, and continued errors use only real available data. An authored
Retry policy cannot reset a refused or spent host transport retry.

For a human choice, draw the existing **On error → Approval** connection and
connect the ordinary **Approved** and **Rejected** outcomes. A chosen direct
return to the original step can authorize one further attempt in the supported
case below. Approve may omit a comment; ordinary browser Reject requires feedback,
and token rejection keeps its existing required reason. Other outcomes simply follow their normal
connections; no new setting, dialog or user-entered system reference is
needed. A cycle must use the existing valid **rework (repeat) path** and its
budget. A directly marked rework edge may leave Rejected or Error, but not
Approved; an Approved-return cycle must put its rework marker on an eligible
other edge. Existing graph validation remains authoritative.

The exact one-use authorization applies to one ledger-declared serial connector
call whose own executed error connection goes directly to this Approval, and
whose chosen outcome immediately dispatches back to that original step. It
retains the same job, operation, input, installation, connection and loop item.
The host checks the current gate and its execution against the job's pinned
definition and original effect; users do not supply those references. If that
exact bound return is blocked because the existing graph budget is already
exhausted, the decision is refused and the
same ordinary Approval remains pending. No decision or retry permission is
recorded, the run does not resume, and the budget is not bypassed. Other
ordinary budget handling is unchanged. A changed or missing operation
declaration, changed request/connection/install, stale gate or lost provenance cannot consume the
grant or fall back to an ordinary provider call.

This is permission to retry an uncertain request, not proof that the first call
failed or had no effect. The decision, original failure provenance, rotated
fence and resume intent commit together; the exact connector claim consumes
one durable permission before calling the provider. Replayed decisions or
competing consumers cannot use it twice, and a stale original callback cannot
overwrite the newer attempt. A database fence cannot stop a remote request
already in flight; consider the duplicate risk before choosing to return.

A separate lookup may query the exact business correlation and map its real
returned ID downstream. It does not reconstruct the lost write response or
settle the original unknown effect. An absent result does not prove that a
delayed write stopped or authorize a resend. The directly bound retry does not
cover an indirect path through other nodes, several unknown effects, a parallel
lane or a changed container/loop item. Same-scope Group/For each cases require
preserved, verified source/frame context. Raw HTTP without a ledger retains
its own graph execution behavior; a return may send again, but it does not gain
these ledger-backed one-use guarantees.

No response, ID, artifact or dataset handle is invented. A host-confirmed
unknown producer's partial result is not promoted as canonical success output.
Independent real lookup data remains usable; a consumer missing a required ID
fails before its external call. Closing or leaving an Approval open records no
decision. Cancellation, stale-attempt, nontransport guardrail and autonomy rules
are unchanged.

The same ordinary pending gate is available in the browser and existing public
WebSocket job events, API, MCP and CLI paths. A voice or other MCP agent can use
`job_approvals_pending`, `job_status` and `job_approval`, ask the person for a
decision, then submit `job_approval_decide` with the current thread/gate and an
idempotency key. Read the gate's reason, content and allowed actions. Existing
token rules still require a current approval identity and idempotency key, and
a rejection reason except for an allowed `abort`; positive approval may omit
the comment. Viewers and read-only tokens cannot decide.
Private effect/source/checkpoint and retry associations are not caller inputs
or public decision fields.

The existing CLI `flow-steward jobs approve <thread_id> --approval-id
<approval_id>` and `jobs reject` use that same gate and normal review prompt;
`--comment` remains optional for this authored Approval. The default local CLI
uses the `flow-steward-cli` system actor, not the browser user's identity. Its
management read may omit checkpoint gate details; obtain the current thread
and approval identity through HTTP/MCP when absent from `jobs show`. Do not
invent an identity or create a replacement job. See
[Errors, Retry And Recovery](user/workflows.md#errors-retry-and-recovery).

### Unknown outcomes from direct extension actions

An action run from **Extensions** uses its existing result card for a factual
completed/not-completed decision; it does not create a job. The host associates
the initial failure with its known effect and the selected project/installation.
A scoped summary can restore that card after reload. The summary contains only
safe effect/version/operation/status and decision/retry availability; it cannot
restore provider outputs or reconstruct the submitted request.

Both decisions need a comment and operator access. Retry needs the exact retained
original request and a validated, unconsumed no-effect decision; clients must
reuse the original idempotency key. The server restores the persisted host
identity and checks the original intent and current fence. Reloading can lose
that in-memory request: the card
still shows the effect, but retry explains that the original request is missing.
Starting a fresh key or editing the request cannot bypass unresolved ambiguity.
An active retry or a decision awaiting consumption cannot be decided a second
time. A direct ambiguity without an existing matching project/install connection
binding is unavailable for this recovery UI; no new binding is inferred.

After a retry succeeds, the result card shows the actual result.

For API clients, the existing direct action endpoint remains
`POST /api/extensions/{extension_id}/action/{action_id}`. A host-owned
`action.response.external_effect` association exposes only `effect_id`,
`claim_version`, `status`, and `operation_id`; an extension cannot author those
host facts. Read current scoped state with
`GET /api/projects/{project_id}/extensions/{install_id}/external-effects`, then
record a decision with
`POST /api/projects/{project_id}/extensions/{install_id}/external-effects/{effect_id}/resolution`:

```json
{"observed_version": 3, "action": "reject", "comment": "Verified that no record was created"}
```

Use the returned `resolution_ref` only with the complete retained original action
body and its original `Idempotency-Key` for a retry. Never manufacture an effect
identity, replacement provider response, or new key to recover the call. The
server checks the full original request and scope again before consuming the
one-use permission. Read access uses `projects.read`; a decision needs
`projects.write` and operator authority through the normal authenticated
transport. See [ADR 0025](architecture/adr/0025-encrypted-external-effect-replay.md#operator-resolution-of-an-unknown-effect)
for the durable resolution contract.

These direct-action scoped read/decision routes are the existing HTTP transport
used by the Extensions result card. This change does not add dedicated MCP or
CLI direct-effect-resolution commands. Workflow Approval decisions remain on
the existing transports described above; do not create a replacement job to
recover a direct action.

---

## Step 6 — Optional Local/Audit UI Testing

Flow Steward keeps Playwright browser execution out of the production runtime
image. The YAML flow runner is available for explicit local/audit test workflows
and images that include Playwright browsers; it is not an automatic workflow
step in the first release.

### Running Browser Flows Explicitly

Use the audit/test environment for Playwright-based checks. The core runner
loads YAML flow files and can be wrapped as an optional LangChain tool by local
or audit code paths that deliberately include browser binaries.

| Field | Default | Description |
|---|---|---|
| `timeout_ms` | `30000` | Playwright step timeout in milliseconds |
| `headless` | `true` | Run browser headless (set to false for local debugging) |

Production app/worker/MCP containers do not install Playwright browsers. Use
`fs-audit`, host-side test tooling, or another explicit browser-capable
environment for these checks.

### Writing Flow Files

Create YAML files in `flows/registry/`. The LLM selects which flows to run based on the task description — flows are matched by name, description, and tags.

**Basic structure:**
```yaml
name: your-flow-name
description: What this flow verifies (used by LLM for selection)
tags:
  - auth
  - smoke

steps:
  - action: navigate
    url: /login
  - action: fill
    selector: "input[name='email']"
    value: "test@example.com"
  - action: click
    selector: "button[type='submit']"
  - action: assert_url
    pattern: /dashboard
```

**Tip:** Prefix example/reference flows with `example-` — the LLM will skip these for real tasks unless no other flows exist.

### All 12 Supported Actions

| Action | Required fields | Optional fields | What it does |
|---|---|---|---|
| `navigate` | `url` | — | Go to URL (relative urls use `dev_server_url` as base) |
| `click` | `selector` | `description` | Click element |
| `fill` | `selector`, `value` | `description` | Clear field and type value |
| `screenshot` | `name` | `description` | Save PNG to screenshot dir |
| `wait_for` | `selector` | `description` | Wait for element to become visible (uses step timeout) |
| `assert_visible` | `selector` | `message` | Fail if element is not visible |
| `assert_text` | `selector`, `expected` | `message` | Fail if element text doesn't contain `expected` |
| `assert_url` | `pattern` | `message` | Fail if current URL doesn't contain `pattern` |
| `select_option` | `selector`, `value` | `description` | Choose value from `<select>` element |
| `hover` | `selector` | `description` | Move mouse over element (triggers tooltips, hover states) |
| `press_key` | `key` | `description` | Press keyboard key (e.g. `Tab`, `Enter`, `Escape`) |
| `wait_network_idle` | — | `description` | Wait for all network requests to complete |

**Selector syntax:** Standard CSS selectors and Playwright's extended selectors both work:
```yaml
# CSS
selector: "input[type='email']"
selector: "#submit-btn"
selector: ".dashboard-header h1"

# Playwright text selector
selector: "button:has-text('Sign in')"

# Multiple fallbacks (tries left to right)
selector: "button[type='submit'], input[type='submit'], .submit-btn"
```

**On failure:** the explicit local/audit runner captures a failure screenshot
(`FAILURE-step{N}-{action}.png`) and stops the flow. Inspect the runner output
for the failure report and screenshot paths.

### Screenshots

Screenshots are saved in the screenshot directory passed to the explicit runner.
Production `fs-app` does not provide Playwright browser execution by default.

---

## Step 7 — Internal Memory

- Stores: task summaries, descriptions, lessons learned, project rules, and curated knowledge in Postgres
- Used by: the memory intake before agent steps and other memory consumers to find related previous tasks
- Agents receive this context with provenance and scope filtering applied before prompt assembly

Internal memory has no standalone project screen. It supports runtime context assembly and is not a user-managed project section.

## Step 8 — Flow Steward MCP Server

The `fs-mcp` container exposes Flow Steward project, workflow, job, report, and
notification tools over MCP. Orchestrator agents and external IDE tools
(IDE agents and other MCP clients) call the same endpoint, so tool schemas and
descriptions come from the live server rather than handwritten client docs.

The server uses MCP SDK v2. Streamable HTTP accepts stateless MCP `2026-07-28`
requests (including `server/discover`) and the SDK's supported legacy initialize
flow. Modern requests do not require `initialize` or `Mcp-Session-Id`. The only
inbound transports are Streamable HTTP at `/mcp` and local/container `stdio`;
the former inbound SSE transport is no longer supported.

**Endpoint:** `https://localhost:8099/mcp` for the default Compose port, or
`https://localhost:<APP_PORT>/mcp` when `APP_PORT` is customized. This is the
supported external endpoint; `http://fs-mcp:8100/mcp` is internal to the Compose
network and is not published on the host.

### Authentication

Flow Steward MCP HTTP auth is enabled by default in the normal stack. External MCP clients must send a bearer token on every request:

```text
Authorization: Bearer <token>
```

Accepted bearer tokens use the same resolution chain as Flow Steward auth:
- browser/session token (`fs_session`)
- personal access token (recommended for human-operated IDE clients)
- machine token / service-account token (recommended for unattended agents and automation)

Get a personal access token from the Account page:
- UI: `/settings/account`
- For workflow authoring, an administrator must explicitly select **Full administrator access (admin:all)** in the personal-token dialog. It is off by default and grants the full existing administrator scope, subject to account, project and workflow restrictions. Granular defaults and machine-token choices remain unchanged.

For unattended agents, create a service account token from the auth API:
- `POST /api/auth/service-accounts`
- `POST /api/auth/service-accounts/{service_account_key}/tokens`

For authenticated HTTP clients, `tools/list` is filtered for each request using
the token's effective scopes. `tools/call` independently enforces the same scope
policy and remains authoritative if a client has cached an older tool list.
PATs and machine/service-account tokens with no selected scopes see no tools and
cannot call them; interactive sessions without persisted token scopes derive
their effective scopes from the current user role. The shared PAT and machine
token selectors offer these categories: `jobs.read`, `jobs.write`,
`projects.read`, `projects.write`, `registry.read`, `registry.write`,
`workflows.read`, `workflows.write`, `reports.read`, `reports.write`,
`notify.read`, and `notify:dispatch`.

Every API read belongs to one of those families -- jobs (`/api/jobs`,
`/api/v2/jobs`, events, messages, workload), projects, registry -- or needs
`read:*` or `admin:all`. The administrative sections -- search, security,
system, settings and trash -- are therefore answered only to a signed-in
session and to tokens holding `read:*` or `admin:all`. Granular selectors do
not grant these scopes: a token using only those choices gets 403 on them by
design. Administrators can separately opt into `admin:all` when creating a
personal token; the shared machine-token selector has no such option. A machine token
with no selected scope never inherits its service account's authority; it
authenticates and every call it makes is refused. `projects.write` creates and
configures a project (its settings, workflows and policy); linking workflows
still requires the administrator role.

The supported installer provisions the packaged worker's dedicated
`flow-steward-worker` identity automatically after the canonical schema is
ready and before the runtime starts. It grants only `projects.read`, binds the
identity to the OSS account, and intentionally leaves `allowed_project_ids`
empty. An empty project allowlist means account-wide access, so projects created
after installation can be grounded without replacing the token. A non-empty
allowlist on any separately managed machine token remains restrictive.

The one-time worker token is written directly to the owner-only deployment env
as `FS_WORKER_MCP_BEARER_TOKEN`; it is not printed by the installer. Rerunning
the installer is idempotent while the credential remains valid. If the stored
credential is revoked, invalid, under-scoped, project-restricted, or belongs to
another account, the installer creates the replacement, atomically replaces the
secret, and then revokes the previous resolvable credential. The worker refuses
to consume jobs when the token is absent, revoked, invalid, or lacks
`projects.read`; never use `FS_MCP_AUTH_DISABLED` as a workaround.

Anonymous `/mcp` access should only be expected in the local dev override started with `infra/docker/docker-compose.dev.yaml`, which sets `FS_MCP_AUTH_DISABLED=1`.

> ⚠️ `FS_MCP_AUTH_DISABLED=1` is a full kill switch, not a relaxation: it
> short-circuits **every** MCP guard — bearer verification, role checks, token
> scope checks — and the caller is then treated as a `system_actor` on the
> default account. With it set, an unauthenticated caller can run mutating tools
> such as `workflow_trigger`, `job_cancel` and `report_generate`. Nothing in
> the product warns you: there is no startup warning and no readiness check for
> this, and the server answers `200` rather than `401`. The variable belongs only
> in the dev override — if it has leaked into your `.env`, every MCP tool on the
> published port is open. Verify with:
>
> ```bash
> docker exec flow-steward-fs-mcp-1 env | grep FS_MCP_AUTH_DISABLED
> ```

### Connect an external MCP client

Claude-style clients that read project-local MCP config can use a repo-local
`.mcp.json` (do not commit it):

```json
{
  "mcpServers": {
    "fs": {
      "type": "http",
      "url": "https://localhost:8099/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Once connected, the client can call any tool directly (for example,
`project_list()`, `project_jobs(...)`, or `workflow_list(...)`).

If your MCP client supports custom HTTP headers, configure it to send:

```text
Authorization: Bearer <token>
```

The exact header configuration key depends on the MCP client. The requirement is the HTTP header itself, not a product-specific wrapper.

### Developer Toolkit: local agent skills

The [Flow Steward agent plugin](agent-skills/flow-steward/README.md) packages
six skills that use current platform contracts:

| Skill | Audience and use |
| --- | --- |
| [Workflows and Operations](agent-skills/flow-steward/skills/flow-steward-workflow-authoring/SKILL.md) | End users: reuse local workflows, create disabled drafts, inspect scoped runs/errors/pending approvals, request operational reports, export and import workflows. |
| [Report an Issue](agent-skills/flow-steward/skills/flow-steward-report-issue/SKILL.md) | End users: report a bug or request a feature on GitHub with the System Diagnostics report attached. |
| [Publish a Workflow](agent-skills/flow-steward/skills/flow-steward-publish-workflow/SKILL.md) | Authors: publish a workflow template to the Flow-Steward/workflows catalog. |
| [Extension Authoring](agent-skills/flow-steward/skills/flow-steward-extension-authoring/SKILL.md) | Developers: build extensions (including custom code for workflows) with the supported CLI and target-release contracts. |
| [Publish an Extension](agent-skills/flow-steward/skills/flow-steward-publish-extension/SKILL.md) | Developers: release an extension and list it in the Flow-Steward/extensions catalog. |
| [Contribute Code](agent-skills/flow-steward/skills/flow-steward-contribute-code/SKILL.md) | Developers: fix a bug or propose an improvement in the core repository, from issue to pull request. |

The end-user skills work through the configured MCP and need no platform
checkout. Extension and core development use the supported CLI and the
Development environment.

#### Install in a local client

Follow the package README's supported Codex local marketplace, Cursor local
plugin or Claude session-local installation steps. Optional standalone skill
installation copies the same `skills/<name>` leaves into native skill folders.
The package works outside this repository and contains no endpoint or token.
Verify the skills in the client, then verify MCP `tools/list` and a scoped read.
Archive extraction or file copying alone does not establish runtime discovery.
Use the same reviewed package source for updates and verify the installed bytes.

#### Inspect operations and reports

Use `job_list(group_by="attention")` for a bounded triage snapshot, or normal
paginated `job_list` for a complete inventory. Grouped attention does not page;
normal responses provide `total_pages`. Exact project/workflow filters retain
the caller's authority. Read pending gates through `job_approvals_pending` and
inspect known jobs with `job_status`, `job_result`, `job_errors` and `job_approval`.
Permission failures remain errors. Approval/restart/cancel actions require the
user's requested action and the tool's existing authority.

`report_generate` supports operational `activity_daily`, `project_summary` and
`project_stats` with exact project/workflow and date/range filters; `report_get`
reads an accessible persisted report. `persist=false` still requires
`reports.write`. `range="7d"` is rolling 168 hours UTC; explicit
`start_date`/`end_date` cover inclusive UTC calendar days. Local "today" needs
the user's timezone and exact UTC boundaries; UTC-day aggregates cannot prove
a local-day total. `job_list` exposes no date arguments, so exact local intervals
require complete normal job pages and filtering actual timestamps. State the
window, coverage and any unperformed checks.

Project-scoped `workflows validate` and `workflows contract` CLI commands can inspect
a linked workflow disabled for authoring before enabling it. Project/workflow scope
and other availability restrictions still apply; these reads never enable a workflow.

#### Use the authoring tools

Connect the client to the existing authenticated MCP endpoint above. Consult
`tools/list` for its current arguments and token-filtered availability.

| Tool | Purpose |
|---|---|
| `workflow_examples_search` | Find permitted local workflows to reuse before creating one. |
| `capability_search` | Search permitted components and report knowledge freshness. |
| `capability_describe` | Inspect live operation contracts, permitted connections and exportable definitions; select `operation_id` when applicable. |
| `workflow_authoring_schema` | Obtain the existing bundle sample and persisted step configuration guidance, including Wait duration sources and Approval outcomes. |
| `workflow_draft_validate` | Check one bundle without writes and report validity, readiness and unperformed checks. |
| `workflow_draft_save` | Revalidate and create one disabled draft, returning its ID, issues and editor link. |
| `workflow_definition` | Read one workflow's steps, groups, execution graph and `revision` through `GET /api/workflows/{workflow_id}`. |
| `workflow_convert_group` | Preview typed group extraction, then atomically create the child and replace the parent group using the reviewed digest and revision. |
| `workflow_apply_changes` | Apply one atomic change set (`expected_revision`, `operations`) to the steps, groups and graph of an existing workflow, through `POST /api/workflows/{workflow_id}/changes`. |
| `workflow_hooks_list` / `workflow_hook_get` | Read the hooks a workflow declares and each one's binding in the project. |
| `workflow_hook_preview` | Run every binding check (child, mappings, invocation chain, binding revision) without saving. |
| `workflow_hook_bind` / `workflow_hook_unbind` | Connect a callable workflow to a hook in one project, or clear it; each save is the next binding revision (`expected_revision`). New runs use it; started runs keep the binding they pinned. |
| `workflow_hook_create_and_bind` | Create the empty workflow that holds a hook's steps and connect it once. |
| `workflow_where_used` | List the Invoke steps and hook bindings of the account that call a workflow. |
| `workflow_export` | Export a workflow linked to the project as its YAML bundle, returned as `bundle_yaml` text (`include_children` adds the workflows it invokes), through `GET /api/projects/{project_id}/workflows/{workflow_id}/export`. |
| `workflow_import` | Import a YAML bundle into the project as disabled drafts, with optional `target_workflow_id` and dependency `resolutions`, through `POST /api/projects/{project_id}/workflow-imports`; it needs the same authority as saving a draft. |
| `workflow_stats` | Read a workflow's run statistics through `GET /api/projects/{project_id}/workflows/{workflow_id}/stats`. |

To inspect a Hook, call `workflow_hooks_list(project_name, workflow_id)` with
the workflow that declares it. Select a returned `hook_id`, then call
`workflow_hook_get(project_name, workflow_id, hook_id)` with the same project
and parent workflow. Here `workflow_id` names the parent, not the bound target.
Get returns the Hook contract and stage information, current binding revision,
and input/output mappings; an empty Hook has `binding: null`.

The returned `binding.target_workflow_id` does not grant ordinary project
workflow read or where-used access to that target. An internal Hook storage
workflow, even if physically linked to the project, can return 404 from
`workflow_contract` or `workflow_where_used` because it is outside the caller's
ordinary visible workflow scope. These Hook reads return contract and binding
information, not the child's implementation graph. `workflow_hook_preview`
checks a proposed binding without saving it and requires its existing write
authority.

Read-only project-bound calls require applicable workflow read authority;
scoped tokens using `project_name` also need `projects.read` for resolution.
Saving requires the existing administrator role. A supported creator is an
admin user's scoped PAT with `admin:all`, subject to its existing account,
project and workflow restrictions. Machine tokens retain the operator role,
even with `admin:all`; launcher `workflows.write` alone cannot create a draft.
Tool visibility does not replace the API's authorization on each call.

Search and inspect suitable local examples first. If one meets the request,
reuse it without saving a duplicate. Otherwise retrieve live schemas and
contracts, compose exactly one explicit ID in `platform.workflow_bundle.v2`
(`steps`, `groups` and `execution_graph`; a phase-based v1 bundle is refused),
and validate and repair the bundle. Only `use_existing` and `map_existing`
dependency resolutions are supported. Save preserves a disabled draft;
readiness warnings remain visible and dependencies are not installed implicitly.
Keep the same ID through repairs and uncertain save retries. A
`workflow_id_conflict` requires inspecting the existing accessible workflow and
reporting the conflict, not automatic renaming or overwrite.
For a disabled draft, use existing
`workflow_list(project_name, enabled_only=false, include_validation=false)` or
the editor; `capability_describe` and project-scoped `workflow_contract` return
404 for disabled workflows under current policy.

Open the returned `editor_url` and inspect the saved steps, mappings and issues.
To change an existing workflow, call `workflow_apply_changes` with the
revision you last read as `expected_revision` (the `revision` that
`workflow_definition` returns, or that of the previous change-set response) and
the operations described in
[Editing A Workflow](user/workflows.md#editing-a-workflow). The
set becomes one new revision or nothing is saved: a stale revision is a `409`
conflict to reload, never an overwrite; an enabled workflow refuses a result
with blocking issues and keeps running its current revision; every agent, child
workflow, connection and extension install an operation names must be usable by
the workflow's account. It requires `admin:all`, like draft creation.

The authoring tools do not enable, execute, schedule or publish workflows. The builder
can test saved disabled drafts through Workflow or Step Test Run, subject to
readiness, project binding and caller permissions. Normal launches, including MCP
`workflow_trigger`, require the workflow to be enabled. Validation does not prove runtime execution or
business intent. Report operations actually executed or suppressed separately;
Test Run does not guarantee that every external mutation is suppressed.
Marketplace workflow search and acquisition are outside this authoring surface.

For extensions, follow the
[agent playbook](extension-authoring-agent-playbook.md) and the
[Development lifecycle](#post-start-extension-build-lifecycle). Use the public
Docker-backed launcher and current subcommand help. `extensions test` requires
its dedicated Development test runtime; a generic MCP/QA/admin client can
refuse. Do not bypass that requirement by setting `FS_EXTENSION_TEST_RUNTIME`.
Install an archive only when requested and authorized, then inspect it with the
existing `extensions list`, `show` and `health` commands. Skills do not publish
or activate an extension automatically.

### Available Tools

`tools/list` is the source of truth for the exact JSON Schema of each argument.
For authenticated HTTP clients it returns only tools permitted by the current
token scopes. The table below is the human-readable navigation map.

For a natural-language workflow request, discover the project with `project_list`,
list its workflows with `workflow_list(project_name)`, and inspect
`workflow_contract(workflow_id, project_name)`. Ask the user to select an ambiguous
workflow and provide missing required inputs. Only then call
`workflow_trigger(project_name, workflow_id, payload=..., files=...)`. For required
`file` inputs, collect the actual file and send its strict base64 bytes through
`files` with the declared `input_name`, filename and MIME type (10 files, 1 MiB
decoded total). Filenames, URLs and invented artifact handles are not uploads.
External webhook invocation belongs to CRM/ERP, forms, and other event providers.

| Area | Tool signature | Use |
|---|---|---|
| Projects | `project_list()` | List project names, IDs, and job counts. |
| Projects | `project_jobs(project_name, limit=10)` | List recent jobs for one project. |
| Workflows | `workflow_list(project_name, enabled_only=true, include_validation=false)` | List workflows linked to a project. |
| Workflows | `workflow_validate(workflow_id, project_name="")` | Validate workflow readiness. With `project_name`, validate in that project binding; without it, use the global workflow endpoint under the caller’s workflow access restrictions. `workflow_suggestions` follows the same rule; `workflow_contract` requires a concrete `project_name`. |
| Workflows | `workflow_contract(workflow_id, project_name)` | Return normalized public input/output contract and schemas. |
| Workflows | `workflow_suggestions(workflow_id, project_name="")` | Return repair suggestions for workflow issues. |
| Workflows | `workflow_trigger(project_name, workflow_id, payload=null, client_context=null, test_mode=false, files=null)` | Start the selected workflow after required inputs are collected; actual files use the bounded `files` upload array. Returns a job ID for status and human approval. |
| Jobs | `job_list(project_id="", workflow_id="", status="", group_by="", search="", page=1, per_page=25)` | Find scoped jobs without knowing job IDs; `group_by="attention"` groups jobs for triage. |
| Approvals | `job_approvals_pending()` | List jobs awaiting approval in the current scope. Queue failures return errors. |
| Jobs | `job_result(job_id, include_messages_limit=100, include_logs_limit=50)` | Return an agent-friendly status/result/findings/artifacts bundle. Both limits accept 1–2000; zero does not disable inclusion. |
| Jobs | `job_status(job_id)` | Return bounded polling progress, current step/group, durable wait and cost. Active steps are capped at 32 and scalar text at 512 characters; counts and truncation indicators are included. Use the dedicated result, approval, errors and trace tools for detail. |
| Jobs | `job_runtime_journal(job_id)` | Return runtime journal entries. |
| Jobs | `job_decision_overrides(job_id, active_only=false, domain="", limit=200)` | List deterministic decision overrides. |
| Jobs | `job_errors(job_id, limit=200)` | List structured job errors. |
| Jobs | `job_trace(job_id, limit=1000)` | Return execution trace rows. |
| Jobs | `job_incident(job_id, limit=100)` | Return incident drilldown data. |
| Jobs | `job_cost_breakdown(job_id)` | Return token/cost details. |
| Jobs | `job_events(job_id, since_id="", limit=200)` | List domain events, optionally incrementally. |
| Jobs | `job_messages(job_id, since_id="", before_id="", limit=200)` | List agent/runtime messages. |
| Jobs | `job_logs(job_id, since_id="", before_id="", newest=false, limit=25)` | List job logs. Use `last_id` with `since_id` for newer entries, or `next_before_id` with `before_id` for older history. Terminal jobs open at the newest window by default. |
| Approvals | `job_approval(job_id)` | Read the active approval gate payload. |
| Approvals | `job_approvals(job_id)` | List approval history. |
| Approvals | `job_approval_decide(thread_id, approval_id, approved, idempotency_key, comment="", action="")` | Approve/reject a paused workflow through the app approval API. `approval_id` identifies the current gate and `idempotency_key` makes retries safe. |
| Lifecycle | `job_cancel(job_id, comment="Cancelled via MCP")` | Cancel a pending/running job. |
| Lifecycle | `job_acknowledge(job_id, actor="")` | Mark a job as acknowledged. |
| Lifecycle | `job_unacknowledge(job_id)` | Remove acknowledgment. |
| Lifecycle | `job_restart(job_id)` | Re-enqueue a failed/cancelled job with the same inputs. |
| Reports | `report_generate(report_type, params=null, persist=true)` | Generate an operator report. `report_type` must be one of `job_summary`, `job_cost_breakdown`, `activity_daily`, `project_summary`, `project_stats`, `core_health_summary`; anything else returns `invalid_argument`. |
| Reports | `report_get(report_id)` | Fetch a persisted report. |
| Notifications | `project_notification_routes(project_name, enabled_only=false)` | List project notification routes. |
| Notifications | `project_notification_deliveries(project_name, job_id="", limit=200)` | List notification delivery records. |
| Notifications | `dispatch_project_notification(project_name, event_type, severity="info", workflow_id="", job_id="", payload=null)` | Dispatch a project notification event. |

For `activity_daily`, `project_summary`, and `project_stats`, `params.workflow_id` selects an exact workflow identifier alongside optional `project_id`, `start_date`/`end_date`, or `range` (default `7d`). Omitted filters retain the caller’s current scope; an unknown workflow returns an empty filtered report.

### Connect from the Terminal (stdio)

```bash
# stdio transport (for debugging or scripting)
docker-compose exec -i fs-mcp python core/entrypoints/mcp/server.py --transport stdio
```

### Test the MCP server directly

Connect any MCP client to `https://localhost:8099/mcp` with the bearer token,
open its tool list (`tools/list`), and call `project_list` with `{}` as its
arguments. A client you write yourself sends each request as a `POST` to `/mcp`:

```http
POST /mcp
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: project_list

{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"project_list","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
```

For `tools/list`, set `Mcp-Method: tools/list`, drop `Mcp-Name`, and send
`"method":"tools/list"` with only the `_meta` params. A local stand uses the
self-signed certificate in `.ssl/cert.pem`; trust it in the client.

The installer and management CLI default to plain, line-oriented interaction.
Enter an answer or a numbered choice, then press Enter. To opt in to existing
full-screen forms and rich output, use `./install.sh --tui` or add `--tui` to a
management command, for example `./flow-steward users create --tui`.
`--plain` overrides `--tui`; `--json` and `--non-interactive` never open a form.
Doctor diagnostics remain line-oriented. These flags change presentation only,
not validation, required confirmation, or the operation being performed.

A fresh interactive install asks for **Public host (domain or IP)** and
**Public port**. Enter the public browser domain/IP, or keep `localhost` for
local use without a domain. Choose `443` for standard HTTPS. These answers
generate `FS_PUBLIC_BASE_URL` before the first start; there is no separate raw
environment-variable question. Configure DNS and any external reverse proxy
to route that address to the server.

Commands that can change state show an interactive review prompt on a capable
TTY. For automation paths that cannot prompt, pass the leaf-only `--confirm`
option after the command path.

---

## Step 9 — Monitoring & Logs

Quick links to enabled service UIs are in the Flow Steward nav bar.

### Runtime heartbeat status

The admin Monitoring page reads the last committed runtime observations from
PostgreSQL through `/api/system/monitoring/summary`. Compact and Scalable use
the same scoped RabbitMQ heartbeat transport; the API does not discover Docker
containers or consume broker messages on request. Service names are reported
by their runtime owners, not maintained in a monitoring catalog.

Reports normally arrive every 60 seconds and become stale after 180 seconds.
The page polls every 20 seconds and supports manual refresh. Last report,
receipt and original check timestamps remain visible: publishing a new report
does not refresh an expired cached check. Lifecycle, readiness and evidenced
health are distinct; an idle registered consumer can be ready without a job,
and process liveness alone does not prove health.

Services separates fresh process observations, stale or retired process
observations, dependency and role probes, and monitoring channels. Every retained
observation remains available with its identity, source and timestamps. A stale
heartbeat means no fresh observation, not proof that the process has stopped.
The process totals include retained incarnations; fresh and stale/unavailable
counts are shown separately and channels are not additional process replicas.

If no fresh process health check is reported, Overview displays `Not measured`
instead of a numeric healthy count. A measured zero requires fresh explicit
health evidence; readiness and heartbeat alone do not supply it. Services shows
readiness and health separately, preserving stale cached results without
promoting them to current health.

Roles, observed process instances and dependencies have separate counts.
Dependency probes and the heartbeat receiver are not process replicas. Missing,
stale, failed and unobserved facts are distinct; invalid or unavailable data
renders unknown instead of a fabricated zero or healthy state. A broker outage
does not prevent reading the last stored report, and the receiver's channel
status is separate from reported service status. A produced stale/unhealthy
report returns HTTP 200; HTTP 503 means the stored report cannot be produced.

Each process incarnation has its own identity. Restarted processes can leave
older incarnation records until they expire or are retained out; this feature
does not provide authoritative fixed-slot replacement. Exact replica
completeness is unknown unless every expected role has a known desired count.
Persist the installation identity across restarts; a clone needs its own scope.
Optional delta history is off by default. Current rows and history have
independent seven-day retention, even when history is disabled. Supported
settings are listed in [Configuration Reference](user/configuration-reference.md#runtime-heartbeat-monitoring).

### Job Log Page

http://localhost:8099/jobs/{job_id}

- Live log stream — refreshes automatically
- Shows: agent reasoning, tool calls, step outputs, approval states
- **Run overview** — the executed definition (*Executed definition: revision
  … · digest*, with a link to the current workflow), or *Definition snapshot
  unavailable* for a run that pinned none; while the run waits, the Wait step's
  due time or a link to the child job it waits for; and the active lanes of a
  parallel group with their step and status
- **💰 Cost summary** — token count and estimated USD cost displayed in the header once LLM calls start
- **Story checklist** — live per-story status panel (pending / in_progress / completed / failed)
- **Cancel job** — available to administrators and operators for `pending`,
  `in_progress`, `running`, `resume_pending`, and `awaiting_approval` jobs
- **Restart job** — available to administrators and operators after the job reaches a terminal status

Open **Jobs**, select a job, then select a step in **Execution waterfall** to
open the **Step details** drawer (the step inspector). It shows recorded inputs,
outputs, and step facts. An **Errors** section appears when the selected step
has a recorded failure. The job page combines its execution summary with this
step inspection. For a detailed JSON job report, select **Download full report**
on the job page. The button downloads `/api/jobs/{job_id}/report` with the
available recorded diagnostic data.

Raw diagnostic data is available separately through authenticated, job-scoped
API reads: `GET /api/jobs/{job_id}/logs` returns log entries,
`GET /api/jobs/{job_id}/messages` returns agent messages,
`GET /api/jobs/{job_id}/errors` returns structured runtime errors, and
`GET /api/jobs/{job_id}/causal-chain` returns the recorded causal chain.
These endpoints are not UI tabs. Their pagination and filters differ; log
polling is described below under **API Polling**.

### Monitoring Events

Administrators can open **Monitoring → Events**. Use the workflow, event family,
subtype, exact event type, dispatch status, and period filters to narrow the
**Events list**. Select an event row to open its detail drawer; the drawer shows
the selected event and its recorded dispatch and command information.

### Search

Click **Search** in the top bar, or press **⌘K** / **Ctrl+K**, and type a query.
Choose **Everything** or one of the category tabs available to your role. Use
**Add filter** to refine the query; Workspace and Project choices appear when
their lists are readable. Search starts in the active workspace; its results
remain limited to your permitted workspace, projects, and entity categories.
Removing a filter or using **Clear all** does not grant additional access.

**Everything** shows a preview of up to five results per category. Open a
category for pages of up to 20 results and use **Load more** when offered. A
count with **+** means the preview may contain more matches; it is not an exact
total. Category tabs can scroll horizontally when they exceed the dialog width.

### Cancelling a Job

Administrators and operators can click **Cancel job** on the job page when the
job is pending, running, resuming, or awaiting approval.

Via API:
```bash
POST http://localhost:8099/api/jobs/{job_id}/cancel
```

Any job that is not finished can be cancelled. Pending jobs are skipped when
the worker next dequeues. Running jobs are marked cancelled; the worker checks
the job status between runtime steps and starts nothing after the cancel, and a
running agent step is terminated. A job parked on an approval, a Wait timer or a
child workflow is cancelled without a worker. Cancelling a parent records a
durable cancel request for each of its unfinished child jobs, which the control
process applies recursively, even after the parent has finished. An external
action that was already sent is not rolled back.

### Polling a job

From the deployment host:

```bash
# Job status + cost summary
./flow-steward jobs show {job_id}

# Incremental logs (use last_id from the previous response as --since-id)
./flow-steward jobs logs {job_id} --since-id 0

# Older history (use the nonempty next_before_id from the previous response)
./flow-steward jobs logs {job_id} --before-id {next_before_id}
```

MCP clients use `job_status(job_id)` and `job_logs(job_id, since_id, before_id)`.
The matching REST routes are `GET /api/jobs/{job_id}`,
`GET /api/jobs/{job_id}/logs?since_id=…` (or `before_id=…`), and, for a
project's queue depth, `GET /api/projects/{project_id}/queue`.

Job status values: `pending` → `in_progress` → `completed` / `failed` /
`cancelled`. A run that waits holds no worker and shows why: `awaiting_approval`
(an Approval step or a recovery approval), `awaiting_timer` (a Wait step) or
`awaiting_child` (a child workflow invoked serially). After a decision or a
wake it is `resume_pending` until a worker continues it. The job detail names
the position with `current_step_id`, `current_group_id`, `active_steps` and,
while it waits, `workflow_wait`; `GET /api/jobs/{job_id}/definition` returns
the definition the run pinned.

Log pages are chronological within each window. Terminal jobs default to the
newest window; `next_before_id` is nonempty when older history remains. `last_id`
is the newest returned entry and remains a watermark for incremental polling,
not a backward pagination cursor. Forward pages with `has_more=true` continue
with `since_id=last_id`; backward pages continue with `before_id=next_before_id`.

Project queue payloads now also expose explicit queue transport and delivery state. Operators should read them as:

- `queue_transport_state=broker_unavailable` — RabbitMQ metrics are unavailable
- `queue_health.state=queued_pending` — normal backlog waiting for pickup
- `queue_health.state=retrying_delivery` — delivery is retrying before execution safely starts
- `queue_health.state=dead_lettered` — delivery retries were exhausted and intervention is required
- `queue_health.state=execution_failed_after_delivery` — RabbitMQ delivery succeeded, but workflow execution failed after claim

Only the project policy field `retry_max_attempts` is applied to RabbitMQ delivery retry. Workflow retry strategy and backoff settings remain business-level controls and do not change queue delivery timing.

### Flow Steward Trace Inspection

Flow Steward keeps its own run/span trace model in the platform database. Use the job detail page and trace APIs to inspect:

- per-step execution order, including loop iterations and parallel lanes
- prompt/tool/message linkage
- token and cost summaries
- workflow/runtime state transitions

### RabbitMQ Management UI

http://localhost:15673 (credentials: your configured RabbitMQ user/password)

- Queue depth per project
- Message rate and delivery stats
- Useful for diagnosing why tasks are not being picked up (worker down, queue full, etc.)

---

## Step 10 — Multi-Model

### Per-Project LLM Provider

Override the default LLM provider and model at the project level, or per individual agent, on the project page's **Models** tab.

**Supported providers:**

| Provider | `provider` value | Example models |
|---|---|---|
| OpenAI | `openai` | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` |
| Anthropic | `anthropic` | `claude-sonnet-4-20250514`, `claude-opus-4-6` |
| Google Gemini | `google` | `gemini-2.0-flash`, `gemini-1.5-pro` |

Global defaults in `.env`:
```bash
FS_DEFAULT_PROVIDER=openai
FS_DEFAULT_MODEL=gpt-4o
FS_DEFAULT_TEMPERATURE=0.1
```

Per-project defaults (set on the project page → **Models** tab).

Per-agent overrides (same tab): e.g. run one agent on `anthropic`/`claude-sonnet-4-20250514` while keeping other agents on GPT-4o.

Project tools are supplied through registered Tool bindings and enabled ToolProvider bindings. Inline HTTP definitions in legacy project configuration are no longer executed; existing stored JSON is preserved.

---

## Extension Python Dependencies

Extension authors declare subprocess Python dependencies in `extension.yaml`
under `python_requirements`. Each entry must use an exact version and one or
more `sha256:` wheel hashes. Dependencies are installed into the extension
bundle's `.fs-python/<version>/site` directory during install/update; the host
Python environment is not mutated.

Recommended authoring flow:

```bash
./flow-steward extensions lock-deps acme.example --platforms manylinux2014_x86_64,manylinux2014_aarch64
./flow-steward extensions bundle acme.example --with-wheels
```

`lock-deps` reads `<extension>/requirements.in` by default and writes
`python_requirements` into the manifest. Use the same platform list that the
bundle will carry. The default lock/bundle targets are
`manylinux2014_x86_64,manylinux2014_aarch64`; use `--platforms active` only for
active-runtime-only local development bundles, never a portable release.
Distributable extensions with Python dependencies must support both Linux
AMD64 (`x86_64`) and ARM64 (`aarch64`). Universal `py3-none-any` wheels can cover
both; compiled dependencies need compatible wheels and hashes for each target.
Docker on macOS or Windows still uses Linux wheels, not desktop-OS wheels.

`bundle --with-wheels` downloads wheels into the bundle `wheels/` directory and
verifies hashes from `python_requirements`. Install/update materializes only
bundled wheels by default. Set `FS_EXTENSION_DEPS_FETCH=allowlist` only when the
operator intentionally allows install-time fetches from the configured default
index; `FS_EXTENSION_DEPS_FETCH=off` keeps installs strictly offline. Use
`FS_EXTENSION_DEPS_SITE_MAX_BYTES` to cap the materialized site directory size.
Export with `--with-wheels`: it prepares both default platform sets automatically
before writing the ZIP. Bare `bundle` does not fetch missing dependencies.
If either platform cannot be resolved, fix the pins or supply compatible built
wheels; do not silently fall back to the developer's current architecture.

Compose also runs the one-shot `extension-dependencies` initializer before app
and worker startup. It materializes already-installed bundles from
their pinned local wheels, so a clean install, `docker compose down` followed by
`up`, or an upgrade cannot start extension consumers with a missing local site.
An ordinary Docker or host restart reuses the persisted `.fs-python` directory.
Install or update extensions from the Installed page using **Install extension from file**
or by dropping a `.zip` archive onto the upload area. Docker runs these operations
in `fs-extension-installer`, sharing the same validation and atomic installation
services as the CLI. The main application and worker keep read-only extension mounts.
The browser uses the normal API authentication and any required browser-code approval.

The installer is started and stopped with the stack. The main Docker API enables
`FS_EXTENSION_INSTALLER_PROXY_ENABLED=1`; the installer leaves it disabled to execute
locally. The private service has no published host port. Custom reverse proxies and
frontend development servers continue to target the main API normally.

CLI installation, updates and uninstall remain available as an alternative:

```bash
./flow-steward extensions install --archive <path>
./flow-steward extensions uninstall <extension-id>
```

Archive paths are resolved from the Flow Steward project checkout and must point
to a project-local file. The launcher routes archive-backed install and
uninstall through an isolated one-shot manager whose checkout mount is read-only
and whose only writable bundle mount is the managed extensions directory.

---

## Step 11 — Extension Notifications

Flow Steward Core does not ship a notification provider. Install one from the
extension catalog (https://github.com/Flow-Steward/extensions) through the
**Marketplace**, by uploading its `.zip` on the Installed page, or with
`./flow-steward extensions install --archive <path>`. When the catalog has a
newer release of an installed extension, its Marketplace page offers
**Update to X**, which runs the same update as uploading the newer `.zip`
(a disabled extension stays disabled). Core only resolves the
generic notification routing path and hands each event to the provider
extension configured for the project.

Delivery uses the installed release, not the latest catalog version. A project
connection selects its exact extension installation. Without a connection,
delivery requires exactly one active installation visible to the event's account
(including global installations); missing or ambiguous installations fail before
invocation. A global installation does not change the event's account scope.

### Setup

1. Install the notification-provider extension and read its connection and
   secret schema on its extension page.
2. Configure a project connection and its secrets. Open **Extensions**, open
   the extension's page, select the project there, click **New connection**,
   fill in the connection fields and secrets from its schema, and save it. With a synthetic provider `acme.notifier` (provider key
   `acme_notifier`), that means a connection field `channel_id` such as
   `example-channel` and a secret `api_token`. The connection then appears on
   the project's **Connections** list.

### Automatic notifications

The project page's **Notifications** tab, titled **Automatic notifications**,
lists every notification the project sends on its own, read from the live
settings each time the tab loads (and every 30 seconds while it is open):

- **Send notification** rules in the **Automation** of each linked workflow, at
  workflow, group and step scope, including rules that name an extension
  channel or e-mail recipients directly without a project subscription;
- the project's existing **event subscriptions** (notification routes), listed
  read-only.

Steps that send a message inside a workflow (an e-mail, a chat message) are
part of the workflow and are not listed, and neither is a connected extension
that no rule or subscription uses. Two rules that use the same channel stay two
entries. Each entry shows its name, its source and scope (project, workflow,
group or step), its event and the conditions that matter (severity, workflow
filter, rule conditions, debounce), how and where it is delivered, and its
state: **On**, **Off** or **Setup problem** with the reason (for example a
disabled extension, a switched-off project connection, a subscription key that
no longer exists, or no enabled subscription matching the event). A rule that
sends to the matching project subscriptions shows that the destination is
decided at run time. A listed entry is configured, not a confirmed delivery.

The tab changes nothing. **Open settings** opens the exact Automation rule on
its workflow, group or step in the builder
(`/registry/workflows/<id>/builder?project_id=<project>&drawer=…&rule=<key>`),
(a project event subscription is listed without a link, because it has no
editor on this tab); a connection problem also offers **Open
connection settings**, the extension's page for this project. The overview is
served by `GET /api/projects/{project_id}/notification-overview` (token scope
`notify.read`), within the caller's account and allowed workflows.

### Notification events

The project's Notifications tab has no editor for project event subscriptions;
automatic notifications come from **Automation** rules. Subscriptions that
exist are only listed there, read-only. In **Settings > Notifications**, create a subscription
(route) by choosing its event and a linked Workflow by name, or **All
workflows**. Route keys are generated
automatically and remain stable across edits. Common event choices include Job
created/completed/failed/awaiting approval, Workflow notification and All events;
existing custom event values are preserved when editing a route.

Without an enabled notification-provider binding, **Create new** is disabled and
the page links to **Extensions**, where the extension is connected to the
project, and to **Runtime Connections**, which lists that connection. An enabled
binding is not proof of configured or successful delivery. Recipient versus
channel controls follow the provider's declared target kind. Missing or failed
Workflow inventory blocks new route creation; Retry keeps the draft. Stored
unavailable selections remain visible during edits, while the backend still
decides whether the route can be saved.

| Job status | Message |
|---|---|
| `completed` | Task completed + workflow result summary |
| `max_iterations` | Needs attention (iteration limit reached) |
| `failed` | Task failed + error message |

How a message is formatted is up to the provider extension.

---

## Environment Variables Reference

The repo now uses layered env templates instead of one mega `.env`.
The full generated inventory is in
[`docs/configuration/flow-steward-env-catalog.md`](configuration/flow-steward-env-catalog.md);
the raw scanner artifact is
[`docs/configuration/flow-steward-env-audit.json`](configuration/flow-steward-env-audit.json).

| File | Use when | Contains |
|---|---|---|
| `.env.example` | First local boot / everyday development | Minimal runtime, provider, auth, and local agent settings |
| `.env.example.prod` | Production deployment | Minimal production stack settings and secrets |
| `.env.example.test` | UI/E2E/Nightfire runs | `APP_BASE_URL`, `TEST_RUNNER_BASE_URL`, `E2E_*`, `TEST_PROJECT_*`, `NIGHTFIRE_*` |
| `.env.example.quality` | SonarQube / quality tooling | `SONAR_*` only |
| `.env.example.ops` | Advanced runtime tuning | email, webhooks, control loops, DB tuning, artifact storage, registry helpers |

Recommended setup flows:

```bash
# Local development
./install.sh --environment development

# Production
./install.sh --environment production
```

### Canonical bundled artifact storage

Select bundled SeaweedFS through the installer rather than assembling S3
variables by hand:

```bash
./install.sh --environment development --file-storage bundled
# or
./install.sh --environment production --file-storage bundled
```

Both environments use `http://seaweedfs:8333` from app, worker, control plane,
CLI, and audit containers. The installer creates the owner-only identity file
`.flow-steward/runtime/seaweedfs-s3.json` and writes its matching access and
secret keys to `.env` or `.env.prod`. The identity file is mounted read-only by
SeaweedFS; runtime consumers receive the same credentials through their selected
environment file. Never commit these generated files or substitute the host
debug port for the container endpoint. Production exposes SeaweedFS only on the
internal artifact network.

`flow-steward status`, `flow-steward doctor`, and configuration/release
validation use an authenticated put/get/delete probe and clean the unique probe
object in a `finally` path. A failed readback, authentication error, or cleanup
failure therefore makes storage readiness fail before a candidate is considered
healthy. For data-plane verification, also exercise a real attachment and
workflow artifact through the application, then verify protected backups with
`flow-steward backup list` / `flow-steward backup verify` and installed release
artifacts with `flow-steward extensions show <extension-id>`.

The installer generates and preserves durable secrets. After containers are
running, use `./flow-steward` for every post-install management operation. The
shared core readiness commands are:

```bash
./flow-steward status
./flow-steward doctor
```

### Reusable CLI runtime

A completed installation includes one private, reusable `fs-cli` container.
The container runs only an idle keepalive; each `./flow-steward` invocation
starts a new Python process for the existing Click command tree and application
services. Application imports and command state are therefore not shared across
invocations.

Use the `./flow-steward` launcher for all management commands. Do not call
`docker compose exec` manually, and do not include `fs-cli` in public `stack`
start, stop, restart, or rebuild operations. The installer and launcher own its
lifecycle. The first command after a Docker stop, configuration apply,
environment switch, or release change may perform one repair before executing
the requested command.

| Launcher condition | Operator action |
|---|---|
| Docker CLI or daemon unavailable | Install or start Docker Desktop/Engine, then retry the same `./flow-steward` command. |
| Runtime repair lock times out | Inspect the reported lock owner and timestamp. Let a live launcher finish; if the owner is stale, follow the stale-owner recovery printed by the launcher, then retry. Do not leave scheduled backups or health checks failing behind the lock. |
| CLI image is missing, stale, or cannot be rebuilt | Inspect the referenced owner-only `.flow-steward/logs/cli-bootstrap-*.log`, rerun `./install.sh`, then retry. |
| `fs-cli` cannot start or pass exact runtime verification | Inspect the referenced owner-only `.flow-steward/logs/cli-runtime-*.log`, rerun `./install.sh`, then retry. |

Launcher progress and recovery diagnostics use stderr, so JSON stdout remains
machine-readable. The runtime has no public port and is not a Python daemon or
an alternative command implementation.

The reusable process has a closed environment boundary. It receives only
`HOME`, `TERM`, `PYTHONPYCACHEPREFIX`, the optional Docker host-project path,
and the lifecycle/provenance keys required by the selected distribution.
Product DB/broker/S3 credentials, worker and app secrets, MCP/model settings,
and E2E passwords remain in their owning app, worker, MCP, or tooling services.

Lifecycle operations still need the authoritative installed configuration.
Development exposes the installer-owned `.env` through the existing source
mount; Production mounts the selected owner-only `.env.prod` at `/app/.env.prod`.
The CLI reads that exact file through the lifecycle configuration store and
never imports the whole file into its process environment. The Production file
remains writable only because `config apply` and environment transitions publish
managed configuration atomically; this is not permission for product commands
to consume secrets in `fs-cli`.

### Environment and storage transitions

Use the public launcher for both directions:

```bash
./flow-steward environment switch production
./flow-steward environment switch development
```

The target environment controls its service inventory. Production stops and
excludes Development-only services while retaining the private `fs-cli`
management runtime. Development materializes its complete preset. Storage is a
separate choice: moving from bundled storage to external S3, or back, does not
copy objects. PostgreSQL-backed users, workflows, jobs, and system configuration
remain available, while each storage backend continues to own its own objects.

The launcher that performs a transition keeps its verified `fs-cli` container;
the next launcher reconciles a changed mount/profile contract once, and later
commands reuse that prepared runtime.

### Post-start extension build lifecycle

Extensions are unrelated to core environment readiness. Validate installed
extensions separately before using them in workflows; a healthy Core does not
validate an extension's own configuration or external service.

Start with an installed, healthy Development stack. User extensions are not
compiled while the system is starting. Run the lifecycle afterward through the
public launcher so Linux-specific build and test work stays inside Docker:

```bash
./flow-steward extensions create acme.example --non-interactive --confirm
./flow-steward extensions validate acme.example --health
./flow-steward extensions test acme.example --non-interactive
./flow-steward extensions lock-deps acme.example --non-interactive --confirm
./flow-steward extensions bundle acme.example --output-dir dist/extensions --non-interactive --confirm
./flow-steward extensions install --archive dist/extensions/acme.example-1.0.0.zip
```

The scaffold starts at version `1.0.0`; bundle names include the manifest version.

Every extension ships its logo as `icon.png` next to `extension.yaml`; there is
no manifest field, the file name is the contract. The scaffold writes a default
grey package glyph. To use your own logo, replace that file with yours and keep
the name. The rules:

- PNG only, recognised by its file signature. No SVG.
- Square, 32-512 px on a side; 128x128 is the recommended size.
- At most 100 KB.

`extensions validate`, `extensions bundle` and install refuse a bundle whose
`icon.png` is missing or breaks a rule, naming the file and the rule.
Use the archive path reported by `extensions bundle` after changing that version.
`lock-deps` reads the scaffold's bundle-local `requirements.in` by default.
Run the commands one at a time and stop on a nonzero exit. Bundle success reports
packaging, so retain the separate validation and test results before installation.

The normal test path requires `requirements-dev.txt` and local pytest targets.
For an extension intentionally lacking `tests/test_*.py`, use
`--allow-no-pytest` and report the omitted pytest coverage; validation, smoke and
dependency failures still fail the run. Test dependencies install offline with
`pip --no-index`, searching `dev-wheels/` and `wheels/`. Packages inherited from
the tooling runtime can satisfy requirements; provide compatible local wheels
for development dependencies that are absent there.

`--test-path tests/test_api.py` (repeatable; a file or a pytest node id inside
the bundle) runs only those targets and stops at the first failure. Without it
the whole suite runs to the end.

An extension that lives in its own repository is checked through the Core
quality gate, inside the disposable `fs-audit` container:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root /abs/path/to/extension-checkout
```

`extension-static` runs the bundle validator, `compileall`, `ruff`, `ruff format
--check`, `mypy` and `bandit`; `extension-tests` runs the `extensions test` path
above, with `--test-paths` for a focused fail-fast run. The checkout is streamed
into the container as a tar archive rather than mounted, and neither check runs
`pip-audit`, which needs the network. See
`docs/standards/testing-standard.md` ("External extension checkouts").

Production keeps test tooling absent. Requesting `extensions test` there gives
the supported `environment switch development` remediation instead of building
or starting a test service in Production.



### Observed CLI status example

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

### Descriptor API and validation diagnostics

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

#### Giving an extension's tests their own environment

`extensions test` runs in a dedicated container, so nothing exported in the host
shell reaches it. An extension whose tests need an input of their own — a
disposable database DSN, a sandbox base URL — names it with `--test-env`, which
is repeatable:

```bash
ACME_SANDBOX_DSN='postgresql://disposable-host/acme' \
./flow-steward extensions test acme.example --test-env ACME_SANDBOX_DSN
```

Only the **name** is passed anywhere: the value is read from your own environment
at the moment the container starts, so it never lands on a command line, in a
config file, or in a log. A test input belongs to one invocation, so there is no
setting to add and nothing to remove afterwards.

Core holds no extension-specific variable names — the extension owns the name of
its own input — and any variable registered in the platform's own environment
manifest is refused, so a platform credential cannot reach third-party bundle
code. A malformed name is reported and skipped rather than acted on.

Without this, such tests skip, and a skipped suite reads as a passing one.

### PostgreSQL diagnostics

Use the public database commands to inspect query statistics and configuration
recommendations for the selected installation:

```bash
./flow-steward database query-diagnostics --help
./flow-steward database tune-env --help
```

Database and connection-pool settings are listed in the
[environment catalog](configuration/flow-steward-env-catalog.md). Apply settings
through the procedure for the installation's [deployment profile](operations/compact-scalable-deployment.md).

### Maintenance and release upgrade

Release upgrade uses the same durable maintenance status surface as every other
operator workflow. Maintenance must already be fully enabled before upgrade and
remains enabled on success or failure. Only an explicit healthy disable reopens
work.

#### Official production upgrade

A fresh install of a published release tag is already an official installation
and upgrades as one. Any other fresh checkout is a verified source installation.
Its semantic ordering is unknown relative to every official version; neither `VERSION` nor Git ancestry
makes it older, newer, or already current. `release check` reads published
stable GitHub Releases tagged exactly `vX.Y.Z` and reports an explicit
transition target. `release upgrade` preflights the exact tag commit and a
complete digest-pinned image set before its resumable checkout handoff.

```bash
./flow-steward release check
./flow-steward backup create
./flow-steward maintenance enable --confirm
./flow-steward release upgrade --confirm

./flow-steward maintenance status
./flow-steward doctor
./flow-steward maintenance disable --confirm
```

The reviewed `release upgrade --confirm` changes a source installation to the
selected official installed identity, or upgrades an existing official
installation. The exact origin, active configuration, and target are checked
again under the lifecycle lock. On interruption, follow the printed recovery
action and rerun the same command; maintenance remains enabled.

During final readiness, the installer can request one refresh of stale security
posture or workflow-knowledge evidence through the installed runtime. It waits
for real completion; requesting a refresh is not a passing check. Maintenance
stays enabled, and unrelated blockers or a failed refresh still prevent release
activation. Read-only Doctor and readiness probes do not perform this repair.

On Scalable, if the retained management container was replaced before the result returned,
`./install.sh --confirm` can resume the existing reviewed source transition.
Keep the same release, repository, and installation configuration, and keep
maintenance fully enabled. Recovery refuses mismatched durable state or a
changed configuration; it does not select a new transition.

Official activation tries GHCR first and Docker Hub second, selecting the whole role set
from one registry. Official activations never build product images locally
or fall back to a floating tag. Development source builds remain separate.
`FS_RELEASE_REGISTRIES` supports an ordered comma-separated namespace override
without changing the no-mixing rule; `FS_RELEASE_REPOSITORY` selects the GitHub
source repository. Release operators should follow
[the native publication runbook](operations/official-release-publication.md).

For a private source repository, use a fine-grained PAT limited to that
repository with GitHub `Contents: read`; organization approval or external
sign-in authorization may also be required. Store it only in the owner-only
deployment-local file:

```bash
mkdir -p .flow-steward/secret-input
chmod 700 .flow-steward/secret-input
# Write the token with an editor or secret-management tool, not a shell argument.
chmod 600 .flow-steward/secret-input/github-release-token
./flow-steward release check
```

The installer reads `.flow-steward/secret-input/github-release-token` only for
`release check`, `release upgrade`, and a fresh install of a release tag. It is never copied into Compose, release
state, Clone archives, or journals. Remove it when the repository becomes public.

During maintenance, ordinary authenticated reads, static assets, liveness,
authentication entry points, and the maintenance status endpoint remain
available. Mutating HTTP/MCP ingress and new worker/control-loop claims are
blocked or deferred. Keep maintenance enabled after a failed upgrade while
completing the printed recovery action. Inspect the host Doctor findings: when
no operation is running, enabled maintenance reports `maintenance_left_enabled`
and exit `4`. After recovery is complete and the other counted fail/warn findings
are resolved, run `./flow-steward maintenance disable --confirm`; this command
still rejects unfinished operations. Then rerun the host Doctor and require
exit `0` before resuming normal work.

Run the installation-aware doctor from the installed checkout on the Docker
host:

```bash
./flow-steward doctor --help
./flow-steward doctor --json
```

The host launcher checks both **Installation** and **Configuration** for
Compact and Scalable through Docker management tooling. Plain diagnosis is
read-only; it reports installation findings such as backup freshness, image
availability, public routing and permissions in addition to configuration.
Its options are `--json`, `--fix [ids ...]`, `--confirm`, `--plain`, `--tui`,
`--no-color`, `--verbose`, `--non-interactive`, and `--help`.
`--probe-mode` is not an option of this installed command. Use `--fix` only for
a reviewed repair; it acquires the installation lock and changes state.

For this installation controller, exit `0` means no counted fail/warn findings;
inspect informational or not-checked findings as well. Exit `2` means invalid
usage, `4` means findings remain (including warnings such as no verified
backup), and `6` means a repair lock is busy. Its Dockerless
`platform_runtime doctor` path reports installation checks as `not_checked`
with `mode: no_docker`, returning `5` when no configuration problem takes
precedence, or `4` when configuration problems remain. Launcher/bootstrap
failures retain their own printed cause and exit status.

Inside the Compact runtime, the ordinary `flow-steward doctor --json` command
uses the container-native core-readiness service. It can succeed while the
Docker-host installation doctor still reports a backup warning. It has the
normal display/JSON options, but no `--fix` or `--probe-mode`; its readiness
result does not verify host configuration, image provenance, backups or proxy
routing. Use the host launcher for those installation checks. The separate
developer probe utility in `core/entrypoints/cli/ops/deployment_doctor.py`
owns `--probe-mode compose|host`; its Compose probes run inside `fs-app` and
its `host` mode requires host-reachable endpoints. Those utility options are
not interchangeable with either installed doctor command.

### Backup and recovery boundary

Nothing backs the database up on its own; schedule `./flow-steward backup create`
with cron as described in [Scheduled maintenance](operations/scheduled-maintenance.md).
Retention of trash, logs, jobs and revisions runs by itself in the control plane.

After updates, local release housekeeping keeps the running release, its rollback
and referenced releases. `FS_INSTALLER_RELEASE_RETENTION` sets a minimum of two.
Unused local image archives can be removed automatically, but global Docker tags,
image IDs, build labels and logs do not prove exclusive installation ownership:
another deployment may retain the same image for rollback without a running
container. Such images and associated release records stay. Doctor reports this
ownership ambiguity separately; `doctor --fix release_retention` cleans only safe
local files and does not resolve ambiguity by deleting shared images. Inspect all
installations' current and rollback records before manual image cleanup.

> **Operational stop condition:** if backup or scheduled lifecycle commands
> report a repair-lock timeout, treat backups as unavailable until the lock
> owner is confirmed live or stale-owner recovery succeeds. Check the latest
> `logs/runtime/backup.log`, create a fresh backup, and run `backup verify`
> before resuming normal operation.

The built-in backup flow is PostgreSQL-only:

```bash
./flow-steward backup create
./flow-steward backup list
./flow-steward backup verify backups/<file>.dump.enc
```

`backup create` produces an encrypted protected-export wrapper around a
PostgreSQL custom-format dump from `pg-fs`. Backup encryption uses only the dedicated
`FS_BACKUP_ENCRYPTION_KEY`; there is no fallback to application signing keys,
database credentials, or a generated key.

The artifact does not include `core/`, `assets/`, logs, temp/cache, pycache,
generated build outputs, derived indexes, RabbitMQ, `extensions/`,
`projects/`, storage objects, or `agent-home`. Storage objects are not copied by
backup or environment switching. Recover application code from a pinned Docker
image tag/digest or Git tag/commit. Preserve `.env.prod`,
`.flow-steward/compose.env`, TLS/certs, image pins, and mutable self-hosted
content such as `extensions/` and `projects/` separately.

Encrypted `*.dump.enc` files may be copied off-host with operator-managed backup
tools. Flow Steward does not implement rsync, S3, Google Drive, or other remote
storage upload integrations in the built-in DB backup command.

`backup verify` is non-mutating and runs only `pg_restore -l`; it never connects
to a restore target. Restore remains separate from the public CLI. Follow the
[PostgreSQL restore runbook](operator-postgresql-restore-runbook.md) only for an
explicit operator recovery. Extensions are unrelated to core environment
readiness; lifecycle commands do not install, enable, disable, or validate them.

## Installation Clone

`clone create` and `clone restore` are the only public Clone commands. They
destructively replace an already installed Compact target from a Compact source,
or a Scalable target from a Scalable source. Clone is not a database-only backup,
profile conversion, live replication, remote transport, or DNS/promotion tool.

Both installations must already be in fully enabled manual maintenance. Create
leaves the source unchanged and maintained. Restore authenticates the entire
archive and exact official release, then shows the target installation identity,
profile, public host, replaced classes, and external-effect warnings before any
mutation. Declining confirmation leaves the target untouched; success leaves it
in maintenance.

Clone requires an official installed identity on both installations. Before the
maintenance steps below, run the source-transition sequence in the preceding
section on any source installation, including a newly installed target. Clone
does not discover or activate a release on the operator's behalf.

```bash
# Source
./flow-steward maintenance enable --confirm
secret-command | ./flow-steward clone create --passphrase-stdin --confirm

# Preinstalled Scalable target: unattended review
./flow-steward maintenance enable --confirm
review_status=0
secret-command | ./flow-steward clone restore backups/source.fsclone \
  --passphrase-stdin --non-interactive || review_status=$?
if [ "$review_status" -eq 0 ]; then echo "Clone review returned unexpected success" >&2; exit 70; fi
if [ "$review_status" -ne 10 ]; then exit "$review_status"; fi
# Repeat with the exact target-bound value printed by the review:
secret-command | ./flow-steward clone restore backups/source.fsclone \
  --passphrase-stdin --non-interactive --confirm '<printed-target-or-plan-value>'
./flow-steward doctor
```

For unattended use, add `--passphrase-stdin` and stream exactly one line. Never
put the Clone passphrase in argv, shell history, environment files, JSON output,
or logs. The Scalable unattended review pass intentionally exits `10` without
mutation; accept that one status as shown above and treat every other nonzero
status as failure.

For Compact, do not use that unattended review command: the host launcher exits
`2` before dispatch when `--non-interactive` is supplied without `--confirm`, so
it cannot print a restore plan or confirmation value. Place the incoming Compact
archive in installer state `clone/inbox`, then use interactive
`clone restore backups/<name>.fsclone` to review the authenticated target plan,
and decline its final confirmation (exit `10`, target unchanged). Only after that
review may an unattended Compact restore use the exact printed target-bound
value with `--non-interactive --confirm`; never invent a confirmation value.
Preflight failures are not successful reviews. The
detailed [Installation Clone runbook](operations/installation-clone.md)
covers preflight, domain journeys, external integrations, resume behavior,
cleanup evidence, and the post-write rollback warning.

### Core dev quickstart variables (`.env.example`)

| Variable | Required | Description |
|---|---|---|
| `FS_DB_DSN` | No | Local PostgreSQL DSN for Docker compose defaults |
| `APP_PORT` | No | Published app port for the local stack |
| Providers Settings | Yes for AI features | Add and rotate encrypted OpenAI, Anthropic, or Google provider credentials |
| `FS_DEFAULT_PROVIDER` | No | Default LLM provider |
| `FS_DEFAULT_MODEL` | No | Default model name |
| `FS_DEFAULT_TEMPERATURE` | No | Default model temperature |
| `FS_LOCAL_AUTH_ENABLED` | No | Enables built-in local auth |
| `FS_SECRET_KEY` | Yes | App signing/encryption key |
| `ENCRYPTION_KEY` | Yes | Stable AES-GCM key for encrypted stored secrets |
| `FS_AUTH_BOOTSTRAP_ENABLED` | No | Enables first-admin bootstrap |
| `MCP_BEARER_TOKEN` | No | Optional token for external MCP clients outside Docker |

### Core prod quickstart variables (`.env.example.prod`)

These product variables are consumed by the app, worker, MCP, database, broker,
and dedicated tooling services. They are not exported into the reusable
`fs-cli`; host-local lifecycle commands read the mounted installer-owned file
through the explicit configuration-store boundary.

| Variable | Required | Description |
|---|---|---|
| `APP_PORT` | No | Published app port |
| `FS_DB_DSN` | Yes | Primary PostgreSQL DSN for the app |
| `LANGGRAPH_POSTGRES_URI` | Yes | LangGraph checkpoint store DSN |
| `RABBITMQ_URL` | Yes | RabbitMQ connection URL; remote brokers require verified `amqps` |
| `FS_RABBITMQ_TRANSPORT_MODE` | No | Default `tls`; canonical Compose declares `compose-local`, disposable audit declares `audit-local`; `loopback-local` requires an explicit numeric loopback address and port |
| `MCP_URL` | Yes | Internal MCP endpoint URL |
| `FS_SECRET_KEY` | Yes | App signing/encryption key |
| `ENCRYPTION_KEY` | Yes | Stable AES-GCM key for encrypted stored secrets |
| `FS_PUBLIC_BASE_URL` | Yes | Generated from CLI **Public host** and **Public port**; see below |
| `FS_AUTH_TRUSTED_ORIGINS` | Yes | Trusted browser origins for auth flows |
| `FS_LOCAL_AUTH_ENABLED` | No | Enables built-in auth if not using external sign-in |
| `FS_AUTH_BOOTSTRAP_TOKEN` | Recommended | One-time bootstrap token for first admin creation |
| Providers Settings | Yes for AI features | Add and rotate encrypted OpenAI, Anthropic, or Google provider credentials |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Yes | Postgres container credentials |
| `RABBITMQ_USER` / `RABBITMQ_PASS` | Yes | RabbitMQ container credentials |
| `FS_IMAGE` / `MCP_IMAGE` / `POSTGRES_IMAGE` / `RABBITMQ_IMAGE` / `TRAEFIK_IMAGE` | No | Optional image tag pins |

### Changing configuration through the CLI

For an installed deployment, use `./flow-steward config set --help` to see the
supported configuration fields. The CLI accepts approved declarative settings,
not arbitrary environment-variable names. **Set does not change the running
application.** It only stages a pending configuration patch. Review that patch
with `config show`, validate it with `config validate`, then run **`config apply`**
to publish and apply it. In the interactive CLI, choose **config apply** after
setting the values; restarting the stack is not a substitute.

For example, configure the public browser address using **Public host** and
**Public port**, rather than searching for a setter named `FS_PUBLIC_BASE_URL`:

```bash
./flow-steward config set --public-host flow.example.test --public-port 443
./flow-steward config show
./flow-steward config validate
./flow-steward config apply
```

Replace the example hostname with the one users open in their browser. `config set`
stages a pending change; `config apply` publishes it and applies the runtime
configuration. `stack restart` only restarts existing containers and does not
apply a pending change or reload their environment from `.env`. The generated
`FS_PUBLIC_BASE_URL` is `https://flow.example.test` for port 443, without a `:443`
suffix; a non-default HTTPS port is retained. OAuth setup distinguishes a missing
or invalid address from DNS resolution failure or a disallowed destination.
`config validate` checks configuration and Compose rendering, not the DNS or
environment observed by the currently running application.

### Optional overlays

- `.env.example.test` keeps test-only knobs out of the main app env. Use it for Playwright, Nightfire, and live E2E runs.
- `.env.example.quality` keeps SonarQube out of the main app env. Append it only when you intentionally run `./core/entrypoints/cli/scripts/lint.sh --sonar`, `--sonar-full`, or backlog/export flows.
- `.env.example.ops` contains everything advanced or situational:
  - `SENTRY_*`
  - `FS_AUTH_PASSWORD_*`
  - `FS_OPENAPI_*` and `FS_DOCS_*` curated schema/docs exposure flags
  - `FS_WATCHDOG_*`
  - `FS_SCHEDULED_JOBS_*`
  - `FS_DASHBOARD_ROLLUP_*`
  - `POSTGRES_*` tuning flags
  - `SECURITY_*`
  - `ARTIFACT_*`
  - `FS_ALLOW_PRIVATE_REMOTE_URLS` (SSRF override; also lets LLM provider extensions reach a self-hosted model server on a private network, see below) and `FS_PROVIDER_WEBHOOK_REQUIRE_PROJECT_SECRET` (opt-in: require a per-project webhook secret for payload-routed provider webhooks; default off)

Notes:

- Scheduled-job `cron_expr` values are configured per project in Workflow **Schedules** (or the scheduled-jobs API), not via env vars.
- `CONTROL_LOOPS_ENABLED` and the `FS_*ROLLUP*` / `FS_*WATCHDOG*` knobs now live in `.env.example.ops` so the quickstart files stay small.
- Curated OpenAPI/docs exposure is off by default. Enable only the exact slices you want:
  `FS_OPENAPI_PUBLIC_ENABLED`, `FS_DOCS_PUBLIC_ENABLED`,
  `FS_OPENAPI_INTERNAL_ENABLED`, `FS_DOCS_INTERNAL_ENABLED`,
  plus `FS_OPENAPI_PUBLIC_REQUIRE_AUTH` / `FS_OPENAPI_INTERNAL_REQUIRE_AUTH`.

### Email

Flow Steward has no built-in mail. Email comes from the **Outgoing Email**
extension: install it from **Extensions**, then create a connection with your
SMTP server, sender address and credentials. With that connection in place:

- **System email** goes out through it: password reset links, email-change
  verification and the welcome email for a new user. If there are several email
  connections, the oldest enabled one is used.
- **Workflow notifications** become available. In a workflow's **Automation**,
  add a *Notify* action on the event you care about (the workflow completes or
  fails, a group or step starts, completes or fails, an approval is waiting
  for a decision, a Wait timer starts), choose Outgoing Email and enter the
  recipients. Every event uses the same plain-text email; its icon and title
  say what happened (✅ completed, ❌ failed, ⚠️ needs your approval, ⏳ waiting
  for a timer), followed by the workflow, the task, the group (only for a step
  in a real group) and the step, the error if any, and a link to the run.

Without an email connection there is no email at all, and the UI says so:

- the sign-in page shows no "Forgot password?" link; an administrator sets a new
  password in **Settings → Users** or with `./flow-steward users set-password`;
- a user created with **Require password change at next sign-in** still gets
  in: the sign-in page accepts the temporary password once and asks for a new
  one in place (new + confirm). The temporary password never opens a session by
  itself; setting the new one signs the user in and ends their other sessions;
- a user cannot change their own sign-in email, because the new address cannot
  be verified;
- new users are created without a welcome email.

Configure **Public host** and **Public port** with `config set`, then run
`config apply` as described above. This generates `FS_PUBLIC_BASE_URL`, which
reset links, verification links and the run links in notifications use.

Local testing uses the Mailpit container from the base compose file: UI at
`http://localhost:8025`, SMTP at `mailpit:1025` from inside the stack:

```bash
docker compose --project-name flow-steward --env-file .env \
  -f infra/docker/docker-compose.yaml up -d mailpit
```

Open `http://localhost:8025` in a browser to see the messages it caught.

Point an Outgoing Email connection at it; the request API stays
enumeration-safe, so an unknown address receives the same accepted response but
produces no Mailpit message.

A project-scoped SMTP connection for the Compose Mailpit typically looks like
the following (field names come from the installed extension's schema):

```json
{
  "provider": "custom_smtp",
  "host": "mailpit",
  "port": 1025,
  "security": "none",
  "from_email": "noreply@flowsteward.local",
  "from_name": "Flow Steward"
}
```

`host` is `mailpit` — the Compose service name, resolved on the stack network,
not `localhost`. Leave the secrets empty, since Mailpit accepts
unauthenticated SMTP.

**One production-policy caveat.** Outbound connections are subject to SSRF
protection, which refuses hosts resolving to private addresses. A Compose
service like `mailpit` is exactly that, so on a stack where the protection is
active the delivery is refused with a disallowed-address error until the
documented development override `FS_ALLOW_PRIVATE_REMOTE_URLS=1` is set.

Keep that override off in production, and never enable it to make an untrusted
production connector reach an internal host: it removes the protection for every
outbound connection in the deployment, not only the one being tested.

**LLM provider extensions on a private network.** An LLM provider extension
such as the Ollama provider may point at a model server on the same machine or
local network (`localhost`, `127.0.0.1`, `10.x`, `172.16–31.x`, `192.168.x`,
`fc00::/7`) only when `FS_ALLOW_PRIVATE_REMOTE_URLS=1`. It is off by default,
and saving such a base URL is then rejected with a message naming this
setting. When it is on:

- private and loopback addresses may use `http://` or `https://`, while public
  addresses still need `https://`;
- link-local addresses, including the cloud metadata service
  `169.254.169.254` and `fe80::/10`, stay refused, as do CGNAT, multicast,
  reserved and unspecified addresses;
- the extension receives the decision as
  `runtime_context.network_policy.allow_private_addresses`, resolves the host
  once and pins the connection to the address it checked.

Inside Docker (and on the Compact install in particular) `localhost` is the
Flow Steward container. To reach a model server on the Docker host, use
`http://host.docker.internal:11434` with Docker Desktop. On Linux, add
`host.docker.internal:host-gateway` to the service's `extra_hosts` or use the
host's LAN address.

---

## Troubleshooting

### Playwright / UI Testing issues

- Screenshots are saved to the screenshot directory configured by the explicit runner invocation.
- If browser launch fails in production runtime, switch to `fs-audit` or a host environment with Playwright browsers installed.
- If the dev server is external to Flow Steward, start and verify it before invoking the YAML flow runner.

### Worker not picking up tasks

- Check the worker is running: `docker-compose ps fs-worker`
- Check RabbitMQ queue depth: http://localhost:15673
- Restart the worker: `docker-compose restart fs-worker`
- View worker logs: `docker-compose logs -f fs-worker`

### Graph fails to compile

```bash
fs-compose exec fs-app python -c "
from core.application.orchestration.graph_runtime import build_registry_graph
g = build_registry_graph()
print('Graph OK:', len(list(g.nodes)), 'nodes')
"
```
## Extracting text from HTML

Use **Text → HTML to text** (`std.text.html_to_text`) with a string `value` to
return a plain-text `result`. The same action is available as
`text.html_to_text` in Transform and Tabular Feed, with no extra parameters.

The action removes tags, comments, and the contents of `head`, `script`,
`style`, and `template`; decodes HTML entities; and collapses whitespace.
Paragraphs, line breaks, and table cells separate words, while inline formatting
preserves word fragments. For example, `<p>&nbsp;</p>` becomes an empty string,
and `<p><b>Two</b> words</p>` becomes `Two words`.

For description checks, keep the original HTML and clean a separate text column,
then use `is_empty` or `text.length` followed by a numeric filter. Tabular Feed
preserves null cells. It parses HTML locally per non-null cell without external
requests; parsing has a higher CPU cost than native text operations like trim.

This is text extraction, not HTML sanitization or browser rendering: CSS
visibility is not evaluated. Render the result as escaped text, because encoded
literal angle brackets can remain in the output.

## Quality evidence reuse

The quality registry assigns every check to one tier. Run a tier through the
public CLI or the compatible `lint.sh` adapters:

```bash
flow-steward quality verify --tier 1
flow-steward quality verify --tier 2
flow-steward quality verify --tier 3
```

The JSON summary separates checks that were `executed`, `reused`,
`invalidated`, or `blocked`. Evidence records additionally distinguish
`incomplete` and `infrastructure_failed`; neither is reusable as a pass.
Relevant input or gate-code changes invalidate only affected fingerprints.
`pip-audit` also fingerprints the current UTC date: acceptance expiry is inclusive
through that UTC day, and prior-day evidence cannot be reused on a new day, even
for the same SHA. Other checks retain ordinary input-bound reuse.

For diagnosis, `--evidence-mode off` forces execution without writing reusable
evidence. Diagnostic runs do not produce retained reusable release evidence.
Tiers 1–3 and SonarQube run locally in Docker. GitHub Actions are disabled;
final release verification requires local Tier 2 and Tier 3 evidence for the same
exact commit SHA. The canonical `--full` command validates or refreshes Tier 2
before the long Tier 3 delta and succeeds only after cumulative evidence validation.
Additional audit checks are available through `lint.sh --checks` when needed.
