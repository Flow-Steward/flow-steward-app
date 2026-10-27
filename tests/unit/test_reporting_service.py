from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if "psycopg_runtime" not in sys.modules:
    psycopg_runtime_mod = types.ModuleType("psycopg_runtime")
    extras_mod = types.ModuleType("psycopg_runtime.extras")
    extras_mod.RealDictCursor = object
    psycopg_runtime_mod.extras = extras_mod
    sys.modules["psycopg_runtime"] = psycopg_runtime_mod
    sys.modules["psycopg_runtime.extras"] = extras_mod

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.application.reports import reporting_service as reporting_service_module
from core.application.reports.reporting_service import ReportingService


class _View:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _FakeConnection:
    def commit(self) -> None:
        return None


class _FakeLogger:
    def __init__(self):
        self.conn = _FakeConnection()

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        if int(project_id) == 7:
            return {"id": 7, "name": "Demo", "account_id": "acc_default"}
        if int(project_id) == 8:
            return {"id": 8, "name": "Other", "account_id": "acc_default"}
        return None

    def list_dashboard_job_facts(
        self,
        *,
        account_id: str | None = None,
        window_start=None,
        window_end=None,
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "acc_default",
                "tenant_id": DEFAULT_TENANT_ID,
                "job_id": 1,
                "project_id": "7",
                "agent": "composer",
                "result": "completed",
                "finished_date": "2026-04-01",
                "duration_seconds": 120.0,
                "cost_usd": 1.10,
            },
            {
                "account_id": "acc_default",
                "tenant_id": DEFAULT_TENANT_ID,
                "job_id": 2,
                "project_id": "7",
                "agent": "proofreader",
                "result": "failed",
                "finished_date": "2026-04-02",
                "duration_seconds": 180.0,
                "cost_usd": 0.40,
            },
            {
                "account_id": "acc_default",
                "tenant_id": DEFAULT_TENANT_ID,
                "job_id": 3,
                "project_id": "8",
                "agent": "composer",
                "result": "completed",
                "finished_date": "2026-04-01",
                "duration_seconds": 240.0,
                "cost_usd": 9.99,
            },
        ]

    def list_report_activity_facts(
        self, *, scope, project_id, workflow_id, window_start, window_end
    ):
        from core.application.jobs.query_scope import job_row_in_scope

        rows = self.list_dashboard_job_facts(
            account_id=scope.account_id, window_start=window_start, window_end=window_end
        )
        return [
            row
            for row in rows
            if job_row_in_scope(scope, row)
            and (not project_id or str(row["project_id"]) == project_id)
            and (not workflow_id or row.get("selected_workflow") == workflow_id)
        ]

    def get_core_slo_metrics(
        self,
        *,
        account_id: str | None = None,
        window_hours: int = 24,
    ) -> dict[str, Any]:
        return {
            "window_hours": window_hours,
            "p95_latency_seconds": 42.0,
            "retry_rate": 0.1,
            "dead_letter_rate": 0.02,
            "stuck_jobs": 1,
            "per_agent_failure_rate": [
                {"agent": "composer", "failure_rate": 0.25},
                {"agent": "proofreader", "failure_rate": 0.5},
            ],
        }


class _FakeJobQueryService:
    def validate_job_diagnostic_report_access(self, job_id: str, *, scope=None) -> None:
        return None

    def get_cost_breakdown(self, job_id: int, *, scope=None) -> _View:
        return _View(
            {
                "breakdown": [
                    {
                        "agent": "composer",
                        "model_used": "gpt-5.4",
                        "calls": 3,
                        "tokens_prompt": 1000,
                        "tokens_completion": 500,
                        "cost_usd": 0.9,
                    },
                    {
                        "agent": "proofreader",
                        "model_used": "gpt-5.4-mini",
                        "calls": 2,
                        "tokens_prompt": 800,
                        "tokens_completion": 200,
                        "cost_usd": 0.52,
                    },
                ],
                "total_cost_usd": 1.42,
            }
        )

    def get_logs_page(self, job_id: int, *, initial_limit: int = 25, scope=None) -> _View:
        return _View(
            {
                "job": {
                    "id": job_id,
                    "project_id": 7,
                    "task_summary": "Ship reporting",
                    "status": "completed",
                    "duration_ms": 180000,
                },
                "project_name": "Demo",
                "events": [{"id": 1}, {"id": 2}],
                "messages": [{"id": 10}],
                "logs": [{"id": 100}],
                "job_failure_reason": "",
            }
        )

    def get_incident_drilldown(self, job_id: int, *, scope=None) -> _View:
        return _View({"root_cause": {"message": ""}})


def _service() -> ReportingService:
    return ReportingService(
        get_logger_only=lambda: _FakeLogger(),
        job_query_service=lambda: _FakeJobQueryService(),
    )


def test_report_generate_job_cost_breakdown_returns_chart_directive():
    payload = _service().report_generate(
        "job_cost_breakdown",
        {"job_id": 42},
        scope=ScopeContext(account_id="acc_default"),
        persist=False,
    )

    assert payload["report_type"] == "job_cost_breakdown"
    assert payload["payload"]["table"]["total_cost_display"] == "$1.42"
    assert "Job 42 cost stats" in payload["summary_text"]
    assert "Total spend: $1.42" in payload["summary_text"]
    assert "Tokens: 2,500" in payload["summary_text"]
    assert "Top spender:" in payload["summary_text"]
    assert payload["chart_payload"]["type"] == "chart"
    assert payload["chart_payload"]["chart_type"] in {"bar", "stacked_bar"}
    assert payload["directives"][0]["chart_library"] == "echarts"


def test_report_generate_job_summary_uses_status_focused_copy():
    payload = _service().report_generate(
        "job_summary",
        {"job_id": 42},
        scope=ScopeContext(account_id="acc_default"),
        persist=False,
    )

    assert payload["report_type"] == "job_summary"
    assert "Job 42 summary" in payload["summary_text"]
    assert "Status: completed" in payload["summary_text"]
    assert "Workflow: Ship reporting" in payload["summary_text"]
    assert "Next step:" in payload["summary_text"]
    assert "Total spend:" not in payload["summary_text"]


def test_report_generate_activity_daily_filters_project_rows():
    payload = _service().report_generate(
        "activity_daily",
        {
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
            "project_id": "7",
        },
        scope=ScopeContext(
            account_id="acc_default",
            allowed_project_ids=frozenset({"7"}),
        ),
        persist=False,
    )

    assert payload["payload"]["project_id"] == "7"
    assert payload["payload"]["kpis"]["total_jobs"] == 2
    assert payload["payload"]["kpis"]["total_spend_usd"] == 1.5
    assert payload["chart_payload"]["dataset"]["x"] == [
        "2026-04-01",
        "2026-04-02",
        "2026-04-03",
    ]


def test_report_generate_project_summary_uses_actionable_project_copy():
    payload = _service().report_generate(
        "project_summary",
        {
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
            "project_id": "7",
        },
        scope=ScopeContext(
            account_id="acc_default",
            allowed_project_ids=frozenset({"7"}),
        ),
        persist=False,
    )

    assert payload["report_type"] == "project_summary"
    assert "Demo summary" in payload["summary_text"]
    assert "Finished jobs: 2 (1 completed, 1 failed)" in payload["summary_text"]
    assert "Next step:" in payload["summary_text"]
    assert payload["payload"]["highlights"]["top_cost_center"]["agent"] is not None


def test_report_generate_project_stats_is_numbers_first():
    payload = _service().report_generate(
        "project_stats",
        {
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
            "project_id": "7",
        },
        scope=ScopeContext(
            account_id="acc_default",
            allowed_project_ids=frozenset({"7"}),
        ),
        persist=False,
    )

    assert payload["report_type"] == "project_stats"
    assert "Demo stats" in payload["summary_text"]
    assert "Total jobs: 2" in payload["summary_text"]
    assert "Completed: 1" in payload["summary_text"]
    assert "Failed: 1" in payload["summary_text"]
    assert "Active days: 2" in payload["summary_text"]


def test_report_generate_core_health_summary_returns_agent_chart():
    payload = _service().report_generate(
        "core_health_summary",
        {"window_hours": 24},
        scope=ScopeContext(account_id="acc_default"),
        persist=False,
    )

    assert payload["payload"]["metrics"]["stuck_jobs"] == 1
    assert payload["chart_payload"]["chart_type"] == "bar"
    assert payload["chart_payload"]["dataset"]["series"][0]["data"] == [25.0, 50.0]


def test_report_generate_with_persist_false_is_ephemeral(monkeypatch):
    def _should_not_persist(**_kwargs):
        raise AssertionError("report store should not be called when persist=False")

    monkeypatch.setattr(
        reporting_service_module.report_run_store, "create_report_run", _should_not_persist
    )

    payload = _service().report_generate(
        "job_cost_breakdown",
        {"job_id": 42},
        scope=ScopeContext(account_id="acc_default"),
        persist=False,
    )

    assert payload["id"] is None
    assert payload["status"] == "generated"
    assert payload["summary_text"]


def test_report_generate_defaults_missing_tenant_for_persisted_reports(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_create_report_run(**kwargs):
        captured.update(kwargs)
        return {
            "id": "report-default-tenant",
            "status": "generated",
            "created_at": datetime(2026, 4, 4, tzinfo=UTC),
        }

    monkeypatch.setattr(
        reporting_service_module.report_run_store, "create_report_run", _fake_create_report_run
    )

    payload = _service().report_generate(
        "core_health_summary",
        {"window_hours": 24},
        scope=ScopeContext(account_id="acc_default"),
        persist=True,
    )

    assert payload["id"] == "report-default-tenant"
    assert captured["tenant_id"] == DEFAULT_TENANT_ID
    assert "session_id" not in captured


def test_report_get_denies_cross_tenant_access(monkeypatch):
    def _fake_get_report_run(**_kwargs):
        return {
            "id": "report-1",
            "account_id": "acc_default",
            "tenant_id": "tenant-a",
            "project_id": None,
            "report_type": "job_cost_breakdown",
            "params_jsonb": {"job_id": 42},
            "payload_jsonb": {"snapshot": True},
            "chart_payload_jsonb": {},
            "summary_text": "saved",
            "status": "generated",
            "generated_by": "operator",
            "created_at": datetime(2026, 4, 4, tzinfo=UTC),
        }

    monkeypatch.setattr(
        reporting_service_module.report_run_store, "get_report_run", _fake_get_report_run
    )

    _raises_callable_392_1 = _service().report_get
    _raises_input_392_2 = ScopeContext(account_id="acc_default", tenant_id="tenant-b")
    with pytest.raises(ServiceNotFoundError):
        _raises_callable_392_1("report-1", scope=_raises_input_392_2)


def test_report_get_uses_persisted_payload_snapshot(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_get_report_run(**kwargs):
        captured.update(kwargs)
        return {
            "id": "report-2",
            "account_id": "acc_default",
            "tenant_id": DEFAULT_TENANT_ID,
            "project_id": None,
            "report_type": "activity_daily",
            "params_jsonb": {"range": "7d"},
            "payload_jsonb": {"kpis": {"total_jobs": 11}, "frozen": True},
            "chart_payload_jsonb": {"type": "chart", "chart_type": "line"},
            "summary_text": "saved snapshot",
            "status": "generated",
            "generated_by": "operator",
            "created_at": datetime(2026, 4, 4, tzinfo=UTC),
        }

    monkeypatch.setattr(
        reporting_service_module.report_run_store, "get_report_run", _fake_get_report_run
    )
    service = _service()

    def _must_not_regenerate(*_args, **_kwargs):
        raise AssertionError("report_get should not regenerate when payload snapshot exists")

    monkeypatch.setattr(service, "_build_report", _must_not_regenerate)

    payload = service.report_get("report-2", scope=ScopeContext(account_id="acc_default"))

    assert captured["tenant_id"] == DEFAULT_TENANT_ID
    assert "session_id" not in captured
    assert payload["payload"] == {"kpis": {"total_jobs": 11}, "frozen": True}
    assert payload["chart_payload"]["chart_type"] == "line"


def test_report_get_regenerates_payload_for_retired_rows_without_snapshot(monkeypatch):
    def _fake_get_report_run(**_kwargs):
        return {
            "id": "retired-1",
            "account_id": "acc_default",
            "tenant_id": None,
            "project_id": None,
            "report_type": "core_health_summary",
            "params_jsonb": {"window_hours": 24},
            "chart_payload_jsonb": {},
            "summary_text": "retired",
            "status": "generated",
            "generated_by": "operator",
            "created_at": datetime(2026, 4, 4, tzinfo=UTC),
        }

    monkeypatch.setattr(
        reporting_service_module.report_run_store, "get_report_run", _fake_get_report_run
    )
    service = _service()
    monkeypatch.setattr(
        service,
        "_build_report",
        lambda *_args, **_kwargs: {"payload": {"source": "regenerated"}},
    )

    payload = service.report_get("retired-1", scope=ScopeContext(account_id="acc_default"))

    assert payload["payload"] == {"source": "regenerated"}


class _ScopedActivityLogger(_FakeLogger):
    def get_project(self, project_id):
        row = super().get_project(project_id)
        return (
            {**row, "tenant_id": DEFAULT_TENANT_ID, "workspace_id": "workspace-a"} if row else None
        )

    def list_dashboard_job_facts(self, **kwargs):
        rows = super().list_dashboard_job_facts(**kwargs)
        for index, row in enumerate(rows):
            row.update(
                account_id="acc_default",
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id="workspace-a",
                selected_workflow="release" if index != 1 else "other",
            )
        rows.extend(
            [
                {**rows[0], "job_id": 4, "tenant_id": "foreign-tenant"},
                {**rows[0], "job_id": 5, "workspace_id": "foreign-workspace"},
            ]
        )
        return rows


def _scoped_report_service():
    return ReportingService(
        get_logger_only=lambda: _ScopedActivityLogger(),
        job_query_service=lambda: _FakeJobQueryService(),
    )


@pytest.mark.parametrize("report_type", ["activity_daily", "project_summary", "project_stats"])
def test_workflow_reports_filter_all_facts_and_secondary_aggregates(report_type):
    result = _scoped_report_service().report_generate(
        report_type,
        {
            "workflow_id": "release",
            "project_id": "7",
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
        },
        scope=ScopeContext(
            account_id="acc_default", allowed_workspace_ids=frozenset({"workspace-a"})
        ),
        persist=False,
    )
    assert result["params"]["workflow_id"] == "release"
    assert result["payload"]["workflow_id"] == "release"
    assert result["payload"]["kpis"]["total_jobs"] == 1
    assert sum(row["job_count"] for row in result["payload"]["spend_by_agent"]) == 1


def test_reports_omitted_filters_preserve_restricted_membership():
    result = _scoped_report_service().report_generate(
        "activity_daily",
        {"start_date": "2026-04-01", "end_date": "2026-04-03"},
        scope=ScopeContext(
            account_id="acc_default",
            allowed_project_ids=frozenset({"7"}),
            allowed_workflow_ids=frozenset({"release"}),
            allowed_workspace_ids=frozenset({"workspace-a"}),
        ),
        persist=False,
    )
    assert result["payload"]["kpis"]["total_jobs"] == 1


@pytest.mark.parametrize(
    "filters, scope",
    [
        (
            {"workflow_id": "other"},
            ScopeContext(account_id="acc_default", allowed_workflow_ids=frozenset({"release"})),
        ),
        (
            {"project_id": "8"},
            ScopeContext(account_id="acc_default", allowed_project_ids=frozenset({"7"})),
        ),
        ({"project_id": "7"}, ScopeContext(account_id="acc_default", tenant_id="foreign-tenant")),
    ],
)
def test_report_filters_reject_foreign_scope(filters, scope):
    from core.application.management.errors import ServiceForbiddenError

    service = _scoped_report_service()
    with pytest.raises(ServiceForbiddenError):
        service.report_generate("activity_daily", filters, scope=scope, persist=False)


def test_unknown_workflow_produces_empty_filtered_report():
    result = _scoped_report_service().report_generate(
        "project_stats",
        {"workflow_id": "missing", "start_date": "2026-04-01", "end_date": "2026-04-03"},
        scope=ScopeContext(account_id="acc_default"),
        persist=False,
    )
    assert result["payload"]["kpis"]["total_jobs"] == 0
    assert result["payload"]["spend_by_agent"] == []


def test_workflow_restricted_caller_cannot_read_broad_persisted_report(monkeypatch):
    from core.application.management.errors import ServiceForbiddenError

    monkeypatch.setattr(
        reporting_service_module.report_run_store,
        "get_report_run",
        lambda **kwargs: {
            "account_id": "acc_default",
            "tenant_id": DEFAULT_TENANT_ID,
            "project_id": None,
            "report_type": "activity_daily",
            "params_jsonb": {"range": "7d"},
            "payload_jsonb": {"kpis": {"total_jobs": 999}},
        },
    )
    service = _service()
    scope = ScopeContext(account_id="acc_default", allowed_workflow_ids=frozenset({"release"}))
    with pytest.raises(ServiceForbiddenError):
        service.report_get("report", scope=scope)


def test_default_tenant_report_cannot_use_tenantless_account_rollups():
    logger = _ScopedActivityLogger()
    logger.list_dashboard_daily_rollups = lambda **kwargs: [
        {"rollup_date": "2026-04-01", "completed_count": 999}
    ]
    service = ReportingService(
        get_logger_only=lambda: logger, job_query_service=lambda: _FakeJobQueryService()
    )
    result = service.report_generate(
        "activity_daily",
        {"start_date": "2026-04-01", "end_date": "2026-04-03"},
        scope=ScopeContext(account_id="acc_default", tenant_id=DEFAULT_TENANT_ID),
        persist=False,
    )
    assert result["payload"]["kpis"]["total_jobs"] == 4


@pytest.mark.parametrize(
    "report_type, params",
    [
        ("job_summary", {"job_id": "job-1"}),
        ("activity_daily", {"project_id": "7", "workflow_id": "release"}),
    ],
)
def test_scoped_saved_reports_preserve_access_to_allowed_job_and_project(
    monkeypatch, report_type, params
):
    monkeypatch.setattr(
        reporting_service_module.report_run_store,
        "get_report_run",
        lambda **kwargs: {
            "account_id": "acc_default",
            "tenant_id": DEFAULT_TENANT_ID,
            "project_id": "7",
            "report_type": report_type,
            "params_jsonb": params,
            "payload_jsonb": {"frozen": True},
        },
    )
    logger = _ScopedActivityLogger()
    jobs = _FakeJobQueryService()
    service = ReportingService(get_logger_only=lambda: logger, job_query_service=lambda: jobs)
    result = service.report_get(
        "report",
        scope=ScopeContext(
            account_id="acc_default",
            allowed_workflow_ids=frozenset({"release"}),
            allowed_workspace_ids=frozenset({"workspace-a"}),
        ),
    )
    assert result["payload"] == {"frozen": True}


def test_restricted_job_report_generate_persist_get_rechecks_current_job_access(monkeypatch):
    from core.application.management.errors import ServiceForbiddenError

    stored = {}
    calls = []
    logger = _ScopedActivityLogger()
    jobs = _FakeJobQueryService()

    def checked_access(job_id, *, scope=None):
        calls.append(scope)
        if scope.allowed_workflow_ids != frozenset({"release"}):
            raise ServiceForbiddenError("Access denied")

    jobs.validate_job_diagnostic_report_access = checked_access

    def persist(**kwargs):
        stored.update(
            id="saved-job",
            account_id=kwargs["account_id"],
            tenant_id=kwargs["tenant_id"],
            project_id=kwargs["project_id"],
            report_type=kwargs["report_type"],
            params_jsonb=kwargs["params"],
            payload_jsonb=kwargs["payload"],
            summary_text=kwargs["summary_text"],
            status="generated",
        )
        return stored

    monkeypatch.setattr(reporting_service_module.report_run_store, "create_report_run", persist)
    monkeypatch.setattr(
        reporting_service_module.report_run_store, "get_report_run", lambda **kwargs: stored
    )
    service = ReportingService(get_logger_only=lambda: logger, job_query_service=lambda: jobs)
    scope = ScopeContext(
        account_id="acc_default",
        allowed_workflow_ids=frozenset({"release"}),
        allowed_project_ids=frozenset({"7"}),
        allowed_workspace_ids=frozenset({"workspace-a"}),
    )
    generated = service.report_generate("job_summary", {"job_id": "job-1"}, scope=scope)
    fetched = service.report_get(generated["id"], scope=scope)
    assert fetched["payload"] == generated["payload"]
    assert calls == [scope, scope]
    foreign_scope = ScopeContext(
        account_id="acc_default", allowed_workflow_ids=frozenset({"other"})
    )
    with pytest.raises(ServiceForbiddenError):
        service.report_get(generated["id"], scope=foreign_scope)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "field, own, foreign",
    [
        ("workflow_id", "release", "other"),
        ("project_id", "7", "other"),
        ("workspace_id", "workspace-a", "other"),
    ],
)
def test_saved_operational_report_enforces_exact_current_scope(monkeypatch, field, own, foreign):
    from core.application.management.errors import ServiceForbiddenError

    stored = {}

    def persist(**kwargs):
        stored.update(
            id="saved-exact",
            account_id=kwargs["account_id"],
            tenant_id=kwargs["tenant_id"],
            project_id=kwargs["project_id"],
            report_type=kwargs["report_type"],
            params_jsonb=kwargs["params"],
            payload_jsonb=kwargs["payload"],
            status="generated",
        )
        return stored

    monkeypatch.setattr(reporting_service_module.report_run_store, "create_report_run", persist)
    monkeypatch.setattr(
        reporting_service_module.report_run_store, "get_report_run", lambda **kwargs: stored
    )
    service = ReportingService(
        get_logger_only=lambda: _ScopedActivityLogger(),
        job_query_service=lambda: _FakeJobQueryService(),
    )
    generated = service.report_generate(
        "activity_daily",
        {"project_id": "7", "workflow_id": "release"},
        scope=ScopeContext(account_id="acc_default"),
    )
    assert (
        service.report_get(
            generated["id"], scope=ScopeContext(account_id="acc_default", **{field: own})
        )["payload"]
        == generated["payload"]
    )
    foreign_scope = ScopeContext(account_id="acc_default", **{field: foreign})
    with pytest.raises(ServiceForbiddenError):
        service.report_get(generated["id"], scope=foreign_scope)


@pytest.mark.parametrize("status", ["awaiting_timer", "awaiting_child"])
def test_job_next_step_for_a_durable_wait_needs_no_one(status: str) -> None:
    next_step = ReportingService._job_next_step(status)

    assert next_step.startswith("No action is required")
    assert "approval" not in next_step.lower()
