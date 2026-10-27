from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from tests import api_runtime_test_surface as api_runtime


def _patch_disabled_workflow_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ContractService:
        @staticmethod
        def interface_for_workflow_id(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(expected_inputs=[])

        @staticmethod
        def validate_inputs(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                ok=True,
                errors=[],
                inputs={},
            )

    monkeypatch.setattr(
        "core.application.registry.workflow_contract_service.WorkflowInterfaceService",
        lambda: _ContractService(),
    )


def test_form_helpers_and_policy_enum() -> None:
    assert api_runtime._form_flag(True) is True
    assert api_runtime._form_flag("yes") is True
    assert api_runtime._form_flag("no") is False

    assert api_runtime._form_int("7", 1, minimum=3) == 7
    assert api_runtime._form_int("bad", 1, minimum=3) == 3

    assert api_runtime._form_float("0.75", 0.5, minimum=0.0, maximum=1.0) == 0.75
    assert api_runtime._form_float("bad", 0.5, minimum=0.0, maximum=1.0) == 0.5
    assert api_runtime._form_float("2.0", 0.5, minimum=0.0, maximum=1.0) == 1.0

    assert (
        api_runtime._parse_project_policy_enum(
            "assisted",
            field_name="mode",
            allowed={"manual", "supervised", "auto"},
            default="supervised",
            aliases={"assisted": "supervised"},
        )
        == "supervised"
    )
    with pytest.raises(ValueError):
        api_runtime._parse_project_policy_enum(
            "invalid",
            field_name="mode",
            allowed={"manual", "supervised", "auto"},
            default="supervised",
        )


def test_build_project_policy_from_form_preserves_aliases_and_retry_normalization() -> None:
    policy = api_runtime._build_project_policy_from_form(
        {
            "policy_autonomy_mode": "assist",
            "policy_retry_strategy": "flat",
            "policy_tool_policy_mode": "allow_all",
            "policy_autonomy_confidence_threshold": "1.0",
            "policy_retry_max_attempts": "-1",
            "policy_retry_backoff_initial_seconds": "4",
            "policy_retry_backoff_max_seconds": "9",
            "policy_concurrency_cap": "1",
        }
    )
    assert policy["autonomy_mode"] == "supervised"
    assert policy["retry_strategy"] == "flat"
    assert "worktree_policy" not in policy
    assert policy["tool_policy_mode"] == "permissive"
    assert policy["autonomy_confidence_threshold"] == 1.0
    assert policy["retry_max_attempts"] == 0
    assert policy["concurrency_cap"] == 1


def test_cron_parse_and_human_summary() -> None:
    parsed = api_runtime._parse_cron_expression("*/15 9-10 * jan,mar mon-fri")
    assert 15 in parsed["minute"]
    assert 9 in parsed["hour"]
    assert 10 in parsed["hour"]
    assert 1 in parsed["month"]
    assert 3 in parsed["month"]
    assert 1 in parsed["dow"]
    assert 5 in parsed["dow"]

    assert api_runtime._cron_human_summary("0 9 * * *", "UTC") == "Daily at 09:00 (UTC)"
    assert "Every 15 minutes" in api_runtime._cron_human_summary("*/15 * * * *", "UTC")
    assert "Every 6 hours" in api_runtime._cron_human_summary("0 */6 * * *", "UTC")
    assert "Monthly" in api_runtime._cron_human_summary("0 10 15 * *", "UTC")

    with pytest.raises(ValueError):
        api_runtime._parse_cron_expression("bad cron")


def test_cron_next_run_at_supports_timezone() -> None:
    reference = datetime(2026, 3, 29, 9, 2, tzinfo=UTC)
    run_at = api_runtime._cron_next_run_at("*/15 * * * *", reference, schedule_tz="UTC")
    assert run_at.tzinfo is UTC
    assert run_at.minute in {15, 30, 45, 0}


def test_workflow_selection_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.infrastructure.runtime.http_app_indexing_workflows._list_workflow_choices",
        lambda enabled_only=False, include_definitions=False: [
            {"workflow_id": "wf-default", "enabled": True},
            {"workflow_id": "wf-other", "enabled": False},
        ],
    )

    selected, source = api_runtime._resolve_workflow_selection(
        requested_workflow_id="",
        project_workflows=[{"workflow_id": "wf-default", "enabled": True, "is_default": True}],
    )
    assert selected == "wf-default"
    assert source == "project_default"

    selected, source = api_runtime._resolve_workflow_selection(
        requested_workflow_id="",
        project_workflows=[{"workflow_id": "wf-default", "enabled": True}],
    )
    assert selected == "wf-default"
    assert source == "project_linked"

    with pytest.raises(ValueError):
        api_runtime._resolve_workflow_selection(
            requested_workflow_id="wf-missing",
            project_workflows=[{"workflow_id": "wf-default", "enabled": True}],
        )

    with pytest.raises(ValueError, match="Workflow 'auto' not found"):
        api_runtime._resolve_workflow_selection("auto", [])


def test_build_rerun_workflow_selection_uses_verified_replay_snapshot() -> None:
    extra_state, rerun_meta = api_runtime._build_rerun_workflow_selection(
        {
            "id": 42,
            "selected_workflow": "wf-stale",
            "meta": {"workflow_selection_source": "project_default", "test_mode": False},
        },
        replay_snapshot={
            "workflow_id": "wf-1",
            "inputs": {"catalog_id": "catalog-7"},
            "files": [
                {
                    "artifact_id": "artifact-7",
                    "filename": "catalog.csv",
                    "sha256": "a" * 64,
                    "size_bytes": 17,
                    "owner_project_id": "project-7",
                    "owner_job_id": "42",
                }
            ],
            "test_mode": True,
            "input_contract": "direct_inputs",
            "task_envelope": {
                "inputs": {"catalog_id": "catalog-7"},
                "files": [
                    {
                        "artifact_id": "artifact-7",
                        "filename": "catalog.csv",
                        "sha256": "a" * 64,
                        "size_bytes": 17,
                        "owner_project_id": "project-7",
                        "owner_job_id": "42",
                    }
                ],
            },
            "_workflow_secret_ref_fingerprints": [],
        },
    )
    assert extra_state["workflow_id"] == "wf-1"
    assert extra_state["inputs"] == {"catalog_id": "catalog-7"}
    assert extra_state["files"] == [
        {
            "artifact_id": "artifact-7",
            "filename": "catalog.csv",
            "sha256": "a" * 64,
            "size_bytes": 17,
            "owner_project_id": "project-7",
            "owner_job_id": "42",
        }
    ]
    assert extra_state["test_mode"] is True
    assert rerun_meta["rerun_from_job_id"] == "42"
    assert rerun_meta["workflow_selection_source"] == "explicit_request"


def test_workflow_exists_handles_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Workflows:
        @staticmethod
        def get_workflow(_workflow_id: str):
            return {"workflow_id": "wf-1"}

    class _Registry:
        workflows = _Workflows()

    class _Manager:
        @staticmethod
        def instance():
            return _Registry()

    import core.application.registry.registry_manager as rm

    monkeypatch.setattr(rm, "RegistryManager", _Manager)
    assert api_runtime._workflow_exists("wf-1") is True
    assert api_runtime._workflow_exists("") is False


def test_scheduled_job_overlap_record_and_run_once_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    scheduled_job = {
        "id": 5,
        "project_id": 9,
        "account_id": "00000000-0000-0000-0000-000000000002",
        "workflow_id": "wf-main",
        "cron_expr": "*/5 * * * *",
        "task_summary": "Do work",
        "inputs": {},
        "next_run_at": now.isoformat(),
        "enabled": True,
        "admission_allowed": True,
        "admission_workflow_revision": 1,
        "current_workflow_revision": 1,
    }

    calls: dict[str, list[Any]] = {"runs": [], "updates": []}

    class _RecordBL:
        @staticmethod
        def create_scheduled_job_run(**kwargs: Any) -> None:
            calls["runs"].append(kwargs)

        @staticmethod
        def update_scheduled_job(project_id: int, scheduled_job_id: int, **kwargs: Any) -> None:
            calls["updates"].append((project_id, scheduled_job_id, kwargs))

    # This fixture models an active owner and keeps its real history/update
    # observations; retirement coordination is exercised by PostgreSQL tests.
    monkeypatch.setattr(
        api_runtime._control_scheduled_jobs,
        "update_scheduled_job_outcome",
        lambda store, *, project_id, scheduled_job_id, workflow_id, account_id, changes: (
            store.update_scheduled_job(
                project_id, scheduled_job_id, account_id=account_id, **changes
            )
        ),
    )
    api_runtime._record_scheduled_job_outcome(
        _RecordBL(),
        scheduled_job=scheduled_job,
        run_type="scheduled",
        status="enqueued",
        reason="",
        job_id=101,
        scheduled_for=now,
        next_run_at=now,
        mark_error=False,
        detail={"k": "v"},
    )
    assert calls["runs"]
    assert calls["updates"]

    invalid_payload = api_runtime._run_scheduled_job_once(
        SimpleNamespace(),
        {"id": 0, "project_id": 0, "workflow_id": ""},
        now_utc=now,
    )
    assert invalid_payload["status"] == "failed"

    no_cfg = api_runtime._run_scheduled_job_once(
        SimpleNamespace(),
        scheduled_job,
        now_utc=now,
    )
    assert no_cfg["status"] == "failed"
    assert "project_run_config_not_found" in no_cfg["reason"]

    class _CfgBL:
        @staticmethod
        def get_project_run_config(_project_id: int) -> dict[str, Any]:
            return {
                "project_name": "demo",
                "project_root": "/target",
                "container_root": "/target",
                "account_id": "00000000-0000-0000-0000-000000000002",
            }

    monkeypatch.setattr(api_runtime, "_validate_explicit_workflow_override", lambda **_kwargs: None)
    _patch_disabled_workflow_contract(monkeypatch)
    monkeypatch.setattr(
        api_runtime._control_scheduled_jobs,
        "_enqueue_scheduled_job",
        lambda *_args, **_kwargs: {"status": "enqueued", "job_id": 202},
    )
    enqueued = api_runtime._run_scheduled_job_once(_CfgBL(), scheduled_job, now_utc=now)
    assert enqueued["status"] == "enqueued"
    assert enqueued["job_id"] == "202"


def test_scheduled_jobs_scan_and_dashboard_rollup(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infrastructure.runtime import http_app_lifecycle, http_app_scheduled_jobs

    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(http_app_lifecycle, "_SCHEDULED_JOBS_ENABLED", False)
    disabled = api_runtime._scheduled_jobs_scan(SimpleNamespace(), now_utc=now)
    assert disabled["disabled"] is True

    monkeypatch.setattr(http_app_lifecycle, "_SCHEDULED_JOBS_ENABLED", True)
    unavailable = api_runtime._scheduled_jobs_scan(SimpleNamespace(), now_utc=now)
    assert unavailable["unavailable"] is True

    job = {
        "id": 7,
        "project_id": 2,
        "workflow_id": "wf-main",
        "cron_expr": "*/5 * * * *",
        "next_run_at": now.isoformat(),
        "task_summary": "scheduled",
    }

    class _ScanBL:
        @staticmethod
        def list_due_scheduled_jobs(now: datetime, limit: int) -> list[dict[str, Any]]:
            assert isinstance(now, datetime)
            assert limit >= 1
            return [job]

        @staticmethod
        def claim_due_scheduled_job(**_kwargs: Any) -> dict[str, Any]:
            return dict(job)

    monkeypatch.setattr(
        http_app_scheduled_jobs,
        "_run_scheduled_job_once",
        lambda *_args, **_kwargs: {"status": "enqueued", "job_id": 333},
    )
    summary = api_runtime._scheduled_jobs_scan(_ScanBL(), now_utc=now)
    assert summary["checked"] == 1
    assert summary["enqueued"] == 1

    rollup_unavailable = api_runtime._dashboard_rollup_scan(SimpleNamespace())
    assert rollup_unavailable["unavailable"] is True

    class _RollupBL:
        @staticmethod
        def refresh_dashboard_rollups() -> dict[str, Any]:
            return {"refreshed_accounts": 2, "details": [{"account_id": "a-1"}]}

    rollup_ok = api_runtime._dashboard_rollup_scan(_RollupBL())
    assert rollup_ok["refreshed_accounts"] == 2
    assert rollup_ok["details"] == [{"account_id": "a-1"}]


def test_workflow_and_reaction_helpers_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    # _workflow_exists exception branch
    class _BrokenManager:
        @staticmethod
        def instance():
            raise RuntimeError("boom")

    import core.application.registry.registry_manager as rm

    monkeypatch.setattr(rm, "RegistryManager", _BrokenManager)
    assert api_runtime._workflow_exists("wf") is False

    assert (
        api_runtime._resolve_job_workflow_id({"meta": {"selected_workflow_id": "wf-a"}}) == "wf-a"
    )
    assert api_runtime._resolve_job_workflow_id({"meta": "not-json"}) == ""

    assert api_runtime._reaction_value_matches({"a": {"b": 1}}, {"a": {"b": 1}}) is True
    assert api_runtime._reaction_value_matches({"a": {"b": 1}}, {"a": {"b": 2}}) is False
    assert api_runtime._reaction_value_matches("x", {"a": 1}) is False
    assert api_runtime._reaction_value_matches("x", {"x", "y"}) is True

    assert api_runtime._lookup_dotted_value({"a": {"b": 1}}, "a.b") == 1
    assert api_runtime._lookup_dotted_value({"a": {"b": 1}}, "a..b") == 1
    assert api_runtime._lookup_dotted_value({"a": 1}, "a.b") is None

    event = {
        "event_type": "job.updated",
        "source": "system",
        "source_id": "s1",
        "event_key": "e1",
        "workflow_id": "wf-a",
        "job_id": 10,
        "payload": {"status": "failed", "nested": {"flag": True}},
    }
    assert api_runtime._reaction_conditions_match(event, {"payload.status": "failed"}) is True
    assert api_runtime._reaction_conditions_match(event, {"payload.nested.flag": True}) is True
    assert api_runtime._reaction_conditions_match(event, {"event_type": "job.created"}) is False


def test_scheduled_job_run_once_failure_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infrastructure.runtime import http_app_scheduled_jobs

    _patch_disabled_workflow_contract(monkeypatch)
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    scheduled_job = {
        "id": 8,
        "project_id": 22,
        "account_id": "00000000-0000-0000-0000-000000000002",
        "workflow_id": "wf-main",
        "cron_expr": "*/5 * * * *",
        "task_summary": "work",
        "inputs": {},
        "next_run_at": now.isoformat(),
        "admission_allowed": True,
        "admission_workflow_revision": 1,
        "current_workflow_revision": 1,
    }

    class _CfgBL:
        @staticmethod
        def get_project_run_config(_project_id: int) -> dict[str, Any]:
            return {
                "project_name": "demo",
                "project_root": "/target",
                "container_root": "/target",
                "account_id": "00000000-0000-0000-0000-000000000002",
            }

    original_next_run_at = http_app_scheduled_jobs._scheduled_job_next_run_at
    monkeypatch.setattr(
        http_app_scheduled_jobs,
        "_scheduled_job_next_run_at",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad-cron")),
    )
    failed_cron = api_runtime._run_scheduled_job_once(_CfgBL(), scheduled_job, now_utc=now)
    assert failed_cron["status"] == "failed"
    assert "invalid_cron_expr" in failed_cron["reason"]
    monkeypatch.setattr(
        http_app_scheduled_jobs,
        "_scheduled_job_next_run_at",
        original_next_run_at,
    )

    class _EmptyCfgBL:
        @staticmethod
        def get_project_run_config(_project_id: int) -> dict[str, Any]:
            return {}

    no_cfg = api_runtime._run_scheduled_job_once(_EmptyCfgBL(), scheduled_job, now_utc=now)
    assert no_cfg["status"] == "failed"
    assert "project_run_config_not_found" in no_cfg["reason"]

    stale_admission = api_runtime._run_scheduled_job_once(
        _CfgBL(),
        {**scheduled_job, "current_workflow_revision": 2},
        now_utc=now,
    )
    assert stale_admission["status"] == "skipped"
    assert stale_admission["reason"] == "workflow_admission_stale"

    monkeypatch.setattr(
        api_runtime._control_scheduled_jobs,
        "_enqueue_scheduled_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("queue-down")),
    )
    enqueue_failed = api_runtime._run_scheduled_job_once(_CfgBL(), scheduled_job, now_utc=now)
    assert enqueue_failed["status"] == "failed"
    assert "enqueue_failed" in enqueue_failed["reason"]


def test_scheduled_scan_claim_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infrastructure.runtime import http_app_lifecycle

    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(http_app_lifecycle, "_SCHEDULED_JOBS_ENABLED", True)

    job = {
        "id": 10,
        "project_id": 3,
        "workflow_id": "wf-main",
        "cron_expr": "invalid",
        "next_run_at": now.isoformat(),
        "task_summary": "scheduled",
    }

    class _BL:
        @staticmethod
        def list_due_scheduled_jobs(**_kwargs: Any) -> list[dict[str, Any]]:
            return [job]

        @staticmethod
        def claim_due_scheduled_job(**_kwargs: Any) -> dict[str, Any]:
            return dict(job)

    summary = api_runtime._scheduled_jobs_scan(_BL(), now_utc=now)
    assert summary["checked"] == 1
    assert summary["failed"] == 1
