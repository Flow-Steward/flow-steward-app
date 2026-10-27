"""Docker and filesystem adapter for the installation doctor's fresh probes."""

from __future__ import annotations

import json
import os
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.application.doctor.checks import InterruptedOperation, MaintenanceFacts
from core.application.doctor.contracts import CheckBlocked
from core.application.doctor.hints import BackupFacts, CertificateFacts, MigrationFacts, WorkerFacts
from core.application.doctor.recovery import interrupted_journals
from core.application.doctor.repairs import RetentionFacts
from core.application.installer import InstallConfig, profile_switch
from core.application.installer.redaction import redact_installer_text
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.installer.doctor_probes import flock_held, narrow, wide_entries
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.gateway_support import _RUNTIME_CERTIFICATE_PATH
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

# Under the state directory everything is the owner's alone, except what holds nothing
# secret: the release store (published extension archives) and Docker's cid file.
_STATE_NOT_SECRET = frozenset({"release", "installer-runtime.cid"})
_PRODUCTION_ENV = ".env.prod"
_COMPACT_OPERATIONS = "/data/state/.flow-steward/operations"
_COMPACT_MAINTENANCE = "/data/state/.flow-steward/runtime/maintenance-state.json"
_SCALABLE_MAINTENANCE = ".flow-steward/runtime/maintenance-state.json"
# Read inside the Compact runtime, standard library only, so it works with whichever
# release built the image: the unfinished journals, and whether their lock is held
# (looked up in /proc/locks, never taken).
_COMPACT_JOURNALS_SCRIPT = r"""
import json, os, pathlib, sys
ops = pathlib.Path(sys.argv[1])
held = False
lock = ops / "active.lock"
if lock.exists():
    st = lock.stat()
    want = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    for line in open("/proc/locks").read().splitlines():
        for part in line.split():
            bits = part.split(":")
            if "FLOCK" in line and "->" not in line and len(bits) == 3:
                try:
                    held = held or (int(bits[0], 16), int(bits[1], 16), int(bits[2])) == want
                except ValueError:
                    pass
out = []
for path in sorted(ops.glob("*.json")) if ops.is_dir() else ():
    try:
        journal = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    if isinstance(journal, dict) and "kind" in journal and "recovery_command" in journal:
        out.append(journal)
print(json.dumps({"held": held, "journals": out}))
"""
_UNFINISHED = {"running", "cancelled", "recovery_required"}
# Run in the runtime service, in the installed image's own code: which migrations of
# that release are pending or drifted, and which the ledger holds that it does not know.
_MIGRATIONS_SCRIPT = r"""
import json
from core.application.tooling.db import migration_runner, upgrade_db
state = upgrade_db.inspect_database_upgrade()
unknown = []
find = getattr(migration_runner, "applied_unknown_migrations", None)
if find is not None:
    conn = upgrade_db.psycopg_runtime.connect(upgrade_db._resolved_dsn())
    try:
        unknown = [str(name) for name in find(conn)]
    finally:
        conn.close()
print(json.dumps({
    "pending": [str(name) for name in state.get("pending_migrations") or ()],
    "drifted": [str(name) for name in state.get("drifted_migrations") or ()],
    "unknown": unknown,
}))
"""
# The backups of a Compact installation, listed where they are: /data/backups.
_COMPACT_BACKUPS_SCRIPT = r"""
import json, pathlib
from core.infrastructure.lifecycle.backup_gateway import FilesystemBackupGateway
root = pathlib.Path("/data")
rows = []
if (root / "backups").is_dir():
    for backup in FilesystemBackupGateway(project_root=root).list_backups():
        rows.append({
            "name": backup.path.name,
            "status": str(getattr(backup.verification_status, "value", backup.verification_status)),
            "verified_at": backup.verified_at.isoformat() if backup.verified_at else None,
        })
print(json.dumps(rows))
"""
_INGRESS_OWNED_TLS = frozenset(
    {"reverse_proxy", "external_resolver", "external_certificate", "platform_managed", "acme"}
)
_START_FIRST = "Fix stack_stopped first (its next step is above), then rerun ./flow-steward doctor."


class TemporaryDoctorLog:
    """A plain doctor's log: outside the installation, gone with the container."""

    def __init__(self) -> None:
        handle, raw = tempfile.mkstemp(prefix="flow-steward-doctor-", suffix=".log")
        os.close(handle)
        self.path = Path(raw)

    def write(self, message: str) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(redact_installer_text(message) + "\n")

    def close(self) -> None:
        return None


class InstallationDoctorProbe:
    """The doctor's view of this installation, each read fresh from the real state."""

    def __init__(
        self,
        *,
        root: Path,
        gateway: DockerInstallerGateway,
        state: InstallerStateStore,
        config: InstallConfig,
        log: Any,
    ) -> None:
        self.root = root
        self.gateway = gateway
        self.state = state
        self.config = config
        self.profile = config.deployment_profile
        self.log = log
        self.holding = False  # doctor --fix holds the installation itself

    # -- what is running --------------------------------------------------------------

    def operation_running(self) -> str:
        if self.holding:
            return ""
        # P3 (live on fs-acc): the launcher saw the ./install.sh process on the host; a
        # lock taken a moment later, or not yet, must not make a plain doctor say none.
        live = str(runtime_env.get("FS_DOCTOR_LIVE_INSTALLER_PID") or "")
        if live and live != "none":
            return f"./install.sh (process {live})"
        if flock_held(self.state.lock_path):
            return "./install.sh or doctor --fix"
        for path in self.state.lifecycle_lock_paths:
            if flock_held(path):
                holder = _lock_metadata(path).get("operation_id") or "unknown"
                return f"lifecycle operation {holder}"
        if self.profile == "compact":
            try:
                facts = self._compact_journals()
            except (OSError, CheckBlocked):
                return ""  # a stopped runtime runs nothing; stack_stopped says it
            if facts.get("held"):
                return "a Compact platform operation"
        return ""

    # -- stack_stopped ----------------------------------------------------------------

    def stopped_services(self) -> list[str]:
        return self.gateway.stopped_services(self.config, log=self.log)

    def sources_differ_from_installation(self) -> str:
        installed = self.gateway.installed_runtime_revision(self.config)
        source = self.gateway.source_revision()
        if installed and source and installed != source:
            return f"these sources are at {source[:12]}, the installation at {installed[:12]}"
        return ""

    def _require_running(self, service: str) -> None:
        """What a check reads must run; if it does not, the check says so instead of
        failing (live: one stopped stack came out as four failures)."""
        if service in self.stopped_services():
            raise CheckBlocked(f"{service} is not running", _START_FIRST)

    def start_stopped_services(self) -> str:
        done = self.gateway.start_stopped_services(self.config, log=self.log)
        self.gateway.verify_health(self.config, log=self.log)
        return done or "Nothing was stopped."

    # -- lifecycle_interrupted --------------------------------------------------------

    def interrupted_operations(self) -> list[InterruptedOperation]:
        found = self._interrupted_journals()
        if found is None:
            return []  # an operation holds its lock: running, not interrupted
        if not self._host_operation_running():
            words = self.gateway.interrupted_update(self.config)
            if words:
                found.append(
                    InterruptedOperation(
                        operation_id="",
                        kind="update",
                        status="interrupted",
                        step=words,
                        recovery_command="./install.sh",
                        continuable=False,
                    )
                )
        return found

    def _host_operation_running(self) -> bool:
        if self.holding:
            return False
        paths = (self.state.lock_path, *self.state.lifecycle_lock_paths)
        return any(flock_held(path) for path in paths)

    def _interrupted_journals(self) -> list[InterruptedOperation] | None:
        if self.profile == "compact":
            facts = self._compact_journals()
            if facts.get("held"):
                return None
            journals = [
                journal
                for journal in facts.get("journals") or ()
                if journal.get("status") in _UNFINISHED
                or (journal.get("status") == "failed" and journal.get("retryable", True))
            ]
        else:
            if any(flock_held(path) for path in self.state.lifecycle_lock_paths):
                return None
            journals = [
                journal.model_dump(mode="json")
                for journal in LifecycleStateStore(self.root).unfinished_journals()
            ]
        switch = self.gateway.unfinished_profile_switch() if self.profile != "compact" else None
        return interrupted_journals(
            journals, profile=self.profile, profile_switch_pending=bool(switch)
        )

    def continue_operation(self, operation: InterruptedOperation) -> str:
        switch = self.gateway.unfinished_profile_switch()
        if switch is not None:
            return " ".join(
                profile_switch.resume_switch(self.gateway, self.config, switch, log=self.log)
            )
        outcome = self.gateway.run_in_scalable_cli(
            self.config, ("config", "apply", "--confirm"), log=self.log
        )
        if outcome.returncode != 0:
            raise RuntimeError(f"config apply did not complete (exit {outcome.returncode})")
        return f"Continued {operation.kind.replace('_', ' ')} {operation.operation_id}."

    def _compact_journals(self) -> dict[str, Any]:
        if "compact-runtime" in self.stopped_services():
            # F1: read where they are, not only through the runtime. Nothing inside a
            # stopped runtime holds its lock; a one-shot fs-cli takes the host lock.
            stdout = self.gateway.read_compact_volume(
                self.config, _COMPACT_JOURNALS_SCRIPT, _COMPACT_OPERATIONS
            )
            return {**_last_json(stdout), "held": False}
        outcome = self.gateway.process.run(
            [
                *self.gateway._compose_prefix(self.config),
                "exec", "-T", "compact-runtime", "python", "-c",
                _COMPACT_JOURNALS_SCRIPT, _COMPACT_OPERATIONS,
            ],
            separate_stderr=True,
        )  # fmt: skip
        if outcome.returncode != 0:
            raise OSError("the Compact runtime could not be read; is it running (stack_stopped)?")
        return json.loads(outcome.stdout or "{}")

    # -- maintenance_left_enabled ------------------------------------------------------

    def maintenance(self) -> MaintenanceFacts | None:
        if self.profile == "compact":
            self._require_running("compact-runtime")
            raw = self.gateway.read_compact_runtime_file(self.config, _COMPACT_MAINTENANCE)
        else:
            path = self.root / _SCALABLE_MAINTENANCE
            raw = path.read_text(encoding="utf-8") if path.is_file() else None
        if raw is None:
            return None
        state = json.loads(raw)
        return MaintenanceFacts(
            phase=str(state.get("phase") or ""),
            owner_kind=str(state.get("owner_kind") or ""),
            owner_id=str(state.get("owner_id") or ""),
            since=str(state.get("activated_at") or "an unknown time"),
        )

    def disable_maintenance(self) -> str:
        if self.profile == "compact":
            outcome = self.gateway.run_compact_platform_command(
                self.config, ("maintenance-disable", "--confirm"), log=self.log
            )
        else:
            outcome = self.gateway.run_in_scalable_cli(
                self.config, ("maintenance", "disable", "--confirm"), log=self.log
            )
        if outcome.returncode != 0:
            raise RuntimeError(f"maintenance disable did not complete (exit {outcome.returncode})")
        return "Turned maintenance mode off."

    # -- shared_dirs_modes -----------------------------------------------------------

    def shared_tree_problems(self) -> list[str] | None:
        reports = self.gateway.shared_tree_reports(self.config)
        if reports is None:
            return None
        return [
            f"{report['root']} has {report['pending']} entries outside the {report['tree']} model"
            for report in reports
            if int(report.get("pending") or 0)
        ]

    def normalize_shared_trees(self) -> str:
        reports = self.gateway.shared_tree_reports(self.config, repair=True) or []
        return " ".join(str(report.get("summary") or "") for report in reports)

    # -- secrets_modes --------------------------------------------------------------

    def _wide(self):
        entries = []
        for root in dict.fromkeys((self.state.root, self.root / ".flow-steward")):
            entries.extend(wide_entries(Path(root), exempt=_STATE_NOT_SECRET))
        for name in (_PRODUCTION_ENV, ".env"):
            entries.extend(wide_entries(self.root / name))
        key = _env_value(self.root / _PRODUCTION_ENV, "FS_TLS_PRIVATE_KEY_PATH")
        if key and Path(key).is_relative_to(self.root):
            entries.extend(wide_entries(Path(key)))
        return list({entry.path: entry for entry in entries}.values())

    def wide_secrets(self) -> list[str]:
        return [entry.words() for entry in self._wide()]

    def narrow_secrets(self) -> str:
        narrowed, foreign = 0, []
        for entry in self._wide():
            if entry.owned:
                narrow(entry)
                narrowed += 1
            else:
                foreign.append(str(entry.path))
        if foreign:
            raise RuntimeError(
                "not owned by this user, so not changed: "
                + ", ".join(foreign)
                + " -- as their owner run: chmod go= "
                + " ".join(foreign)
            )
        return f"Took group and other access from {narrowed} entries."

    # -- the hint checks (D2) ------------------------------------------------------

    def now(self) -> datetime:
        return datetime.now(UTC)

    def migration_state(self) -> MigrationFacts:
        self._require_running(self._runtime_service())
        self._require_running("pg-fs")
        state = _last_json(self.gateway.runtime_python(self.config, _MIGRATIONS_SCRIPT))
        return MigrationFacts(
            pending=tuple(state.get("pending") or ()),
            drifted=tuple(state.get("drifted") or ()),
            unknown=tuple(state.get("unknown") or ()),
        )

    def backup_key_present(self) -> bool:
        return bool(_env_value(self.root / _PRODUCTION_ENV, "FS_BACKUP_ENCRYPTION_KEY"))

    def backups(self) -> BackupFacts:
        key_present = self.backup_key_present()
        rows = self._backup_rows()
        verified = [row for row in rows if row["status"] == "verified" and row["verified_at"]]
        newest = max(verified, key=lambda row: row["verified_at"], default=None)
        return BackupFacts(
            key_present=key_present,
            newest_verified=str(newest["name"]) if newest else "",
            newest_verified_at=datetime.fromisoformat(newest["verified_at"]) if newest else None,
            unverified=len(rows) - len(verified),
        )

    def _backup_rows(self) -> list[dict[str, Any]]:
        rows = self._host_backup_rows()
        if self.profile == "compact":
            try:
                self._require_running("compact-runtime")
            except CheckBlocked:
                # Freshness of a known host backup is observable while the runtime
                # is stopped. This is not a complete inventory of the native store.
                if any(row["status"] == "verified" and row["verified_at"] for row in rows):
                    return rows
                raise
            native = _last_json(self.gateway.runtime_python(self.config, _COMPACT_BACKUPS_SCRIPT))
            return rows + [
                {**row, "name": f"compact:/data/backups/{row['name']}"} for row in native
            ]
        return rows

    def _host_backup_rows(self) -> list[dict[str, Any]]:
        from core.infrastructure.lifecycle.backup_gateway import FilesystemBackupGateway

        return [
            {
                "name": (
                    f"host:backups/{backup.path.name}"
                    if self.profile == "compact"
                    else backup.path.name
                ),
                "status": str(getattr(backup.verification_status, "value", "")),
                "verified_at": backup.verified_at.isoformat() if backup.verified_at else None,
            }
            for backup in FilesystemBackupGateway(project_root=self.root).list_backups(
                read_only=True
            )
        ]

    def certificate(self) -> CertificateFacts | None:
        mode = str(self.config.tls_mode)
        if mode in _INGRESS_OWNED_TLS:
            return None
        recorded = _env_value(self.root / _PRODUCTION_ENV, "FS_TLS_CERTIFICATE_PATH")
        input_path = Path(recorded) if recorded else self.root / self.config.certificate_path
        if not input_path.is_absolute():
            input_path = self.root / input_path
        # Observe the same installed certificate that TLS sync and public health use.
        # An operator input may be outside this read-only management mount; it is
        # renewal context, never a fallback for a missing installed mirror.
        path = self.root / _RUNTIME_CERTIFICATE_PATH
        not_after = self.gateway.certificate_not_after(path) if path.is_file() else None
        return CertificateFacts(
            path=str(path),
            not_after=not_after,
            provided=mode == "provided",
            input_path=str(input_path),
        )

    def _not_during_an_operation(self) -> None:
        """What an operation restarts is no fault while it runs (P3, live: a 502 and a
        refused connection were reported as failures in the middle of an update)."""
        if self.holding:
            return
        live = str(runtime_env.get("FS_DOCTOR_LIVE_INSTALLER_PID") or "")
        # The cheap signals only, read afresh for each check: the ./install.sh the
        # launcher saw, and the host locks, which Compact operations take through the mount.
        if live and live != "none":
            running = f"./install.sh (process {live})"
        elif self._host_operation_running():
            running = "a lifecycle operation"
        else:
            return
        raise CheckBlocked(
            f"an operation is running ({running}); what it restarts is not a fault",
            "Rerun ./flow-steward doctor after it has finished.",
        )

    def public_route_problem(self) -> tuple[str, str]:
        self._not_during_an_operation()
        if self.stopped_services():
            raise CheckBlocked("the stack is not running", _START_FIRST)
        return self.gateway.public_route_problem(self.config)

    def host_gateway_problem(self) -> tuple[str, str]:
        return self.gateway.host_gateway_problem()

    def queue_consumers(self) -> int | None:
        self._not_during_an_operation()
        self._require_running("rabbitmq-fs")
        # F4: no consumer because the workers are down is stack_stopped's to say.
        self._require_running("compact-runtime" if self.profile == "compact" else "fs-worker")
        return self.gateway.queue_consumers(self.config)

    def worker_counts(self) -> WorkerFacts:
        self._not_during_an_operation()
        env = self.root / _PRODUCTION_ENV
        if self.profile == "compact":
            self._require_running("compact-runtime")
            expected = _env_value(env, "FS_WORKER_PROCESSES") or str(self.config.worker_processes)
            return WorkerFacts(
                expected=int(expected),
                running=_answered(lambda: self.gateway.running_worker_count(self.config)),
                unit="worker processes",
                apply_command="./install.sh (it brings the worker processes to the configuration)",
            )
        expected = _env_value(env, "FS_WORKER_REPLICAS") or str(self.config.worker_replicas or 1)
        return WorkerFacts(
            expected=int(expected),
            running=self.gateway.running_worker_count(self.config),
            unit="fs-worker replicas",
            apply_command="./flow-steward config apply --confirm",
        )

    def missing_images(self) -> list[tuple[str, str]]:
        return self.gateway.missing_recorded_images(self.config)

    # -- the housekeeping repairs (D3) ---------------------------------------------

    def retention(self) -> RetentionFacts:
        # Both profiles (AC17-R178: Compact keeps its images by the same rule): without the
        # running release retention cannot tell what it must keep.
        current = self.gateway.installed_runtime_revision(self.config)
        if not current:
            raise CheckBlocked("no runtime container says which release runs", _START_FIRST)
        plan = self.gateway.superseded_releases(self.config, current=current)
        return RetentionFacts(
            releases=plan.releases,
            archives=plan.archives,
            ambiguous_images=plan.ambiguous_images,
            kept=plan.kept,
            free_bytes=self.gateway.free_disk_bytes(),
            minimum_free_bytes=self.gateway.minimum_free_disk_bytes(),
        )

    def prune_releases(self) -> str:
        current = self.gateway.installed_runtime_revision(self.config)
        if not current:
            raise RuntimeError("no runtime container says which release runs; nothing removed")
        done = self.gateway.prune_superseded_releases(
            self.config, current=current, previous="", log=self.log
        )
        return done or "Nothing was beyond the retention."

    def temporary_names(self) -> list[tuple[str, str, str]]:
        return [
            (repair.container_id, repair.name, repair.replaced_id)
            for repair in self.gateway.temporary_container_names(self.config)
        ]

    def restore_names(self) -> str:
        self.gateway.restore_container_names(self.config)
        return "Gave the containers back their own names."

    def cli_runtime_mismatch(self) -> str:
        return self.gateway.cli_runtime_mismatch(self.config)

    def refresh_cli_runtime(self) -> str:
        self.gateway.ensure_cli_runtime(self.config, log=self.log)
        return "Refreshed the retained fs-cli."

    def installation_lock_held(self) -> bool:
        """An install or lifecycle lock held by someone else: an orphan still working."""
        return self._host_operation_running()

    def orphan_installers(self) -> list[tuple[str, str]]:
        """Installer containers of this installation whose ./install.sh is not the live
        one. The launcher says which lives -- it runs on the host, which the doctor's
        container cannot see; doctor --fix owns the installation, so none other lives."""
        live = (
            "none" if self.holding else str(runtime_env.get("FS_DOCTOR_LIVE_INSTALLER_PID") or "")
        )
        if not live:
            raise CheckBlocked(
                "doctor was not started by ./flow-steward, which says which ./install.sh lives",
                "./flow-steward doctor",
            )
        me = socket.gethostname()
        return [
            (container["id"], container["host_pid"])
            for container in self.gateway.management_containers()
            if container["module"] == "installer"
            and not (me and container["id"].startswith(me))
            and (container["host_pid"] != live if container["host_pid"] else live == "none")
        ]

    def stop_orphan_installers(self) -> str:
        own = str(runtime_env.get("FS_INSTALLER_HOST_PID") or "")
        record = self.state.root / "installer-host.pid"
        stopped = []
        for container_id, _pid in self.orphan_installers():
            # Right before each stop: this run still owns the installation -- no other
            # ./install.sh has taken its record since (the auditor's condition).
            holder = record.read_text(encoding="utf-8").strip() if record.is_file() else ""
            if not own or holder != own:
                raise RuntimeError(
                    "another ./install.sh took the installation meanwhile; stopped "
                    + (", ".join(stopped) or "nothing")
                )
            if self.gateway.stop_container(container_id):
                stopped.append(container_id[:12])
        if not stopped:
            return "Nothing needed stopping."
        return "Stopped installer containers nobody ran: " + ", ".join(stopped) + "."

    def _runtime_service(self) -> str:
        return "compact-runtime" if self.profile == "compact" else "fs-app"

    # -- graph_catalog --------------------------------------------------------------

    def stale_graphs(self) -> list[str]:
        self._require_running("pg-fs")
        return self.gateway.stale_graphs(self.config)

    def repair_graph_catalog(self) -> str:
        self.gateway.repair_graph_catalog(self.config, log=self.log)
        return "Repaired the graph catalogue."


def _answered(read: Any) -> Any:
    """A read of the runtime's own health endpoint; a runtime that does not answer yet is
    not checked, not failed (P3: "could not run: URLError ... Connection refused")."""
    try:
        return read()
    except OSError as exc:
        text = str(exc)
        if "URLError" in text or "Connection refused" in text or "HTTP Error 503" in text:
            raise CheckBlocked(
                "the runtime does not answer yet",
                "Rerun ./flow-steward doctor in a minute; ./flow-steward status shows what starts.",
            ) from exc
        raise


def _last_json(stdout: str) -> Any:
    """The last line of a runtime snippet's output: imports may print before it."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def _lock_metadata(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        value = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _env_value(path: Path, name: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        key, _sep, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"')
    return ""
