"""Durable application orchestration of a complete official release upgrade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Literal, NoReturn, Protocol, cast
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
from core.application.release.platform_modules import (
    ModuleActivator,
    ModuleSet,
    NoModuleActivator,
    PreparedModuleRef,
    canonical_module_set,
)
from core.application.release.service import (
    PreparedReleaseUpgrade,
    ReleaseResult,
    ReleaseService,
    ReleaseUpgradeRequest,
)


class ReleaseUpgradeJournal(BaseModel):
    """Pin the reviewed target, module set and backup decision across checkout replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # Additive fields keep version 1: each has a default, so a journal written
    # before platform modules loads as "no modules, not yet activated".
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
    # ADR 0082 §5: the platform-module set pinned before any mutation. When no
    # change was requested it records the installed set and modules_required is
    # False; a resume activates this set, never a recomputed one.
    prepared_modules: ModuleSet = ()
    # The set installed when this journal was created: what a module rollback
    # re-activates. None in a journal written before the field existed, which
    # therefore never rolls back automatically.
    previous_modules: ModuleSet | None = None
    modules_required: bool = False
    # The module-loading roles were stopped before the list is written.
    modules_roles_stopped: bool = False
    modules_activated: bool = False
    # Module-only change: the database step (Core, then module migrations) was
    # started / finished on the activated set. A release change records its
    # migration in ``migration_required`` instead.
    modules_migration_started: bool = False
    modules_migrated: bool = False
    # Only a module-only change restarts the roles itself; a release change
    # restarts them through the installer handoff.
    roles_restarted: bool = False
    # Module-only rollback after a failed database step, restart or health gate.
    modules_rollback_started: bool = False
    modules_rolled_back: bool = False
    # The automatic rollback was refused (the new set may have applied a module
    # migration); the reason is kept for the operator. Exits: repeat the same
    # upgrade to go forward, or request ``restore_previous_modules``.
    modules_rollback_refused: bool = False
    modules_rollback_refused_reason: str = ""
    # The rollback was the operator's explicit ``restore_previous_modules`` choice.
    modules_restore_requested: bool = False

    @property
    def module_only(self) -> bool:
        return self.modules_required and (
            self.prepared.target_release == self.prepared.installed_release
        )

    @property
    def module_roles_down(self) -> bool:
        """A module-only change stopped the roles and has not started them again."""
        return (
            self.module_only
            and self.modules_roles_stopped
            and not self.roles_restarted
            and not self.modules_rolled_back
        )

    @property
    def finished(self) -> bool:
        """Completed, or ended by a successful module rollback: nothing left to resume."""
        return self.completed or self.modules_rolled_back

    @property
    def active_modules(self) -> tuple[PreparedModuleRef, ...]:
        """The set in ``modules.json`` now: previous after a rollback, else target once written."""
        if self.modules_rolled_back:
            return self.previous_modules or ()
        if self.modules_activated or not self.modules_required or self.previous_modules is None:
            return self.prepared_modules
        return self.previous_modules


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
        modules: ModuleActivator | None = None,
    ) -> None:
        self.state = state
        self.maintenance = maintenance
        self.backup = backup
        self.migration = migration
        self.modules: ModuleActivator = modules if modules is not None else NoModuleActivator()
        # Owner 2026-11-21 (D3): a development installation stops what runs code from
        # its checkout before the handoff checks the target release out in place.
        self.checkout_quiesce = checkout_quiesce

    def prepare_resume(
        self, release: ReleaseService, request: ReleaseUpgradeRequest
    ) -> PreparedReleaseUpgrade | None:
        journal = self.state.load_release_upgrade()
        if journal is None or journal.finished:
            return None
        prepared = journal.prepared
        if request.target_version and request.target_version != prepared.target_release.version:
            raise self._conflict(journal)
        if release.selector.verify(prepared.target_release) != prepared.target_release:
            raise self._conflict(journal)
        if (
            request.target_modules is not None
            and request.target_modules != journal.prepared_modules
        ):
            raise self._conflict(journal)
        # An omitted flag retains the previous explicit waiver. A new explicit
        # waiver is allowed only before activation/migration ever started.
        if request.skip_db_backup and not prepared.skip_db_backup:
            if journal.activation_started:
                raise self._conflict(journal)
            prepared = prepared.model_copy(update={"skip_db_backup": True})
        return prepared.model_copy(
            update={
                "dry_run": request.dry_run,
                "timeout_seconds": request.timeout_seconds,
                "restore_previous_modules": request.restore_previous_modules,
            }
        )

    def review(self, prepared: PreparedReleaseUpgrade) -> dict[str, object]:
        self.require_module_support(prepared)
        review: dict[str, object] = {
            "status": "already_current"
            if prepared.target_release == prepared.installed_release
            and not self._modules_change(prepared)
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
        if prepared.target_modules is not None:
            review["platform_modules"] = self._module_summary(prepared.target_modules)
            if prepared.target_release == prepared.installed_release:
                review["platform_modules_on_failure"] = (
                    "If the database step, the role restart or the health gate fails and "
                    "the new set adds no module migration, the previous set is activated "
                    "again and the roles restarted; otherwise the roles stay stopped until "
                    "the same upgrade is repeated or the previous set is restored. "
                    "Maintenance remains enabled either way."
                )
        return review

    def run(
        self, release: ReleaseService, prepared: PreparedReleaseUpgrade, **sinks: object
    ) -> ReleaseResult:
        self.require_module_support(prepared)
        existing = self.state.load_release_upgrade()
        if prepared.dry_run:
            return ReleaseResult(
                status="planned",
                dry_run=True,
                target_release=prepared.target_release,
                target_version=prepared.target_release.version,
                preflight=self.review(prepared),
            )
        if prepared.restore_previous_modules:
            self._restore_previous_modules(sinks)
        if (
            (existing is None or existing.finished)
            and prepared.target_release == prepared.installed_release
            and not self._modules_change(prepared)
        ):
            return release._activate_upgrade(prepared, **sinks)
        with self.state.operation_lock():
            journal = self.state.load_release_upgrade()
            if journal is None or journal.finished:
                # Without a requested set the installed one is only recorded: a
                # deployment that cannot report it (an older Compact image without
                # the module command, a runtime directory this process cannot
                # inspect) still upgrades its release, with the set left unknown.
                recorded = (
                    self._installed_modules()
                    if prepared.target_modules is not None
                    else self._installed_modules_if_known()
                )
                installed_modules = recorded or ()
                modules_required = self._modules_change(prepared, installed_modules)
                journal = ReleaseUpgradeJournal(
                    prepared=prepared,
                    prepared_modules=prepared.target_modules
                    if modules_required and prepared.target_modules is not None
                    else installed_modules,
                    previous_modules=recorded,
                    modules_required=modules_required,
                )
            elif journal.prepared.target_release != prepared.target_release or (
                prepared.target_modules is not None
                and prepared.target_modules != journal.prepared_modules
            ):
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
            if journal.modules_rollback_started:
                # An interrupted rollback is finished before anything else.
                self._rollback_modules(journal, sinks, failure=None, step="modules_rollback")
            if not journal.activation_completed:
                if not journal.activation_started:
                    # Repeat origin/configuration validation under the mutation
                    # lock before creating a dump or touching the checkout.
                    release._activate_upgrade(prepared.model_copy(update={"dry_run": True}))
                journal = self._backup(journal, sinks)
                if not journal.activation_started:
                    journal = journal.model_copy(update={"activation_started": True})
                    self.state.save_release_upgrade(journal)
                if journal.modules_required:
                    # The list is written only after the roles of the installed
                    # release are stopped (see ModuleActivator), so no role of the
                    # old release can start on the new set before this operation
                    # starts it again.
                    journal = self._stop_module_roles(journal, sinks)
                    if not journal.modules_activated:
                        journal = self._activate_modules(journal, sinks)
                if journal.module_only:
                    journal = self._finish_module_only(journal, sinks)
                else:
                    # Release change. The target module set is active before the
                    # release is: the installer's migration-history check and the
                    # one database upgrade (`upgrade_db` in the target image, Core's
                    # migrations then the modules') both read `modules.json`, so the
                    # target release's migrator sees the target module set, and the
                    # roles the installer recreates load it on their first start.
                    # Activating modules after the release would start the target
                    # roles on the old set and leave the new modules' schema
                    # unmigrated. There is one database step, not one per change.
                    activation_sinks = {**sinks, "held_locks": held_locks}
                    if prepared.transition_binding is not None:
                        activation_sinks["transition_binding"] = prepared.transition_binding
                    journal = self._activate_release(release, journal, activation_sinks, sinks)
                journal = journal.model_copy(update={"activation_completed": True})
                self.state.save_release_upgrade(journal)
        self._progress(sinks, "maintenance_disable", "Verify health and reopen the installation")
        self._cancelled(sinks, journal)
        # Compact's public mutation client acquires its own lifecycle lock.
        # Installer exclusion must be released before this final health gate.
        reopened = False
        try:
            self.maintenance.disable()
            reopened = True
            with self.state.operation_lock():
                journal = journal.model_copy(update={"completed": True})
                self.state.save_release_upgrade(journal)
        except BaseException as failure:
            # A signal may arrive after the terminal replacement succeeds.
            # Restore resumable state as well as maintenance before reporting
            # an incomplete command, so a same-target retry can reopen safely.
            try:
                with self.state.operation_lock():
                    journal = journal.model_copy(update={"completed": False})
                    self.state.save_release_upgrade(journal)
            finally:
                self.maintenance.enable()
            # Only a failed health gate is a reason to roll the module set back; a
            # failure to record completion after a healthy reopen is not.
            if not reopened and journal.module_only and self._rollback_applies(failure):
                with self.state.operation_lock():
                    self._rollback_modules(
                        journal, sinks, failure=failure, step="maintenance_disable"
                    )
            raise
        return ReleaseResult(
            status="upgraded",
            active_environment=release.environment,
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
                "platform_modules": self._module_summary(journal.prepared_modules),
                "platform_modules_changed": journal.modules_required,
            },
        )

    def _activate_release(
        self,
        release: ReleaseService,
        journal: ReleaseUpgradeJournal,
        activation_sinks: dict[str, object],
        sinks: dict[str, object],
    ) -> ReleaseUpgradeJournal:
        prepared = journal.prepared
        if journal.migration_required:
            self._migrate(journal, sinks)
        if prepared.target_release == prepared.installed_release:
            # A module-only change: the installed release stays active as it is.
            return journal
        if self.checkout_quiesce is not None:
            self._progress(sinks, "checkout_quiesce", "Stop the services that run checkout code")
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
        return journal

    def _stop_module_roles(
        self, journal: ReleaseUpgradeJournal, sinks: dict[str, object]
    ) -> ReleaseUpgradeJournal:
        if journal.modules_roles_stopped:
            return journal
        self._progress(sinks, "modules_quiesce", "Stop the roles that load platform modules")
        self._cancelled(sinks, journal)
        self.modules.stop_roles()
        journal = journal.model_copy(update={"modules_roles_stopped": True})
        self.state.save_release_upgrade(journal)
        return journal

    def _activate_modules(
        self, journal: ReleaseUpgradeJournal, sinks: dict[str, object]
    ) -> ReleaseUpgradeJournal:
        self._progress(sinks, "modules_activate", "Activate the pinned platform modules")
        self._cancelled(sinks, journal)
        if self.modules.supports_role_verification:
            # Each role's own image is the authority on what it will refuse at
            # start; without it the activator's CLI-side loader check is the fallback.
            self.modules.verify_in_roles(journal.prepared_modules)
        self.modules.activate(journal.prepared_modules)
        journal = journal.model_copy(update={"modules_activated": True})
        self.state.save_release_upgrade(journal)
        return journal

    def _finish_module_only(
        self, journal: ReleaseUpgradeJournal, sinks: dict[str, object]
    ) -> ReleaseUpgradeJournal:
        """Migrate the module schema, then start the roles on the activated set.

        The database step always runs for a module-only change rather than after a
        read-only inspection: ``ReleaseMigration`` has no inspection operation, and
        ``database upgrade`` is idempotent -- with nothing pending it applies
        nothing. It runs after the list is written because ``upgrade_db`` reads the
        module set from ``modules.json``, and before any role starts, so no role runs
        new module code against the old module schema.
        """
        if not journal.modules_migrated:
            if not journal.modules_migration_started:
                journal = journal.model_copy(update={"modules_migration_started": True})
                self.state.save_release_upgrade(journal)
            try:
                self._migrate(journal, sinks)
            except BaseException as failure:
                # The database step is the first check of the new set inside the
                # application image; a repeating failure there (module SQL guard,
                # schema ownership, downgrade, loader refusal) must not leave the
                # roles stopped with no way out.
                if not self._rollback_applies(failure):
                    raise
                self._rollback_modules(journal, sinks, failure=failure, step="database_upgrade")
            journal = journal.model_copy(update={"modules_migrated": True})
            self.state.save_release_upgrade(journal)
        if not journal.roles_restarted:
            try:
                journal = self._restart_roles(journal, sinks)
            except BaseException as failure:
                if not self._rollback_applies(failure):
                    raise
                self._rollback_modules(journal, sinks, failure=failure, step="modules_restart")
        return journal

    def _restart_roles(
        self, journal: ReleaseUpgradeJournal, sinks: dict[str, object]
    ) -> ReleaseUpgradeJournal:
        self._progress(sinks, "modules_restart", "Restart the roles that load platform modules")
        self._cancelled(sinks, journal)
        self.modules.restart_roles()
        journal = journal.model_copy(update={"roles_restarted": True})
        self.state.save_release_upgrade(journal)
        return journal

    @staticmethod
    def _rollback_applies(failure: BaseException) -> bool:
        """A failed step rolls back; an interruption or lost maintenance resumes instead."""
        if not isinstance(failure, Exception):
            return False
        return not (
            isinstance(failure, LifecycleError)
            and failure.code
            in {"lifecycle_cancelled", "lifecycle_forced_interrupt", "release_maintenance_required"}
        )

    def _restore_previous_modules(self, sinks: dict[str, object]) -> NoReturn:
        """The operator's explicit way back from an unfinished module-only change.

        Re-activates ``previous_modules``, restarts the roles and ends the journal as
        rolled back. The database is not touched, so this is valid only after the
        operator restored the pre-upgrade backup or confirmed that no migration of
        the new set was applied. Maintenance stays enabled.
        """
        with self.state.operation_lock():
            journal = self.state.load_release_upgrade()
            if (
                journal is None
                or journal.finished
                or not journal.module_only
                or journal.previous_modules is None
            ):
                raise LifecycleError.stable_failure(
                    code="release_modules_restore_unavailable",
                    cause=(
                        "There is no unfinished platform-module-only change whose previous "
                        "module set is recorded."
                    ),
                    remediation="Run release upgrade without restore_previous_modules.",
                    step="modules_rollback",
                )
            if self.maintenance.status().status != "enabled":
                self.maintenance.enable()
            if not journal.modules_rollback_started:
                journal = journal.model_copy(update={"modules_restore_requested": True})
                self.state.save_release_upgrade(journal)
            self._rollback_modules(
                journal, sinks, failure=None, step="modules_rollback", forced=True
            )

    def _rollback_modules(
        self,
        journal: ReleaseUpgradeJournal,
        sinks: dict[str, object],
        *,
        failure: BaseException | None,
        step: str,
        forced: bool = False,
    ) -> NoReturn:
        """Re-activate ``previous_modules`` after a module-only change failed.

        Always raises: the operation ends as failed. Maintenance stays enabled even
        after a successful rollback (ADR 0074 retains maintenance on every failed
        upgrade); the operator reads the logs and reopens with ``maintenance
        disable``, which the rolled-back journal no longer blocks.

        The automatic rollback is refused when the database step ran (or was
        started) for a set that ships a module migration the previous set lacks:
        module migrations are forward-only, the module runner refuses to run the
        previous set on the newer schema, and the release workflow has no read-only
        way to ask the database which of them were applied (``ReleaseMigration``
        offers only ``upgrade``). The journal then records the refusal, the roles
        stay stopped if they were, and two exits remain: repeat the same upgrade to
        go forward, or request ``restore_previous_modules`` (``forced``) after
        restoring the backup. Once ``modules_rollback_started`` is recorded the
        checks are not repeated, so an interrupted rollback always finishes.
        """
        cause = str(failure) if failure is not None else ""
        previous = journal.previous_modules
        if not journal.modules_rollback_started:
            refusal = self._rollback_refusal(journal, forced=forced)
            if refusal is not None:
                code, reason = refusal
                journal = journal.model_copy(
                    update={
                        "modules_rollback_refused": True,
                        "modules_rollback_refused_reason": f"{cause} {reason}".strip(),
                    }
                )
                self.state.save_release_upgrade(journal)
                raise LifecycleError.recovery_required(
                    code=code,
                    cause=f"{cause} {reason}".strip(),
                    remediation=self._refusal_remediation(journal),
                    step=step,
                    recovery_command=self._resume_command(journal),
                    details={"platform_module_roles_stopped": journal.module_roles_down},
                ) from failure
            journal = journal.model_copy(update={"modules_rollback_started": True})
            self.state.save_release_upgrade(journal)
        if previous is None:  # _rollback_refusal refuses this before the start
            raise LifecycleError.internal(
                step="modules_rollback", recovery_command=self._resume_command(journal)
            )
        self._progress(sinks, "modules_rollback", "Activate the previous platform modules again")
        # The list is rewritten only while the roles are stopped, as on the way in.
        self.modules.stop_roles()
        self.modules.activate(previous)
        self.modules.restart_roles()
        journal = journal.model_copy(update={"modules_rolled_back": True})
        self.state.save_release_upgrade(journal)
        reason = (
            "The previous platform-module set was restored on request"
            if journal.modules_restore_requested
            else f"{cause} The new platform-module set failed at {step}"
        )
        raise LifecycleError.stable_failure(
            code="release_modules_rolled_back",
            cause=(
                f"{reason.strip()}; the previous set ({self._module_names(previous)}) is "
                "active again and the roles are running."
            ),
            remediation=(
                "Maintenance stays enabled. Read ./flow-steward stack logs, then reopen "
                "with `flow-steward maintenance disable`, or stage a corrected module set "
                "and run release upgrade again."
            ),
            step="modules_rollback",
        ) from failure

    def _rollback_refusal(
        self, journal: ReleaseUpgradeJournal, *, forced: bool
    ) -> tuple[str, str] | None:
        previous = journal.previous_modules
        if previous is None:
            return (
                "release_modules_rollback_unavailable",
                "This journal does not record the previously installed module set.",
            )
        if forced:
            return None
        database_ran = journal.modules_migration_started or journal.modules_migrated
        if database_ran and self.modules.adds_module_migrations(previous, journal.prepared_modules):
            return (
                "release_modules_rollback_refused",
                "The new platform-module set ships module migrations that may already be "
                "applied, and module migrations are forward-only, so the previous set is "
                "not activated again automatically.",
            )
        return None

    def _refusal_remediation(self, journal: ReleaseUpgradeJournal) -> str:
        roles = (
            "The roles that load platform modules are stopped. "
            if journal.module_roles_down
            else ""
        )
        resume = self._resume_command(journal)
        if journal.previous_modules is None:
            return (
                f"Maintenance stays enabled. {roles}Correct the reported problem and "
                f"repeat `{resume}` to finish the new module set."
            )
        backup = journal.backup_path or "none: the backup was waived"
        return (
            f"Maintenance stays enabled. {roles}Choose one: correct the reported problem "
            f"and repeat `{resume}` to finish the new set; or restore the pre-upgrade "
            f"database backup ({backup}) as docs/operations/compact-scalable-deployment.md "
            "describes under 'Going back across a database migration', then run "
            "`flow-steward release modules-apply --restore-previous --confirm` "
            f"to activate the previous set ({self._module_names(journal.previous_modules)}) "
            "and restart the roles."
        )

    @staticmethod
    def _module_names(modules: tuple[PreparedModuleRef, ...]) -> str:
        return ", ".join(f"{module.id}@{module.version}" for module in modules) or "no modules"

    def require_module_support(self, prepared: PreparedReleaseUpgrade) -> None:
        """Refuse a module set this deployment cannot activate before any mutation."""
        if prepared.target_modules and not self.modules.supports_activation:
            reason = str(getattr(self.modules, "unavailable_reason", "") or "").strip()
            raise LifecycleError.stable_failure(
                code="release_modules_unsupported",
                cause=" ".join(
                    part
                    for part in ("This deployment cannot activate platform modules.", reason)
                    if part
                ),
                remediation="Repeat the release upgrade without a platform-module set.",
                step="release_policy",
            )

    def _installed_modules(self) -> tuple[PreparedModuleRef, ...]:
        return canonical_module_set(tuple(self.modules.installed_set()))

    def _installed_modules_if_known(self) -> tuple[PreparedModuleRef, ...] | None:
        try:
            return self._installed_modules()
        except LifecycleError:
            return None

    def _modules_change(
        self,
        prepared: PreparedReleaseUpgrade,
        installed: tuple[PreparedModuleRef, ...] | None = None,
    ) -> bool:
        if prepared.target_modules is None:
            return False
        return prepared.target_modules != (
            installed if installed is not None else self._installed_modules()
        )

    @staticmethod
    def _module_summary(modules: tuple[PreparedModuleRef, ...]) -> list[dict[str, str]]:
        return [module.model_dump(mode="json") for module in modules]

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
