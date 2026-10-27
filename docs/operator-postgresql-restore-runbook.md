# PostgreSQL Restore Runbook

Flow Steward exposes PostgreSQL backup creation, listing, and verification
through `./flow-steward backup`. Restore is intentionally not a public CLI
command because it is destructive and must target an explicit operator-owned
recovery environment.

## Recovery boundary

- Restore only to the intended recovery target. Use a disposable database for
  drills; stop application writers before a real recovery.
- Verify the encrypted artifact first with
  `./flow-steward backup verify backups/<file>.dump.enc`.
- Export the same dedicated `FS_BACKUP_ENCRYPTION_KEY` used to create the
  artifact. Never substitute `FS_SECRET_KEY`, `ENCRYPTION_KEY`, or database
  credentials.
- The PostgreSQL backup does not contain storage objects, extensions, projects
  on the host filesystem, TLS material, image pins, queues, or derived indexes.
  Storage objects are not copied; recover those through separate
  operator-managed procedures.
- Keep the active installation untouched during a restore drill. Public
  lifecycle commands never restore a backup over the live database.

## Operator procedure

Capture source parity evidence before recovery when the source is available:

```bash
./flow-steward database restore-parity-capture \
  --output backups/restore/baseline_<timestamp>.json \
  --confirm
```

After selecting and isolating the target database, invoke the internal recovery
script from that installation's repository checkout. Pass the exact Compose
project recorded in that checkout's `.flow-steward/compose.env`:

```bash
export FS_BACKUP_ENCRYPTION_KEY=<same-dedicated-key-used-for-this-backup>
core/entrypoints/cli/scripts/db_restore.sh backups/<file>.dump.enc \
  --target-compose-project <exact-compose-project> \
  --confirm
```

After inspecting a custom-format archive, the script forcibly disconnects the
confirmed target database, replaces it from pristine `template0`, and restores
without `pg_restore --clean`. Replacing the whole database avoids invalid
object-by-object cleanup of Apache AGE-owned label tables. The script refuses
to replace `postgres`, `template0`, or `template1`, and rejects a target name
that differs from the selected checkout's Compose project. Protected-export decryption runs in a one-shot
Linux application container (`fs-app` or `compact-runtime`) with pulls disabled
and without requesting a build. It uses the container-local, owner-only
`/tmp/backups` staging directory; application Python and cryptography do not run on the host.
For custom-format archive preflight, the authenticated plaintext stream is
fully consumed into an owner-only temporary file inside the `pg-fs` container,
inspected there with `pg_restore -l`, and removed by a shell trap before the
database reset begins. No plaintext backup file is written to the host. The
script does not infer a key from an environment file or print the key.

PostgreSQL logical dumps preserve AGE `regnamespace` and `regclass` names, but
AGE 1.6 stores graph identities in additional bare `oid` columns. After restore,
the wrapper transactionally reconciles those catalog identities to the restored
schema OIDs and recreates the AGE foreign key. Any archive inspection, database
replacement, `pg_restore`, or AGE finalization error remains fatal.

Verify restored parity:

```bash
./flow-steward database restore-parity-verify \
  --baseline backups/restore/<baseline_file>.json \
  --output backups/restore/restored_<timestamp>.json \
  --confirm
```

Also execute a real Cypher read for every restored application graph. For the
default workflow graph, the minimum smoke query is:

```sql
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT *
FROM cypher('platform_workflow_graph', $$
  MATCH (node)
  RETURN count(node)
$$) AS (node_count agtype);
```

Compare the returned count with the source evidence. A successful
`pg_restore`, surviving relational rows, or populated AGE tables alone do not
prove graph usability.

Record the backup identity, target identity, operator, start/end timestamps,
parity result, RTO, and RPO in the recovery change record.
