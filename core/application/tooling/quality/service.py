"""Typed quality tooling operations for schema-driven public CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.application.management.command_specs.contracts import OperationProgressEvent
from core.application.tooling.contracts import ToolRunner, ToolRunResult
from core.application.tooling.quality.models import (
    QualityCheckCiExcludesRequest,
    QualityCheckNamingRequest,
    QualityLintRequest,
    QualityReleaseGateRequest,
    QualitySonarEnsureRulesRequest,
    QualitySonarExportIssuesRequest,
    QualitySonarGenerateBacklogRequest,
    QualityVerifyRequest,
)
from core.application.tooling.quality.runtime_routing import (
    AUDIT_SERVICE,
    already_delegated,
    delegation_command,
)


def _classify_sonar_failure(result: ToolRunResult) -> ToolRunResult:
    if result.exit_code == 0:
        return result
    diagnostic = f"{result.stderr_full}\n{result.stderr}".casefold()
    if "no sonar_issues" in diagnostic or "input csv not found" in diagnostic:
        code = "sonar_input_missing"
        remediation = "Provide a non-empty Sonar issue CSV export and retry."
    elif "http 401" in diagnostic or "http 403" in diagnostic:
        code = "sonar_authentication_failed"
        remediation = "Verify the protected Sonar token and its project permissions."
    elif "failed to reach sonarqube" in diagnostic:
        code = "sonar_unreachable"
        remediation = "Verify the Sonar URL and network reachability, then retry."
    else:
        code = "sonar_failed"
        remediation = "Inspect the sanitized operation log for the Sonar failure detail."
    return result.model_copy(
        update={"details": {**result.details, "error_code": code, "remediation": remediation}}
    )


def _audit_runtime_command(command: list[str]) -> list[str]:
    if already_delegated():
        return command
    return delegation_command(AUDIT_SERVICE, command)


class QualityToolingService:
    """Run quality tooling leaves through explicit typed request models."""

    def __init__(self, *, runner: ToolRunner, lint_script_path: str | Path) -> None:
        self._runner = runner
        self.lint_script_path = Path(lint_script_path)

    def lint(
        self,
        request: QualityLintRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        args: list[str] = ["--inside", "--permission-preflight"]
        if request.mode != "default":
            args.append(f"--{request.mode}")
        if request.checks:
            args.extend(["--checks", request.checks])
        return self._runner.run_process(
            operation="quality.lint",
            command=_audit_runtime_command(["bash", str(self.lint_script_path), *args]),
            timeout_seconds=request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def check_ci_excludes(
        self,
        request: QualityCheckCiExcludesRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.quality import check_ci_excludes

        return self._runner.run_callable(
            operation="quality.check-ci-excludes",
            target=lambda: check_ci_excludes.run_check_ci_excludes(path=request.path),
            timeout_seconds=request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def check_naming(
        self,
        request: QualityCheckNamingRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.quality import check_naming

        files = [item.strip() for item in request.files.split(",") if item.strip()]
        return self._runner.run_callable(
            operation="quality.check-naming",
            target=lambda: check_naming.run_check_naming(
                base="" if files else request.base,
                files=files or None,
            ),
            timeout_seconds=request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def release_gate(
        self,
        request: QualityReleaseGateRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.quality import release_gate

        return self._runner.run_callable(
            operation="quality.release-gate",
            target=release_gate.run_release_gate,
            timeout_seconds=request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def verify(
        self,
        request: QualityVerifyRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        command = [
            "python",
            "-m",
            "core.application.tooling.quality.verify",
            "--tier",
            str(request.tier),
            "--evidence-mode",
            request.evidence_mode,
            "--fingerprint-source",
            request.fingerprint_source,
            "--trust",
            request.trust,
            "--repository-root",
            request.repository_root,
            "--evidence-root",
            request.evidence_root,
            "--timeout-seconds",
            str(request.timeout_seconds),
        ]
        if request.commit_sha:
            command.extend(["--commit-sha", request.commit_sha])

        repository_root = Path(request.repository_root)
        return self._runner.run_process(
            operation="quality.verify",
            command=_audit_runtime_command(command),
            timeout_seconds=request.timeout_seconds,
            artifacts=[str(repository_root / request.evidence_root)],
            progress_sink=progress_sink,
        )

    def sonar_export_issues(
        self,
        request: QualitySonarExportIssuesRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.quality import sonar_export_issues

        return _classify_sonar_failure(
            self._runner.run_callable(
                operation="quality.sonar-export-issues",
                target=lambda: sonar_export_issues.export_sonar_issues(
                    host_url=request.host_url,
                    project_key=request.project_key,
                    statuses=request.statuses,
                    severities=request.severities,
                    types=request.types,
                    page_size=request.page_size,
                    output_dir=request.output_dir,
                    sonar_token=request.sonar_token,
                ),
                timeout_seconds=request.timeout_seconds,
                artifacts=[request.output_dir],
                progress_sink=progress_sink,
            )
        )

    def sonar_ensure_rules(
        self,
        request: QualitySonarEnsureRulesRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.quality import sonar_ensure_rules

        return _classify_sonar_failure(
            self._runner.run_callable(
                operation="quality.sonar-ensure-rules",
                target=lambda: sonar_ensure_rules.ensure_sonar_rules(
                    host_url=request.host_url,
                    rules=request.rules,
                    sonar_token=request.sonar_token,
                ),
                timeout_seconds=request.timeout_seconds,
                progress_sink=progress_sink,
            )
        )

    def sonar_generate_backlog(
        self,
        request: QualitySonarGenerateBacklogRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.quality import sonar_generate_backlog

        return _classify_sonar_failure(
            self._runner.run_callable(
                operation="quality.sonar-generate-backlog",
                target=lambda: sonar_generate_backlog.generate_sonar_backlog(
                    input_csv=request.input_csv,
                    input_dir=request.input_dir,
                    output=request.output,
                    top_rules=request.top_rules,
                    top_files=request.top_files,
                    max_p0=request.max_p0,
                    max_p1=request.max_p1,
                ),
                timeout_seconds=request.timeout_seconds,
                artifacts=[request.output],
                progress_sink=progress_sink,
            )
        )
