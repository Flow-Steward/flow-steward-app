from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import core.infrastructure.runtime.control.scans_scheduled_jobs as scheduled_mod
from core.infrastructure.runtime.control.config import ControlConfig
from core.infrastructure.runtime.control.scans_rollups import (
    dashboard_rollup_scan,
    security_posture_scan,
)


def _cfg(**overrides: Any) -> ControlConfig:
    base = {
        "watchdog_enabled": True,
        "watchdog_interval": 30.0,
        "watchdog_auto_apply": True,
        "watchdog_stuck_seconds": 120,
        "watchdog_orphan_grace_seconds": 120,
        "watchdog_pickup_grace_seconds": 45,
        "watchdog_max_journal_entries": 200,
        "scheduled_jobs_enabled": True,
        "scheduled_jobs_interval": 30.0,
        "scheduled_jobs_batch_limit": 20,
        "dashboard_rollup_enabled": True,
        "dashboard_rollup_interval": 300.0,
        "dashboard_rollup_backfill_days": 180,
        "dashboard_rollup_lookback_days": 3,
        "security_posture_enabled": True,
        "security_posture_interval": 600.0,
        "workflow_knowledge_sync_enabled": True,
        "workflow_knowledge_sync_interval": 120.0,
        "workflow_knowledge_sync_changelog_limit": 200,
    }
    base.update(overrides)
    return ControlConfig(**base)


def test_dashboard_rollup_scan_branches() -> None:
    disabled = dashboard_rollup_scan(SimpleNamespace(), _cfg(dashboard_rollup_enabled=False))
    assert disabled["disabled"] is True

    unavailable = dashboard_rollup_scan(SimpleNamespace(), _cfg(dashboard_rollup_enabled=True))
    assert unavailable["unavailable"] is True

    class _BL:
        @staticmethod
        def refresh_dashboard_rollups(**_kwargs: Any) -> dict[str, Any]:
            return {
                "details": [{"account_id": "a1"}],
                "refreshed_accounts": 2,
                "facts_rows_deleted": 3,
                "facts_rows_inserted": 4,
                "daily_rows_deleted": 5,
                "daily_rows_inserted": 6,
                "unavailable": True,
                "refreshed_at": "2026-03-29T00:00:00+00:00",
            }

    ok = dashboard_rollup_scan(_BL(), _cfg())
    assert ok["refreshed_accounts"] == 2
    assert ok["details"] == [{"account_id": "a1"}]
    assert ok["unavailable"] is True
    assert ok["refreshed_at"] == "2026-03-29T00:00:00+00:00"

    class _ErrBL:
        @staticmethod
        def refresh_dashboard_rollups(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("rollup-failed")

    failed = dashboard_rollup_scan(_ErrBL(), _cfg())
    assert failed["status"] == "failed"
    assert "rollup-failed" in failed["error"]


def test_security_posture_scan_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = security_posture_scan(SimpleNamespace(), _cfg(security_posture_enabled=False))
    assert disabled["disabled"] is True

    monkeypatch.setitem(
        sys.modules,
        "core.infrastructure.security.config",
        SimpleNamespace(security_enabled=lambda: False),
    )
    scan_disabled = security_posture_scan(SimpleNamespace(), _cfg(security_posture_enabled=True))
    assert scan_disabled["scanning_disabled"] is True

    monkeypatch.setitem(
        sys.modules,
        "core.infrastructure.security.config",
        SimpleNamespace(security_enabled=lambda: True),
    )
    _raises_input_100_1 = SimpleNamespace()
    _raises_input_100_2 = _cfg()
    with pytest.raises(RuntimeError, match="required security posture repository port"):
        security_posture_scan(_raises_input_100_1, _raises_input_100_2)

    class _Store:
        def __init__(self) -> None:
            self.attempts = 0
            self.successes = 0
            self.failures: list[str] = []

        @staticmethod
        def list_active_project_ids() -> list[int]:
            return [1, 2, 3]

        def record_security_posture_loop_attempt(self) -> None:
            self.attempts += 1

        def record_security_posture_loop_success(self) -> None:
            self.successes += 1

        def record_security_posture_loop_failure(self, reason: str) -> None:
            self.failures.append(reason)

    saved: list[int] = []

    def _compute(project_id: int) -> dict[str, Any] | None:
        saved.append(project_id)
        if project_id == 2:
            return None
        if project_id == 3:
            raise RuntimeError("snapshot-failed")
        return {"project_id": project_id}

    monkeypatch.setitem(
        sys.modules,
        "core.infrastructure.security.posture",
        SimpleNamespace(compute_and_save_posture=_compute),
    )
    store = _Store()
    _raises_input_138_1 = _cfg()
    with pytest.raises(RuntimeError, match="snapshot-failed"):
        security_posture_scan(store, _raises_input_138_1)
    assert store.attempts == 1
    assert store.successes == 0
    assert store.failures == ["snapshot-failed"]

    monkeypatch.setitem(
        sys.modules,
        "core.infrastructure.security.posture",
        SimpleNamespace(compute_and_save_posture=lambda project_id: {"project_id": project_id}),
    )
    ok = security_posture_scan(store, _cfg())
    assert ok["projects_scanned"] == 3
    assert ok["snapshots_saved"] == 3
    assert isinstance(ok["computed_at"], str)
    assert store.attempts == 2
    assert store.successes == 1
    assert store.failures == ["snapshot-failed"]


def test_datetime_redaction_and_small_helpers() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    assert scheduled_mod._as_utc_datetime(now) == now
    assert scheduled_mod._as_utc_datetime(datetime(2026, 3, 29, 12, 0)).tzinfo is UTC
    assert scheduled_mod._as_utc_datetime("2026-03-29T12:00:00Z").tzinfo is not None
    assert scheduled_mod._as_utc_datetime("invalid", fallback=now) == now
    assert scheduled_mod._as_utc_datetime("invalid", fallback=None) is None

    payload = {
        "api_key": "secret",
        "nested": {"dsn": "hidden", "ok": "value"},
        "arr": [{"token": "x"}, "safe"],
    }
    redacted = scheduled_mod._build_project_config_snapshot(payload)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["dsn"] == "[REDACTED]"
    assert redacted["nested"]["ok"] == "value"
    assert redacted["arr"][0]["token"] == "[REDACTED]"
    assert scheduled_mod._build_project_config_snapshot("bad") == {}


def test_record_scheduled_job_outcome_updates_history_and_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_job = {
        "id": 1,
        "project_id": 7,
        "account_id": "acc-demo",
        "workflow_id": "wf-1",
        "enabled": True,
        "last_job_id": 10,
    }

    calls: dict[str, list[Any]] = {"runs": [], "updates": []}

    class _RunBL:
        @staticmethod
        def create_scheduled_job_run(**kwargs: Any) -> None:
            calls["runs"].append(kwargs)

        @staticmethod
        def update_scheduled_job(project_id: int, scheduled_job_id: int, **kwargs: Any) -> None:
            calls["updates"].append((project_id, scheduled_job_id, kwargs))

    # This orchestration fixture models an active schedule. Physical current-owner
    # coordination is covered by the real PostgreSQL lifecycle controls.
    monkeypatch.setattr(
        scheduled_mod,
        "update_scheduled_job_outcome",
        lambda store, *, project_id, scheduled_job_id, workflow_id, account_id, changes: (
            store.update_scheduled_job(
                project_id, scheduled_job_id, account_id=account_id, **changes
            )
        ),
    )
    scheduled_mod._record_scheduled_job_outcome(
        _RunBL(),
        scheduled_job=scheduled_job,
        run_type="scheduled",
        status="enqueued",
        job_id=99,
        scheduled_for=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
        next_run_at=datetime(2026, 3, 29, 11, 0, tzinfo=UTC),
        detail={"k": "v"},
    )
    assert calls["runs"]
    assert calls["updates"]


def test_run_scheduled_job_once_main_paths() -> None:
    cfg = _cfg()
    base_job = {
        "id": 11,
        "project_id": 7,
        "account_id": "acc-demo",
        "workflow_id": "wf-1",
        "cron_expr": "*/5 * * * *",
        "next_run_at": datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
        "last_job_id": 0,
        "admission_allowed": True,
        "admission_workflow_revision": 1,
        "current_workflow_revision": 1,
    }
    now = datetime(2026, 3, 29, 10, 1, tzinfo=UTC)

    events: list[tuple[str, Any]] = []

    def _record(_bl: Any, **kwargs: Any) -> None:
        events.append((kwargs["status"], kwargs.get("reason")))

    bl = SimpleNamespace(
        get_project_run_config=lambda _project_id: {"project_name": "Demo", "project_root": "/tmp"}
    )

    skipped = scheduled_mod._run_scheduled_job_once(
        bl,
        base_job,
        cfg,
        now_utc=now,
        _record_scheduled_job_outcome_fn=_record,
        _enqueue_scheduled_job_fn=lambda *_args, **_kwargs: {
            "status": "conflict",
            "blocking_job_id": 101,
        },
        _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
    )
    assert skipped["status"] == "skipped"

    events.clear()
    enqueue_failed = scheduled_mod._run_scheduled_job_once(
        bl,
        base_job,
        cfg,
        now_utc=now,
        _record_scheduled_job_outcome_fn=_record,
        _enqueue_scheduled_job_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("queue-down")
        ),
        _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
    )
    assert enqueue_failed["status"] == "failed"
    assert enqueue_failed["reason"].startswith("enqueue_failed:")

    events.clear()
    ok = scheduled_mod._run_scheduled_job_once(
        bl,
        base_job,
        cfg,
        now_utc=now,
        _record_scheduled_job_outcome_fn=_record,
        _enqueue_scheduled_job_fn=lambda *_args, **_kwargs: {"status": "enqueued", "job_id": 777},
        _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
    )
    assert ok["status"] == "enqueued"
    assert ok["job_id"] == "777"
    assert events
    assert events[-1][0] == "enqueued"


def test_run_scheduled_job_once_passes_uuid_safe_payloads_to_enqueue() -> None:
    cfg = _cfg()
    now = datetime(2026, 3, 29, 10, 0, tzinfo=UTC)
    project_id = uuid.UUID("58eeaeb3-a9c1-4c51-b292-14eb86899d86")
    scheduled_job_id = uuid.UUID("d28b9bea-f129-41bc-9631-6fb740904ee5")
    account_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    captured: dict[str, Any] = {}

    def _enqueue(_bl: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "enqueued", "job_id": 777}

    result = scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(
            get_project_run_config=lambda _project_id: {
                "project_name": "Demo",
                "project_root": "/tmp",
                "account_id": account_id,
                "project_workflows": [{"project_id": project_id, "workflow_id": "weekly_report"}],
            }
        ),
        {
            "id": scheduled_job_id,
            "project_id": project_id,
            "account_id": account_id,
            "workflow_id": "weekly_report",
            "inputs": {
                "title": "Nightly check",
                "description": "Check pipeline health",
                "priority": "medium",
            },
            "cron_expr": "* * * * *",
            "next_run_at": now,
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
        },
        cfg,
        now_utc=now,
        _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        _enqueue_scheduled_job_fn=_enqueue,
        _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
    )

    assert result["status"] == "enqueued"
    assert captured["project_id"] == str(project_id)
    assert captured["scheduled_job_id"] == str(scheduled_job_id)
    assert captured["account_id"] == str(account_id)
    assert captured["project_config"]["project_workflows"][0]["project_id"] == str(project_id)
    assert captured["extra_state"]["scheduled_job_id"] == str(scheduled_job_id)
    assert captured["lock_key"] == (f"scheduled_job:{account_id}:{project_id}:{scheduled_job_id}")
    retired = {
        "scheduled_instructions",
        "scheduled_job_concurrency_scope",
        "scheduled_job_concurrency_key",
        "scheduled_job_catchup_policy",
        "scheduled_job_resolved_concurrency_key",
        "scheduled_job_concurrency_summary",
    }
    assert retired.isdisjoint(captured["extra_state"])
    assert retired.isdisjoint(captured["meta"])
    assert captured["task_summary"] == "Workflow run: weekly_report"
    assert captured["meta"].get("task_summary_key") == "react.jobs.generated_workflow_run_summary"
    assert captured["meta"].get("task_summary_params") == {"workflow_id": "weekly_report"}
    task_envelope = captured["extra_state"]["task_envelope"]
    assert task_envelope["entry_path"] == "schedule"
    assert task_envelope["source_system"] == "schedule"
    assert task_envelope["project_id"] == str(project_id)
    assert task_envelope["requested_workflow_id"] == "weekly_report"
    assert task_envelope["inputs"]["title"] == "Nightly check"
    assert "task_envelope" not in captured["meta"]
    assert "inputs" not in captured["meta"]
    assert captured["replay_snapshot"]["workflow_id"] == "weekly_report"
    assert captured["replay_snapshot"]["task_envelope"] == task_envelope
    assert captured["meta"]["invocation"]["channel"] == "schedule"
    assert captured["meta"]["invocation"]["trigger_family"] == "schedule"
    assert captured["meta"]["scheduled_job_account_id"] == str(account_id)


def test_strict_zero_input_schedule_enqueues_the_stored_empty_payload() -> None:
    captured: dict[str, Any] = {}

    result = scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(
            get_project_run_config=lambda _project_id: {
                "project_name": "Demo",
                "project_root": "/tmp",
            }
        ),
        {
            "id": "schedule-1",
            "project_id": "project-1",
            "account_id": "account-1",
            "workflow_id": "strict-zero-inputs",
            "inputs": {},
            "cron_expr": "* * * * *",
            "next_run_at": datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
        },
        _cfg(),
        now_utc=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
        _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        _enqueue_scheduled_job_fn=lambda _bl, **kwargs: (
            captured.update(kwargs) or {"status": "enqueued", "job_id": "job-1"}
        ),
        _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
    )

    assert result["status"] == "enqueued"
    assert captured["extra_state"]["task_envelope"]["inputs"] == {}


def test_schedule_api_rejects_retired_workflow_input_payload() -> None:
    from core.entrypoints.admin.project_schedules import ApiScheduledJobCreateRequest

    with pytest.raises(ValidationError):
        ApiScheduledJobCreateRequest.model_validate(
            {
                "workflow_id": "wf-1",
                "cron_expr": "0 2 * * *",
                "workflow_input_payload": {"legacy": True},
            }
        )


def test_run_scheduled_job_once_invalid_payload_and_missing_config() -> None:
    cfg = _cfg()

    invalid = scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(),
        {"id": 0, "project_id": 0, "workflow_id": ""},
        cfg,
    )
    assert invalid["status"] == "failed"

    missing_api = scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(),
        {
            "id": 1,
            "project_id": 2,
            "account_id": "acc-demo",
            "workflow_id": "wf",
            "cron_expr": "*/5 * * * *",
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
        },
        cfg,
    )
    assert missing_api["reason"] == "project_run_config_unavailable"

    not_found = scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(get_project_run_config=lambda _project_id: {}),
        {
            "id": 1,
            "project_id": 2,
            "account_id": "acc-demo",
            "workflow_id": "wf",
            "cron_expr": "*/5 * * * *",
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
        },
        cfg,
    )
    assert not_found["reason"] == "project_run_config_not_found"


def test_scheduled_jobs_scan_branches() -> None:
    cfg = _cfg(scheduled_jobs_enabled=False)
    disabled = scheduled_mod.scheduled_jobs_scan(SimpleNamespace(), cfg)
    assert disabled["disabled"] is True

    unavailable = scheduled_mod.scheduled_jobs_scan(
        SimpleNamespace(), _cfg(scheduled_jobs_enabled=True)
    )
    assert unavailable["unavailable"] is True

    now = datetime(2026, 3, 29, 10, 0, tzinfo=UTC)
    due = [
        {
            "id": 1,
            "project_id": 10,
            "workflow_id": "wf-a",
            "cron_expr": "*/5 * * * *",
            "next_run_at": now,
        },
        {
            "id": 2,
            "project_id": 10,
            "workflow_id": "wf-b",
            "cron_expr": "*/5 * * * *",
            "next_run_at": now,
        },
        {
            "id": 3,
            "project_id": 10,
            "workflow_id": "wf-c",
            "cron_expr": "*/5 * * * *",
            "next_run_at": now,
        },
    ]

    class _BL:
        @staticmethod
        def list_due_scheduled_jobs(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return list(due)

    statuses = iter(
        [
            {"status": "enqueued", "job_id": 101},
            {"status": "skipped", "reason": "claim_lost_or_already_processed"},
            {"status": "failed", "reason": "boom"},
        ]
    )

    summary = scheduled_mod.scheduled_jobs_scan(
        _BL(),
        _cfg(scheduled_jobs_enabled=True),
        _as_utc_datetime_fn=lambda value, fallback=None: value or fallback or now,
        _run_scheduled_job_once_fn=lambda *_args, **_kwargs: next(statuses),
        _scheduled_job_next_run_at_fn=lambda *_args, **_kwargs: now,
        _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
    )
    assert summary["checked"] == 3
    assert summary["enqueued"] == 1
    assert summary["skipped"] == 1
    assert summary["failed"] == 1
    assert len(summary["details"]) == 3


@pytest.mark.parametrize("claim_enabled", [False, True], ids=["advance", "claim"])
def test_control_schedule_next_slot_preserves_own_timezone_across_dst(claim_enabled):
    now = datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    row = {
        "id": "schedule-1",
        "project_id": "project-1",
        "account_id": "acc-demo",
        "workflow_id": "wf-1",
        "cron_expr": "0 9 * * *",
        "next_run_at": now,
        "schedule_timezone": "Europe/Vilnius",
        "project_schedule_timezone": "UTC",
        "enabled": True,
        "admission_allowed": True,
        "admission_workflow_revision": 1,
        "current_workflow_revision": 1,
    }
    bl = SimpleNamespace(
        list_due_scheduled_jobs=lambda **_kwargs: [dict(row)],
        get_project_run_config=lambda _project_id: {"project_name": "Demo", "project_root": "/tmp"},
    )

    def claim(**kwargs):
        assert kwargs["expected_next_run_at"] == now
        row["next_run_at"] = kwargs["next_run_at"]
        return dict(row)

    if claim_enabled:
        bl.claim_due_scheduled_job = claim

    def record(_bl, **kwargs):
        row["next_run_at"] = kwargs["next_run_at"]

    def run_once(store, job, cfg, **kwargs):
        return scheduled_mod._run_scheduled_job_once(
            store,
            job,
            cfg,
            **kwargs,
            _record_scheduled_job_outcome_fn=record,
            _enqueue_scheduled_job_fn=lambda _bl, **_kwargs: {
                "status": "enqueued",
                "job_id": "job-1",
            },
            _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
        )

    result = scheduled_mod.scheduled_jobs_scan(
        bl,
        _cfg(),
        now_utc=now,
        _run_scheduled_job_once_fn=run_once,
        _record_scheduled_job_outcome_fn=record,
    )
    assert result["enqueued"] == 1, result
    assert row["next_run_at"] == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)


def test_scheduled_jobs_scan_claim_paths() -> None:
    now = datetime(2026, 3, 29, 10, 0, tzinfo=UTC)
    due = [
        {"id": 1, "project_id": 10, "workflow_id": "wf-a", "cron_expr": "bad", "next_run_at": now},
        {
            "id": 2,
            "project_id": 10,
            "workflow_id": "wf-b",
            "cron_expr": "*/5 * * * *",
            "next_run_at": now,
        },
        {
            "id": 3,
            "project_id": 10,
            "workflow_id": "wf-c",
            "cron_expr": "*/5 * * * *",
            "next_run_at": now,
        },
        {
            "id": 4,
            "project_id": 10,
            "workflow_id": "wf-d",
            "cron_expr": "*/5 * * * *",
            "next_run_at": now,
        },
    ]

    claimed = {
        1: due[0],  # cron invalid path + claimed dict -> failed record
        2: None,  # claim lost
        3: {**due[2], "id": 30},  # claimed dict -> run with advance_schedule=False
        4: True,  # non-dict claim -> run with advance_schedule=True
    }
    run_calls: list[dict[str, Any]] = []
    recorded: list[tuple[str, str]] = []

    class _BL:
        @staticmethod
        def list_due_scheduled_jobs(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return list(due)

        @staticmethod
        def claim_due_scheduled_job(*_args: Any, **kwargs: Any) -> Any:
            return claimed.get(int(kwargs.get("scheduled_job_id") or 0))

    def _next_run(cron_expr: str, **_kwargs: Any) -> datetime:
        if cron_expr == "bad":
            raise ValueError("invalid-cron")
        return now

    def _run_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        scheduled_job = args[1]
        run_calls.append(
            {
                "scheduled_job_id": int(scheduled_job["id"]),
                "advance_schedule": bool(kwargs["advance_schedule"]),
            }
        )
        return {"status": "enqueued", "job_id": 900 + int(scheduled_job["id"])}

    def _record(_bl: Any, **kwargs: Any) -> None:
        recorded.append((kwargs["status"], str(kwargs["reason"])))

    summary = scheduled_mod.scheduled_jobs_scan(
        _BL(),
        _cfg(scheduled_jobs_enabled=True),
        _as_utc_datetime_fn=lambda value, fallback=None: value or fallback or now,
        _run_scheduled_job_once_fn=_run_once,
        _scheduled_job_next_run_at_fn=_next_run,
        _record_scheduled_job_outcome_fn=_record,
    )
    assert summary["checked"] == 4
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["enqueued"] == 2
    assert any(status == "failed" and "invalid_cron_expr" in reason for status, reason in recorded)
    assert run_calls == [
        {"scheduled_job_id": 30, "advance_schedule": False},
        {"scheduled_job_id": 4, "advance_schedule": True},
    ]
