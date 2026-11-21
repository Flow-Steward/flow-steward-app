"""Durable application orchestration of a complete official release upgrade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Literal, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.application.lifecycle.backup_service import (
    BackupCreateRequest,
    BackupResult,
    BackupService,
    BackupVerificationStatus,
)
from core.application.lifecycle.contracts import CancellationToken
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.ports import ProgressSink
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.maintenance.ports import MaintenanceServicePort
from core.application.management.command_specs.contracts import (
    OperationProgressEvent,
    OperationStepStatus,
)
from core.application.release.service import (
    PreparedReleaseUpgrade,
    ReleaseResult,
    ReleaseService,
    ReleaseUpgradeRequest,
)


class ReleaseUpgradeJournal(BaseModel):
    """Pin the reviewed target and backup decision across checkout replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    operation_id: str = Field(
        default_factory=lambda: uuid4().hex, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    )
    prepared: PreparedReleaseUpgrade
    maintenance_enabled: bool = False
    backup_completed: bool = False
    backup_path: str = ""
    activation_started: bool = False
    activation_completed: bool = False
    migration_required: bool = False
    completed: bool = False


class ReleaseUpgradeState(Protocol):
    def load_release_upgrade(self) -> ReleaseUpgradeJournal | None: ...

    def save_release_upgrade(self, journal: ReleaseUpgradeJournal) -> None: ...

    def operation_lock(self) -> AbstractContextManager[Mapping[str, int]]: ...


class ReleaseMigration(Protocol):
    def upgrade(self, *, skip_db_backup: bool, timeout_seconds: int) -> None: ...


class ReleaseUpgradeWorkflow:
    """Keep maintenance enabled after every incomplete mutating upgrade."""

    def __init__(
        self,
        *,
        state: ReleaseUpgradeState,
        maintenance: MaintenanceServicePort,
        backup: BackupService,
        migration: ReleaseMigration,
        checkout_quiesce: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.maintenance = maintenance
        self.backup = backup
        self.migration = migration
        # Owner 2026-11-21 (D3): a development installation stops what runs code from
        # its checkout before the handoff checks the target release out in place.
        self.checkout_quiesce = checkout_quiesce

    def prepare_resume(
        self, release: ReleaseService, request: ReleaseUpgradeRequest
    ) -> PreparedReleaseUpgrade | None:
        journal = self.state.load_release_upgrade()
        if journal is None or journal.completed:
            return None
        prepared = journal.prepared
        if request.target_version and request.target_version != prepared.target_release.version:
            raise self._conflict(journal)
        if release.selector.verify(prepared.target_release) != prepared.target_release:
            raise self._conflict(journal)
        # An omitted flag retains the previous explicit waiver. A new explicit
        # waiver is allowed only before activation/migration ever started.
        if request.skip_db_backup and not prepared.skip_db_backup:
            if journal.activation_started:
                raise self._conflict(journal)
            prepared = prepared.model_copy(update={"skip_db_backup": True})
        return prepared.model_copy(
            update={"dry_run": request.dry_run, "timeout_seconds": request.timeout_seconds}
        )

    @staticmethod
    def review(prepared: PreparedReleaseUpgrade) -> dict[str, object]:
        return {
            "status": "already_current"
            if prepared.target_release == prepared.installed_release
            else "ready",
            "next_steps": [],
            "target_version": prepared.target_release.version,
            "target_commit_sha": prepared.target_release.commit_sha,
            "target_images": [
                image.model_dump(mode="json") for image in prepared.target_release.images
            ],
            "deployment_profile": prepared.deployment_profile,
            "platform": prepared.platform,
            "maintenance": "enabled automatically; disabled after successful health checks",
            "on_failure": "Maintenance remains enabled; repeat the same release upgrade.",
            "database_backup": "SKIPPED: operator accepts the risk"
            if prepared.skip_db_backup
            else "create and verify before activation",
            "skip_db_backup": prepared.skip_db_backup,
            "migration_policy": "apply the target release's migrations automatically when required",
        }

    def run(
        self, release: ReleaseService, prepared: PreparedReleaseUpgrade, **sinks: object
    ) -> ReleaseResult:
        existing = self.state.load_release_upgrade()
        if prepared.dry_run:
            return ReleaseResult(
                status="planned",
                dry_run=True,
                target_release=prepared.target_release,
                target_version=prepared.target_release.version,
                preflight=self.review(prepared),
            )
        if (
            existing is None or existing.completed
        ) and prepared.target_release == prepared.installed_release:
            return release._activate_upgrade(prepared, **sinks)
        with self.state.operation_lock():
            journal = self.state.load_release_upgrade()
            if journal is None or journal.completed:
                journal = ReleaseUpgradeJournal(prepared=prepared)
            elif journal.prepared.target_release != prepared.target_release:
                raise self._conflict(journal)
            else:
                journal = journal.model_copy(update={"prepared": prepared})
            self.state.save_release_upgrade(journal)
        token = sinks.get("cancellation")
        if isinstance(token, CancellationToken):
            token.set_recovery_command(self._resume_command(journal))
        if not journal.maintenance_enabled or (
            not journal.activation_completed and self.maintenance.status().status != "enabled"
        ):
            self._progress(sinks, "maintenance_enable", "Enable maintenance and drain active work")
            self._cancelled(sinks, journal)
            if self.maintenance.status().status != "enabled":
                self.maintenance.enable()
            with self.state.operation_lock():
                journal = journal.model_copy(update={"maintenance_enabled": True})
                self.state.save_release_upgrade(journal)
        with self.state.operation_lock() as held_locks:
            current = self.state.load_release_upgrade()
            if current is None or current.operation_id != journal.operation_id:
                raise self._conflict(journal)
            journal = current
            if not journal.activation_completed:
                if not journal.activation_started:
                    # Repeat origin/configuration validation under the mutation
                    # lock before creating a dump or touching the checkout.
                    release._activate_upgrade(prepared.model_copy(update={"dry_run": True}))
                journal = self._backup(journal, sinks)
                if not journal.activation_started:
                    journal = journal.model_copy(update={"activation_started": True})
                    self.state.save_release_upgrade(journal)
                activation_sinks = {**sinks, "held_locks": held_locks}
                if prepared.transition_binding is not None:
                    activation_sinks["transition_binding"] = prepared.transition_binding
                if journal.migration_required:
                    self._migrate(journal, sinks)
                if (
                    self.checkout_quiesce is not None
                    and prepared.target_release != prepared.installed_release
                ):
                    self._progress(
                        sinks, "checkout_quiesce", "Stop the services that run checkout code"
                    )
                    self._cancelled(sinks, journal)
                    self.checkout_quiesce()
                self._progress(sinks, "release_activate", "Activate the pinned release")
                self._cancelled(sinks, journal)
                result = release.activator.activate(
                    prepared.target_release,
                    timeout_seconds=prepared.timeout_seconds,
                    **activation_sinks,
                )
                if result.status == "migration_required":
                    journal = journal.model_copy(update={"migration_required": True})
                    self.state.save_release_upgrade(journal)
                    self._migrate(journal, sinks)
                    result = release.activator.activate(
                        prepared.target_release,
                        timeout_seconds=prepared.timeout_seconds,
                        **activation_sinks,
                    )
                if result.status not in {"upgraded", "already_current"}:
                    raise LifecycleError.stable_failure(
                        code="release_upgrade_incomplete",
                        cause=result.detail or "Release activation did not complete.",
                        remediation=self._resume_command(journal),
                        step="release_activate",
                    )
                journal = journal.model_copy(update={"activation_completed": True})
                self.state.save_release_upgrade(journal)
        self._progress(sinks, "maintenance_disable", "Verify health and reopen the installation")
        self._cancelled(sinks, journal)
        # Compact's public mutation client acquires its own lifecycle lock.
        # Installer exclusion must be released before this final health gate.
        try:
            self.maintenance.disable()
            with self.state.operation_lock():
                journal = journal.model_copy(update={"completed": True})
                self.state.save_release_upgrade(journal)
        except BaseException:
            # A signal may arrive after the terminal replacement succeeds.
            # Restore resumable state as well as maintenance before reporting
            # an incomplete command, so a same-target retry can reopen safely.
            try:
                with self.state.operation_lock():
                    journal = journal.model_copy(update={"completed": False})
                    self.state.save_release_upgrade(journal)
            finally:
                self.maintenance.enable()
            raise
        return ReleaseResult(
            status="upgraded",
            current_release=prepared.target_release,
            current_version=prepared.target_release.version,
            target_release=prepared.target_release,
            target_version=prepared.target_release.version,
            deployment_profile=prepared.deployment_profile,
            platform=prepared.platform,
            installed_origin="official",
            operation_id=journal.operation_id,
            maintenance_status=MaintenanceStatusResult(status="disabled"),
            next_steps=(),
            preflight={
                "skip_db_backup": prepared.skip_db_backup,
                "backup_path": journal.backup_path,
            },
        )

    def _backup(
        self, journal: ReleaseUpgradeJournal, sinks: dict[str, object]
    ) -> ReleaseUpgradeJournal:
        if journal.prepared.skip_db_backup:
            return journal
        if journal.backup_completed:
            saved = self.backup.find_verified(journal.operation_id)
            if saved is not None and saved.path == journal.backup_path:
                return journal
            if not journal.activation_started:
                journal = journal.model_copy(update={"backup_completed": False, "backup_path": ""})
            else:
                raise LifecycleError.stable_failure(
                    code="release_backup_evidence_missing",
                    cause="The verified pre-upgrade backup is no longer available.",
                    remediation="Recover the original verified backup before resuming this upgrade.",
                    step="backup",
                )
        if journal.activation_started:
            raise LifecycleError.stable_failure(
                code="release_backup_evidence_missing",
                cause="The pre-upgrade backup was not recorded before activation.",
                remediation=self._resume_command(journal),
                step="backup",
            )
        self._progress(sinks, "database_backup", "Create and verify the database backup")
        self._cancelled(sinks, journal)
        result = self.backup.find_verified(journal.operation_id)
        if result is None:
            result = self.backup.create(
                BackupCreateRequest(operation_id=journal.operation_id), **sinks
            )
        if (
            not isinstance(result, BackupResult)
            or result.verification_status != BackupVerificationStatus.VERIFIED
        ):
            raise LifecycleError.stable_failure(
                code="release_backup_unverified",
                cause="The database backup is not verified.",
                remediation=self._resume_command(journal),
                step="backup",
            )
        journal = journal.model_copy(update={"backup_completed": True, "backup_path": result.path})
        self.state.save_release_upgrade(journal)
        return journal

    def _migrate(self, journal: ReleaseUpgradeJournal, sinks: dict[str, object]) -> None:
        self._progress(sinks, "database_upgrade", "Apply the target release's database migrations")
        self._cancelled(sinks, journal)
        if self.maintenance.status().status != "enabled":
            raise LifecycleError.stable_failure(
                code="release_maintenance_required",
                cause="Maintenance must remain enabled before migration.",
                remediation=self._resume_command(journal),
                step="database_upgrade",
            )
        self.migration.upgrade(
            skip_db_backup=journal.prepared.skip_db_backup,
            timeout_seconds=journal.prepared.timeout_seconds,
        )

    @staticmethod
    def _progress(sinks: dict[str, object], step: str, label: str) -> None:
        sink = cast(ProgressSink | None, sinks.get("progress_sink"))
        if sink is not None:
            sink(
                OperationProgressEvent(
                    step_id=step, status=OperationStepStatus.RUNNING, label=label
                )
            )

    @staticmethod
    def _resume_command(journal: ReleaseUpgradeJournal) -> str:
        waiver = " --skip-db-backup" if journal.prepared.skip_db_backup else ""
        return f"flow-steward release upgrade --target-version {journal.prepared.target_release.version}{waiver} --confirm"

    @classmethod
    def _conflict(cls, journal: ReleaseUpgradeJournal) -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_upgrade_state_changed",
            cause="An unfinished release upgrade is pinned to another target or policy.",
            remediation=cls._resume_command(journal),
            step="release_upgrade",
        )

    @classmethod
    def _cancelled(cls, sinks: dict[str, object], journal: ReleaseUpgradeJournal) -> None:
        token = sinks.get("cancellation")
        if isinstance(token, CancellationToken) and token.requested:
            raise LifecycleError.interrupted(
                step="release_upgrade",
                recovery_command=cls._resume_command(journal),
                forced=token.forced,
            )
