# Installation Clone runbook

Installation Clone creates one encrypted point-in-time archive and destructively
replaces a preinstalled target with its allowlisted Flow Steward product state.
The source and target must use the same deployment profile, the same data placement,
and a compatible Linux CPU platform. Clone preserves the target deployment shell
and imports the source release and product data; it never merges the two installations.

Clone is official-only: both the source and target must have an official
installed identity. A completed local build is a source installation and must
be transitioned explicitly before Clone is composed; Clone never performs an
implicit update.

## Safety boundary

Clone requires fully enabled **manual** maintenance on both sides. It does not
enable or disable maintenance, back up the target, transfer the archive, change
DNS, switch traffic, or contact the source server during restore.

The target-local shell keeps only its installation/Compose identity, public host
and ports, ingress/proxy and TLS ownership, Docker network names, host UID/GID,
filesystem locations, and PostgreSQL server-role credentials. Restore replaces
the allowlisted database, broker, local or bundled artifact state, extensions,
durable application state, source-owned secrets, and installed release identity.
RabbitMQ user/password credentials travel with the broker state; its runtime
connection URL is regenerated from those credentials. PostgreSQL server-role
credentials stay target-local because the database is restored logically.
Source-code checkouts and task worktrees are not installation state and are not
copied. Unrelated containers, volumes, accounts, websites, and filesystem roots
are outside the replacement plan.

Deployment-local `secret-input` and `secret-output` directories are not archived
or imported. The target preserves its own files in these directories, including
`.flow-steward/secret-input/github-release-token`, if configured, and continues
to use that deployment-local credential for later release checks and upgrades.
These names are reserved throughout durable state; they are not a portable
application-secret store. Source-owned application secrets selected by the
configuration catalog still use the separate encrypted Clone secret payload.
An older archive containing these local credential paths is rejected before
target replacement; recreate it with the corrected capture policy.

External S3 objects are not copied. Their source endpoints, references, and
credentials remain application data, so the restored installation can still
address the same external service. Keep the source in maintenance until the
target is validated to prevent two writers.

## Prerequisites

- Source and target are successful official-image installations.
- Both are the same profile: Compact-to-Compact or Scalable-to-Scalable.
- Both use the same data placement: `host_directories` or `compact_volume`.
  Clone does not convert between these archive layouts. A mismatch returns
  `clone_restore_data_placement_mismatch` during preflight, before target replacement.
- Both use compatible `linux/amd64` or `linux/arm64` platforms.
- The common exact-release resolver can materialize the archived `vX.Y.Z`
  identity and its complete digest-pinned image set from one configured registry.
  Clone does not perform a second GitHub discovery flow.
- Manual maintenance is fully enabled and no installer/lifecycle operation is
  unfinished.
- Private staging and target filesystems have enough free space.
- The operator has a separate rollback Clone of the target when full target
  rollback is required. Clone never creates one automatically.

Run `./flow-steward maintenance status`, `./flow-steward release info`, and
`./flow-steward doctor` on each installation before starting.

If either installation reports source mode, upgrade both the source and target
independently before enabling Clone maintenance:

```bash
./flow-steward release check
./flow-steward backup create
./flow-steward maintenance enable --confirm
./flow-steward release upgrade --confirm
./flow-steward doctor
./flow-steward maintenance disable --confirm
```

For a private release repository, each installation uses its own owner-only
`.flow-steward/secret-input/github-release-token` only for that check/upgrade.
The credential is not copied by Clone; remove it after it is no longer needed.

## Create the archive

On the source:

```bash
./flow-steward maintenance enable --confirm
./flow-steward maintenance status
./flow-steward clone create
```

The passphrase prompt is not echoed. Automation must stream one non-empty line:

```bash
secret-command | ./flow-steward clone create --passphrase-stdin --confirm
```

Do not place the passphrase in an option, environment variable, environment
file, log, or redirected JSON result. Transfer the single
`flow-steward-clone-<UTC>-<profile>-<version>.fsclone` file using an
operator-controlled encrypted channel. Clone provides no upload/download or SSH
transport.

Creation verifies the installed official release, captures a PostgreSQL custom
dump, briefly stops only state-owning broker/bundled-storage services, records
their prior running state, restores that exact state on success or failure, and
publishes the archive atomically. The source remains in maintenance. Plaintext
staging and incomplete output are not deliverables.

Directory metadata preserves authenticated setgid and sticky bits, including the
broker volume's standard `1777` root. Private staging directories remain `0700`;
setuid directories and special mode bits on regular files or symlinks are rejected.

Host-directory restore retains each mounted directory root and publishes its
children through a confined disposable helper. It does not rename a bind-mount
root or require write access to unrelated parent directories. Host filesystem
ownership is normalized to the retained target UID/GID; Compact volume metadata
continues to follow its separate runtime ownership policy.

On every installation, `backups/<name>.fsclone` is the confined path visible to
the management container. For Scalable, place the host file in the configured
`FS_BACKUPS_DIR` (the default is the repository `backups/` directory). On
Compact installations, `backups` is a host-visible confined transfer area:
created archives appear under installer state `clone/outbox`. Place an incoming
archive under `clone/inbox` and address it as `backups/<name>.fsclone` when
restoring. The runtime does not expose arbitrary host paths through `/data`.

## Restore onto a populated target

The target may contain user workload. Restore is replacement, not an emptiness
check, and no target product data survives merely because it already exists.
Before continuing, run `flow-steward clone create` on the current target if you
need a complete rollback point. Restore does not create that backup automatically;
an existing database backup protects PostgreSQL only.

```bash
./flow-steward maintenance enable --confirm
./flow-steward maintenance status
./flow-steward clone restore backups/source.fsclone
```

For Scalable unattended restore, first run preflight/review with the passphrase on stdin
and record the printed target-bound confirmation value. Then repeat the command
with that exact value; the archive is authenticated again and the reviewed plan
must still match:

```bash
review_status=0
secret-command | ./flow-steward clone restore backups/source.fsclone \
  --passphrase-stdin --non-interactive || review_status=$?
if [ "$review_status" -eq 0 ]; then echo "Clone review returned unexpected success" >&2; exit 70; fi
if [ "$review_status" -ne 10 ]; then exit "$review_status"; fi
secret-command | ./flow-steward clone restore backups/source.fsclone \
  --passphrase-stdin --non-interactive --confirm '<printed-target-or-plan-value>'
```

The first Scalable unattended pass is a review-only cancellation and intentionally exits
`10`; it does not mutate the target. The guard accepts exactly that status (also
under `set -e`) and propagates every other failure.
The confirmed restore succeeds only with exit `0`, after restored parity and
core readiness verification. A nonzero confirmed-restore exit is a failure,
even if database or artifact parity was already restored; review exit `10`
must not be treated as a successful restore.

Compact's host launcher rejects explicit `--non-interactive` without `--confirm`
with exit `2` before dispatch. Do not use the Scalable review command above for
Compact: it cannot print a restore plan or confirmation value. Use the interactive
restore command, review the authenticated target plan and decline its final
confirmation to leave the target unchanged (exit `10`). Only after that review
may an unattended Compact invocation use the exact printed target-bound value
with `--non-interactive --confirm`; never invent a value or accept arbitrary
nonzero statuses as successful review. Preflight failures remain failures, not
review-only cancellation.

Before any destructive mutation, restore:

1. opens a regular archive without following symlinks;
2. authenticates the complete ciphertext into owner-only staging;
3. validates the closed manifest, allowlisted paths, types, sizes, checksums,
   profile/platform, and free space;
4. verifies the archived exact identity and complete image set through the
   shared release resolver/activator contract, using one registry as a whole;
5. snapshots the current target-owned deployment shell; and
6. prints the target installation ID, profile, public host, retained ports,
   ingress/TLS, networks, filesystem roots, UID/GID and PostgreSQL identity
   evidence, replacement classes, external-effect categories, plan digest, and
   rollback recommendation. Secret values are never printed.

The final prompt is bound to that reviewed target plan. Declining or entering a
different confirmation value performs no database, volume, filesystem,
configuration, container, or lifecycle mutation.

After confirmation, journaled phases stop the target, replace product state,
restore the source database and state, regenerate derived configuration through
the normal configuration service, activate the archived exact release without
running migrations, start in maintenance, and verify restored parity/readiness.
An older source release may replace a newer bootstrap release when the archive
schema is understood and the exact official release remains available.

## Domain journeys

### Same-domain move

For `production.com` to a replacement target also configured as
`production.com`, prevent both servers from accepting writes. Keep the old
source in maintenance, restore and validate the target locally, then change DNS
or routing manually. Clone never changes either.

### Different-domain replacement

For `dev.production.com` to a target whose retained host is `production.com`,
the target remains `production.com`. Clone does not globally rewrite stored
URLs. OAuth callbacks, webhooks, allowlists, and other external integrations may
still refer to the source environment and must be reviewed before writes resume.

### Production-to-development validation

A production archive can be restored onto a same-profile disposable target
configured with a development hostname. Treat copied schedules, mail, polling,
webhooks, shared storage, and credentials as live external effects even though
the local target remains in maintenance.

### `clone.production.com` validation and cutover

Install the target with `clone.production.com`, restore, and validate while it
remains in maintenance. To make the target canonical later, use only the
existing configuration path:

```bash
./flow-steward config set --public-host production.com
./flow-steward config apply --confirm
```

Update DNS manually after validation. There is no Clone `promote`, `switch`, or
DNS command. In-progress OAuth authorizations may be abandoned and restarted;
Clone does not rewrite them.

## External effects checklist

Before disabling maintenance, review every category printed by restore:

- scheduled jobs and polling connections;
- outbound mail and notifications;
- OAuth callbacks and provider allowlists;
- inbound webhooks;
- shared external object storage;
- integration tokens, endpoints, and third-party accounts.

Clone reports categories without exposing secrets. It does not sanitize,
disable, rotate, rewrite, or validate provider-side configuration automatically.

## Failure and resume

Wrong passphrases, corrupt bytes, unsafe paths, incompatible profiles/platforms,
or unavailable exact releases fail before target mutation and remove private
staging. Once mutation begins, the lifecycle journal records every proven phase.
On interruption, repeat the printed `clone restore` recovery command. Completed
phases with matching plan evidence are skipped; an unproven phase is repeated
idempotently. If staging is gone, the command reports that the archive must be
re-authenticated before resuming the same plan.

Do not disable maintenance or start services manually after a failed restore.
Follow the exact recovery instruction and inspect the retained lifecycle log.
There is no broad cross-resource rollback transaction.

## Validation and rollback

After success, keep maintenance enabled and verify:

```bash
./flow-steward maintenance status
./flow-steward release info
./flow-steward doctor
```

Also validate database counts/business records, credential decryption,
extensions, artifact reads and hashes, broker
state, target public routing, and the external-effects checklist. Only then
disable maintenance explicitly.

```bash
./flow-steward maintenance disable --confirm
./flow-steward maintenance status
./flow-steward status
```

Run this final sequence only after the confirmed restore returned `0` and the
checks above passed. Follow the printed recovery instruction on failure instead
of reopening admission or manually starting services.

DNS rollback to the old maintained source is lossless only **before the new
target accepts writes**. After the target accepts writes, switching back loses
or diverges those writes unless an independent reverse Clone or recovery plan is
performed. The old source is a point-in-time rollback, not live replication.

## Disposable acceptance evidence

Live acceptance is opt-in and must use disposable installations. Record the
candidate commit, exact release tag/commit/image digests, source and target
installation identities, every assertion, every owned Docker/filesystem
resource, and one cleanup result for each resource. Missing registry credentials,
native architecture, target manifests, or other prerequisites are blockers; do
not replace them with synthetic “passed” evidence.

The deterministic contract and harness live in
`tests/support/disposable_clone.py`. The required matrix covers both profiles,
all domain journeys, populated targets, declined confirmation, corruption,
wrong passphrase, unavailable exact release, whole-set registry fallback,
external-effect reporting, and interruption/resume.

The opt-in test reads only explicit, non-secret topology/evidence inputs:
`FS_CLONE_ACCEPTANCE_DRIVER`, `FS_CLONE_ACCEPTANCE_SOURCE_ROOT`,
`FS_CLONE_ACCEPTANCE_TARGET_ROOT`, `FS_CLONE_ACCEPTANCE_VERSION`,
`FS_CLONE_ACCEPTANCE_TAG`, `FS_CLONE_ACCEPTANCE_COMMIT`,
`FS_CLONE_ACCEPTANCE_IMAGE_DIGESTS_JSON`, and
`FS_CLONE_ACCEPTANCE_OWNED_RESOURCES_JSON`. The executable driver receives one
case ID plus canonical JSON on stdin and must obtain Clone passphrases from its
own secret provider, streaming them only to `--passphrase-stdin`. It returns the
validated evidence JSON and must include a cleanup result for every declared
owned resource. When any required input is absent, the live test reports the
exact blocker and skips; it never manufactures successful evidence.
