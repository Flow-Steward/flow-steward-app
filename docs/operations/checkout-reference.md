# Checkout Operations and Contributor Reference

Detailed repository instructions retained when the product README was shortened.
Start with [the user guide](../GUIDE.md) for operator journeys and use
[the engineering standards](../standards/README.md) for authoritative quality rules.
Command availability is profile-specific; Scalable examples are not automatically
available on Compact. The official documentation is being prepared for the first
public release at https://docs.flowsteward.org.

A self-hosted AI workflow automation platform powered by LangGraph. Build workflows from steps connected by one execution graph in the manual workflow editor, connect them to external systems through extensions, and run them from the web UI, the REST API, webhooks, MCP, or schedules, with Approval and Wait steps, datasets, artifacts, and internal project context.

Production supports `compact` and `scalable` deployment profiles. See [Compact and Scalable Production Deployment](../../docs/operations/compact-scalable-deployment.md) for topology, ingress/TLS, immutable images, provider lifecycle, backup, and profile switching. For destructive same-profile installation replacement, use the [Installation Clone runbook](../../docs/operations/installation-clone.md). Persistent paths and role permissions are listed in the [Compact Deployment Data and Permission Registry](../../docs/operations/compact-deployment-data-registry.md).

## Legal and Community

- [License (Apache-2.0)](../../LICENSE)
- [Notice](../../NOTICE)
- [Contributing Guide](../../CONTRIBUTING.md)
- [Security Policy](../../SECURITY.md)
- [Code of Conduct](../../CODE_OF_CONDUCT.md)

---

## Quick Start

### Before running the installer

You need a running Docker engine with Linux containers and Docker Compose v2
on the machine where you run `./install.sh`. This applies to both Compact and
Scalable, on a server or a desktop. Cloning the repository does not install Docker.

Install Docker from the official [Docker Engine](https://docs.docker.com/engine/install/)
or [Ubuntu](https://docs.docker.com/engine/install/ubuntu/) instructions on a server,
or use [Docker Desktop](https://docs.docker.com/desktop/) on a supported desktop.
Docker Compose v2 is required; Flow Steward does not install Docker automatically.
Docker Desktop includes Compose. With Docker Engine on Linux, install the
[Compose plugin](https://docs.docker.com/compose/install/linux/) too.
Follow the [Linux post-install instructions](https://docs.docker.com/engine/install/linux-postinstall/)
if the deployment user cannot access Docker; do not run Flow Steward as root.

Before continuing, run these as the same user who will run the installer:

```bash
docker --version
docker compose version
docker info
```

If the CLI or Compose is missing, follow the appropriate official installation
guide. If `docker info` fails, start Docker Desktop/the Engine or correct access
to its socket. These checks must succeed before installation; Flow Steward's
missing-Docker/Compose errors also link to the installation guides.

### Install and verify

For a Linux server, read the [Linux host autostart guidance](compact-scalable-deployment.md#linux-host-autostart)
for systemd behavior, required sudo setup, and inspecting or disabling the installed unit.

A clean checkout exactly at its published `v<VERSION>` tag installs that stable
release's official, digest-pinned images and builds nothing; if the release or
its images are unavailable, the installer stops with `release_images_unavailable`
instead of building. Any other checkout builds the current local checkout in
Docker. It does not require a release tag or call the GitHub Releases API:

```bash
git clone <flow-steward-repo-url> flow-steward
cd flow-steward
./install.sh
```

Use a full clone: the Scalable build uses Git history for release bookkeeping.
Both Compact and Scalable production installations support a branch checkout.
Docker may download base and dependency images; this is not a second Git clone.
Official release upgrade and Clone activation retain their exact image identity checks.

```bash
# Install or safely resume core. The guided flow asks for environment,
# storage, network, and TLS choices.
./install.sh

# Verify core readiness. Missing AI credentials remain optional warnings.
./flow-steward status
./flow-steward doctor
```

The product runs entirely in containers, so the same installation works on
macOS, Linux and Windows. The two host entrypoints, `./install.sh` and
`./flow-steward`, are POSIX shell scripts and need a POSIX shell to start them:
on Windows use Git Bash or WSL. Docker itself is the same requirement
everywhere.

`install.sh` bootstraps its Python installer inside Docker, so a host Python
runtime is not required for the normal path. It is a one-time install/resume
entrypoint and has no public subcommands. Set
`FS_INSTALLER_USE_HOST_PYTHON=1` only for local development fallback.
Recoverable non-signal failures keep their verified journal checkpoints. An
initial install interrupted by `SIGINT` or `SIGTERM` instead removes only its
Compose runtime and volumes, clears that operation journal, and the next
`./install.sh` starts a new operation from the beginning. Built images may stay
cached; external S3 data and user-provided TLS material are never rollback
targets.

Scalable production uses PostgreSQL with Apache AGE, pgvector, RabbitMQ, app, MCP,
worker, control plane, and the private reusable `fs-cli` management runtime.
The guided installer supports either bundled self-hosted storage or external
S3-compatible storage. Production does not include the test runner, audit
tools, Sonar, Mailpit, backend reload, or Vite HMR.

In the guided UI, place S3 credentials and provided TLS files under the
repository (for example `.flow-steward/import/`). To use an owner-only file
elsewhere on the host, pass its absolute path directly as an `./install.sh`
option; the launcher mounts that input file read-only. Generated TLS output
paths must stay inside the repository.

If the installer detects a running pre-Phase3 stack without
`.flow-steward/install-state.json`, it refuses automatic adoption and preserves
all configuration and volumes. Continue managing that stack with
`./flow-steward`; use a clean checkout and distinct Compose project identity
for a new Phase3 installation. Do not delete volumes to bypass this guard.

For an existing host reverse proxy, select `--tls-mode reverse_proxy`. The
installer exposes `fs-app` as private HTTP on `127.0.0.1:18099` by default and
does not start its bundled Traefik; point the existing proxy at that backend.
Use `--reverse-proxy-backend-port` to change the private port. The backend host
is restricted to loopback so the unauthenticated HTTP hop is never published.

For local development, choose the local stack explicitly:

```bash
./install.sh \
  --environment development \
  --file-storage bundled
```

Development is one complete preset: core plus frontend and continuously ready
test/audit runtimes. Mailpit and SonarQube/scanner are available on demand. Hot
reload is disabled by default; set `FS_AUTO_RELOAD=1` only when you want backend
reload.

For bundled artifact storage, run the installer with `--file-storage bundled`.
It provisions one owner-only S3 identity at
`.flow-steward/runtime/seaweedfs-s3.json` and installs the matching
`ARTIFACT_S3_ACCESS_KEY` and `ARTIFACT_S3_SECRET_KEY` into the selected runtime
environment file. App, worker, control plane, CLI, and audit tooling all use the
same internal endpoint, `http://seaweedfs:8333`; do not replace it with the
development-only host port. SeaweedFS listens on every attached container
interface, while production keeps the artifact network internal. Readiness is
an authenticated put/get/delete probe whose temporary object is removed on both
success and failure, so DNS or TCP reachability alone is never reported as
storage readiness. Do not hand-edit or commit either generated credential.

Initial schema and canonical seed setup are owned by `./install.sh` on both
Compact and Scalable. Compact also bootstraps schema/seed before starting its
role processes. The database container loads the `db/schema/` 1.0 baseline;
`db/migrations/` holds post-1.0 SQL migrations and is empty at 1.0. After
installation, inspect the schema with the management launcher:

```bash
./flow-steward database status

# Create the first admin in the web bootstrap printed by the installer.
# Non-loopback installs also print an owner-only setup-token file path.
# If that 60-minute token expires before first-user creation, rerun the
# installer: it issues a replacement link on every deployment profile.
# (`users issue-bootstrap-token` is Scalable-only. Compact supports `users create`,
# but replacement setup tokens remain installer-owned.)
./install.sh

# Production runtime images use direct API provider keys. CLI provider auth is
# local/optional only; use a CLI-enabled local image if you select cli providers.
```

Later SQL migrations use `database upgrade` in both profiles, after enabling
maintenance and verifying a backup. Follow the
[source-update procedure](compact-scalable-deployment.md#update-an-installation-to-new-sources):

```bash
./flow-steward database upgrade --author operator --maintenance-confirmed --confirm
```

The following commands are Scalable-only: an explicit operator seed repair, and
source authoring of a post-1.0 SQL migration. Compact seed failures are repaired
by correcting the reported problem and rerunning `./install.sh`.

```bash
./flow-steward database bootstrap-seed --author operator --confirm
./flow-steward database generate-migration add_status_index
```

For non-loopback setup, open the complete link in the installer's framed
`NEXT STEP: CREATE THE FIRST ADMINISTRATOR` block. It appears on stderr, even
when `--json` stdout is redirected, and can be copied from its own line.
Do not share it. The owner-only token file is a recovery option; if constructing
the link manually, put the token only in a URL fragment such as
`https://flow.example.test:8099/bootstrap#setup-token=<token>`. The React page
removes the fragment immediately and sends the token only in the
`x-platform-setup-token` request header. Never place it in a query string.

Extensions install from the extension catalog and own any seed data or
workflow artifacts they publish; core database initialization does not install
extension content. External MCP
clients authenticate with a personal access token or machine token:

```bash
export MCP_BEARER_TOKEN=<fs_pat_or_machine_token>

open https://localhost:8099
```

The built-in MCP server is an API-backed adapter for projects, workflows, jobs,
reports, and notifications. It does not provide repository code indexing,
code-search/graph tools, or schema-index snapshots. The same application
authorization applies to every tool call; scoped discovery is not an access
grant. See [the current tool reference](../../docs/GUIDE.md#step-8--flow-steward-mcp-server).

Installation/resume uses `./install.sh`; all post-install management uses
`./flow-steward`. Core readiness uses `./flow-steward status` and
`./flow-steward doctor`.

### Pre-1.0 development databases

Flow Steward 1.0 starts from a clean `db/schema/` baseline and ships no
historical migrations, so a database created by a pre-release (pre-1.0) build
cannot be upgraded. `database upgrade` and the installer refuse it, unchanged,
and name the pre-1.0 migrations its ledger records. Recreate it instead: install
1.0 from a clean checkout with a distinct Compose project identity (never delete
the old volumes to bypass the installer's guard) with `./install.sh`. The installer
initializes the new canonical schema and seed; verify it with `database status`.
Pre-1.0 data is not carried over. Post-1.0 schema changes reach existing
databases through `db/migrations/` and `database upgrade` (ADR 0012).

## Memory Backend Profiles

Canonical memory, vector search, and graph search run in PostgreSQL. The base
Compose stack builds the canonical PostgreSQL image with Apache AGE + pgvector,
so the standard stack does not need extra database overlays.

Use the guided `./install.sh` flow to select the supported first-release stack.
Production uses the unified Postgres profile with Apache AGE, pgvector,
pg_trgm, and unaccent.

Use `.env.example.memory` as supporting reference for advanced `FS_MEMORY_*`,
`FS_CONTEXT_GRAPH_BACKEND`, and `FS_PGVECTOR_DSN`
variables. Startup prints a redacted backend matrix and fails fast on retired
memory env names or unsupported backend combinations.

## Repository Root Hygiene

Intentional root metadata/config exceptions:
- `.git`, `.github`, `.env*`, `.pre-commit-config.yaml`, `.dockerignore`, `.mcp.json`
- `pyproject.toml`, `babel.cfg`, `eslint.config.mjs`, `tailwind.config.js`
- static Docker manifests now live under `infra/docker/`; static lint/Sonar configs now live under `infra/tooling/`

Current root-level hidden/state inventory:

| Path | Classification | Notes |
|---|---|---|
| `__pycache__` | disposable generated cache/artifact | Root bytecode cache; new Python bytecode is routed to `logs/tool-cache/python/pycache/`. |
| `.mypy_cache` | disposable generated cache/artifact | Safe to delete locally; mypy now uses `logs/tool-cache/mypy/`. |
| `.pytest_cache` | disposable generated cache/artifact | Safe to delete locally; pytest now uses `logs/tool-cache/pytest/`. |
| `.ruff_cache` | disposable generated cache/artifact | Safe to delete locally; Ruff now uses `logs/tool-cache/ruff/`. |
| `.sonar-cache` | disposable generated cache/artifact | Safe to delete locally; Sonar scanner cache now uses `logs/tool-cache/sonar/`. |
| `.coverage` | disposable generated cache/artifact | Safe to delete locally; coverage data now lives under `logs/coverage/`. |
| `coverage.xml` | disposable generated cache/artifact | Safe to delete locally; coverage XML now lives under `logs/coverage/`. |
| `test-results` | disposable generated cache/artifact | Safe to delete locally; Playwright/test-runner outputs now live under `logs/test-runs/`. |
| `.tmp` | disposable generated cache/artifact | Safe to delete locally; scratch output should stay out of the repo root. |
| `.ssl` | local persistent developer state | Local dev certificates generated by startup tooling. |
| `.venv` | local persistent developer state | Local virtualenv; keep or recreate as needed. |
| root Postgres data dir | infrastructure runtime data | Old root-level Postgres data directory from pre-volume dev setups; default dev stack now uses Docker named volumes instead. |

Generated artifact homes:
- coverage reports: `logs/coverage/`
- lint/scanner outputs: `logs/quality/`
- test run outputs: `logs/test-runs/`
- tool caches and centralized Python bytecode: `logs/tool-cache/`

Default Docker runtime data now lives in Docker named volumes (`fs-postgres`, `fs-rabbitmq`, and related service volumes), not in repository-root data directories.

## Management CLI

The public post-install management entrypoint is the `./flow-steward` launcher.
Installer completion prepares a private reusable `fs-cli` container, and the
launcher exposes the schema-driven Click/Textual command tree through it. Every
command still starts a fresh Python process; the container retains no imported
application state between commands. The first call after Docker stops the
container, or after a configuration or release change, may repair that runtime
once before starting the requested command.

`fs-cli` receives only lifecycle/provenance/terminal/path variables. Product
database, broker, S3, MCP, model-provider, application-auth, and E2E secrets
belong to `fs-app`, `fs-worker`, `fs-mcp`, or their dedicated tooling runtime;
Compose does not export them into the reusable CLI process. Host-local lifecycle
commands read the selected installer-owned `.env` or `.env.prod` as a mounted
configuration file when needed. Mounting that file does not make it a process
`env_file`, and implicit dotenv loading is disabled in `fs-cli`.

Use the `./flow-steward` launcher for all management commands. Do not invoke
`docker compose exec` directly or add `fs-cli` to public `stack` operations;
it is a private control runtime owned by the installer and launcher. Shell wrappers in
`core/entrypoints/cli/scripts/` remain internal ops helpers and are not the
public management command surface.

Warm-runtime diagnostics are written only to stderr. If Docker is unavailable,
start or install Docker and retry the same command. A repair-lock timeout means
another launcher is repairing the runtime; wait for it to finish and retry. An
image build failure is retained in owner-only
`.flow-steward/logs/cli-bootstrap-*.log`; a start, inspection, or identity
failure is retained in owner-only `.flow-steward/logs/cli-runtime-*.log`. For
either runtime failure, inspect the referenced log, rerun `./install.sh`, and
then retry `./flow-steward`.

Command discovery:

```bash
./flow-steward --help
./flow-steward users --help
./flow-steward extensions --help
./flow-steward projects --help
./flow-steward workflows --help
./flow-steward jobs --help
./flow-steward reports --help
./flow-steward database --help
./flow-steward stack --help
./flow-steward environment --help
./flow-steward maintenance --help
./flow-steward release --help
./flow-steward backup --help
./flow-steward config --help
./flow-steward testing --help
./flow-steward quality --help
./flow-steward localization --help
```

Public command map:

| Domain | Purpose |
|---|---|
| `users` | Local-auth user operations: list, create, password reset, lockout, roles |
| `extensions` | Extension inventory, install/uninstall planning, enable/disable, scaffold, validation, testing, bundling, dependency locking |
| `projects` | Project lookup and notification diagnostics |
| `workflows` | Workflow listing, entrypoints, validation, contract, suggestions |
| `jobs` | Job status, logs, messages, approvals, ack/restart/cancel, health snapshot |
| `reports` | Generate and fetch operator reports |
| `quality` | Linting, naming checks, CI exclude checks, release gates, Sonar exports |
| `database` | Database bootstrap, upgrade, diagnostics, migration authoring, restore parity checks |
| `stack` | Status, start, stop, restart, rebuild, and logs for the installed stack |
| `environment` | Show or safely switch the installed development/production environment |
| `maintenance` | Show, enable, and explicitly disable durable Maintenance Mode |
| `release` | Installed-release information, checks, and safe upgrades |
| `backup` | Create, list, and verify encrypted PostgreSQL backups |
| `clone` | Create or destructively restore an encrypted same-profile installation Clone |
| `config` | Show, validate, stage, and apply installed configuration |
| `testing` | Smoke, Nightfire regression, fuzzing, and cutover preflight/smoke helpers |
| `localization` | Localization catalog maintenance |

Configuration changes require **Apply**, not a restart. `config set` only stages
pending changes; review them with `config show`, check them with `config validate`,
then run `config apply` to apply them to the running installation:

```bash
./flow-steward config set --public-host flow.example.test --public-port 443
./flow-steward config show
./flow-steward config validate
./flow-steward config apply
```

Replace the example hostname with your own. `stack restart` does not apply pending
configuration or reload container environment variables. Public host and Public
port generate `FS_PUBLIC_BASE_URL` automatically; there is no generic arbitrary
environment-variable setter. See [Changing configuration through the CLI](../../docs/GUIDE.md#changing-configuration-through-the-cli).

Generic task submission is retired. To launch a selected workflow, use MCP
`workflow_trigger` with `project_name`, `workflow_id`, and declared inputs in
`payload`, or JSON `POST /api/workflow-runs` with explicit `project_id`,
`workflow_id`, and `inputs`. Both enforce the callable MCP-channel contract;
arbitrary runtime state and caller-selected provenance are not accepted.

Extension packages can declare per-extension Python dependencies in
`extension.yaml` via `python_requirements`. Lock dependency hashes before
bundling wheels so native packages include every target platform hash:

```bash
./flow-steward extensions lock-deps acme.example --input requirements.in --platforms manylinux2014_x86_64,manylinux2014_aarch64
./flow-steward extensions bundle acme.example --with-wheels
```

Run the extension lifecycle only after the selected stack is installed and
healthy: `create`, `validate --health`, `test`, `lock-deps`, `bundle`, then
install the archive. The public launcher delegates build/test work to the
Docker/Linux tool runtimes; system startup does not compile user extensions.
In Production, `extensions test` reports the Development-switch remediation
instead of materializing test tooling.

Use `--platforms active` only for an active-tool-runtime development bundle. The default lock
and bundle targets are `manylinux2014_x86_64,manylinux2014_aarch64`.

For distribution, **support both Linux AMD64 and ARM64**: lock the full transitive
dependency set and export with `bundle --with-wheels`. That command automatically
downloads hash-verified wheels for both default targets before creating the ZIP;
plain `bundle` does not download missing wheels. A universal `py3-none-any` wheel
can serve both targets, while compiled dependencies need compatible wheels for
each. Docker on Apple Silicon and ARM Linux servers uses Linux ARM64 wheels,
not macOS wheels. Do not publish an `active`-only development package as portable.
See the [extension authoring agent playbook](../../docs/extension-authoring-agent-playbook.md#portable-python-dependencies-and-export)
for the packaging checklist and the separate native-executable archive rules.

Built-in backup is PostgreSQL-only:

```bash
./flow-steward backup create
./flow-steward backup list
./flow-steward backup verify backups/<file>.dump.enc
```

The encrypted `*.dump.enc` artifact contains a protected PostgreSQL custom-format
dump from `pg-fs`. It does not include `core/`, `assets/`, logs, temp/cache,
pycache, generated build outputs, derived indexes, RabbitMQ,
`extensions/`, `projects/`, or storage objects. Storage objects
are not copied by `backup create` or by an environment switch. Preserve `.env.prod`,
`.flow-steward/compose.env`, TLS/certs, pinned image tags/digests or the Git
tag/commit, and any mutable self-hosted `extensions/` or `projects/` content
separately. Encrypted backups require the durable
`FS_BACKUP_ENCRYPTION_KEY` value used when they were created. Off-host copies are operator-managed; use your own backup,
rsync, cloud, or storage tooling for that layer.

Backup verification is non-mutating and checks the decrypted dump with
`pg_restore -l`. Restore is intentionally absent from the public CLI and is a
separate, destructive operator procedure; follow the
[PostgreSQL restore runbook](../../docs/operator-postgresql-restore-runbook.md).
Extensions are unrelated to core environment readiness and are not installed,
enabled, disabled, or validated by lifecycle commands.

Environment and storage selection are configuration changes, not data-copy
operations. For example, Development with bundled storage can switch to
Production with external S3 and later return to Development. PostgreSQL-backed
users, workflows, and system configuration remain; objects are neither copied
nor migrated between the local and external storage backends.

Release upgrades require durable Maintenance Mode to be fully enabled before
changing the checkout or runtime, and they never reopen work automatically.

#### Official release upgrade

A fresh install of a published release tag is an official installation. Any
other fresh production checkout is a verified source installation. Its semantic
ordering is unknown relative to every published version, even when its commit
matches or is related to a release commit. `release check` reports an explicit
transition opportunity without calling the source older, newer, or current.
Stable GitHub Releases tagged exactly `vX.Y.Z` are the official source of truth;
the installer verifies the tag commit and a complete digest-pinned image set
before changing the checkout.

```bash
./flow-steward release check
./flow-steward backup create
./flow-steward maintenance enable --confirm
./flow-steward release upgrade --confirm
./flow-steward maintenance status
./flow-steward doctor
./flow-steward maintenance disable --confirm
```

The reviewed `release upgrade --confirm` transitions a source installation onto
the selected official installed identity, or upgrades an existing official
installation. The handoff is resumable and keeps maintenance enabled on success
or failure; follow its printed recovery action and rerun the same command.
Official release upgrades resolve the complete role set from GHCR,
then Docker Hub as a whole-set fallback. They never mix registries, use floating
tags, or build product images locally. Development source builds remain separate.
Override the ordered namespaces only when required with the comma-separated
`FS_RELEASE_REGISTRIES`; override GitHub discovery with `FS_RELEASE_REPOSITORY`.

While the source repository is private, create a fine-grained GitHub PAT limited
to that repository with `Contents: read`, then write it directly to the
deployment-local owner-only file. Do not pass it as an argument or environment
variable.

```bash
mkdir -p .flow-steward/secret-input
chmod 700 .flow-steward/secret-input
# Write the token with an editor or secret-management tool.
chmod 600 .flow-steward/secret-input/github-release-token
./flow-steward release check
```

Only release check/upgrade reads this file. Remove it after the repository
becomes public to return to anonymous discovery.
See [official release publication](../../docs/operations/official-release-publication.md)
for the native two-architecture release process.

On success or failure, inspect the reported recovery details and keep
maintenance enabled until health, storage, and preservation checks are green.

#### Destructive installation Clone

Clone moves an exact point-in-time installation onto an already installed target
of the same profile and Linux architecture. It replaces Flow Steward product
state while retaining the target host, ports, ingress/TLS, Compose identity,
filesystem locations, and PostgreSQL server-role credentials. It does not merge
state, change DNS, copy external S3 objects, or provide a promote/switch command.

Clone requires an official installed identity on both the source and target. If
either is still a source installation, complete the release check, backup,
manual-maintenance, and confirmed transition sequence above on that installation
before using Clone. Clone never upgrades an installation implicitly.

Both commands require fully enabled manual maintenance. Passphrases are prompted
securely; automation must stream one line through stdin rather than putting it in
arguments or environment files:

```bash
./flow-steward maintenance enable --confirm
secret-command | ./flow-steward clone create --passphrase-stdin --confirm

# On the preinstalled Scalable target: unattended review
./flow-steward maintenance enable --confirm
review_status=0
secret-command | ./flow-steward clone restore backups/<archive>.fsclone \
  --passphrase-stdin --non-interactive || review_status=$?
if [ "$review_status" -eq 0 ]; then echo "Clone review returned unexpected success" >&2; exit 70; fi
if [ "$review_status" -ne 10 ]; then exit "$review_status"; fi
# Repeat with the exact target-bound value printed by the review:
secret-command | ./flow-steward clone restore backups/<archive>.fsclone \
  --passphrase-stdin --non-interactive --confirm '<printed-target-or-plan-value>'
```

Restore authenticates and stages the full archive, resolves its exact official
release, prints the target-bound replacement plan, and asks for strong
confirmation before mutation. The Scalable unattended review intentionally exits `10`
without changing the target; the guard above accepts only that expected status
and still fails on every other error. Compact rejects `--non-interactive` without
`--confirm` with exit `2` before dispatch, so that command cannot print a plan or
confirmation value. For Compact, place the incoming archive in installer state
`clone/inbox`, then use interactive `clone restore backups/<archive>.fsclone`
and decline the authenticated target plan (exit `10`, target unchanged). Only
after that review may unattended restore use the exact printed target-bound
confirmation value; preflight failures are not successful reviews.
Success leaves maintenance enabled. See the
[Installation Clone runbook](../../docs/operations/installation-clone.md) for domain
journeys, external-effect review, interruption recovery, and rollback limits.

Examples:

```bash
# Create admin user when UI bootstrap is already closed
./flow-steward users create \
  --email admin@example.com \
  --password-stdin \
  --role admin

# Optional for multi-account installs: --account-id expects an account scope UUID,
# not the new user's id. Omit it to use FS_DEFAULT_ACCOUNT_ID.

# Force password reset for an existing user
./flow-steward users force-reset \
  --email admin@example.com

# List recent jobs
./flow-steward jobs list --limit 20

# Cancel a running job
./flow-steward jobs cancel job_123 --comment "Operator cancel" --confirm

# Restart a failed/cancelled job
./flow-steward jobs restart job_123 --confirm
```

The CLI and installer use plain, line-oriented prompts and output by default.
Type an answer (or a numbered choice) and press Enter; no mouse or full-screen
navigation is required. Use `--tui` to opt in to the existing full-screen forms
and rich output where supported:

```bash
./install.sh
./install.sh --tui
./flow-steward users create --tui
```

`--plain` remains available and overrides `--tui`. JSON and non-interactive
execution never open full-screen forms. Doctor diagnostics remain line-oriented.

During a fresh interactive install, enter **Public host (domain or IP)** and
**Public port** for the browser address. Keep `localhost` for local use without
a domain; use port `443` for a standard HTTPS domain. Installation generates
`FS_PUBLIC_BASE_URL` from these answers before the first start. DNS and any
external reverse proxy must already route this address to the server.

Global presentation/runtime flags such as `--json`, `--plain`, `--tui`, `--no-color`,
`--verbose`, and `--non-interactive` may go before or after the command path.
Command inputs such as `--input-json` belong to the executable leaf:

```bash
./flow-steward --json jobs list --limit 20
./flow-steward jobs list --json --limit 20
./flow-steward users create --input-json user.json
```

Change/destructive leaves show an interactive review prompt on a capable TTY.
Automation paths that cannot prompt (`--json`, `--non-interactive`, or
redirected output) must pass the leaf-only `--confirm` option after the command
path.

## API Documentation

FastAPI's default `/openapi.json`, `/docs`, and `/redoc` endpoints are disabled.
Flow Steward exposes curated schemas only when the matching env flags are enabled:

| Surface | Schema | Swagger UI | Enable flags |
|---|---|---|---|
| Public integration API | `/openapi-public.json` | `/docs/api` | `FS_OPENAPI_PUBLIC_ENABLED=1`, `FS_DOCS_PUBLIC_ENABLED=1` |
| Internal app API | `/openapi-internal.json` | `/docs/internal` | `FS_OPENAPI_INTERNAL_ENABLED=1`, `FS_DOCS_INTERNAL_ENABLED=1` |

Both surfaces require auth by default. Keep
`FS_OPENAPI_PUBLIC_REQUIRE_AUTH=1` and `FS_OPENAPI_INTERNAL_REQUIRE_AUTH=1`
unless you are deliberately exposing docs in a trusted local environment.

The public schema is intentionally narrow: task submission, job listing/detail,
job cancellation, approval decisions, workflow catalog/entrypoints, and workflow
contracts. The internal schema covers authenticated `/api/*` JSON routes and
excludes health, metrics, public auth routes, and raw webhook/callback ingress.

## Isolated Test Runner

For long E2E and UI suites, use the dedicated compose runner instead
of `fs-compose exec fs-app ...`. This keeps test processes out of the web
server container, so app restarts or rebuilds do not kill the running pytest process.

```bash
# One-off runner container
fs-compose run --rm -T fs-test-runner python -m pytest tests/e2e/test_workflow_parallelism.py -q -rs

# Convenience wrapper
core/entrypoints/cli/scripts/run_test_runner.sh python -m pytest tests/e2e/test_workflow_parallelism.py -q -rs

# Example UI regression extension
core/entrypoints/cli/scripts/run_test_runner.sh python -m pytest tests/ui/test_job_detail_page.py -q -rs
```

The runner talks to the live app over `http://fs-app:8099` by default. Override with
`TEST_RUNNER_BASE_URL` if you need a different target.

## Frontend Development

Build React assets from `assets/frontend/` with `npm run build`. This does not
enable backend reload or Vite HMR.

Hot reload is opt-in only. When a local-development env explicitly sets
`FS_AUTO_RELOAD=1`, `fs-app` runs `uvicorn --reload` for backend code changes.
The installer still starts the selected runtime services only; start the Vite
HMR service explicitly when you want browser-side hot reload:

```bash
fs-compose up -d fs-frontend
```

Use `http://localhost:15173/app/` while iterating on React/TypeScript. Keep `https://localhost:8099` for the integrated React app surface and full-stack validation.

Useful dev commands:

```bash
cd assets/frontend && npm run build
fs-compose logs -f fs-app fs-frontend
```

If you need a one-off production bundle validation, build it explicitly:

```bash
cd assets/frontend && npm run build
```

The brownfield React migration workspace now lives under
`assets/frontend/`. It is served by FastAPI under
`/app/*` after a production build.

Frontend workspace commands:

```bash
cd assets/frontend
npm ci
npm run lint
npm run test
npm run build
npm run test:e2e
```

Asset/runtime contract for the React shell:

- React source lives in `assets/frontend/`
- Vite build output goes to `assets/frontend/dist/`
- FastAPI serves the React shell from `/app/*`
- public product routes are React-owned by default
- old pre-cutover aliases such as `/events`, `/account-settings`, `/processes`, `/system/monitoring`, and old project/job deep links are intentionally not registered
- `/security`, `/security/projects/{id}`, `/registry/changelog`, and `/bootstrap` are canonical React routes
- built assets are served from `/app/assets/*`
- hashed asset names come from the Vite manifest; no server-rendered UI template edits are required
- `/bootstrap` is React-owned in SPA mode
- normalized React migration APIs are exposed under `/api/v2/*`

## Code Quality Gate

The Development preset keeps `fs-test-runner` and `fs-audit` ready. SonarQube
and its scanner remain on demand:

```bash
./install.sh --environment development
./core/entrypoints/cli/scripts/lint.sh --sonar
```

Run the full quality/security gate with one command:

```bash
./core/entrypoints/cli/scripts/lint.sh
```

Fast mode for local iteration:

```bash
./core/entrypoints/cli/scripts/lint.sh --fast
```

Additional local audit checks (dead code, duplication, and complexity):

```bash
./core/entrypoints/cli/scripts/lint.sh --checks vulture,deptry,pylint-dup,radon,xenon
```

Local Tier 3 + SonarQube in one command:

```bash
./core/entrypoints/cli/scripts/lint.sh --full --with-sonar
```

The exact checks and tier ownership are defined in
`infra/tooling/quality/check-registry.json`; do not infer tier membership from
a manually maintained tool list.

All gate modes run in the on-demand `fs-audit` container by default, so
runtime app containers stay lean.
Before its first `fs-audit` run, `lint.sh` compares the image's
`org.flowsteward.audit-toolchain` label with a digest of `infra/docker/Dockerfile`,
`requirements-runtime.lock`, `requirements-audit.txt`, `requirements-semgrep.txt` and
`requirements-cli.lock`, and builds the image when it is missing or the digest differs,
so a tool upgrade reaches the gate without a manual rebuild. Source changes never
trigger a rebuild: the gate bind-mounts the checkout over `/app`.

Enable git hooks (fast gate on `pre-commit`):

```bash
pre-commit install --hook-type pre-commit
```

Optional OWASP ZAP baseline scan during audit (targets deployed/staging URL):

```bash
FS_ZAP_TARGET_URL=https://staging.example.com ./core/entrypoints/cli/scripts/lint.sh --checks zap
```

Run only SonarQube scan (uses `quality` profile in Compose):

```bash
./core/entrypoints/cli/scripts/lint.sh --sonar
```

Run full Sonar profile (includes backend Python scope for production code):

```bash
./core/entrypoints/cli/scripts/lint.sh --sonar-full
```

Export SonarQube issues to machine-readable files (for agents/automation):

```bash
./core/entrypoints/cli/scripts/lint.sh --sonar-issues
```

Export SonarQube issues and generate developer backlog Markdown:

```bash
./core/entrypoints/cli/scripts/lint.sh --sonar-backlog
```

Use external SonarQube (instead of local `quality` profile):

```bash
SONAR_LOCAL=0 SONAR_HOST_URL=https://sonar.example.com ./core/entrypoints/cli/scripts/lint.sh --sonar
```

Start SonarQube UI manually (optional):

```bash
fs-compose --profile quality up -d sonarqube
# UI: http://localhost:${SONAR_PORT}
```

Notes:
- Keep `SONAR_TOKEN` in `.env`; `core/entrypoints/cli/scripts/lint.sh` auto-loads it.
- Default Sonar profile is tuned for reliability (frontend/sql).
- Python quality/security remains mandatory via Ruff/Mypy/Bandit/Semgrep in `./core/entrypoints/cli/scripts/lint.sh` gates.
- Set `SONAR_PROFILE=full` if you want `--with-sonar` to run full profile instead of stable profile.
- Set `SONAR_EXPORT=1` to auto-export issues immediately after `./core/entrypoints/cli/scripts/lint.sh --sonar`.
- Exports go to `SONAR_EXPORT_DIR` (default `logs/quality/sonar`) as raw JSON, flat JSON, CSV, and summary JSON.
- Set `SONAR_BACKLOG=1` to auto-generate backlog after export (file path via `SONAR_BACKLOG_PATH`).
- One-shot backlog command: `./core/entrypoints/cli/scripts/lint.sh --sonar-backlog` (default output `logs/quality/sonar/sonar-dev-backlog.md`).
- For OOM issues, tune Sonar memory in `.env`: `SONAR_MEM_LIMIT`, `SONAR_SCANNER_MEM_LIMIT`, `SONAR_WEB_JAVAOPTS`, `SONAR_CE_JAVAOPTS`, `SONAR_SEARCH_JAVAOPTS`, `SONAR_SCANNER_JAVA_OPTS`.
- `--sonar-full` intentionally skips `tests/**` to keep scans stable and avoid scanner OOM on large suites.
- `--sonar-full` auto-applies tuned RAM defaults (configurable via `*_FULL` vars in `.env.example`).

GitHub Actions are disabled. Run Tier 1–3 and SonarQube locally in Docker;
no GitHub CI artifact or status check is required for merge or release.

### Image Targets

The runtime Dockerfile now lives at `infra/docker/Dockerfile` and has split targets:

```bash
# Lean runtime image (prod app/worker)
docker build -f infra/docker/Dockerfile --target runtime -t flow-steward:runtime .

# Local runtime image (CLI-backed dev workflows)
docker build -f infra/docker/Dockerfile --target local-runtime -t flow-steward:local-runtime .

# Audit image (linters/security/deep checks)
docker build -f infra/docker/Dockerfile --target audit -t flow-steward:audit .
```

Requirements split:
- `requirements-runtime.txt` — runtime deps only
- `requirements-audit.txt` — lint/security/audit deps
- `requirements.txt` — aggregate for local dev/CI
- `infra/tooling/sonar-project.properties` — SonarQube scan config

### UI i18n Catalog Workflow

UI translations are stored in gettext catalogs:

- `locales/<locale>/LC_MESSAGES/ui.po` (editable source)
- `locales/<locale>/LC_MESSAGES/ui.mo` (compiled runtime catalog)
- `locales/<locale>/meta.json` (locale labels shown in UI)

Runtime locale discovery is based on compiled catalogs.  
A locale appears in the language switcher only when `ui.mo` is present for that locale.

Key-based IDs are preserved (`t('shell.nav.dashboard')`, `translate_ui_text(...)`), but the source of truth is now file-backed catalogs instead of Python dictionaries.
Runtime request handling uses Python stdlib `gettext`; Babel/`pybabel` is only required in tooling environments for catalog management.

Where translations are used:

- React/email surfaces: `t('some.key')` or `translate_email(...)`
- Python: `translate_ui_text('some.key', ...)`
- Browser-side JS: `uiT('some.key', fallback, params)`

Quick rules:

- The English catalog `locales/en/LC_MESSAGES/ui.po` is the source of truth for key inventory.
- Add a new key to the English catalog first, then rebuild/update other locale catalogs.
- A locale appears in the language switcher only after its `ui.mo` file has been compiled.
- Adding a new language does not require Python code changes; add `meta.json`, `ui.po`, then compile `ui.mo`.

Catalog tooling:

```bash
# one-time bootstrap from the old services/ui_i18n.py dictionary
./flow-steward localization bootstrap-catalog

# rebuild POT from default locale keys
./flow-steward localization rebuild-pot

# initialize a new locale (example: de)
./flow-steward localization init-locale de --label "German" --native-label "Deutsch"

# update existing locale catalogs from POT
./flow-steward localization update

# compile .po -> .mo
./flow-steward localization compile
# or directly
pybabel compile -D ui -d locales

# validate referenced Python/React keys in all supported locales and POT
./flow-steward localization validate-keys
```

Validation requires every referenced key in `locales/ui.pot` and a nonblank
translation in each locale declared by `meta.json` or shipped with `ui.mo`
(including English), with matching compiled `ui.mo` entries for every used key.
Empty POT translations and unused locale entries are allowed.
The Tier 2 `ui-i18n` check runs the same read-only validation.

Typical workflows:

```bash
# 1. Add a new UI key
# - use the new key in a template/Python/JS call site
# - add the English text to locales/en/LC_MESSAGES/ui.po

# 2. Rebuild the template catalog from English keys
./flow-steward localization rebuild-pot

# 3. Propagate new keys into every existing locale catalog
./flow-steward localization update

# 4. Translate the new entries in locales/<locale>/LC_MESSAGES/ui.po

# 5. Compile runtime catalogs
./flow-steward localization compile

# 6. Validate referenced keys in all supported locales and POT
./flow-steward localization validate-keys
```

```bash
# Add a brand-new locale (example: de)
./flow-steward localization init-locale de --label "German" --native-label "Deutsch"

# Edit:
# - locales/de/LC_MESSAGES/ui.po
# - locales/de/meta.json

# Compile the new locale so it becomes visible in the UI
./flow-steward localization compile
```

If you edit `.po` files and do not re-run `compile`, the app will keep serving the old compiled text from `.mo`.
If `pybabel` is missing in your current shell, run the catalog commands from a tooling environment that has `requirements-audit.txt` installed.

See **[docs/GUIDE.md](../../docs/GUIDE.md)** for the operator guide and **[docs/user/README.md](../../docs/user/README.md)** for user documentation. Follow **[production deployment](compact-scalable-deployment.md)** for installation and updates, **[scheduled maintenance](scheduled-maintenance.md)** for backups, and the **[PostgreSQL restore runbook](../operator-postgresql-restore-runbook.md)** for recovery.

## Documentation Freshness

Use these documents as the current source of truth:

- `README.md` for local setup, day-1 operations, and repo navigation
- `docs/GUIDE.md` for the main user/operator guide
- `docs/user/*` for release-oriented feature and configuration docs
- `core/infrastructure/config/flow_steward_env_manifest.json` for environment variables
- `docs/standards/*` and `docs/architecture/adr/*` for canonical engineering and architecture rules

If a dated architecture or testing note conflicts with the docs above or with current code, treat it as stale and do not rely on it for release setup or runtime behavior.

---

## How It Works

```
Task (web UI / REST API / provider webhook)
        │
        ▼
  Web Server (port 8099) ──► RabbitMQ ──► Worker process
                                               │
                                               ▼
                                     LangGraph workflow runtime
                         ┌─────────────────┼──────────────────┐
                         ▼                 ▼                  ▼
                   PostgreSQL           Extensions        Artifacts
                (job state, logs,    (connections,    (files, datasets,
                 memory, search)      actions)         reports)
                       │
                       ▼
                     approvals → workflow outputs → notifications
```

---

## Queue Delivery Retry And DLQ

- RabbitMQ remains the transport layer for per-project queues, while PostgreSQL remains the source of truth for operator-visible job state.
- Delivery retry is intentionally narrower than workflow retry. RabbitMQ delivery retry only covers failures before a worker safely claims the job into execution.
- Workflow or business retry semantics do not change here. If a workflow fails after claim, the job is recorded as an execution failure and is not automatically requeued as a delivery retry.
- The existing retry policy vocabulary is reused, but only `retry_max_attempts` applies to RabbitMQ delivery retry. `retry_strategy` and backoff fields stay workflow-level only.
- Each project queue now has a matching dead-letter queue: `platform.project.{project_id}` routes terminal delivery failures to `platform.project.{project_id}.dead_letter`.
- Operator-visible queue states are mirrored into job metadata and exposed through job runtime, project queue, readiness, and admin monitoring payloads:
  - `queued_pending`
  - `retrying_delivery`
  - `dead_lettered`
  - `execution_failed_after_delivery`
  - `discarded`

---

## Services

| Service | Port | Purpose |
|---|---|---|
| `fs-app` | 8099 | Web UI + REST API + webhook receiver |
| `pg-fs` | 55432 -> 5432 | PostgreSQL — job state, logs, LangGraph checkpoints |
| `rabbitmq-fs` | 5673 -> 5672 / 15673 -> 15672 | Task queue + management UI |
| `fs-worker` | — | Ready-queue worker process; scale horizontally for more concurrent jobs |

---

## Key Features

- **Approval and Wait steps** — pause a run for a reviewer's decision (approve, or reject along the workflow's rejected path) or for a set time, without holding a worker; cancel a run at any time
- **External workflow authoring** — discover contracts and validate disabled drafts through the shared MCP/API services
- **Workflow builder** — steps, real loop/parallel groups and one execution graph with contracts, Test Run, webhooks, nested workflows, HTTP requests, datasets, artifacts, and notifications
- **Core tabular feeds** — built-in `tabular_feed` steps process artifact/dataset-backed CSV, TSV, JSON, NDJSON, Parquet, and XLSX files without putting full rows in workflow state
- **Multi-model support** — OpenAI, Anthropic (Claude), Google (Gemini); per-project and per-agent overrides
- **Custom tools** — define extra HTTP calls agents can use, per project
- **Internal project context** — Postgres-backed project memory supplies scoped context to workflow runs without a standalone project screen
- **Cost tracking** — per-LLM-call token and USD cost tracking shown on the job log page
- **Extension notifications** — outbound notifications through a notification-provider extension installed from the extension catalog
- **Optional UI testing** — Playwright flow runner is available for local/audit images and explicit test workflows; it is not part of production workflow runs
- **Job queue management** — per-project RabbitMQ queues; cancel pending/running jobs from the UI

User-facing release docs start at [docs/user/README.md](../../docs/user/README.md).

---

## Backend Policy Baseline (Closeout v1)

- **Extension-first integrations** — product/provider integrations run through extensions/project connections; deterministic first-party workflow primitives such as `http_request`, `transform`, and `tabular_feed` remain core-owned.
- **Sentry scope in core** — Sentry remains only for platform observability and is not exposed as a product connector or webhook ingress.
- **Job logs are non-deletable** — log read endpoints/pages remain available, while log-delete endpoints are intentionally unavailable.
- **Authentication extension points** — optional authentication integrations plug in through neutral runtime hooks; core auth behavior stays unchanged unless an extension is installed and configured.
- **Custom providers are self-hosted gated** — named providers (`openai_compatible`, `azure_openai`) are managed only in self-hosted mode; env-based model resolution remains fallback.

---

## Project Structure

The repository follows the canonical clean-slate layout below.
Top-level buckets outside this layout must not receive new permanent code.

```text
core/
  entrypoints/     Transport/controller surfaces (admin, api, web, ws, cli, mcp)
  application/     First-party application logic by business area
  infrastructure/  Shared config, DB, mail, observability, security, runtime support
extensions/        External or self-hosted extension packages
db/
  schema/          Canonical SQL schema (the 1.0 baseline)
  seeds/           Canonical seed-data snapshots
assets/            Frontend static assets
docs/              Architecture, operations, and supporting documentation
tests/             Automated test suites
infra/             Deployment manifests and operational wrappers (`core/entrypoints/cli/scripts/`, `infra/cron/`)
logs/              Generated operational artifacts
backups/           Generated encrypted backups and restore drill artifacts
locales/           Localization catalogs
```

Existing top-level buckets such as `routes/`, `services/`, `models/`, `schemas/`,
`integrations/`, `ops/`, and `projects/` are not part of the target repository
structure. Root `serve.py` remains the convenience launcher that re-exports the
canonical HTTP app composition.
Canonical database artifacts live in `db/schema/` (the 1.0 baseline) and
`db/seeds/`. There are no historical migrations: post-1.0 schema changes are
ordered SQL files under `db/migrations/`, which is empty at 1.0, applied by
`database upgrade` (see ADR 0012); a root-level migration directory is not used.

---

## Environment Variables

API providers are configured in **Admin → Providers Settings**. Add the
provider key there, then choose its provider/model in project or agent settings.
Provider keys are encrypted at rest and are intentionally not loaded from
`.env` files.

Optional (defaults shown):

```bash
FS_DEFAULT_PROVIDER=openai
FS_DEFAULT_MODEL=gpt-4o
FS_DEFAULT_TEMPERATURE=0.1
FS_PROJECT_LEASE_TTL_SECONDS=45
FS_PROJECT_LEASE_HEARTBEAT_SECONDS=15
```

### Cron And Rollup Scheduling

Cron-like automation is configured in two layers:

1. **Per-project cron expression (`cron_expr`)**
   Set in the UI on **Project → Automation** (stored in `scheduled_jobs.cron_expr`).
   The server validates and computes next run time through `core/entrypoints/admin/project_schedules.py` plus the shared cron helpers in `core/application/workflows/serve_workflow_helpers.py`.
2. **Scheduler poll/refresh intervals (env vars)**
   Used by control loops in `core/infrastructure/runtime/control/config.py` and `core/infrastructure/runtime/control/scans.py`.

These advanced runtime knobs live in `.env.example.ops`; most users do not need
them for the first local or production boot.

Recommended env block:

```bash
# Loop ownership in compose:
# - fs-app: CONTROL_LOOPS_ENABLED=0
# - control-plane: CONTROL_LOOPS_ENABLED=1
CONTROL_LOOPS_ENABLED=0

# Scheduled jobs polling (executes due cron rows)
FS_SCHEDULED_JOBS_ENABLED=1
FS_SCHEDULED_JOBS_INTERVAL_SECONDS=30
FS_SCHEDULED_JOBS_BATCH_LIMIT=20

# Dashboard historical aggregation refresh
FS_DASHBOARD_ROLLUP_ENABLED=1
FS_DASHBOARD_ROLLUP_INTERVAL_SECONDS=300
FS_DASHBOARD_ROLLUP_BACKFILL_DAYS=180
FS_DASHBOARD_ROLLUP_LOOKBACK_DAYS=3
FS_DASHBOARD_ROLLUP_READ_ENABLED=1
```

`FS_DASHBOARD_ROLLUP_READ_ENABLED` toggles hybrid reads in `/api/dashboard/summary`:
historical windows from precomputed rollups + current day live data.

Full reference: [docs/GUIDE.md — Environment Variables](../../docs/GUIDE.md#environment-variables-reference).
Generated env catalog: [docs/configuration/flow-steward-env-catalog.md](../../docs/configuration/flow-steward-env-catalog.md).

### Environment files

| File | Purpose |
|---|---|
| `.env.example` | Minimal local-dev quickstart — only what is needed for first boot |
| `.env.example.prod` | Minimal production quickstart — only what is needed to boot the prod stack |
| `.env.example.test` | Optional E2E/UI/Nightfire overlay |
| `.env.example.quality` | Reference for SonarQube / quality variables used by the complete Development preset |
| `.env.example.ops` | Optional advanced runtime overlay: email, webhooks, external auth adapters, control loops, DB tuning, artifact storage |
| `infra/docker/docker-compose.dev.yaml` | Legacy optional local Docker override; normal local selection is the guided `./install.sh` flow |

For local development, use the installer:

```bash
./install.sh --environment development
```

For production, start from the installer-managed production stack:

```bash
./install.sh --environment production
```

The installer generates durable internal secrets, including the backup key,
and never rotates them on resume. After the stack is running, use the shared
readiness gate:

```bash
./flow-steward status
./flow-steward doctor
```

By default, the live gate verifies Compose service state from the Docker host and
then runs internal API, Postgres, RabbitMQ, MCP, and CLI-provider probes inside
the `fs-app` service with `docker compose exec`. Use `--probe-mode host` only
when the current process is already running on the same network boundary as the
runtime services.

Dev mail testing with Mailpit:

- Mailpit UI: `http://localhost:8025`
- SMTP sink: `localhost:1025`
- Mailpit is defined in the **base** compose file (`infra/docker/docker-compose.yaml`),
  not in the dev override. Start just the sink with:

  ```bash
  docker compose --project-name flow-steward --env-file .env \
    -f infra/docker/docker-compose.yaml up -d mailpit
  ```

- **The platform sends mail only through the Outgoing Email extension.** Install
  it, create a connection with host `mailpit`, port `1025`, security `none` and
  no credentials, and both system email (password reset, email verification)
  and workflow notifications land in the sink. See the Email section in
  `docs/GUIDE.md` for an example connection and for the SSRF private-network
  override a Compose host requires. Then open the Mailpit inbox at
  `http://localhost:8025` in a browser.

  Password-reset requests intentionally return the same accepted response for
  existing and unknown addresses; use the Mailpit inbox, not the HTTP response,
  to confirm delivery.
