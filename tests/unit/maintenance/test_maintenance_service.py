"""Maintenance application policy tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceState
from core.application.maintenance.service import MaintenanceService
from core.infrastructure.lifecycle.maintenance_gateway import (
    FilesystemMaintenanceGateway,
)


@dataclass
class _ActiveWork:
    observations: int = 0

    def wait_for_idle(self) -> None:
        self.observations += 1


@dataclass
class _Unfinished:
    items: tuple[str, ...] = ()

    def unfinished_operation_ids(self) -> tuple[str, ...]:
        return self.items


@dataclass
class _Health:
    issues: tuple[str, ...] = ()
    events: list[str] | None = None

    def mandatory_health_issues(self) -> tuple[str, ...]:
        if self.events is not None:
            self.events.append("health")
        return self.issues


@dataclass
class _Reconciler:
    events: list[str]
    failure: Exception | None = None

    def reconcile(self) -> None:
        self.events.append("reconcile")
        if self.failure is not None:
            raise self.failure


@dataclass
class _Harness:
    service: MaintenanceService
    active_work: _ActiveWork
    unfinished: _Unfinished
    health: _Health
    reconciler: _Reconciler


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    active_work = _ActiveWork()
    unfinished = _Unfinished()
    events: list[str] = []
    health = _Health(events=events)
    reconciler = _Reconciler(events=events)
    return _Harness(
        service=MaintenanceService(
            state=FilesystemMaintenanceGateway(
                project_root=tmp_path,
                state_model=MaintenanceState,
                lifecycle_error=LifecycleError,
            ),
            active_work=active_work,
            unfinished_operations=unfinished,
            health=health,
            readiness_reconciler=reconciler,
        ),
        active_work=active_work,
        unfinished=unfinished,
        health=health,
        reconciler=reconciler,
    )


def test_enable_is_idempotent_and_reobserves_idle(harness: _Harness) -> None:
    first = harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")
    second = harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")

    assert first.status == "enabled"
    assert first.changed is True
    assert second.status == "already_enabled"
    assert second.changed is False
    assert harness.active_work.observations == 2
    assert harness.service.status().status == "enabled"


def test_internal_ownership_check_does_not_expose_owner_id(harness: _Harness) -> None:
    harness.service.enable(owner_kind="release", owner_id="release-upgrade-1")

    assert harness.service.owned_by(
        owner_kind="release",
        owner_id="release-upgrade-1",
    )
    assert not harness.service.owned_by(
        owner_kind="release",
        owner_id="release-upgrade-2",
    )
    assert "owner_id" not in harness.service.status().model_dump()


def test_disable_refuses_unfinished_lifecycle_operation(harness: _Harness) -> None:
    harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")
    harness.unfinished.items = ("release-upgrade-1",)

    with pytest.raises(LifecycleError, match="unfinished") as captured:
        harness.service.disable()

    assert captured.value.code == "maintenance_unfinished_operations"
    assert harness.service.status().status == "enabled"


def test_disable_refuses_failed_mandatory_health(harness: _Harness) -> None:
    harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")
    harness.health.issues = ("database is unavailable",)

    with pytest.raises(LifecycleError, match="health") as captured:
        harness.service.disable()

    assert captured.value.code == "maintenance_health_failed"
    assert harness.service.status().status == "enabled"


def test_disable_reconciles_derived_readiness_before_strict_health(harness: _Harness) -> None:
    harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")

    harness.service.disable()

    assert harness.reconciler.events == ["reconcile", "health"]


def test_disable_keeps_admission_closed_when_reconciliation_fails(harness: _Harness) -> None:
    harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")
    harness.reconciler.failure = RuntimeError("catalog sync failed")

    with pytest.raises(LifecycleError) as captured:
        harness.service.disable()

    assert captured.value.code == "maintenance_reconciliation_failed"
    assert harness.service.status().status == "enabled"
    assert harness.reconciler.events == ["reconcile"]


def test_disable_reopens_only_after_green_health_and_is_idempotent(harness: _Harness) -> None:
    harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")

    first = harness.service.disable()
    second = harness.service.disable()

    assert first.status == "disabled"
    assert first.changed is True
    assert second.status == "already_disabled"
    assert second.changed is False
    assert harness.service.status().status == "disabled"


def test_release_owner_cannot_replace_preexisting_manual_owner(harness: _Harness) -> None:
    harness.service.enable(owner_kind="manual", owner_id="manual-maintenance")

    with pytest.raises(LifecycleError, match="another owner"):
        harness.service.enable(owner_kind="release", owner_id="release-upgrade-1")

    status = harness.service.status()
    assert status.status == "enabled"
    assert status.owner_kind == "manual"
