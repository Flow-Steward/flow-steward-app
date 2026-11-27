from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from core.application.lifecycle.backup_service import BackupResult, BackupVerificationStatus
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.service import ReleaseResult, ReleaseUpgradeRequest
from tests.unit.test_release_service import Harness


class WorkflowState:
    journal = None

    def load_release_upgrade(self):
        return self.journal

    def save_release_upgrade(self, journal):
        self.journal = journal

    @contextmanager
    def operation_lock(self):
        yield {}


class Maintenance:
    def __init__(self, events):
        self.events = events
        self.phase = "disabled"
        self.fail_disable = False

    def status(self):
        return MaintenanceStatusResult(status=self.phase)

    def enable(self):
        self.events.append("maintenance_on")
        self.phase = "enabled"

    def disable(self):
        self.events.append("maintenance_off")
        if self.fail_disable:
            raise RuntimeError("health failed")
        self.phase = "disabled"


class Backup:
    def __init__(self, events):
        self.events = events
        self.saved = {}
        self.fail = False

    def find_verified(self, operation_id):
        return self.saved.get(operation_id)

    def create(self, request, **_kwargs):
        self.events.append("backup")
        if self.fail:
            raise RuntimeError("backup failed")
        result = BackupResult(
            path="/backups/upgrade.dump.enc",
            size_bytes=123,
            created_at=datetime.now(UTC),
            verification_status=BackupVerificationStatus.VERIFIED,
        )
        self.saved[request.operation_id] = result
        return result


class Migration:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.fail = False

    def upgrade(self, *, skip_db_backup, timeout_seconds):
        self.events.append("migrate")
        self.calls.append(skip_db_backup)
        if self.fail:
            raise RuntimeError("migration failed")


@pytest.fixture
def workflow():
    from core.application.release.upgrade_workflow import ReleaseUpgradeWorkflow

    harness = Harness()
    events = []
    maintenance = Maintenance(events)
    harness.service.maintenance = maintenance
    backup = Backup(events)
    migration = Migration(events)
    state = WorkflowState()
    harness.service.workflow = ReleaseUpgradeWorkflow(
        state=state, maintenance=maintenance, backup=backup, migration=migration
    )
    outcomes = ["upgraded"]

    def activate(release, **kwargs):
        assert "held_locks" in kwargs
        events.append("activate")
        status = outcomes.pop(0)
        if status == "error":
            raise RuntimeError("activation failed")
        return ReleaseResult(status=status, target_release=release)

    harness.service.activator.activate = activate
    return harness, events, maintenance, backup, migration, state, outcomes


def test_release_request_accepts_explicit_backup_waiver():
    assert ReleaseUpgradeRequest(skip_db_backup=True).skip_db_backup is True


def test_upgrade_automates_maintenance_backup_and_healthy_reopen(workflow):
    harness, events, maintenance, _, _, state, _ = workflow
    result = harness.service.upgrade(ReleaseUpgradeRequest())
    assert events == ["maintenance_on", "backup", "activate", "maintenance_off"]
    assert result.status == "upgraded"
    assert result.maintenance_status.status == "disabled"
    assert result.next_steps == ()
    assert maintenance.phase == "disabled"
    assert state.journal.completed is True


@pytest.mark.parametrize("skip", [False, True])
def test_upgrade_runs_target_migrations_then_resumes_activation(workflow, skip):
    harness, events, _, _, migration, _, outcomes = workflow
    outcomes[:] = ["migration_required", "upgraded"]
    result = harness.service.upgrade(ReleaseUpgradeRequest(skip_db_backup=skip))
    assert events == [
        "maintenance_on",
        *([] if skip else ["backup"]),
        "activate",
        "migrate",
        "activate",
        "maintenance_off",
    ]
    assert migration.calls == [skip]
    assert result.status == "upgraded"


@pytest.mark.parametrize("failure", ["backup", "activation", "migration", "health"])
def test_failure_keeps_maintenance_enabled(workflow, failure):
    harness, events, maintenance, backup, migration, state, outcomes = workflow
    backup.fail = failure == "backup"
    migration.fail = failure == "migration"
    maintenance.fail_disable = failure == "health"
    outcomes[:] = (
        ["error"]
        if failure == "activation"
        else ["migration_required", "upgraded"]
        if failure == "migration"
        else ["upgraded"]
    )
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest())
    assert maintenance.phase == "enabled"
    assert state.journal.completed is False
    if failure == "backup":
        assert "activate" not in events


def test_resume_after_activation_failure_reuses_verified_backup(workflow):
    harness, events, _, _, _, _, outcomes = workflow
    outcomes[:] = ["error"]
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest())
    outcomes[:] = ["upgraded"]
    harness.service.upgrade(ReleaseUpgradeRequest())
    assert events.count("backup") == 1
    assert events[-1] == "maintenance_off"


def test_resume_after_health_failure_does_not_activate_or_backup_again(workflow):
    harness, events, maintenance, _, _, _, _ = workflow
    maintenance.fail_disable = True
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest())
    maintenance.fail_disable = False
    harness.service.upgrade(ReleaseUpgradeRequest())
    assert events.count("activate") == 1
    assert events.count("backup") == 1
    assert maintenance.phase == "disabled"


def test_resume_preserves_explicit_backup_waiver(workflow):
    harness, events, _, _, _, _, outcomes = workflow
    outcomes[:] = ["error"]
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest(skip_db_backup=True))
    outcomes[:] = ["migration_required", "upgraded"]
    harness.service.upgrade(ReleaseUpgradeRequest())
    assert "backup" not in events


def test_backup_failure_can_be_resumed_with_explicit_waiver(workflow):
    harness, events, _, backup, _, state, _ = workflow
    backup.fail = True
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest())
    harness.service.upgrade(ReleaseUpgradeRequest(skip_db_backup=True))
    assert state.journal.prepared.skip_db_backup is True
    assert events.count("backup") == 1


def test_dry_run_does_not_change_maintenance_or_create_backup(workflow):
    harness, events, _, _, _, state, _ = workflow
    result = harness.service.upgrade(ReleaseUpgradeRequest(dry_run=True))
    assert result.status == "planned"
    assert events == []
    assert state.journal is None


def test_already_current_does_not_enter_maintenance(workflow):
    harness, events, _, _, _, state, _ = workflow
    result = harness.service.upgrade(
        ReleaseUpgradeRequest(target_version=harness.installed.version)
    )
    assert result.status == "already_current"
    assert events == []
    assert state.journal is None


def test_resume_refuses_different_target(workflow):
    harness, _, _, _, _, _, outcomes = workflow
    outcomes[:] = ["error"]
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest())
    with pytest.raises(LifecycleError):
        harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.installed.version))


def test_resume_refuses_lost_pre_upgrade_backup(workflow):
    harness, events, maintenance, backup, _, _, outcomes = workflow
    outcomes[:] = ["error"]
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest())
    backup.saved.clear()
    with pytest.raises(LifecycleError, match="backup"):
        harness.service.upgrade(ReleaseUpgradeRequest())
    assert events.count("activate") == 1
    assert events.count("backup") == 1
    assert maintenance.phase == "enabled"


def test_cooperative_cancellation_keeps_maintenance_enabled(workflow):
    from core.application.lifecycle.contracts import CancellationToken

    harness, events, maintenance, _, _, _, _ = workflow
    token = CancellationToken()

    def progress(event):
        if event.step_id == "database_backup":
            token.request()

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(ReleaseUpgradeRequest(), cancellation=token, progress_sink=progress)
    assert error.value.code == "lifecycle_cancelled"
    assert events == ["maintenance_on"]
    assert maintenance.phase == "enabled"


def test_terminal_journal_failure_restores_maintenance(workflow):
    harness, _, maintenance, _, _, state, _ = workflow
    save = state.save_release_upgrade

    def fail_completed(journal):
        if journal.completed:
            raise OSError("journal unavailable")
        save(journal)

    state.save_release_upgrade = fail_completed
    with pytest.raises(OSError):
        harness.service.upgrade(ReleaseUpgradeRequest())
    assert maintenance.phase == "enabled"


def test_automatic_review_retains_origin_and_exact_target_context(workflow):
    harness, _, _, _, _, _, _ = workflow
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest(skip_db_backup=True))
    review = harness.service.review_upgrade(prepared)
    assert review["installed_version"] == harness.installed.version
    assert review["target_commit_sha"] == prepared.target_release.commit_sha
    assert review["skip_db_backup"] is True
    assert review["next_steps"] == []


def test_interrupt_after_terminal_write_restores_resumable_state(workflow):
    harness, events, maintenance, _, _, state, _ = workflow
    save = state.save_release_upgrade

    def interrupt_after_completed(journal):
        save(journal)
        if journal.completed:
            raise KeyboardInterrupt

    state.save_release_upgrade = interrupt_after_completed
    with pytest.raises(KeyboardInterrupt):
        harness.service.upgrade(ReleaseUpgradeRequest())
    assert maintenance.phase == "enabled"
    assert state.journal.completed is False
    state.save_release_upgrade = save
    harness.service.upgrade(ReleaseUpgradeRequest())
    assert maintenance.phase == "disabled"
    assert events.count("activate") == 1


def test_development_upgrade_stops_checkout_writers_right_before_activation(workflow):
    """Owner 2026-11-21 (D3): the handoff checks the release out under running services.

    A development installation's Core services run code bind-mounted from that
    checkout, so they are stopped after the verified backup and immediately before
    activation; the installer starts them again on the new images.
    """
    harness, events, *_ = workflow
    harness.service.workflow.checkout_quiesce = lambda: events.append("quiesce")

    result = harness.service.upgrade(ReleaseUpgradeRequest())

    assert result.status == "upgraded"
    assert events == ["maintenance_on", "backup", "quiesce", "activate", "maintenance_off"]


@pytest.mark.parametrize("request_kind", ("dry_run", "already_current"))
def test_development_upgrade_that_changes_nothing_stops_nothing(workflow, request_kind):
    """A plan or an already-current target never touches the running services."""
    harness, events, *_ = workflow
    harness.service.workflow.checkout_quiesce = lambda: events.append("quiesce")
    request = (
        ReleaseUpgradeRequest(dry_run=True)
        if request_kind == "dry_run"
        else ReleaseUpgradeRequest(target_version=harness.installed.version)
    )

    harness.service.upgrade(request)

    assert "quiesce" not in events


def test_checkout_writer_quiesce_stops_bind_mounted_services_and_frontend():
    """The quiesce stops the Core writers and the frontend dev server, nothing else."""
    from core.application.lifecycle.contracts import ComposeTarget
    from core.infrastructure.release.upgrade_gateway import (
        CheckoutWriterQuiesce,
        stop_checkout_writers,
    )

    calls = []

    def run_compose(target, arguments, **kwargs):
        calls.append(tuple(arguments))
        stdout = "fs-app\nfs-frontend\npg-fs\nfs-cli\ncontrol-plane\n"
        return type("Outcome", (), {"stdout": stdout if arguments[0] == "ps" else ""})()

    target = ComposeTarget(
        stack_environment="development",
        file_storage="bundled",
        env_file=".env",
        compose_files=("infra/docker/docker-compose.yaml",),
        compose_profiles=(),
        required_services=("fs-app",),
        runtime_services=("fs-app",),
        rebuild_services=("fs-app",),
        project_name="development-project",
    )
    CheckoutWriterQuiesce(
        target_provider=lambda: target,
        stop=lambda selected, *, environment: stop_checkout_writers(
            selected, run_compose=run_compose, environment=environment
        ),
        environment_provider=dict,
    )()

    assert calls == [
        ("ps", "--status", "running", "--services"),
        ("stop", "fs-app", "control-plane", "fs-frontend"),
    ]


def test_checkout_writer_quiesce_failure_keeps_a_typed_resumable_error():
    """A Docker failure surfaces as a stable lifecycle error naming the resume path."""
    from core.application.lifecycle.stack_service import StackGatewayError
    from core.infrastructure.release.upgrade_gateway import CheckoutWriterQuiesce

    def fail(_target, *, environment):
        raise StackGatewayError(action="release_quiesce", detail="docker unavailable")

    quiesce = CheckoutWriterQuiesce(
        target_provider=lambda: object(), stop=fail, environment_provider=dict
    )

    with pytest.raises(LifecycleError) as raised:
        quiesce()

    assert raised.value.code == "release_checkout_quiesce_failed"
    assert "release upgrade" in raised.value.remediation


def test_official_install_switched_to_development_upgrades_and_says_so(workflow):
    """Owner 2026-11-21: after `environment switch development` an official installation
    upgrades in development: the review says `development (unchanged)`, the checkout's
    writers are quiesced before activation, and the result names the environment."""
    harness, events, *_ = workflow
    harness.service.environment = "development"
    harness.service.workflow.checkout_quiesce = lambda: events.append("quiesce")

    review = harness.service.review_upgrade(
        harness.service.prepare_upgrade(ReleaseUpgradeRequest(dry_run=True))
    )
    result = harness.service.upgrade(ReleaseUpgradeRequest())

    assert review["active_environment"] == "development (unchanged)"
    assert result.status == "upgraded"
    assert result.active_environment == "development"
    assert events.index("quiesce") < events.index("activate")


class Modules:
    """Platform-module activator fake that records every stop, activation and restart."""

    supports_activation = True
    supports_role_verification = False
    unavailable_reason = ""

    def __init__(self, events):
        self.events = events
        self.installed = ()
        self.staged = ()
        self.calls = []
        self.restarts = 0
        self.fail = False
        # Number of restart_roles calls that fail before one succeeds.
        self.fail_restarts = 0
        self.restart_error: BaseException = RuntimeError("role restart failed")
        self.adds_migrations = False
        # What the running roles report as skipped, by the active module set.
        self.load_failures: dict = {}
        self.load_checks: list[bool] = []

    def installed_set(self):
        return self.installed

    def staged_modules(self):
        return self.staged

    def verify_in_roles(self, modules):
        raise AssertionError("not supported by this fake")

    def stop_roles(self):
        self.events.append("stop")

    def activate(self, modules):
        self.events.append("modules")
        self.calls.append(modules)
        if self.fail:
            raise RuntimeError("module activation failed")
        self.installed = modules

    def restart_roles(self):
        self.events.append("restart")
        self.restarts += 1
        if self.fail_restarts:
            self.fail_restarts -= 1
            raise self.restart_error

    def module_load_failures(self, *, wait):
        self.load_checks.append(wait)
        return tuple(self.load_failures.get(self.installed, ()))

    def adds_module_migrations(self, previous, target):
        return self.adds_migrations


def _module(module_id, version="1.0.0"):
    from core.application.release.platform_modules import PreparedModuleRef

    return PreparedModuleRef(id=module_id, version=version, digest="sha256:" + "ab" * 32)


@pytest.fixture
def module_workflow(workflow):
    harness, events, *_ = workflow
    modules = Modules(events)
    harness.service.workflow.modules = modules
    return (*workflow, modules)


def _interrupt_once_after(state, predicate):
    save = state.save_release_upgrade
    interrupted = []

    def interrupting_save(journal):
        save(journal)
        if predicate(journal) and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt

    state.save_release_upgrade = interrupting_save


def test_journal_round_trips_the_pinned_module_set():
    from core.application.release.upgrade_workflow import ReleaseUpgradeJournal

    prepared = Harness().service.prepare_upgrade(
        ReleaseUpgradeRequest(target_modules=(_module("reports"), _module("audit")))
    )
    journal = ReleaseUpgradeJournal(
        prepared=prepared,
        prepared_modules=prepared.target_modules,
        previous_modules=(_module("reports", "0.9.0"),),
        modules_required=True,
        modules_roles_stopped=True,
        modules_activated=True,
        modules_migrated=True,
        roles_restarted=True,
        modules_rollback_started=True,
        modules_rolled_back=True,
    )

    loaded = ReleaseUpgradeJournal.model_validate_json(journal.model_dump_json())

    assert loaded == journal
    assert [module.id for module in loaded.prepared_modules] == ["audit", "reports"]
    assert loaded.previous_modules == (_module("reports", "0.9.0"),)
    assert loaded.modules_activated is True
    assert loaded.finished is True


def test_journal_written_before_platform_modules_still_loads():
    from core.application.release.upgrade_workflow import ReleaseUpgradeJournal

    journal = ReleaseUpgradeJournal(
        prepared=Harness().service.prepare_upgrade(ReleaseUpgradeRequest()),
        activation_started=True,
    )
    payload = journal.model_dump(mode="json")
    for key in (
        "prepared_modules",
        "previous_modules",
        "modules_required",
        "modules_roles_stopped",
        "modules_activated",
        "modules_migrated",
        "roles_restarted",
        "modules_migration_started",
        "modules_rollback_started",
        "modules_rolled_back",
        "modules_rollback_refused",
        "modules_rollback_refused_reason",
        "modules_restore_requested",
    ):
        del payload[key]
    del payload["prepared"]["target_modules"]
    del payload["prepared"]["restore_previous_modules"]

    loaded = ReleaseUpgradeJournal.model_validate(payload)

    assert loaded.schema_version == 1
    assert loaded.prepared_modules == ()
    assert loaded.previous_modules is None
    assert loaded.modules_rolled_back is False
    assert loaded.modules_rollback_refused is False
    assert loaded.prepared.restore_previous_modules is False
    assert loaded.finished is False
    assert loaded.modules_required is False
    assert loaded.modules_activated is False
    assert loaded.activation_started is True


def test_module_set_refuses_two_versions_of_one_module():
    with pytest.raises(ValueError, match="one version"):
        ReleaseUpgradeRequest(target_modules=(_module("reports"), _module("reports", "2.0.0")))


def test_modules_are_activated_before_the_release(module_workflow):
    harness, events, maintenance, *_, state, _, modules = module_workflow
    target = (_module("reports"),)

    result = harness.service.upgrade(ReleaseUpgradeRequest(target_modules=target))

    assert result.status == "upgraded"
    assert events == [
        "maintenance_on",
        "backup",
        "stop",
        "modules",
        "activate",
        "maintenance_off",
    ]
    assert modules.calls == [target]
    assert modules.restarts == 0
    assert state.journal.modules_activated is True
    assert state.journal.roles_restarted is False
    assert maintenance.phase == "disabled"


def test_interruption_between_module_and_release_activation_resumes_at_release(
    module_workflow,
):
    harness, events, maintenance, *_, state, _, modules = module_workflow
    _interrupt_once_after(
        state, lambda journal: journal.modules_activated and not journal.activation_completed
    )
    with pytest.raises(KeyboardInterrupt):
        harness.service.upgrade(ReleaseUpgradeRequest(target_modules=(_module("reports"),)))
    assert "activate" not in events
    assert maintenance.phase == "enabled"

    harness.service.upgrade(ReleaseUpgradeRequest())

    assert len(modules.calls) == 1
    assert events.count("activate") == 1
    assert events.count("backup") == 1
    assert events.index("modules") < events.index("activate")
    assert maintenance.phase == "disabled"
    assert state.journal.completed is True


def test_resume_after_module_activation_failure_activates_the_pinned_set(module_workflow):
    harness, events, maintenance, *_, state, _, modules = module_workflow
    target = (_module("reports"),)
    modules.fail = True
    with pytest.raises(RuntimeError, match="module activation failed"):
        harness.service.upgrade(ReleaseUpgradeRequest(target_modules=target))
    assert state.journal.activation_started is True
    assert state.journal.modules_activated is False
    assert "activate" not in events
    assert maintenance.phase == "enabled"

    modules.fail = False
    # The resume request names no module set; the journal's pinned set is activated.
    result = harness.service.upgrade(ReleaseUpgradeRequest())

    assert result.status == "upgraded"
    assert modules.calls == [target, target]
    assert events.count("backup") == 1
    assert events.count("activate") == 1
    assert events[-1] == "maintenance_off"
    assert maintenance.phase == "disabled"
    assert state.journal.completed is True


def test_module_only_change_migrates_after_activation_and_before_restart(module_workflow):
    """Finding 3: the module schema is migrated on the activated set before any role starts."""
    harness, events, maintenance, _, migration, state, _, modules = module_workflow
    target = (_module("reports"),)

    result = harness.service.upgrade(
        ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=target)
    )

    assert result.status == "upgraded"
    assert events == [
        "maintenance_on",
        "backup",
        "stop",
        "modules",
        "migrate",
        "restart",
        "maintenance_off",
    ]
    assert migration.calls == [False]
    assert modules.calls == [target]
    assert modules.restarts == 1
    assert maintenance.phase == "disabled"
    assert state.journal.prepared_modules == target
    assert state.journal.previous_modules == ()
    assert state.journal.modules_migrated is True
    assert state.journal.roles_restarted is True
    assert state.journal.completed is True


@pytest.mark.parametrize(
    "boundary",
    [
        "modules_roles_stopped",
        "modules_activated",
        "modules_migrated",
        "roles_restarted",
    ],
)
def test_module_only_change_resumes_at_each_boundary(module_workflow, boundary):
    harness, events, maintenance, _, migration, state, _, modules = module_workflow
    order = ["modules_roles_stopped", "modules_activated", "modules_migrated", "roles_restarted"]
    following = order[order.index(boundary) + 1 :]
    request = ReleaseUpgradeRequest(
        target_version=harness.installed.version, target_modules=(_module("reports"),)
    )
    _interrupt_once_after(
        state,
        lambda journal: (
            getattr(journal, boundary)
            and not any(getattr(journal, field) for field in following)
            and not journal.activation_completed
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        harness.service.upgrade(request)
    assert maintenance.phase == "enabled"

    harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.installed.version))

    # Every step ran exactly once across the interrupted run and the resume.
    assert events.count("stop") == 1
    assert len(modules.calls) == 1
    assert len(migration.calls) == 1
    assert modules.restarts == 1
    assert events.count("backup") == 1
    assert "activate" not in events
    assert events.index("modules") < events.index("migrate") < events.index("restart")
    assert maintenance.phase == "disabled"
    assert state.journal.completed is True


def test_release_and_module_change_activates_modules_before_the_single_migration(
    module_workflow,
):
    """The target release's migrator runs once, after the target module set is active."""
    harness, events, maintenance, _, migration, state, outcomes, modules = module_workflow
    outcomes[:] = ["migration_required", "upgraded"]

    result = harness.service.upgrade(ReleaseUpgradeRequest(target_modules=(_module("reports"),)))

    assert result.status == "upgraded"
    assert events == [
        "maintenance_on",
        "backup",
        "stop",
        "modules",
        "activate",
        "migrate",
        "activate",
        "maintenance_off",
    ]
    assert len(migration.calls) == 1
    # The installer recreates the roles on the target release; nothing restarts here.
    assert modules.restarts == 0
    assert state.journal.modules_migrated is False
    assert state.journal.migration_required is True
    assert maintenance.phase == "disabled"


@pytest.mark.parametrize("failure", ["restart", "health"])
def test_module_only_failure_without_new_migrations_rolls_back(module_workflow, failure):
    harness, events, maintenance, *_, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    if failure == "restart":
        modules.fail_restarts = 1
    else:
        maintenance.fail_disable = True

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(
            ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=target)
        )

    assert error.value.code == "release_modules_rolled_back"
    assert modules.calls == [target, previous]
    assert modules.installed == previous
    assert events[-1] == "restart"
    assert state.journal.previous_modules == previous
    assert state.journal.modules_rollback_started is True
    assert state.journal.modules_rolled_back is True
    assert state.journal.completed is False
    # Maintenance is retained after a successful rollback; the operator reopens.
    assert maintenance.phase == "enabled"

    # The rolled-back journal pins nothing: an unchanged request is already current.
    maintenance.fail_disable = False
    result = harness.service.upgrade(
        ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=previous)
    )
    assert result.status == "already_current"


def test_module_only_failure_after_module_migrations_is_not_rolled_back(module_workflow):
    harness, _events, maintenance, *_, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    modules.adds_migrations = True
    modules.fail_restarts = 1
    request = ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=target)

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(request)

    assert error.value.code == "release_modules_rollback_refused"
    assert "forward-only" in error.value.cause
    assert "release upgrade" in error.value.remediation
    assert "backup" in error.value.remediation
    assert modules.calls == [target]
    assert modules.installed == target
    assert state.journal.modules_rollback_started is False
    assert state.journal.modules_rolled_back is False
    assert state.journal.modules_rollback_refused is True
    assert "role restart failed" in state.journal.modules_rollback_refused_reason
    assert maintenance.phase == "enabled"

    # Rolling forward with the same command finishes the new set.
    harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.installed.version))

    assert modules.calls == [target]
    assert modules.restarts == 2
    assert maintenance.phase == "disabled"
    assert state.journal.completed is True


def _load_failure(module_id, version="1.0.0", role="worker", reason="ImportError: boom"):
    return {"role": role, "id": module_id, "version": version, "load_error": reason}


def test_module_that_fails_to_load_after_the_restart_rolls_the_set_back(module_workflow):
    """ADR 0082 §5 item 7: a role keeps running without a failed module, so the
    workflow reads the roles' load status and rolls back as for a failed restart."""
    harness, events, maintenance, *_, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    modules.load_failures = {target: [_load_failure("reports")]}

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(_module_only_request(harness, target))

    assert error.value.code == "release_modules_rolled_back"
    assert "reports 1.0.0 in the worker role: ImportError: boom" in error.value.cause
    assert "modules_verify" in error.value.cause
    assert "reports@0.9.0" in error.value.cause
    assert modules.calls == [target, previous]
    assert modules.installed == previous
    # Read once before the roles stop, once (waiting) after the restart.
    assert modules.load_checks == [False, True]
    assert events[-1] == "restart"
    assert "maintenance_off" not in events
    assert maintenance.phase == "enabled"
    assert state.journal.modules_load_verified is False
    assert state.journal.modules_rolled_back is True


def test_failures_present_before_the_change_do_not_fail_it(module_workflow):
    harness, _, maintenance, *_, state, _, modules = module_workflow
    audit = _module("audit")
    previous = (audit, _module("reports", "0.9.0"))
    target = (audit, _module("reports"))
    modules.installed = previous
    still_failing = [_load_failure("audit", role="app")]
    modules.load_failures = {previous: still_failing, target: still_failing}

    result = harness.service.upgrade(_module_only_request(harness, target))

    assert result.status == "upgraded"
    assert modules.calls == [target]
    assert state.journal.modules_failed_before == ("app audit 1.0.0",)
    assert state.journal.modules_load_verified is True
    assert maintenance.phase == "disabled"


def test_the_same_module_failing_in_another_role_is_a_new_failure(module_workflow):
    harness, *_, state, _, modules = module_workflow
    audit = _module("audit")
    previous = (audit, _module("reports", "0.9.0"))
    target = (audit, _module("reports"))
    modules.installed = previous
    modules.load_failures = {
        previous: [_load_failure("audit", role="app")],
        target: [_load_failure("audit", role="app"), _load_failure("audit", role="control")],
    }

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(_module_only_request(harness, target))

    assert error.value.code == "release_modules_rolled_back"
    assert "audit 1.0.0 in the control role" in error.value.cause
    assert "in the app role" not in error.value.cause
    assert state.journal.modules_rolled_back is True


def test_load_failure_after_module_migrations_is_refused_not_rolled_back(module_workflow):
    harness, _, maintenance, *_, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    modules.adds_migrations = True
    modules.load_failures = {target: [_load_failure("reports", role="app")]}

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(_module_only_request(harness, target))

    assert error.value.code == "release_modules_rollback_refused"
    assert "reports 1.0.0 in the app role" in error.value.cause
    assert modules.calls == [target]
    assert state.journal.modules_rollback_refused is True
    assert "reports 1.0.0" in state.journal.modules_rollback_refused_reason
    assert state.journal.modules_load_verified is False
    assert maintenance.phase == "enabled"


def test_load_check_runs_when_resumed_after_the_restart(module_workflow):
    harness, _, maintenance, *_, state, _, modules = module_workflow
    target = (_module("reports"),)
    _interrupt_once_after(
        state,
        lambda journal: journal.roles_restarted and not journal.modules_load_verified,
    )

    with pytest.raises(KeyboardInterrupt):
        harness.service.upgrade(_module_only_request(harness, target))
    assert modules.load_checks == [False]

    harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.installed.version))

    assert modules.load_checks == [False, True]
    assert modules.restarts == 1
    assert state.journal.modules_load_verified is True
    assert maintenance.phase == "disabled"


def test_a_release_change_does_not_read_the_load_status(module_workflow):
    """The installer recreates the roles on a release change; its own gate applies."""
    harness, _, _, _, _, state, outcomes, modules = module_workflow
    outcomes[:] = ["upgraded"]

    harness.service.upgrade(ReleaseUpgradeRequest(target_modules=(_module("reports"),)))

    assert modules.load_checks == []
    assert state.journal.modules_failed_before == ()


def test_interrupted_rollback_is_finished_by_the_next_run(module_workflow):
    harness, _, maintenance, *_, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    modules.fail_restarts = 2  # the new set's restart, then the rollback's restart
    request = ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=target)

    with pytest.raises(RuntimeError):
        harness.service.upgrade(request)
    assert state.journal.modules_rollback_started is True
    assert state.journal.modules_rolled_back is False
    assert maintenance.phase == "enabled"

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.installed.version))

    assert error.value.code == "release_modules_rolled_back"
    assert modules.calls == [target, previous, previous]
    assert modules.installed == previous
    assert state.journal.modules_rolled_back is True
    assert maintenance.phase == "enabled"


def test_cancellation_during_restart_resumes_instead_of_rolling_back(module_workflow):
    harness, _, maintenance, *_, state, _, modules = module_workflow
    modules.installed = (_module("reports", "0.9.0"),)
    modules.fail_restarts = 1
    modules.restart_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        harness.service.upgrade(
            ReleaseUpgradeRequest(
                target_version=harness.installed.version, target_modules=(_module("reports"),)
            )
        )

    assert len(modules.calls) == 1
    assert state.journal.modules_rollback_started is False
    assert maintenance.phase == "enabled"


def test_journal_without_previous_set_is_never_rolled_back(module_workflow):
    harness, _, maintenance, *_, state, _, modules = module_workflow
    target = (_module("reports"),)
    modules.fail_restarts = 1
    save = state.save_release_upgrade

    def save_as_older_journal(journal):
        save(journal.model_copy(update={"previous_modules": None}))

    state.save_release_upgrade = save_as_older_journal

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(
            ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=target)
        )

    assert error.value.code == "release_modules_rollback_unavailable"
    assert modules.calls == [target]
    assert maintenance.phase == "enabled"


def test_ordinary_upgrade_without_a_requested_set_never_activates_modules(module_workflow):
    harness, events, *_, state, _, modules = module_workflow
    modules.installed = (_module("reports"),)

    result = harness.service.upgrade(ReleaseUpgradeRequest())

    assert result.status == "upgraded"
    assert modules.calls == []
    assert modules.restarts == 0
    assert events == ["maintenance_on", "backup", "activate", "maintenance_off"]
    assert state.journal.prepared_modules == (_module("reports"),)
    assert state.journal.modules_required is False


def test_requested_set_equal_to_installed_is_not_activated(module_workflow):
    harness, events, *_, state, _, modules = module_workflow
    modules.installed = (_module("reports"),)

    harness.service.upgrade(ReleaseUpgradeRequest(target_modules=(_module("reports"),)))

    assert modules.calls == []
    assert "modules" not in events
    assert state.journal.modules_required is False


def test_unchanged_release_and_module_set_still_short_circuits(module_workflow):
    harness, events, *_, state, _, modules = module_workflow
    modules.installed = (_module("reports"),)

    result = harness.service.upgrade(
        ReleaseUpgradeRequest(
            target_version=harness.installed.version, target_modules=(_module("reports"),)
        )
    )

    assert result.status == "already_current"
    assert events == []
    assert modules.calls == []
    assert state.journal is None


@pytest.mark.parametrize("path", ["upgrade", "review"])
def test_module_set_is_refused_before_maintenance_without_activation_support(workflow, path):
    harness, events, maintenance, *_, state, _ = workflow
    request = ReleaseUpgradeRequest(target_modules=(_module("reports"),))

    with pytest.raises(LifecycleError) as error:
        if path == "upgrade":
            harness.service.upgrade(request)
        else:
            harness.service.review_upgrade(harness.service.prepare_upgrade(request))

    assert error.value.code == "release_modules_unsupported"
    assert events == []
    assert maintenance.phase == "disabled"
    assert state.journal is None


def test_module_activation_failure_keeps_maintenance_enabled(module_workflow):
    harness, events, maintenance, *_, state, _, modules = module_workflow
    modules.fail = True

    with pytest.raises(RuntimeError):
        harness.service.upgrade(
            ReleaseUpgradeRequest(
                target_version=harness.installed.version, target_modules=(_module("reports"),)
            )
        )

    assert maintenance.phase == "enabled"
    assert "maintenance_off" not in events
    assert state.journal.completed is False
    assert state.journal.modules_activated is False


def test_resume_refuses_a_different_module_set(module_workflow):
    harness, *_, modules = module_workflow
    modules.fail = True
    with pytest.raises(RuntimeError):
        harness.service.upgrade(ReleaseUpgradeRequest(target_modules=(_module("reports"),)))

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(ReleaseUpgradeRequest(target_modules=(_module("audit"),)))

    assert error.value.code == "release_upgrade_state_changed"


def test_module_only_change_activates_staged_artifacts_on_disk(workflow, tmp_path):
    from core.infrastructure.release.platform_module_activator import (
        FilesystemModuleActivator,
    )
    from tests.unit.test_platform_module_activator import (
        CACHE_TAG,
        MACHINE,
        FakeRestarter,
        _stage,
    )

    harness, events, maintenance, *_, state, _ = workflow
    root = tmp_path / "runtime" / "platform-modules"
    root.mkdir(parents=True)
    restarter = FakeRestarter()
    activator = FilesystemModuleActivator(
        root, restarter=restarter, cache_tag=CACHE_TAG, machine=MACHINE
    )
    harness.service.workflow.modules = activator
    target = (_stage(root, "acme.ledger", "1.0.0"),)

    result = harness.service.upgrade(
        ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=target)
    )

    assert result.status == "upgraded"
    assert "activate" not in events
    assert events.index("migrate") < events.index("maintenance_off")
    assert activator.installed_set() == target
    assert restarter.stopped == list(reversed(restarter.roles))
    assert restarter.restarted == list(restarter.roles)
    assert state.journal.modules_activated is True
    assert state.journal.roles_restarted is True
    assert maintenance.phase == "disabled"


def _module_only_request(harness, target):
    return ReleaseUpgradeRequest(target_version=harness.installed.version, target_modules=target)


def test_database_failure_without_new_migrations_rolls_back(module_workflow):
    """Blocker: a repeating database-step failure restarts the roles on the previous set."""
    harness, events, maintenance, _, migration, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    migration.fail = True

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(_module_only_request(harness, target))

    assert error.value.code == "release_modules_rolled_back"
    assert "database_upgrade" in error.value.cause
    assert events[events.index("migrate") :] == ["migrate", "stop", "modules", "restart"]
    assert modules.calls == [target, previous]
    assert modules.installed == previous
    assert modules.restarts == 1
    journal = state.journal
    assert journal.modules_migration_started is True
    assert journal.modules_migrated is False
    assert journal.modules_rolled_back is True
    assert journal.finished is True
    assert journal.module_roles_down is False
    assert journal.active_modules == previous
    assert maintenance.phase == "enabled"

    # Nothing is left to resume: an unchanged request is already current.
    migration.fail = False
    result = harness.service.upgrade(_module_only_request(harness, previous))
    assert result.status == "already_current"
    assert len(migration.calls) == 1


def test_database_failure_with_new_migrations_is_refused_and_can_go_forward(module_workflow):
    """Without a read-only inspection the workflow cannot tell whether a module
    migration was applied before the failure (before any file or after one), so a
    set that adds migrations is refused in both cases, with the roles kept stopped
    and both exits named."""
    harness, events, maintenance, _, migration, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    modules.adds_migrations = True
    migration.fail = True

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(_module_only_request(harness, target))

    assert error.value.code == "release_modules_rollback_refused"
    assert error.value.step == "database_upgrade"
    assert error.value.details == {"platform_module_roles_stopped": True}
    assert "are stopped" in error.value.remediation
    assert "release upgrade --target-version" in error.value.remediation
    assert "release modules-apply --restore-previous --confirm" in error.value.remediation
    assert modules.calls == [target]
    assert modules.restarts == 0
    journal = state.journal
    assert journal.modules_rollback_refused is True
    assert "migration failed" in journal.modules_rollback_refused_reason
    assert journal.module_roles_down is True
    assert journal.finished is False
    assert maintenance.phase == "enabled"

    # Exit 1: repeat the same command; the database step runs again, then the restart.
    migration.fail = False
    result = harness.service.upgrade(
        ReleaseUpgradeRequest(target_version=harness.installed.version)
    )

    assert result.status == "upgraded"
    assert len(migration.calls) == 2
    assert modules.calls == [target]
    assert modules.restarts == 1
    assert events.count("backup") == 1
    assert state.journal.completed is True
    assert maintenance.phase == "disabled"


def test_restore_previous_modules_after_a_refused_rollback(module_workflow):
    """Exit 2: the operator restored the backup and explicitly goes back."""
    harness, _, maintenance, _, migration, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    modules.adds_migrations = True
    migration.fail = True
    with pytest.raises(LifecycleError):
        harness.service.upgrade(_module_only_request(harness, target))

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(
            ReleaseUpgradeRequest(
                target_version=harness.installed.version, restore_previous_modules=True
            )
        )

    assert error.value.code == "release_modules_rolled_back"
    assert "restored on request" in error.value.cause
    # The database is not touched by the restore.
    assert len(migration.calls) == 1
    assert modules.calls == [target, previous]
    assert modules.installed == previous
    assert modules.restarts == 1
    journal = state.journal
    assert journal.modules_restore_requested is True
    assert journal.modules_rolled_back is True
    assert journal.finished is True
    assert maintenance.phase == "enabled"


def test_interrupted_restore_is_finished_by_the_next_plain_run(module_workflow):
    harness, _, maintenance, _, migration, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    modules.adds_migrations = True
    migration.fail = True
    with pytest.raises(LifecycleError):
        harness.service.upgrade(_module_only_request(harness, target))
    modules.fail_restarts = 1
    with pytest.raises(RuntimeError, match="role restart failed"):
        harness.service.upgrade(
            ReleaseUpgradeRequest(
                target_version=harness.installed.version, restore_previous_modules=True
            )
        )
    assert state.journal.modules_rollback_started is True

    # The resume does not re-apply the refusal rule: the operator's choice stands.
    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.installed.version))

    assert error.value.code == "release_modules_rolled_back"
    assert modules.installed == previous
    assert state.journal.modules_rolled_back is True
    assert len(migration.calls) == 1
    assert maintenance.phase == "enabled"


def test_restore_previous_modules_without_an_unfinished_change_is_refused(module_workflow):
    harness, events, maintenance, *_, state, _, modules = module_workflow

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(
            ReleaseUpgradeRequest(
                target_version=harness.installed.version, restore_previous_modules=True
            )
        )

    assert error.value.code == "release_modules_restore_unavailable"
    assert events == []
    assert modules.calls == []
    assert state.journal is None
    assert maintenance.phase == "disabled"


def test_database_failure_rollback_resumes_after_interruption(module_workflow):
    harness, _, maintenance, _, migration, state, _, modules = module_workflow
    previous = (_module("reports", "0.9.0"),)
    target = (_module("reports"),)
    modules.installed = previous
    migration.fail = True
    modules.fail_restarts = 1  # the rollback's own restart fails once

    with pytest.raises(RuntimeError, match="role restart failed"):
        harness.service.upgrade(_module_only_request(harness, target))
    assert state.journal.modules_rollback_started is True
    assert state.journal.modules_rolled_back is False

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.installed.version))

    assert error.value.code == "release_modules_rolled_back"
    assert modules.installed == previous
    assert len(migration.calls) == 1
    assert state.journal.modules_rolled_back is True
    assert maintenance.phase == "enabled"


# -- ADR 0082 §5 follow-up: robust composition and the operator's module change ----


def test_unprobeable_module_root_never_blocks_unrelated_commands(workflow, tmp_path, monkeypatch):
    """Evidence 2026-11-25: every management command failed with ``[Errno 13]``.

    The release service is composed by the real runtime while probing the module root
    raises PermissionError. Composition must not probe at all; an unrelated upgrade
    still runs; a request that names platform modules is refused before maintenance,
    and the refusal says why.
    """
    from pathlib import Path
    from types import SimpleNamespace

    import core.infrastructure.platform_modules.loader as loader
    from core.application.installer.contracts import InstallConfig
    from core.application.release.platform_modules import LazyModuleActivator
    from core.entrypoints.cli.runtime import CLIRuntime

    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(loader, "platform_modules_root", lambda: runtime_dir / "platform-modules")
    real_is_dir = Path.is_dir
    probes = []

    def is_dir(self, *args, **kwargs):
        if self == runtime_dir:
            probes.append(self)
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    active = InstallConfig(environment="production", deployment_profile="scalable")

    class Store:
        def read_durable_environment(self, config):
            assert config == active
            return {}

    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda required=False: active, store=Store()
    )
    runtime.__dict__["maintenance_service"] = SimpleNamespace()

    composed = runtime.release_service.workflow.modules

    assert isinstance(composed, LazyModuleActivator)
    assert composed.built is False
    assert probes == []

    harness, events, maintenance, *_, state, _ = workflow
    harness.service.workflow.modules = composed
    result = harness.service.upgrade(ReleaseUpgradeRequest())

    # The ordinary upgrade records the installed set only where it can be read: the
    # probe answered "not available here" instead of failing the upgrade.
    assert result.status == "upgraded"
    assert probes == [runtime_dir]
    assert state.journal.prepared_modules == ()

    events.clear()
    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(
            ReleaseUpgradeRequest(
                target_version=harness.installed.version, target_modules=(_module("reports"),)
            )
        )

    assert error.value.code == "release_modules_unsupported"
    assert "cannot be inspected" in error.value.cause
    assert "Permission denied" in error.value.cause
    assert probes == [runtime_dir]  # the factory ran once
    assert events == []
    assert maintenance.phase == "disabled"
    assert state.journal.completed is True


def test_module_upgrade_request_pins_the_highest_staged_versions(module_workflow):
    harness, events, *_, state, _, modules = module_workflow
    old = _module("reports", "0.9.0")
    new = _module("reports", "1.0.0")
    audit = _module("audit", "2.0.0")
    modules.staged = (old, new, audit)

    request = harness.service.module_upgrade_request(_modules_apply())
    result = harness.service.upgrade(harness.service.prepare_upgrade(request))

    assert request.target_version == harness.installed.version
    assert request.target_modules == (audit, new)
    assert result.status == "upgraded"
    assert modules.calls == [(audit, new)]
    assert "activate" not in events
    assert state.journal.module_only is True


def test_module_upgrade_request_pins_the_named_versions(module_workflow):
    harness, *_, modules = module_workflow
    old = _module("reports", "0.9.0")
    modules.staged = (old, _module("reports", "1.0.0"))

    request = harness.service.module_upgrade_request(_modules_apply(modules="reports@0.9.0"))

    assert request.target_modules == (old,)


@pytest.mark.parametrize(
    ("selection", "code"),
    [
        ("reports@3.0.0", "release_module_artifact_missing"),
        ("reports", "release_module_selection_invalid"),
        ("reports@1.0.0,reports@0.9.0", "release_module_selection_invalid"),
    ],
)
def test_module_upgrade_request_refuses_an_unusable_selection(module_workflow, selection, code):
    harness, events, *_, modules = module_workflow
    modules.staged = (_module("reports", "0.9.0"), _module("reports", "1.0.0"))

    with pytest.raises(LifecycleError) as error:
        harness.service.module_upgrade_request(_modules_apply(modules=selection))

    assert error.value.code == code
    assert events == []


def test_module_upgrade_request_refuses_when_nothing_is_staged(module_workflow):
    harness, *_ = module_workflow

    with pytest.raises(LifecycleError) as error:
        harness.service.module_upgrade_request(_modules_apply())

    assert error.value.code == "release_modules_none_staged"


def test_module_upgrade_request_maps_restore_previous(module_workflow):
    harness, *_ = module_workflow

    request = harness.service.module_upgrade_request(_modules_apply(restore_previous=True))

    assert request.target_version == harness.installed.version
    assert request.restore_previous_modules is True
    assert request.target_modules is None


def test_module_upgrade_request_refuses_a_source_installation(module_workflow):
    from tests.unit.test_release_service import _source_origin

    harness, *_, modules = module_workflow
    modules.staged = (_module("reports"),)
    harness.origin.value = _source_origin()

    with pytest.raises(LifecycleError) as error:
        harness.service.module_upgrade_request(_modules_apply())

    assert error.value.code == "release_modules_source_installation"


def test_module_upgrade_request_names_why_activation_is_unavailable(workflow):
    from core.application.release.platform_modules import NoModuleActivator

    harness, *_ = workflow
    harness.service.workflow.modules = NoModuleActivator(reason="No runtime directory here.")

    with pytest.raises(LifecycleError) as error:
        harness.service.module_upgrade_request(_modules_apply())

    assert error.value.code == "release_modules_unsupported"
    assert "No runtime directory here." in error.value.cause


def test_modules_list_reports_active_and_staged_sets(module_workflow):
    harness, events, *_, modules = module_workflow
    active = _module("reports", "0.9.0")
    modules.installed = (active,)
    modules.staged = (active, _module("reports", "1.0.0"))

    from core.application.release.service import ReleaseModulesListRequest

    result = harness.service.modules_list(ReleaseModulesListRequest())

    assert result.activation_supported is True
    assert result.active_modules == (active,)
    assert result.staged_modules == modules.staged
    assert result.next_steps == ("flow-steward release modules-apply --confirm",)
    assert events == []


def test_modules_list_without_support_says_why(workflow):
    from core.application.release.platform_modules import NoModuleActivator
    from core.application.release.service import ReleaseModulesListRequest

    harness, *_ = workflow
    harness.service.workflow.modules = NoModuleActivator(reason="Not here.")

    result = harness.service.modules_list(ReleaseModulesListRequest())

    assert result.activation_supported is False
    assert result.detail == "Not here."
    assert result.staged_modules == ()


def _modules_apply(**values):
    from core.application.release.service import ReleaseModulesApplyRequest

    return ReleaseModulesApplyRequest(**values)


def test_ordinary_upgrade_survives_an_unreadable_installed_set(module_workflow):
    """An older Compact image has no module command; its release must still upgrade."""
    harness, events, *_, state, _, modules = module_workflow

    def unreadable():
        raise LifecycleError.stable_failure(
            code="release_module_command_failed",
            cause="No module named core.entrypoints.cli.ops.platform_modules",
            remediation="",
            step="modules_inspect",
        )

    modules.installed_set = unreadable

    result = harness.service.upgrade(ReleaseUpgradeRequest())

    assert result.status == "upgraded"
    assert state.journal.previous_modules is None
    assert state.journal.modules_required is False
    assert events == ["maintenance_on", "backup", "activate", "maintenance_off"]


def test_module_change_still_fails_closed_on_an_unreadable_installed_set(module_workflow):
    harness, events, maintenance, *_, modules = module_workflow

    def unreadable():
        raise LifecycleError.stable_failure(
            code="release_module_command_failed", cause="gone", remediation="", step="x"
        )

    modules.installed_set = unreadable

    with pytest.raises(LifecycleError) as error:
        harness.service.upgrade(
            ReleaseUpgradeRequest(
                target_version=harness.installed.version, target_modules=(_module("reports"),)
            )
        )

    assert error.value.code == "release_module_command_failed"
    assert "maintenance_on" not in events
    assert maintenance.phase == "disabled"
