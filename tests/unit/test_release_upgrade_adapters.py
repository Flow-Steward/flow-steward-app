"""Target image migration and durable lifecycle guard regression tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import StackGatewayError
from core.application.release.service import ReleaseUpgradeRequest
from core.application.release.upgrade_workflow import ReleaseUpgradeJournal
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.docker_runtime_probes import compact_maintenance_operation
from core.infrastructure.lifecycle.state_store import LifecycleStateStore
from core.infrastructure.release.upgrade_gateway import (
    TargetReleaseMigration,
    upgrade_target_database,
)
from tests.unit.maintenance.test_compact_maintenance_client import _target
from tests.unit.test_release_service import Harness


@pytest.mark.parametrize("profile", ["compact", "scalable"])
@pytest.mark.parametrize("skip", [False, True])
def test_target_migration_stops_only_writers_and_passes_policy(profile, skip):
    target = _target().model_copy(update={"deployment_profile": profile})
    calls = []
    environment = {"FS_BUILD_SHA": "target-revision"}

    def run(target, arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[0] == "ps":
            return SimpleNamespace(
                stdout="pg-fs\nrabbitmq\ncompact-runtime\nfs-app\nfs-worker\nfs-cli\n"
            )
        return SimpleNamespace(stdout='{"ok":true}')

    upgrade_target_database(
        target, skip_db_backup=skip, timeout_seconds=900, run_compose=run, environment=environment
    )
    assert calls[1][0] == (
        ("stop", "compact-runtime") if profile == "compact" else ("stop", "fs-app", "fs-worker")
    )
    command = calls[-1][0]
    assert command[:5] == ("run", "--rm", "--no-deps", "-T", "--entrypoint")
    assert "--maintenance-confirmed" in command
    assert ("--skip-db-backup" in command) is skip
    assert ("core.entrypoints.cli.compact_container_cli" in command) is (profile == "compact")
    assert all(kwargs["environment"] == environment for _, kwargs in calls)


def test_target_migration_does_not_run_after_stop_failure():
    calls = []

    def run(target, arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "stop":
            raise StackGatewayError(action="database_upgrade", detail="stop failed")
        return SimpleNamespace(stdout="compact-runtime\n")

    with pytest.raises(StackGatewayError):
        upgrade_target_database(
            _target(), skip_db_backup=True, timeout_seconds=900, run_compose=run, environment={}
        )
    assert [command[0] for command in calls] == ["ps", "stop"]


def test_migration_adapter_reads_refreshed_environment_and_maps_failure():
    observed = {}

    def upgrade(target, **kwargs):
        observed.update(kwargs)
        raise StackGatewayError(action="database_upgrade", detail="migration failed")

    migration = TargetReleaseMigration(
        target_provider=_target,
        upgrade=upgrade,
        environment_provider=lambda: {"FS_BUILD_SHA": "target"},
    )
    with pytest.raises(LifecycleError) as error:
        migration.upgrade(skip_db_backup=True, timeout_seconds=123)
    assert error.value.code == "release_database_upgrade_failed"
    assert observed == {
        "skip_db_backup": True,
        "timeout_seconds": 123,
        "environment": {"FS_BUILD_SHA": "target"},
    }


def test_stopped_compact_status_reads_existing_marker_without_starting_writers():
    calls = []

    def run(target, arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "exec":
            raise StackGatewayError(action="maintenance", detail="stopped")
        if arguments[0] == "ps":
            return SimpleNamespace(stdout="")
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "schema": "flow-steward.cli.v1",
                    "ok": True,
                    "command": "maintenance.status",
                    "result": {"status": "enabled"},
                }
            )
        )

    assert compact_maintenance_operation(_target(), "maintenance-status", run_compose=run) == {
        "status": "enabled"
    }
    assert [command[0] for command in calls] == ["exec", "ps", "run"]
    assert "--no-deps" in calls[-1]
    assert "10007:10007" in calls[-1]
    assert calls[-1][-2:] == ("maintenance-status", "--json")


def test_release_workflow_journal_blocks_reopen_until_activation_completes(tmp_path):
    state = InstallerStateStore(tmp_path / ".flow-steward")
    journal = ReleaseUpgradeJournal(
        prepared=Harness().service.prepare_upgrade(ReleaseUpgradeRequest())
    )
    state.save_release_upgrade(journal)
    assert state.load_release_upgrade() == journal
    lifecycle = LifecycleStateStore(tmp_path)
    assert len(lifecycle.unfinished_journals()) == 1
    state.save_release_upgrade(journal.model_copy(update={"activation_completed": True}))
    assert lifecycle.unfinished_journals() == ()


@pytest.mark.parametrize("activation_completed", [False, True])
def test_module_rollback_journal_blocks_until_the_rollback_finishes(tmp_path, activation_completed):
    state = InstallerStateStore(tmp_path / ".flow-steward")
    lifecycle = LifecycleStateStore(tmp_path)
    journal = ReleaseUpgradeJournal(
        prepared=Harness().service.prepare_upgrade(ReleaseUpgradeRequest()),
        previous_modules=(),
        modules_required=True,
        activation_completed=activation_completed,
        modules_rollback_started=True,
    )
    state.save_release_upgrade(journal)
    assert len(lifecycle.unfinished_journals()) == 1

    state.save_release_upgrade(journal.model_copy(update={"modules_rolled_back": True}))

    # A finished rollback ends the operation as failed: maintenance disable and
    # new work are no longer blocked, and the primary journal is kept.
    assert lifecycle.unfinished_journals() == ()
    assert state.load_release_upgrade().modules_rolled_back is True


def test_projection_names_the_module_set_that_is_actually_active(tmp_path):
    from core.application.release.platform_modules import PreparedModuleRef

    previous = (PreparedModuleRef(id="acme.ledger", version="1.0.0", digest="sha256:" + "a" * 64),)
    target = (PreparedModuleRef(id="acme.ledger", version="2.0.0", digest="sha256:" + "b" * 64),)
    state = InstallerStateStore(tmp_path / ".flow-steward")
    journal = ReleaseUpgradeJournal(
        prepared=Harness().service.prepare_upgrade(ReleaseUpgradeRequest()),
        prepared_modules=target,
        previous_modules=previous,
        modules_required=True,
        modules_activated=True,
        activation_completed=True,
    )

    def projected(saved):
        state.save_release_upgrade(saved)
        path = state.lifecycle_lock_paths[0].parent / f"{saved.operation_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))["parameters"]

    succeeded = projected(journal)
    assert succeeded["platform_modules"] == ["acme.ledger@2.0.0"]
    assert succeeded["platform_modules_target"] == ["acme.ledger@2.0.0"]
    assert succeeded["platform_modules_previous"] == ["acme.ledger@1.0.0"]

    rolled_back = projected(
        journal.model_copy(update={"modules_rollback_started": True, "modules_rolled_back": True})
    )
    assert rolled_back["platform_modules"] == ["acme.ledger@1.0.0"]
    assert rolled_back["platform_modules_target"] == ["acme.ledger@2.0.0"]


def test_terminal_projection_failure_keeps_primary_journal_resumable(tmp_path, monkeypatch):
    state = InstallerStateStore(tmp_path / ".flow-steward")
    journal = ReleaseUpgradeJournal(
        prepared=Harness().service.prepare_upgrade(ReleaseUpgradeRequest()),
        activation_completed=True,
    )
    state.save_release_upgrade(journal)
    write = state._write_json

    def fail_projection(path, value):
        if path.parent.name == "operations":
            raise OSError("projection unavailable")
        write(path, value)

    monkeypatch.setattr(state, "_write_json", fail_projection)
    with pytest.raises(OSError):
        state.save_release_upgrade(journal.model_copy(update={"completed": True}))
    assert state.load_release_upgrade().completed is False
