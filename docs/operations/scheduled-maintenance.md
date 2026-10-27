# Scheduled maintenance

What Flow Steward does on a schedule by itself, what you have to schedule, and how
to set it up on each deployment profile. This is the one page to read before
leaving an installation running unattended.

## What runs by itself

The control plane runs these loops on every profile: Compact, Scalable, and a
Compact image on a platform such as Railway. Nothing needs to be installed on the
host for them.

| Loop | What it does | Default cadence | Tune or disable |
|---|---|---|---|
| Retention | Applies each account's retention policy: purges the trash, old job logs, action logs, checkpoints of finished jobs, old jobs, events, and revisions beyond the kept count | Once a day, plus once each time the control plane starts | `FS_RETENTION_INTERVAL_SECONDS` (minimum 3600), `FS_RETENTION_ENABLED=0` |
| Watchdog | Marks stuck and orphaned jobs | 30 s | `FS_WATCHDOG_*` |
| Scheduled jobs | Starts the workflow schedules you define in a project's Automation | 10 s | `FS_SCHEDULED_JOBS_*` |
| Outbox dispatch | Delivers queued platform events and reconciles artifact purges | 30 s | `FS_ARTIFACT_CLEANUP_RECONCILIATION_BUDGET` |
| Dashboard rollup | Refreshes dashboard figures | 5 min | `FS_DASHBOARD_ROLLUP_*` |
| Security posture | Re-scans the security posture | 5 min | `FS_SECURITY_POSTURE_*` |
| Workflow knowledge sync | Keeps the workflow knowledge graph current | 5 min | `FS_WORKFLOW_KNOWLEDGE_SYNC_*` |
| External-effect maintenance | Expires stale external-effect leases | 5 min | `FS_EXTERNAL_EFFECT_MAINTENANCE_*` |
| Monitoring cleanup | Prunes old runtime-status rows | `FS_MONITORING_INTERVAL_SECONDS` | `FS_MONITORING_*_RETENTION_DAYS` |

Each process also deletes its own daily log files older than
`FS_LOG_RETENTION_DAYS` (30 by default).

On Compact the `FS_*_ENABLED` switches do not reach the control process, so there
these loops can be tuned but not turned off. `FS_RETENTION_ENABLED` is no
exception.

### Retention policy

An administrator sets it per account in **Settings → Retention**:

| Setting | Default | Range | Meaning |
|---|---|---|---|
| Action logs (days) | 30 | 1–3650 | Audit and action-log entries older than this are deleted |
| Job logs (days) | 30 | 1–3650 | Agent messages of jobs older than this are deleted |
| Trash (days) | 30 | 1–3650 | Items stay in the trash this long, then are deleted for good |
| Terminal job checkpoints (days) | 30 | 1–3650 | Checkpoints of finished jobs older than this are deleted |
| Revisions kept per item | 5 | 1–100 | Each agent, tool, skill, workflow, guardrail, dataset and project-settings history keeps this many newest revisions; older ones are deleted for good |

Revisions are kept by count, not age, so an item that has not changed for months
still shows its last revisions and who made them. Trash and restore markers are
not counted as revisions and are never removed by this setting.

An account that never saved a policy uses the environment defaults:
`FS_RETENTION_LOGS_DAYS`, `FS_RETENTION_MESSAGES_DAYS`, `FS_RETENTION_TRASH_DAYS`,
`FS_RETENTION_TERMINAL_JOB_CHECKPOINTS_DAYS` and `FS_RETENTION_REVISIONS_KEEP`.
Platform-wide windows have no setting in the UI: `FS_RETENTION_JOBS_DAYS` (30),
`FS_RETENTION_EVENTS_DAYS` (30) and `FS_RETENTION_RETRY_QUEUE_DAYS` (14).

## What you have to schedule

### Backups

Nothing backs the database up on its own. `./flow-steward doctor` warns when the
newest verified backup is more than 7 days old.

`./flow-steward backup create` writes an encrypted backup and verifies it. It works
on Compact and Scalable, asks no questions, and is the backup the doctor counts.
Schedule it with cron on the installation host.

1. Open the crontab of the account that owns the installation (not root):

   ```bash
   crontab -e
   ```

2. Add this line, with the absolute path of your installation. Cron starts in
   your home directory, so a relative path does not work.

   ```cron
   0 2 * * * cd /opt/flow-steward && ./flow-steward backup create --json >> .flow-steward/logs/backup-cron.log 2>&1
   ```

3. Check that it ran the next day:

   ```bash
   ./flow-steward backup list
   ./flow-steward doctor
   ```

Where backups go after that is yours to decide: keep them on the host, copy them
to cloud storage, or hand them to your own backup system. Add those steps to the
same cron line or to a script it calls, after `backup create` succeeds. Flow
Steward does not rotate backups either: delete old ones from `backups/` (Scalable)
or the runtime's backup volume (Compact) yourself, and keep at least one verified
backup.

`infra/cron/fs_ops.crontab` holds the same line as a template.

### Railway and other platforms without a host

There is no host to run cron and no Docker Compose. The control-plane loops above
still run. For the database, use the platform's own backups and point-in-time
recovery.

## Checking that retention runs

Each pass logs one line from the control plane, for example
`Retention pass removed trash=4, registry_changelog=12`, or
`Retention pass removed nothing`. A changed policy takes effect at the next
pass, at most a day later, or when the control plane next starts.

- Scalable: `./flow-steward stack logs --service control-plane --tail 200`
- Compact: `docker compose -p <project> logs --tail 200 compact-runtime` (the
  launcher's `stack` commands are not available on Compact)

## Older host-cron tooling

`core/entrypoints/cli/scripts/ops_schedule.sh` predates the retention loop.
It runs with the host's own Python and the checkout's `backups/`. Use
`./flow-steward backup create` instead of `ops_schedule.sh backup`: the doctor does
not count the backups that `ops_schedule.sh backup` writes.
