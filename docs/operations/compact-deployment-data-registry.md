# Compact Deployment Data and Permission Registry

This registry defines supported production paths. A fresh Compact installation
consolidates application-owned durable files under `/data`; PostgreSQL and RabbitMQ
retain independent volumes. Profile switching reuses the existing data placement
(host directories or the Compact volume), without copying files or changing the
database, broker, encryption keys or artifact backend. See the
[deployment runbook](compact-scalable-deployment.md#switching-profile).

| Existing Scalable path | Compact path | Consumer and owner | Access | Class | Backup and profile switch |
|---|---|---|---|---|---|
| `${FS_EXTENSIONS_DIR:-extensions}` mounted at `/app/extensions` | `/data/extensions` | installer UID 10005, extension GID 10103; host-bound trees retain host ownership | installer create/write/delete; app, worker, MCP read/execute | durable | Include active projections, `.releases`, dependency sites, publication metadata and archives in backups/clones. Copy only with writers stopped, preserving names and modes. Core images contain no bundled extensions. |
| `${ARTIFACT_LOCAL_ROOT_DIR:-logs/artifacts}` | `/data/artifacts` | operator UID 10007, artifact GID 10100 | app/worker read/write through artifact grants | durable for `local_fs` | Include when `file_storage=local_fs`. Do not copy S3 objects or change backend during profile switch. |
| `.flow-steward/` installer/lifecycle state | `/data/state/.flow-steward` | operator UID 10007 | operator/one-shot lifecycle commands read/write | durable | Include configuration, journals, operation state, release metadata, and installation identity. |
| generated identity/bootstrap state | `/data/state/compact-bootstrap.json` plus database records | operator UID 10007 and bootstrap owner | bootstrap read/write; runtime read | durable | Include file state and database backup. Re-running bootstrap is idempotent and never rotates credentials automatically. |
| host `backups/` | embedded/provider `/data/backups` | host operator or embedded operator UID 10007 | lifecycle command read/write, mode 0700 | durable | These are distinct stores: host management writes host backups; embedded/provider commands write the private data-volume store. Artifacts still require verification. |
| `${FS_LOGS_DIR:-logs}` | `/data/logs/{app,worker,control,installer}` | role UIDs 10001/10002/10003/10005 | each role writes only its directory | diagnostic | stdout/stderr is primary. Persist these files only when required by retention policy; exclude caches. |
| Python/tool caches under `/app/logs/tool-cache` | `/tmp/flow-steward` via `PYTHONPYCACHEPREFIX` | each process UID | role-local read/write | ephemeral | Exclude from backup and migration. |
| image application source `/app` | `/app` | root-owned image content | runtime roles read/execute; immutable | release artifact | Never copy to a data backup. Restore by the same immutable image digest. |
| PostgreSQL `/var/lib/postgresql/data` | unchanged dedicated `fs-postgres` volume | PostgreSQL image UID/GID | database only | durable | Use `pg_dump -Fc` plus volume/provider recovery appropriate to the environment. Same supported major version for this procedure. |
| RabbitMQ `/var/lib/rabbitmq` | unchanged dedicated `fs-rabbitmq` volume | RabbitMQ image UID/GID | broker only | durable | Stop writers and preserve the broker volume. Definitions export does not preserve messages. |
| SeaweedFS `/data` | unchanged only when `file_storage=bundled` | SeaweedFS | service only | durable | Compact defaults to `local_fs` and does not start SeaweedFS. An existing bundled installation retains its backend during profile changes. |
| bundled Traefik `acme.json` | installation-owned TLS state volume/path | Traefik | Traefik only, mode 0600 | durable when ACME selected | Include only for bundled ingress. External proxy/provider certificate state remains outside Flow Steward backup and restore. |
| supplied certificate/key | configured installation-owned secret paths | bundled Traefik | proxy read-only; operator replaces atomically | durable secret | Validate PEM, key match, SAN, validity, and issuer chain before replacement. Never include the private key in evidence. |

The Compact volume root is mode `0755`; extensions are setgid `2775`, artifacts
`2770`, state `0750`, runtime state `2750`, backups `0700` and role log directories
`0700`. Bootstrap validates known paths with descriptor-relative symlink-safe
checks and normalizes supported earlier extension-tree modes before startup;
it does not blindly recursively change restored ownership. Source of truth is
`core/infrastructure/runtime/compact/volume.py`.

Operator-supplied secrets remain in the environment/provider secret store and
must be preserved separately. Provider-native generated runtime secrets are
persisted in `/data/state/runtime-secrets.json`, mode `0600`, read by the supervisor
only; include that state in a protected transfer. A restore with different
encryption keys is invalid even when database and files are intact. Database,
broker, backup, machine, storage and integration credentials retain their owning
configuration/secret-store contracts.
