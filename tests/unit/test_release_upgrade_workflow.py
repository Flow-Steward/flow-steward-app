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
