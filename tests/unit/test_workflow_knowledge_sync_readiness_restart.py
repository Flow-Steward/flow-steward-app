"""AC17-R149: restarting the control plane must not make the application unready.

Measured by the auditor on a Scalable installation: `config apply` recreated the control
plane, and /health/ready then answered 503 on workflow_knowledge_sync ("the latest
workflow knowledge sync iteration did not complete") for up to one sync interval, so the
candidate probe rolled the change back -- and the rollback recreated the control plane
again.

Why: each control-plane scan stamped last_attempt_at and then enqueued a request whose
idempotency key is fixed per cadence bucket. A restart inside the bucket of the last
successful sync enqueued a duplicate, the outbox dropped it (ON CONFLICT DO NOTHING), no
worker ever ran it, and the stamped attempt stayed newer than the last success until the
next bucket. The same stamp also cleared an in-flight worker's claim marker, and it made
every ordinary iteration look unfinished until a worker claimed it.

These tests drive the real scan, the real request builder, the real status store
functions and the real readiness check; only the database rows are held in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.application.jobs import workflow_knowledge_sync_requests as requests_module
from core.infrastructure.db import system_settings_store as settings
from core.infrastructure.runtime import readiness_runtime
from core.infrastructure.runtime.control import scans
from core.infrastructure.runtime.control.config import ControlConfig


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Platform:
    """In-memory rows behind the real store functions: settings and the outbox."""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.outbox: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, bool] = {}

    # -- the ports the control-plane scan uses
    def list_recent_registry_changelog(self, **_kwargs: Any) -> list[dict]:
        return []

    def enqueue_domain_event(self, *, event_id: str, **kwargs: Any) -> None:
        if event_id not in self.outbox:  # ON CONFLICT (event_id) DO NOTHING
            self.outbox[event_id] = {"id": f"outbox-{len(self.outbox) + 1}", "status": "pending"}

    def record_workflow_knowledge_sync_loop_attempt(self, **claim: Any) -> dict:
        return settings.record_workflow_knowledge_sync_loop_attempt(conn=None, **claim)

    def record_workflow_knowledge_sync_loop_success(self) -> dict:
        return settings.record_workflow_knowledge_sync_loop_success(conn=None)

    def record_workflow_knowledge_sync_loop_failure(self, reason: str) -> dict:
        return settings.record_workflow_knowledge_sync_loop_failure(conn=None, reason=reason)

    # -- the ports readiness uses
    def get_workflow_knowledge_sync_loop_status(self) -> dict:
        return settings.get_workflow_knowledge_sync_loop_status(conn=None)

    def get_domain_event_outbox_claim_status(self, *, outbox_id: str, **_claim: Any) -> dict:
        return {"active": self.claims.get(outbox_id, False)}

    # -- what a worker does with a pending request (the worker's own code, in order)
    def worker_claims(self) -> str:
        pending = next(row for row in self.outbox.values() if row["status"] == "pending")
        pending["status"] = "claimed"
        self.claims[pending["id"]] = True
        self.record_workflow_knowledge_sync_loop_attempt(
            outbox_id=pending["id"], claim_owner="worker-1", claim_version=1
        )
        return pending["id"]

    def worker_succeeds(self, outbox_id: str) -> None:
        self.claims[outbox_id] = False
        next(row for row in self.outbox.values() if row["id"] == outbox_id)["status"] = "published"
        self.record_workflow_knowledge_sync_loop_success()

    def worker_dies(self, outbox_id: str) -> None:
        self.claims[outbox_id] = False  # the lease expired with nobody finishing


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> tuple[_Platform, _Clock]:
    rows = _Platform()
    clock = _Clock(datetime(2026, 9, 23, 10, 17, 0, tzinfo=UTC))

    def get_system_setting(*, setting_key: str, **_kwargs: Any) -> dict | None:
        value = rows.settings.get(setting_key)
        return None if value is None else {"setting_value_jsonb": dict(value)}

    def save_system_setting(*, setting_key: str, value: Any, **_kwargs: Any) -> dict:
        rows.settings[setting_key] = dict(value)
        return {"setting_value_jsonb": dict(value)}

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is not None else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(settings, "get_system_setting", get_system_setting)
    monkeypatch.setattr(settings, "save_system_setting", save_system_setting)
    monkeypatch.setattr(settings, "datetime", _FrozenDatetime)
    monkeypatch.setattr(requests_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(readiness_runtime, "datetime", _FrozenDatetime)
    return rows, clock


def _scan(rows: _Platform) -> dict:
    return scans.workflow_knowledge_sync_scan(rows, rows, ControlConfig.from_env())


def _ready(rows: _Platform) -> dict:
    return readiness_runtime._workflow_knowledge_sync_check(rows)


def _completed_iteration(rows: _Platform, clock: _Clock) -> None:
    _scan(rows)
    clock.advance(2)
    rows.worker_succeeds(rows.worker_claims())
    assert _ready(rows)["ok"] is True, _ready(rows)


def test_a_control_plane_restart_inside_the_last_bucket_leaves_readiness_ready(
    platform: tuple[_Platform, _Clock],
) -> None:
    rows, clock = platform
    _completed_iteration(rows, clock)

    clock.advance(60)  # recreated by `config apply`, still inside the same cadence bucket
    _scan(rows)

    # Non-vacuity: the restart's request really was the duplicate the outbox dropped.
    assert len(rows.outbox) == 1
    assert _ready(rows)["ok"] is True, _ready(rows)


def test_an_iteration_waiting_for_a_worker_is_not_an_unfinished_one(
    platform: tuple[_Platform, _Clock],
) -> None:
    rows, clock = platform
    _completed_iteration(rows, clock)

    clock.advance(ControlConfig.from_env().workflow_knowledge_sync_interval)
    _scan(rows)  # a new bucket: a new request, not yet claimed by any worker

    assert len(rows.outbox) == 2
    assert _ready(rows)["ok"] is True, _ready(rows)


def test_a_worker_that_claims_and_dies_is_still_reported(
    platform: tuple[_Platform, _Clock],
) -> None:
    rows, clock = platform
    _completed_iteration(rows, clock)
    clock.advance(ControlConfig.from_env().workflow_knowledge_sync_interval)
    _scan(rows)
    rows.worker_dies(rows.worker_claims())

    result = _ready(rows)

    assert result["ok"] is False
    assert result["detail"] == "the latest workflow knowledge sync iteration did not complete"


def test_a_claim_in_flight_survives_the_next_control_plane_scan(
    platform: tuple[_Platform, _Clock],
) -> None:
    rows, clock = platform
    _completed_iteration(rows, clock)
    clock.advance(ControlConfig.from_env().workflow_knowledge_sync_interval)
    _scan(rows)
    rows.worker_claims()  # a long sync, still running

    clock.advance(30)
    _scan(rows)  # the control plane restarts meanwhile

    result = _ready(rows)
    assert result["ok"] is True, result
    assert result.get("in_progress") is True


def test_a_request_that_cannot_be_enqueued_is_still_a_failure(
    platform: tuple[_Platform, _Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, clock = platform
    _completed_iteration(rows, clock)

    def refuse(**_kwargs: Any) -> None:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(rows, "enqueue_domain_event", refuse)
    clock.advance(60)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        _scan(rows)

    result = _ready(rows)
    assert result["ok"] is False
    assert result["last_failure_reason"] == "outbox unavailable"


def test_a_sync_nobody_runs_goes_stale(platform: tuple[_Platform, _Clock]) -> None:
    rows, clock = platform
    _completed_iteration(rows, clock)
    interval = ControlConfig.from_env().workflow_knowledge_sync_interval
    for _ in range(6):  # requests keep arriving; no worker ever claims one
        clock.advance(interval)
        _scan(rows)

    result = _ready(rows)
    assert result["ok"] is False, result
