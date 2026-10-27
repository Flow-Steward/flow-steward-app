"""Durable owner-only maintenance state tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceState
from core.infrastructure.lifecycle.maintenance_gateway import (
    FilesystemMaintenanceGateway,
)


def _gateway(project_root: Path, *, runtime_root: Path | None = None):
    return FilesystemMaintenanceGateway(
        project_root=project_root,
        runtime_root=runtime_root,
        state_model=MaintenanceState,
        lifecycle_error=LifecycleError,
    )


def test_begin_writes_owner_only_versioned_state(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    state = gateway.begin(owner_kind="manual", owner_id="manual-maintenance")

    assert state.phase == "draining"
    assert state.owner_kind == "manual"
    assert state.owner_id == "manual-maintenance"
    marker = tmp_path / ".flow-steward/runtime/maintenance-state.json"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_text(encoding="utf-8"))["schema_version"] == 1


def test_explicit_runtime_root_writes_the_shared_container_mount(tmp_path: Path) -> None:
    shared_runtime = tmp_path / "mounted-installer-runtime"
    gateway = _gateway(tmp_path / "container-app", runtime_root=shared_runtime)

    gateway.begin(owner_kind="manual", owner_id="manual-maintenance")

    assert (shared_runtime / "maintenance-state.json").is_file()
    assert not (tmp_path / "container-app/.flow-steward/runtime/maintenance-state.json").exists()


def test_compact_shared_marker_can_be_group_readable(tmp_path: Path) -> None:
    shared_runtime = tmp_path / "runtime"
    gateway = FilesystemMaintenanceGateway(
        project_root=tmp_path / "state",
        runtime_root=shared_runtime,
        state_model=MaintenanceState,
        lifecycle_error=LifecycleError,
        marker_mode=0o640,
    )

    gateway.begin(owner_kind="manual", owner_id="manual-maintenance")

    assert (shared_runtime / "maintenance-state.json").stat().st_mode & 0o777 == 0o640


def test_only_owner_can_transition_or_clear(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    gateway.begin(owner_kind="release", owner_id="release-1")

    assert gateway.mark_enabled(owner_id="manual-maintenance") is False
    assert gateway.clear(expected_owner_id="manual-maintenance") is False
    state = gateway.read()
    assert state is not None
    assert state.owner_id == "release-1"
    assert state.phase == "draining"


def test_repeated_same_owner_calls_are_idempotent(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    first = gateway.begin(owner_kind="manual", owner_id="manual-maintenance")
    second = gateway.begin(owner_kind="manual", owner_id="manual-maintenance")

    assert second == first
    assert gateway.mark_enabled(owner_id="manual-maintenance") is True
    assert gateway.mark_enabled(owner_id="manual-maintenance") is True
    state = gateway.read()
    assert state is not None
    assert state.phase == "enabled"
    assert gateway.clear(expected_owner_id="manual-maintenance") is True
    assert gateway.clear(expected_owner_id="manual-maintenance") is False


def test_different_owner_cannot_begin_existing_maintenance(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    gateway.begin(owner_kind="manual", owner_id="manual-maintenance")

    with pytest.raises(LifecycleError, match="another owner"):
        gateway.begin(owner_kind="release", owner_id="release-1")


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        json.dumps({"schema_version": 2}),
        json.dumps(
            {
                "schema_version": 1,
                "phase": "unknown",
                "owner_kind": "manual",
                "owner_id": "manual-maintenance",
                "activated_at": "2026-08-23T10:00:00Z",
            }
        ),
    ],
)
def test_invalid_state_fails_closed(tmp_path: Path, payload: str) -> None:
    marker = tmp_path / ".flow-steward/runtime/maintenance-state.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(payload, encoding="utf-8")

    _raises_callable_106_1 = _gateway(tmp_path).read
    with pytest.raises(LifecycleError, match="maintenance state"):
        _raises_callable_106_1()


def test_symlink_state_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    marker = tmp_path / ".flow-steward/runtime/maintenance-state.json"
    marker.parent.mkdir(parents=True)
    marker.symlink_to(target)

    _raises_callable_117_1 = _gateway(tmp_path).read
    with pytest.raises(LifecycleError, match="maintenance state"):
        _raises_callable_117_1()


def test_invalid_owner_identifier_is_rejected(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    with pytest.raises(ValueError, match="owner"):
        gateway.begin(owner_kind="manual", owner_id="../../escape")
