# Compact and Scalable Production Deployment

Flow Steward ships two production topologies from one release. `compact` runs app, MCP, installer, control, the internal HTTP router, and a bounded worker process pool in one runtime container. PostgreSQL and RabbitMQ remain separate. `scalable` retains separate role containers and scales workers by container replicas (`worker_replicas`). Compact supports exactly one runtime replica and scales its worker processes inside it (`worker_processes`, `FS_WORKER_PROCESSES`); changing it requires recreate or redeploy. The two settings are not interchangeable: each applies to one profile, and setting `worker_replicas` above 1 on Compact is refused.

Every command in this runbook is run from the host through `./install.sh` or
`./flow-steward`, both POSIX shell scripts. On Windows run them from Git Bash or
WSL; the containers they drive are identical on every platform.

## Selecting ingress and TLS

Choose `deployment_profile`, then `ingress_mode`, then the TLS mode owned by that ingress:

| Ingress | TLS choices | Runtime exposure |
|---|---|---|
| `bundled_traefik` | `local`, `self_signed`, `provided`, `acme` | bundled Traefik owns ports 80/443 or configured equivalents |
| `external_traefik` | `external_resolver`, `external_certificate` | services join `FS_TRAEFIK_NETWORK`; unique router/service names use `FS_COMPOSE_PROJECT_NAME` |
| `external_proxy` | `reverse_proxy` | host placement binds loopback ports; Docker placement joins `FS_PROXY_NETWORK` with installation-specific aliases |
| `platform` | `platform_managed` | Compact only; provider sends the public domain to `PORT`/`FS_RUNTIME_HTTP_PORT` over HTTP |

For `external_traefik` in either Compact or Scalable, `traefik_http_entrypoint`
(projected as `FS_TRAEFIK_HTTP_ENTRYPOINT`) must name an HTTP entrypoint already
configured by the external Traefik owner. A nonempty value contributes a
permanent HTTP-to-HTTPS redirect router; explicitly setting this configuration
value empty omits Flow Steward's HTTP redirect router when HTTP is handled
elsewhere. The default is `web`, but use it only if that entrypoint exists; do
not invent an entrypoint on the external proxy. This setting does not alter the
existing HTTPS or MCP routes or take over external TLS ownership.

The application public origin is a scheme and hostname with an optional port and no path prefix. `/mcp` and `/mcp/…` route to MCP; `/mcpx` routes to the app. The installer, metrics ports, database, and broker stay private. Forwarded identity is trusted only from configured proxy CIDRs.

For Nginx or Apache, run `flow-steward proxy-fragment` (add `--json` for the machine-readable form): it renders the fragment from the proxy kind, public host and backend ports this installation already recorded, and prints the backends it assumes. The renderer behind it is `ProxyRenderConfig` in `core.application.lifecycle.proxy_renderer`. Caddy is not a supported `proxy_kind`; an installation that selected it before is read as `nginx`, and its own Caddy keeps running untouched, but the fragment offered will be an Nginx one. The Apache fragment needs `mod_proxy`, `mod_proxy_http` and `mod_proxy_wstunnel`; the last carries the application's WebSocket routes under `/ws/`. Host placement uses `127.0.0.1:<port>` backends. Docker placement uses the selected external network and installation-specific aliases; `localhost` inside a proxy container is never an application backend.

Bundled ACME uses HTTP-01 and requires `public_host`, `FS_ACME_CONTACT`, `FS_ACME_DIRECTORY_URL`, reachable port 80, and DNS pointing at the host. Its account/certificate store is installation-owned and persistent. Use a staging or controlled CA for tests. For `provided`, replace the fullchain and key together only after `config validate` passes. External Traefik, proxy, and platform TLS remain owned by that external system; Flow Steward never imports their keys or overwrites their certificate stores.

## Immutable image input

`compact` is the default profile, and `./install.sh` installs it without any image input: when you supply none, the installer builds the runtime image from this checkout and records the result as `FS_COMPACT_IMAGE`. It stamps the build arguments the runtime verifies, so a self-built image passes the same embedded-identity checks as a supplied one.

Supplying an image is still the right choice for a controlled rollout, and it skips the build. A registry installation uses a digest-pinned reference such as `registry.example/flow-steward@sha256:…`, never a mutable tag. Export these for the installer, which forwards them and records them in `.env.prod`:

```bash
export FS_COMPACT_IMAGE_REGISTRY_REF=<repository>@sha256:<oci-index-or-manifest>
export FS_COMPACT_IMAGE_REVISION=<40-character-git-revision>
export FS_COMPACT_IMAGE_PLATFORM=linux/amd64
```

For an image already built on the install host, export `FS_COMPACT_IMAGE=<local image ID>` instead; the installer resolves its digests and revision itself. Every other `FS_COMPACT_IMAGE_*` value is derived, never supplied.

### Building the Compact image

The `compact-runtime` stage bakes its own provenance, and both build arguments default to `unknown`. `FS_IMAGE_PLATFORM` is a manual argument — BuildKit's automatic `TARGETPLATFORM` is not substituted for it. An image built without them is accepted by the installer and then rejected by the runtime, which exits `78` (`EX_CONFIG`) and restarts, *after* configuration, secrets, the TLS certificate and the database schema have been written:

```
Compact configuration error: embedded image revision does not match release metadata
```

Build it with both arguments set, matching the platform you install on:

```bash
docker buildx build --target compact-runtime \
  --build-arg BUILD_SHA="$(git rev-parse HEAD)" \
  --build-arg FS_IMAGE_PLATFORM=linux/arm64 \
  --tag flow-steward-compact:"$(git rev-parse --short HEAD)" \
  --file infra/docker/Dockerfile --load .
```

Then export the resulting local image ID as `FS_COMPACT_IMAGE`, as above. Do not override `org.opencontainers.image.revision` with `--label`: a command-line label replaces the one from the Dockerfile while `FS_EMBEDDED_IMAGE_REVISION` keeps its build-argument value, and the two then disagree silently.

The host installer resolves the OCI index, selects exactly one requested platform manifest, verifies the manifest and config digests, pulls it, validates the revision and `compact-runtime` role labels, and passes the real local config/image ID into the existing exact-prebuilt Compose path. It retains registry reference, index digest, platform manifest digest, config digest, revision, role, and platform as separate metadata. Inside a provider runtime, only embedded revision/role/platform are independently available; the provider deployment digest belongs in external release evidence.

## Provider-managed ingress contract

The provider must supply one persistent runtime volume at `/data`, compatible PostgreSQL 17 with AGE/pgvector, RabbitMQ, durable secrets, a pre-start or console execution facility that mounts the same volume, one public HTTP route to the configured runtime port, and a shutdown grace period of at least 180 seconds. Map the provider port variable to `PORT`; no provider brand detection or API token is required by runtime code.

Run bootstrap from a one-shot console command with the same image, network, secrets, and `/data` mount:

```bash
python -m core.entrypoints.cli.platform_runtime bootstrap
python -m core.entrypoints.cli.platform_runtime status --json
python -m core.entrypoints.cli.platform_runtime doctor --json
```

### Secrets without an installer

The same path produces no long-lived secrets either, and the first one missing is
fatal on the first action rather than at start: a workflow launch can fail
with `ENCRYPTION_KEY is not set; encrypted secret storage is unavailable`. The
runtime therefore establishes them itself, once, before anything can write an
encrypted value.

Which secrets is derived rather than listed: every leaf secret the installer
generates that some Compact role is allowed to inherit, minus the two an external
service owns. Today that is `ENCRYPTION_KEY`, `FS_SECRET_KEY`, `SECURITY_PII_SALT`
and `FS_METRICS_TOKEN`. `POSTGRES_PASSWORD` and `RABBITMQ_PASS` are excluded because
a generated value would authenticate against nothing -- supply them inside `FS_DB_DSN`
and `RABBITMQ_URL`. `FS_BACKUP_ENCRYPTION_KEY` is not included because backups run in
a separate one-shot container that reads its own environment; supply it there.

Unlike the setup link these must survive a restart, so they are written once to
`/data/state/runtime-secrets.json` (mode 0600, read by the supervisor alone) and read
back on every later start. A key regenerated on a second start would not fail, it
would make everything written under the first one unreadable.

Anything the operator supplies always wins and is never copied into that file; only a
fingerprint of it is recorded. That fingerprint decides the one case the runtime
refuses: an installation that ran with an operator-supplied key and then starts
without it stops, rather than continuing under a fresh key that cannot read what is
already stored. Supply the key again, or remove its entry from the state file to
accept the loss deliberately. A key the operator *changes* is reported and honoured,
because rotation is a supported operation.

One line names what happened, with no values in it:

```
Runtime secrets: generated=ENCRYPTION_KEY, FS_METRICS_TOKEN, FS_SECRET_KEY,
SECURITY_PII_SALT; restored=none; supplied=none.
```

### First administrator without an installer

Nothing in this path runs `./install.sh`, so nothing mints the one-time setup token that `POST /api/auth/bootstrap` requires from a non-loopback caller. The runtime therefore mints one for itself at start, but only while local authentication is enabled, bootstrap has not been switched off, no setup token is configured for this installation, and the installation has no administrator yet. It writes the same owner-only state file the request path already validates against, names it for the application process, and prints one line to the container log:

```
Flow Steward has no administrator yet. Open this one-time setup link before <expiry> to create the first one, and treat it as a secret: https://<your host>/bootstrap#setup-token=<token>
```

The link carries the token in a fragment, so it never reaches a server log as a query parameter, and the log line holds the only copy: the state file stores a hash. It lives for 15 minutes rather than the installer's hour because a container log is shipped and retained where a terminal is not. If it expires, or was never seen, restart the runtime: while no administrator exists a new link is minted on every start, and the previous one stops working. Set `FS_PUBLIC_BASE_URL` to get an absolute link; without it the line carries the path and fragment alone.

`FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY` keeps its default throughout. An installation that self-minted is reachable only with the token, which is a stricter gate than the localhost rule, not a looser one.

Public readiness is checked separately through the HTTPS domain after DNS and certificates are ready. Internal runtime readiness never depends on public DNS.

## Lifecycle command matrix

| Operation | Docker host | Provider console/job |
|---|---|---|
| install/bootstrap | `./install.sh` with the selected canonical config | `python -m core.entrypoints.cli.platform_runtime bootstrap`, as a one-shot job. It stays on the long form: the in-image `flow-steward` reads the runtime secrets the first start writes, so it cannot run before that |
| status/doctor | `flow-steward status`, `flow-steward doctor` | `flow-steward status`, `flow-steward doctor` (add `--json` for the machine-readable form). The provider's container has no Docker, so `doctor` shows the configuration checks and names each installation check it could not run, and why; `doctor --fix [ids]` names each repair it cannot make there. Exit 5: the installation was not checked; 4: a configuration problem |
| worker capacity | Scalable: `./flow-steward config set --worker-replicas N` then `./flow-steward config apply --confirm` (fs-worker containers; kept by every later apply, rollback and update). Do not use `docker compose --scale`: the next `compose up` returns fs-worker to the recorded count, and `config apply` and `./install.sh` refuse to run over a count that was not recorded. Compact: `./install.sh --worker-processes N` (processes inside the one runtime container), at install time and on an installed Compact alike: it records N, recreates the runtime, waits until exactly N workers have registered and prints `Compact worker processes: 2 -> N`. The same N changes nothing and recreates nothing; a run killed half-way is finished by the next `./install.sh`. It is the one setting `./install.sh` changes on an installed system: any other flag that differs from the applied configuration is refused by name (`installed_setting_not_applied`), never ignored | change Compact env and redeploy; overlapping Compact replicas are unsupported |
| maintenance | `flow-steward maintenance enable|disable --confirm` | `flow-steward maintenance enable|disable --confirm` |
| schema status | `flow-steward database status` | `flow-steward database status` |
| upgrade | maintenance, verified backup, then `flow-steward database upgrade --maintenance-confirmed --author <name> --confirm` | `flow-steward database upgrade --maintenance-confirmed --author <name> --confirm` |
| backup | `flow-steward backup create|list|verify` | `flow-steward backup create|list`; verify with `flow-steward backup verify <path>` |
| restore | stop writers, verify/decrypt the protected artifact, use the native `FilesystemBackupGateway.restore_database` transport with an exact recovery target, then run parity/readiness checks | not supported; restore from the provider's own database backups (see "Operating a platform deployment" below) |
| uninstall | existing installer uninstall for owned Docker resources | remove provider services only after exporting backups and confirming external storage ownership |

In the provider column, `flow-steward` is the command the Compact image carries, run in the provider's console for the runtime service; it is not the host launcher `./flow-steward`. Host-only Docker commands fail when no Docker host exists; provider operations use the fixed dispatcher and existing application services. No runtime Docker socket, shell control plane, or anonymous maintenance endpoint is required.

### Operating a platform deployment

A platform deployment (Railway, for example) is a supported way to try Flow Steward, build and debug workflows, and run small installations. It runs the same Compact image, and the product works there in full. What differs is how you operate the installation.

**The console is the only management path.** The host launcher `./flow-steward` needs a host, and a platform has none. The Compact image therefore carries its own `flow-steward` command: open the provider's console for the runtime service and run it there, with the same two-word commands as on a server, for example `flow-steward maintenance status`. `flow-steward --help` lists what this deployment can run. Commands that need host Docker or installer tooling (`config`, `stack`, `environment`, `release`, `quality`, `testing`, `extensions install|uninstall`) are refused by name. Install extensions through the web UI or the API instead.

Over HTTP, the product offers only reads and reports: `GET /api/system/maintenance`, the monitoring summary and storage reports, and storage orphan cleanup. No HTTP endpoint creates a backup, upgrades the schema, or turns maintenance mode on or off. If the provider gives you no console into the running service, you cannot run any of these operations on that deployment.

What that means in practice:

| Task | On a platform |
|---|---|
| Health, readiness | `flow-steward status` and `flow-steward doctor` in the console; the public `/health` route from outside |
| Maintenance mode | `flow-steward maintenance enable --confirm`, then `flow-steward maintenance disable --confirm`. The web UI shows the state but cannot change it |
| Schema status and upgrade | `flow-steward database status`; then, in maintenance and after a backup, `flow-steward database upgrade --maintenance-confirmed --author <name> --confirm` |
| Backup | `flow-steward backup create` needs `FS_BACKUP_ENCRYPTION_KEY` in the service variables. It writes to `/data/backups`, the same volume as the data it protects, so copy each backup off the volume. Keep the provider's own database backups as well |
| Restore | Not supported; see "Update an installation to new sources" below. Restore from the provider's database backups |
| Profile switch, `./install.sh`, `config`, `stack` | Not available: there is no Compose project and no second profile |

**When to move to a server.** If you need a supported restore, or maintenance without a provider console, run the installation on a server with `./install.sh`. Decide this before the platform installation holds data you depend on: moving an installation from a platform to a server has not been verified.

## Upgrade and profile switch

### RabbitMQ transport policy rollout

The [RabbitMQ transport decision](../architecture/adr/0043-rabbitmq-transport-trust-boundary.md)
uses verified TLS by default. An existing explicit plaintext `RABBITMQ_URL`
outside the declared local deployment context is refused after upgrade. There is
no automatic downgrade or URL rewrite. The authoritative setting and exact local
endpoint constraints are in the [configuration reference](../user/configuration-reference.md#rabbitmq-transport).

Before replacing runtime images:

1. Inspect the effective broker endpoint and transport setting for every queue and
   heartbeat client using the deployment's existing configuration owner. Keep URL
   userinfo and credentials out of terminal transcripts and diagnostic reports.
2. For an owned canonical Compose broker, retain the generated deployment context
   and exact endpoint. Verify that the rendered production configuration does not
   publish broker ports and that any development publication stays on numeric
   loopback. An override pointing to another host requires TLS; a local setting
   does not authorize a remote plaintext URL.
3. For a remote broker, provision its TLS listener and a certificate identifying
   the canonical URL host. Configure `amqps` with the correct listener port. If a
   private CA is required, mount its bundle read-only into every affected runtime
   and reference it through the existing client `ssl_options` configuration.
   Preserve the intended vhost and credentials; do not assume changing the scheme
   also changes an explicitly configured port. Never disable certificate or
   hostname checks or substitute a different TLS peer name.
4. Apply configuration and images through the existing deployment lifecycle and
   maintenance procedure. Run deployment doctor in that same effective context.
   A transport refusal is a configuration failure; fix the endpoint or trust
   material before proceeding. Doctor's TCP reachability check alone does not
   validate the TLS handshake or broker login.
5. Before accepting the rollout, verify runtime readiness, an authenticated worker
   broker connection and fresh heartbeat ingestion for the same installation.
   Confirm that queue delivery and acknowledgement still work in the deployment's
   normal acceptance check. Preserve queued work and broker volumes throughout.

If these checks fail, keep the deployment in its existing maintenance/recovery
procedure. Do not bypass the boundary with insecure TLS options, an arbitrary
local mode, or an unreviewed plaintext endpoint.

### Linux host autostart

On Linux with systemd and the local system Docker daemon, a successful Compact
installation configures `flow-steward-<compose-project>.service`. This root-owned
oneshot unit runs Docker actions as the deployment owner. It starts the exact
recorded container IDs after host boot or Docker restart; it does not pull, build,
migrate or choose a different image. Infrastructure starts first and waits for
bounded health checks before the runtime starts.

Docker remains the only automatic container-failure retry owner. The unit has
`Restart=no` and is bound to `docker.service`; it is not another failure loop.
Setup requires sudo once and when the helper/container record changes. Platforms
without systemd or with another Docker endpoint are skipped with an explanation.

Inspect the installer-reported unit name, then use `systemctl status <unit>` or
`journalctl -u <unit>` for diagnosis. `sudo systemctl disable --now <unit>` disables
boot activation and stops only the recorded installation. Re-enable with
`sudo systemctl enable --now <unit>`. Later installer runs preserve an explicit
disable. Missing/replaced IDs or ownership mismatches require rerunning the
installer, never starting guessed replacement containers. Switching to Scalable
retires only that checkout's Compact unit and preserves shared services.

### Switching profile

Switch an installed system between the profiles with `./install.sh`, from its checkout, in either direction. The data stays where it is: artifacts, extensions and the test project are mounted by the target profile where the source profile left them -- Scalable's host directories, or the Compact data volume -- and nothing is copied.

```bash
./flow-steward maintenance enable --confirm
./flow-steward backup create
./install.sh --deployment-profile compact     # or: --deployment-profile scalable
./flow-steward maintenance disable --confirm
```

What the run does, and says in its result:

- It builds the target profile's images, then switches with the lifecycle's own journaled `config apply` (preserving the database, broker, volumes and keys, rolling back on failure).
- Scalable to Compact: Compact's roles reach the host directories through fixed groups, so their entries get those groups and modes first -- 10100 for artifacts, 10103 for extensions (`02770`/`0660`, and `02775`/`0664` for extensions). Owners do not change; symbolic links are not followed; private entries (`0600`, `0700`) and what is under a private directory stay as they are. The result lists what it changed and what it left alone.
- If the host already has a group with one of those gids, the switch is refused before anything changes, naming the group: its members would read the data. Remove or renumber it -- or, to give host users access on purpose, name it `flow-steward-artifacts` or `flow-steward-extensions` (`groupadd -g 10100 flow-steward-artifacts`, then `usermod -aG` the users). Only the host's `/etc/group` is checked; a group known only to a network directory service is not seen.
- Compact to Scalable: the Scalable services mount the Compact data volume by subpath.
- Bundled object storage (SeaweedFS) runs beside either profile on the same volumes, so an installation that stores its files there keeps them across the switch. It is never published: only the runtime joins its internal network.
- Interrupted anywhere -- a dropped session, `kill -9` -- rerun `./install.sh`: the preparation repeats harmlessly, and an unfinished switch continues from its journal, whatever arguments the rerun has.

A provider deployment without Compose (Railway, for example) runs the Compact image on one volume and has no switch: it has neither `./install.sh` nor a second profile.

To update an installation within its profile, see "Update an installation to new sources" below.

### Advanced: switching with `config apply` and copying the trees

The earlier procedure switches with `./flow-steward config apply` from a Scalable installation and copies the trees into the target profile's own place instead of keeping them. It remains for installations that want the data moved.

1. Record the active config, immutable image identities, application/PG/RabbitMQ versions, storage backend, and ingress/TLS ownership.
2. Enable maintenance and wait for admission to stop and admitted jobs to finish or reach the existing interrupted/recovery state.
3. Create and verify a PostgreSQL backup. Stop RabbitMQ for its consistent volume snapshot, preserve that snapshot, and then restart the same PostgreSQL and RabbitMQ services with the same images and volumes. Verify both retained services before continuing. Copy `/data` or the mapped Scalable extensions, artifacts, state, and backups according to the data registry. Export provider-managed secrets separately.
4. Keep maintenance enabled and stop only the old application runtimes and writers. Keep the retained PostgreSQL and RabbitMQ services running for `config apply`. Never run Compact and Scalable workers together against the same broker/database/storage.
5. Keep the same database, broker, encryption keys, artifact backend, bucket/endpoint, project paths, public origin, ingress network/entrypoints/resolver, and external certificate owner. Apply only the explicit `deployment_profile` change.
6. Start bootstrap/upgrade once, then start the target runtime. Verify schema, internal readiness, worker count, extensions, workspaces, artifact hashes, credentials, queued/approval job states, public login/API/MCP/WebSocket/streaming/webhook paths, and TLS certificate.
7. Disable maintenance. Retain the backup and old immutable image identity until the verification window closes.

`config apply` validates source storage after step 4 with a disposable `docker compose run --rm --no-deps` container from the selected source service. It reuses that service's image, environment, networks, and storage mounts without starting dependencies or workers; Compact runs the probe as app UID 10001 with artifact GID 10100. Compact-to-Scalable switching exports the stopped Compact lifecycle state through the same disposable Compose mechanism rather than requiring the Compact runtime container to be running. A failed read/write/delete probe remains a preservation failure and reports `storage_connectivity` in the failed fields.

If an apply reports `RECOVERY_REQUIRED`, run `flow-steward config apply` again before changing the journal or pending configuration. The command first restores the exact saved topology, image identities, configuration artifacts, and preservation manifest. A primary application service that was stopped in the captured baseline remains stopped during recovery; the disposable source probe still verifies its storage. Successful recovery returns `runtime_restored=true`, retains the pending patch, and closes the failed operation. Restart or verify the retained PostgreSQL and RabbitMQ services according to step 3, then retry the same pending profile switch.

The reverse procedure is identical. A schema-incompatible rollback requires restoring the matching data backup. Profile switching does not migrate S3 objects to local storage, change database/broker major versions, or alter the business schema.

## Update an installation to new sources

Both profiles update through the installer, from the updated checkout:

```bash
git fetch && git checkout <release tag>
./install.sh
```

The installer compares three things: the sources, the images it recorded, and what is actually running. When the images it built are older than the sources, it rebuilds them, has the new image check the database schema, and only then restarts the runtime on it. Configuration, secrets, TLS and the initial preservation manifest are left as they are. The result says `Updated from <old revision> to <new revision>.`; a rerun on an installation that is already current still says it is already installed.

What follows depends on whether the release changes the database, and the installer tells you which case you are in.

**A code-only update** — no pending migration — is complete at that point. The result adds that the schema check passed, so the update carried no database migration and needed no backup.

**An update that carries a database migration** stops at the schema step with `schema_baseline_failed` and output saying the migration history does not match this release. The installer applies no migration and takes no backup itself. The running application was not restarted and stays on its previous version and its previous schema. Then:

```bash
./flow-steward maintenance enable --confirm
./flow-steward backup create
./flow-steward database upgrade --maintenance-confirmed --confirm
./install.sh
./flow-steward maintenance disable --confirm
```

The second `./install.sh` finishes the update the first one prepared. It reports the update and does not claim there was no migration, because this one had one.

Between the stopped update and that second run, `.env.prod` already names the new image while the old one is still running. Do not start the runtime by any other means in that window — a bare `docker compose up`, for example — because that would put the new code on the unmigrated database. `./install.sh` and `./flow-steward` do not.

**Going back to the previous release** is `git checkout <previous revision>` and `./install.sh`. While the previous release's images are still in Docker they are recorded again without a rebuild ("Reusing the images built for …"); otherwise they are built again, and the log says so.


**Going back across a database migration** is refused before anything restarts: the older release does not know a migration the database already has, and `./install.sh` stops with `database_newer_than_release`, naming it. The newer release keeps running. Either check out the newer release again and rerun `./install.sh`, or return the database to the backup taken before that migration (the steps above take one). On Scalable:

```bash
./flow-steward maintenance enable --confirm
./flow-steward backup list          # the backup taken before the newer release's migration
./flow-steward stack stop --confirm
(
  # Both values are in the installation's .env.prod; the subshell keeps them out of
  # the ./install.sh that follows.
  export FS_COMPOSE_PROJECT_NAME=<the installation's Compose project>
  export FS_BACKUP_ENCRYPTION_KEY=<the installation's backup key>
  core/entrypoints/cli/scripts/db_restore.sh backups/<that backup> \
    --target-compose-project "$FS_COMPOSE_PROJECT_NAME" --confirm
)
git checkout <the older revision>
./install.sh
./flow-steward maintenance disable --confirm
```

On Compact, where `stack` is refused, the restore script stops the runtime itself.
The host management command `./flow-steward backup create` stores its encrypted
artifact and verification metadata in the installation's host `backups/` directory,
using the installed Compose database target and backup key. It does not access or
change the private volume directory. The embedded/provider-native Compact commands
still keep their backups in `/data/backups`; those are a separate store, with the
same protected archive format.

Doctor observes both stores when the runtime is running, qualifying their names as
`host:backups/<name>` and `compact:/data/backups/<name>`. These prefixes identify the
store, not CLI paths. With the runtime stopped it can still report freshness of a
verified host backup; that does not prove the native store is empty or fully checked.
Without a verified host backup it reports that the native check is blocked.

To restore a backup made through the host management command:

```bash
./flow-steward maintenance enable --confirm
./flow-steward backup list          # the backup taken before the newer release's migration
(
  # Both values are in the installation's .env.prod; the subshell keeps them out of
  # the ./install.sh that follows.
  export FS_COMPOSE_PROJECT_NAME=<the installation's Compose project>
  export FS_BACKUP_ENCRYPTION_KEY=<the installation's backup key>
  # <that backup> is the host-relative path listed by backup list: backups/<name>.dump.enc
  core/entrypoints/cli/scripts/db_restore.sh <that backup> \
    --target-compose-project "$FS_COMPOSE_PROJECT_NAME" --confirm
)
git checkout <the older revision>
./install.sh
./flow-steward maintenance disable --confirm
```

`db_restore.sh` checks the backup before anything is changed, then stops every service that writes to the database and is still running (`compact-runtime` on Compact; `fs-app`, `fs-worker`, `control-plane`, `fs-mcp` and `fs-extension-installer` on Scalable), starts the database alone and replaces it from the backup. What it stopped it leaves stopped, and says so: `./install.sh` starts it again and verifies the installation, which the script cannot. It is deliberately not part of the public CLI.

For an existing embedded/native volume backup, first copy the exact encrypted
artifact from `/data/backups` into host `backups/` while the runtime is available,
then use its host-relative path with the same restore procedure. For example,
`docker cp "$FS_COMPOSE_PROJECT_NAME-compact-runtime-1:/data/backups/<name>.dump.enc" backups/`.
The installed backup key must match that artifact. Do not relax the volume's private
permissions to let the host CLI read it; copied archives are not assumed verified
merely because an archive with the same basename exists in the other store.

**Restoring a backup on the same release** is the same set of steps without the checkout. `./install.sh` on the release that is already installed brings the stopped services back up ("Started the stopped services: …"), then verifies health as usual. On Scalable:

```bash
./flow-steward maintenance enable --confirm
./flow-steward backup list
./flow-steward stack stop --confirm
(
  # Both values are in the installation's .env.prod; the subshell keeps them out of
  # the ./install.sh that follows.
  export FS_COMPOSE_PROJECT_NAME=<the installation's Compose project>
  export FS_BACKUP_ENCRYPTION_KEY=<the installation's backup key>
  core/entrypoints/cli/scripts/db_restore.sh backups/<that backup> \
    --target-compose-project "$FS_COMPOSE_PROJECT_NAME" --confirm
)
./install.sh
./flow-steward maintenance disable --confirm
```

On Compact:

```bash
./flow-steward maintenance enable --confirm
./flow-steward backup list
(
  # Both values are in the installation's .env.prod; the subshell keeps them out of
  # the ./install.sh that follows.
  export FS_COMPOSE_PROJECT_NAME=<the installation's Compose project>
  export FS_BACKUP_ENCRYPTION_KEY=<the installation's backup key>
  # <that backup> is the host-relative path listed by backup list: backups/<name>.dump.enc
  core/entrypoints/cli/scripts/db_restore.sh <that backup> \
    --target-compose-project "$FS_COMPOSE_PROJECT_NAME" --confirm
)
./install.sh
./flow-steward maintenance disable --confirm
```

The same `./install.sh` also brings back a stack stopped for any other reason, such as a host reboot without restart policies. `./install.sh` and the lifecycle commands (`config`, `stack`, `maintenance`, `backup`, `database`) never run at the same time: whichever starts second is refused and names the one that is running.

**On a provider without Compose** (Railway, for example) there is no supported restore. `db_restore.sh` drives a Compose project: it stops the runtime, starts the database alone, decrypts the backup inside the runtime image, and after `pg_restore` repairs the graph catalogue, whose ids a dump does not keep. None of that has a Flow Steward command on a provider, and a hand restore that skips the graph repair leaves graph search broken. Keep the provider's own database backups for that deployment; this path is unverified.

**Images you pinned yourself** are never rebuilt underneath you. If they are behind the sources, `./install.sh` says so rather than calling the installation current, and leaves them in place. To move them, publish images built from the new sources and redeploy them as below.

## Redeploy new images on the same profile

This is the path for images you publish yourself, on Scalable. On Compact, `config` and `stack` are refused (they need a retained CLI container the profile does not have); update Compact as described above.

Publishing a new image and re-pinning a running installation is an ordinary redeploy, not a profile switch. `stack start` deliberately preserves the running container image identity, so a plain `stack start` after re-pinning fails closed and names the observed transition (`fs-app was sha256:…, now sha256:…`). Declare the change instead:

```bash
./flow-steward maintenance enable --confirm
./flow-steward backup create
# Update the pinned references in the installed durable environment, for example
# FS_APP_IMAGE / FS_APP_IMAGE_DIGEST / FS_WORKER_IMAGE / FS_COMPACT_IMAGE.
./flow-steward config validate
./flow-steward stack start --confirm --adopt-image-pins
./flow-steward maintenance disable --confirm
```

`--adopt-image-pins` records the declared intent in the operation journal and its plan preview. It relaxes nothing else: every managed service that moved must land on exactly the image identity the selected Compose model resolves, and the full preservation comparison (project and volume identities, database identities, counts, schema, decryptability, artifact storage connectivity, readiness) still has to pass. A service that moved to anything other than a configured pin is still a preservation failure and the prior runtime is restored.

Keep the previous immutable image in the local image store until the verification window closes. It is what an unsuccessful redeploy is rolled back to.

### If a redeploy reports `RECOVERY_REQUIRED`

Run the recovery command the failure prints, which for a start is:

```bash
./flow-steward stack start --confirm
```

Recovery restores the exact recorded image identity for every service that drifted, restores the exact prior running/stopped topology, re-verifies readiness and preservation, and closes the operation. Every attempt — successful or not — is recorded in the operation journal under `step_outputs.recovery` (`attempts`, `last_attempt_at`, `last_error_code`) together with a refreshed `log_path` and `updated_at`, so a repeat is visible to support.

Recovery pins the recorded images under temporary `flow-steward-rollback:<digest>` tags and writes `<operation>.rollback.compose.json` while it recreates the drifted services. Both are released only after every post-recovery check has run, then removed. A container that recovery recreated keeps Compose's own `com.docker.compose.project.config_files` label naming that override until it is next recreated; Flow Steward never reads that label, but a bare `docker compose` command that does will not resolve the released tags. Recreate the container, or pass the compose files explicitly, before driving the installation with Docker Compose directly.

Recovery cannot succeed if the recorded prior image has been removed from the local image store. The failure names the missing identity and offers two next actions: restore that exact image and rerun the recovery command, or run `./flow-steward stack start --confirm --adopt-image-pins` to adopt the currently configured pins. `maintenance disable` stays blocked with `maintenance_unfinished_operations` until the operation is closed by one of them.

## Comparing deployment profiles

Use this method for an explicitly authorized comparison under
[ADR 0029](../architecture/adr/0029-compact-and-scalable-deployment.md#validation-and-consequences).
It describes how to collect evidence, not a measured result or permission to run
a benchmark.

1. Freeze the pre-wiring baseline SHA, image digests, workload definitions, job
   counts, durations, and resource limits before changing candidate deployment
   wiring. If no recoverable frozen baseline exists, report that limitation;
   do not fabricate one or substitute old release results.
2. Compare the baseline with both candidate profiles at N=1/2/4 where supported.
   Keep the same total app-family CPU/RAM budget, PostgreSQL/RabbitMQ versions and
   configuration, data, and HTTP worker settings. Comparing only the two
   candidates can hide a regression shared by both.
3. Use deterministic I/O-bound work with a controlled API fixture, CPU-bound work,
   and a long workflow with an extension subprocess. For each workload, warm up,
   then perform at least three measured runs with the same fixed jobs, durations,
   and limits.
4. Report every run's throughput, p50/p95 latency, app-family and per-role peak
   memory and CPU, database connections, errors/retries, and terminal job states.
   Retain failed and OOM outcomes before changing resources. If the budget is
   increased, increase it equally for both sides and retain the original results.
5. For concurrency and recovery validation, use real job/message/lease evidence
   with controlled barriers or events, not arbitrary sleeps. Bound waits by the
   recorded runtime/lease budgets. Do not repeat a full Cartesian matrix of
   proxy, TLS, storage, and worker-count combinations without a specific
   dependency or regression that requires it.

Report unexplained regressions separately with a reproducible scenario. More
workers do not create CPU/RAM or override project concurrency caps. Do not infer
a performance SLA, minimum-RAM recommendation, speedup, or measured outcome from
this method, and do not optimize core merely to improve a comparison. Keep local,
provider, and exact-candidate release evidence separate.

## Backup consistency

The supported backup is a maintenance backup with writers stopped. It consists of a verified encrypted PostgreSQL custom-format dump, application-owned persistent files, the stopped RabbitMQ volume or provider-native consistent snapshot, bundled ACME state when selected, and separately exported secrets. A PostgreSQL dump alone is not a complete installation backup, and RabbitMQ definitions do not preserve messages.

After restore, use the same app/PG/RabbitMQ versions, verify extension identities and active releases, artifact and workspace hashes, credential decryption, queued/running/approval recovery states, and the public endpoint. External proxy and certificate stores are inspected, not overwritten.

## Retired extension release maintenance

Extension updates deliberately keep the previous immutable release so an invocation that already selected it can finish. Before installing or updating an archive, the installer checks both the archive limits and the temporary filesystem capacity. The host installer also requires at least 4 GiB free before changing a deployment. Monitor the persistent extension filesystem separately:

```bash
df -Pk /data/extensions
du -sk /data/extensions/.releases 2>/dev/null || true
```

Run cleanup only in a maintenance window with a verified backup. Enable maintenance, wait for admitted work to drain, and stop every app, worker, MCP, control, and installer process that can start an extension invocation. The following query reports release rows that no install or OAuth authority still references. Foreign-key enforcement makes the delete fail closed if another protected reference remains.

```bash
psql "$FS_DB_DSN" -v ON_ERROR_STOP=1 -At -F $'\t' <<'SQL' > /tmp/flow-steward-retired-releases.tsv
WITH candidates AS (
  SELECT r.release_id,
         r.descriptor_jsonb ->> 'bundle_root' AS bundle_root
  FROM public.extension_releases AS r
  WHERE NOT EXISTS (
    SELECT 1 FROM public.extension_installs AS i WHERE i.release_id = r.release_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM public.extension_oauth_configurations AS o
    WHERE o.extension_release_id = r.release_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM public.extension_oauth_authorization_states AS o
    WHERE o.extension_release_id = r.release_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM public.project_connections AS c
    WHERE c.oauth_extension_release_id = r.release_id
  )
)
SELECT release_id, bundle_root
FROM candidates
WHERE bundle_root LIKE '/data/extensions/.releases/%/%'
ORDER BY release_id;
SQL
cat /tmp/flow-steward-retired-releases.tsv
```

Review the exact IDs and paths. For each path, confirm it is a physical directory below `/data/extensions/.releases`, and confirm no active symlink resolves to it:

```bash
candidate=/data/extensions/.releases/<extension>/<release>
test -d "$candidate" && test ! -L "$candidate"
for active in /data/extensions/*; do
  test -L "$active" || continue
  test "$(readlink -f "$active")" != "$(readlink -f "$candidate")" || exit 1
done
```

Delete the reviewed release row through a transaction first; a remaining protected database reference aborts the transaction. Then remove only the matching reviewed physical directory:

```bash
psql "$FS_DB_DSN" -v ON_ERROR_STOP=1 -v release_id='<reviewed-release-id>' <<'SQL'
BEGIN;
DELETE FROM public.extension_artifact_snapshots WHERE release_id = :'release_id';
DELETE FROM public.extension_releases WHERE release_id = :'release_id';
COMMIT;
SQL
rm -rf -- "$candidate"
```

Restart the selected profile, verify extension inventory and a new invocation, then disable maintenance. Never run this procedure while a runtime role is alive; the database has no distributed invocation refcount by design.
