"""Typed testing tooling operations for schema-driven public CLI."""

from __future__ import annotations

from collections.abc import Callable

from core.application.management.command_specs.contracts import OperationProgressEvent
from core.application.tooling.contracts import ToolRunner, ToolRunResult
from core.application.tooling.testing.models import (
    TestingCutoverPreflightRequest,
    TestingCutoverSmokeRequest,
    TestingFuzzApiRequest,
    TestingFuzzMcpRequest,
    TestingFuzzUiRequest,
    TestingNightfireRegressionRequest,
    TestingSmokeTestRequest,
)


def _classify_cutover_failure(result: ToolRunResult) -> ToolRunResult:
    if int(result.exit_code or 0) == 0:
        return result
    diagnostic = f"{result.stderr_full}\n{result.stderr}\n{result.stdout}".lower()
    if "authentication" in diagnostic or "http 401" in diagnostic:
        error_code = "authentication_failed"
        remediation = "Supply a fresh administrator PAT or valid username/password."
    elif "http 403" in diagnostic or "forbidden" in diagnostic:
        error_code = "scope_forbidden"
        remediation = "Use an administrator credential with access to the selected project."
    elif "connection error" in diagnostic or "failed to establish" in diagnostic:
        error_code = "transport_unavailable"
        remediation = "Verify the candidate base URL, network, and TLS option, then retry."
    elif "no linked, enabled workflow" in diagnostic or "/contract" in diagnostic:
        error_code = "workflow_precondition_failed"
        remediation = "Link and enable a runnable workflow with a valid contract for the project."
    elif "timed out" in diagnostic or "did not complete" in diagnostic:
        error_code = "runtime_timeout"
        remediation = (
            "Inspect the candidate worker/job state and retry after the runtime is healthy."
        )
    else:
        error_code = "cutover_runtime_failed"
        remediation = "Inspect the reported candidate API/runtime failure and retry."
    details = dict(result.details)
    details.update({"error_code": error_code, "remediation": remediation})
    return result.model_copy(update={"details": details})


class TestingToolingService:
    """Run testing tooling leaves through explicit typed request models."""

    def __init__(
        self,
        *,
        runner: ToolRunner,
        database_dsn_provider: Callable[[], str] | None = None,
        rabbitmq_url_provider: Callable[[], str] | None = None,
    ) -> None:
        self._runner = runner
        self._database_dsn_provider = database_dsn_provider
        self._rabbitmq_url_provider = rabbitmq_url_provider

    def _database_dsn(self) -> str:
        if self._database_dsn_provider is None:
            return ""
        return str(self._database_dsn_provider() or "").strip()

    def _rabbitmq_url(self) -> str:
        if self._rabbitmq_url_provider is None:
            return ""
        return str(self._rabbitmq_url_provider() or "").strip()

    def smoke_test(
        self,
        request: TestingSmokeTestRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.testing import smoke_test

        return self._runner.run_callable(
            operation="testing.smoke-test",
            target=lambda: smoke_test.run_smoke_test(
                project_id=request.project_id,
                workflow_id=request.workflow_id,
                lane=request.lane,
                base_url=request.base_url,
                verify_ssl=request.verify_ssl,
            ),
            timeout_seconds=request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def nightfire_regression(
        self,
        request: TestingNightfireRegressionRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.testing import nightfire_regression

        return self._runner.run_callable(
            operation="testing.nightfire-regression",
            target=lambda: nightfire_regression.run_nightfire_regression(
                lane=request.lane,
                project_id=request.project_id,
                base_url=request.base_url,
                verify_ssl=request.verify_ssl,
                auth_role=request.auth_role,
                auth_username=request.auth_username,
                auth_password=request.auth_password,
                auth_display_name=request.auth_display_name,
                source_workflow_id=request.source_workflow_id,
                report_path=request.report_path,
                with_cutover_smoke=request.with_cutover_smoke,
                with_full_ui=request.with_full_ui,
                fail_fast=request.fail_fast,
            ),
            timeout_seconds=request.timeout_seconds,
            artifacts=[request.report_path] if request.report_path else [],
            progress_sink=progress_sink,
        )

    def fuzz_api(
        self,
        request: TestingFuzzApiRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.testing import fuzz_api

        return self._runner.run_callable(
            operation="testing.fuzz-api",
            target=lambda: fuzz_api.run_fuzz_api(
                base_url=request.base_url,
                seed=request.seed,
                max_text_size=request.max_text_size,
                timeout=request.timeout,
                report_path=request.report_path,
                role=request.role,
                username=request.username,
                password=request.password,
                skip_auth=request.skip_auth,
                strict=request.strict,
            ),
            timeout_seconds=max(1.0, float(request.timeout) * 20),
            artifacts=[request.report_path] if request.report_path else [],
            progress_sink=progress_sink,
        )

    def fuzz_mcp(
        self,
        request: TestingFuzzMcpRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.testing import fuzz_mcp

        return self._runner.run_callable(
            operation="testing.fuzz-mcp",
            target=lambda: fuzz_mcp.run_fuzz_mcp(
                app_base_url=request.app_base_url,
                mcp_base_url=request.mcp_base_url,
                seed=request.seed,
                timeout=request.timeout,
                burst_size=request.burst_size,
                report_path=request.report_path,
                role=request.role,
                username=request.username,
                password=request.password,
                bearer_token=request.bearer_token,
                skip_auth=request.skip_auth,
                strict=request.strict,
            ),
            timeout_seconds=max(1.0, float(request.timeout) * 20),
            artifacts=[request.report_path] if request.report_path else [],
            progress_sink=progress_sink,
        )

    def fuzz_ui(
        self,
        request: TestingFuzzUiRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.testing import fuzz_ui

        return self._runner.run_callable(
            operation="testing.fuzz-ui",
            target=lambda: fuzz_ui.run_fuzz_ui(
                base_url=request.base_url,
                seed=request.seed,
                timeout=request.timeout,
                report_path=request.report_path,
                role=request.role,
                username=request.username,
                password=request.password,
                skip_auth=request.skip_auth,
                project_id=request.project_id,
                routes=request.routes,
                lighthouse_routes=request.lighthouse_routes,
                lighthouse_threshold=request.lighthouse_threshold,
                lighthouse_timeout=request.lighthouse_timeout,
                skip_lighthouse=request.skip_lighthouse,
                run_chrome_mcp=request.run_chrome_mcp,
                chrome_mcp_script=request.chrome_mcp_script,
                strict=request.strict,
            ),
            timeout_seconds=max(1.0, float(request.timeout) * 30),
            artifacts=[request.report_path] if request.report_path else [],
            progress_sink=progress_sink,
        )

    def cutover_preflight(
        self,
        request: TestingCutoverPreflightRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.testing import cutover_preflight

        database_dsn = self._database_dsn()
        rabbitmq_url = self._rabbitmq_url()
        return self._runner.run_callable(
            operation="testing.cutover-preflight",
            target=lambda: cutover_preflight.run_preflight(
                project_id=request.project_id,
                base_url=request.base_url,
                verify_ssl=request.verify_ssl,
                database_dsn=database_dsn,
                rabbitmq_url=rabbitmq_url,
            ),
            timeout_seconds=request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def cutover_smoke(
        self,
        request: TestingCutoverSmokeRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.testing import cutover_smoke

        result = self._runner.run_callable(
            operation="testing.cutover-smoke",
            target=lambda: cutover_smoke.run_smoke(
                project_id=request.project_id,
                base_url=request.base_url,
                verify_ssl=request.verify_ssl,
                summary=request.summary,
                timeout_seconds=request.timeout_seconds,
                poll_seconds=request.poll_seconds,
                username=request.username,
                password=request.password,
                bearer_token=request.bearer_token,
            ),
            timeout_seconds=request.timeout_seconds + 60,
            progress_sink=progress_sink,
        )
        return _classify_cutover_failure(result)
