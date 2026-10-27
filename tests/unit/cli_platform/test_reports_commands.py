"""Contract tests for schema-driven report commands."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from click.testing import CliRunner

from core.application.management.scope import ScopeContext
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


class _ReportingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def report_generate(
        self,
        report_type: str,
        params: dict[str, object],
        *,
        scope: object,
        persist: bool = True,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "report_generate",
                {
                    "report_type": report_type,
                    "params": params,
                    "scope": scope,
                    "persist": persist,
                },
            )
        )
        return {
            "id": "report_1" if persist else None,
            "report_type": report_type,
            "status": "generated",
            "params": params,
            "summary_text": "ok",
            "payload": {"rows": []},
            "chart_payload": {},
            "directives": [],
            "created_at": "2026-07-17T00:00:00+00:00",
        }

    def report_get(self, report_id: str, *, scope: object) -> dict[str, object]:
        self.calls.append(("report_get", {"report_id": report_id, "scope": scope}))
        return {
            "id": report_id,
            "report_type": "job_summary",
            "status": "generated",
            "params": {},
            "summary_text": "saved",
            "payload": {"rows": []},
            "chart_payload": {},
            "directives": [],
            "created_at": "2026-07-17T00:00:00+00:00",
        }


@dataclass
class _Runtime:
    reporting_service: _ReportingService
    project_query_service: object = object()
    project_command_service: object = object()
    job_query_service: object = object()
    job_command_service: object = object()
    approval_command_service: object = object()
    workflow_operator_service: object = object()
    user_query_service: object = object()
    user_command_service: object = object()
    extension_query_service: object = object()
    extension_binding_service: object = object()
    extension_scaffold_service: object = object()


def _app() -> tuple[object, _ReportingService]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    service = _ReportingService()
    return build_click_app(ServiceRegistry(_Runtime(service)), SimpleNamespace()), service


def _assert_cli_scope(value: object) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "system_actor"
    return value


def test_reports_generate_and_get_delegate_to_reporting_service() -> None:
    app, service = _app()

    generated = CliRunner().invoke(
        app,
        [
            "reports",
            "generate",
            "activity_daily",
            "--params",
            '{"project_id": "proj_1"}',
            "--no-persist",
            "--confirm",
            "--plain",
        ],
        input="y\n",
    )
    assert generated.exit_code == 0, generated.output
    method, payload = service.calls[-1]
    scope = payload.pop("scope")
    assert method == "report_generate"
    assert payload == {
        "report_type": "activity_daily",
        "params": {"project_id": "proj_1"},
        "persist": False,
    }
    _assert_cli_scope(scope)

    fetched = CliRunner().invoke(app, ["reports", "get", "report_1", "--plain"])
    assert fetched.exit_code == 0, fetched.output
    method, payload = service.calls[-1]
    scope = payload.pop("scope")
    assert method == "report_get"
    assert payload == {"report_id": "report_1"}
    _assert_cli_scope(scope)


def test_reports_generate_validates_params_before_service_call() -> None:
    app, service = _app()

    result = CliRunner().invoke(
        app,
        ["reports", "generate", "job_summary", "--json", "--params", "not-json"],
        input="y\n",
    )

    assert result.exit_code == 3
    assert service.calls == []


def test_reports_generate_rejects_nested_secret_params_before_service_call() -> None:
    app, service = _app()
    raw_secret = "report-secret-value"

    result = CliRunner().invoke(
        app,
        [
            "reports",
            "generate",
            "job_summary",
            "--json",
            "--params",
            f'{{"filters": {{"token": "{raw_secret}"}}}}',
        ],
        input="y\n",
    )

    assert result.exit_code == 3
    assert service.calls == []
    assert raw_secret not in result.output


def test_project_report_choices_and_workflow_params_reach_shared_service():
    app, service = _app()
    for report_type in ("project_summary", "project_stats"):
        result = CliRunner().invoke(
            app,
            [
                "reports",
                "generate",
                report_type,
                "--params",
                '{"workflow_id": "release", "range": "7d"}',
                "--no-persist",
                "--confirm",
                "--plain",
            ],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert service.calls[-1][1]["params"] == {"workflow_id": "release", "range": "7d"}
