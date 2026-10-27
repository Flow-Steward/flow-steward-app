from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from core.application.readiness import CoreReadinessService, ReadinessArea, ReadinessStatus
from core.entrypoints.cli.platform.click_adapter import build_click_app


def _probe(checks):
    return lambda: {"checks": checks}


def test_readiness_service_groups_core_optional_and_recommendations() -> None:
    service = CoreReadinessService(
        probe=_probe(
            [
                {"id": "database", "category": "runtime", "title": "Database", "status": "pass"},
                {
                    "id": "llm_provider",
                    "category": "providers",
                    "title": "Model provider",
                    "status": "warn",
                    "remediation": "Configure after login.",
                },
                {
                    "id": "log_retention",
                    "category": "operations",
                    "title": "Old logs",
                    "status": "warn",
                },
                {
                    "id": "extensions",
                    "category": "extensions",
                    "title": "Extension packages",
                    "status": "fail",
                },
            ]
        )
    )

    report = service.check()

    assert [finding.area for finding in report.findings] == [
        ReadinessArea.CORE,
        ReadinessArea.OPTIONAL,
        ReadinessArea.RECOMMENDATION,
    ]
    assert report.findings[1].status is ReadinessStatus.WARNING
    assert report.core_failed == 0
    assert report.process_exit_code == 0


def test_core_failure_sets_precondition_exit_without_hiding_report() -> None:
    service = CoreReadinessService(
        probe=_probe(
            [
                {
                    "id": "database",
                    "category": "runtime",
                    "title": "Database",
                    "status": "fail",
                    "detail": "connection refused",
                    "remediation": "Start PostgreSQL.",
                }
            ]
        )
    )

    report = service.check()

    assert report.core_failed == 1
    assert report.process_exit_code == 4
    assert report.findings[0].detail == "connection refused"


def test_status_keeps_blocking_findings_and_exit_code() -> None:
    service = CoreReadinessService(
        probe=_probe(
            [
                {"id": "database", "category": "runtime", "status": "fail"},
                {"id": "llm_provider", "category": "providers", "status": "warn"},
            ]
        )
    )

    report = service.status()

    assert [finding.check_id for finding in report.findings] == ["database"]
    assert report.core_failed == 1
    assert report.optional_warnings == 1
    assert report.process_exit_code == 4


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"checks": None},
        {"checks": []},
        {"checks": "not-a-list"},
        {"checks": [{"id": "database", "status": "mystery"}]},
        {"checks": [{"status": "pass"}]},
    ),
)
def test_readiness_contract_fails_closed_for_malformed_payload(payload) -> None:
    report = CoreReadinessService(probe=lambda: payload).check()

    assert report.status == "unknown"
    assert report.core_failed == 1
    assert report.process_exit_code == 4
    assert report.findings[0].check_id == "readiness_contract"


@pytest.mark.parametrize(
    "checks",
    (
        [{"id": "extensions", "category": "extensions", "status": "pass"}],
        [{"id": "llm_provider", "category": "providers", "status": "warn"}],
    ),
)
def test_readiness_requires_explicit_core_evidence(checks) -> None:
    report = CoreReadinessService(probe=_probe(checks)).check()

    assert report.status == "unknown"
    assert report.core_passed == 0
    assert report.core_failed == 1
    assert report.process_exit_code == 4
    assert report.findings[0].check_id == "readiness_contract"


class _Registry:
    def __init__(self, *, blocker: bool = False) -> None:
        self.blocker = blocker
        self.calls: list[str] = []

    async def invoke(self, key, _value, **_kwargs):
        self.calls.append(key)
        return (
            CoreReadinessService(
                probe=_probe(
                    [
                        {
                            "id": "database",
                            "category": "runtime",
                            "title": "Database",
                            "status": "fail" if self.blocker else "pass",
                            "remediation": "Start PostgreSQL." if self.blocker else "",
                        },
                        {
                            "id": "llm_provider",
                            "category": "providers",
                            "title": "AI provider",
                            "status": "warn",
                        },
                    ]
                )
            )
            .check()
            .model_dump(mode="json")
        )

    async def choices(self, _key):
        return ()


def test_root_help_exposes_status_and_doctor() -> None:
    app = build_click_app(_Registry(), SimpleNamespace())

    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "status" in result.output
    assert "doctor" in result.output


def test_doctor_json_keeps_optional_warning_successful() -> None:
    registry = _Registry()
    app = build_click_app(registry, SimpleNamespace())

    result = CliRunner().invoke(app, ["doctor", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["result"]["core_failed"] == 0
    assert payload["result"]["optional_warnings"] == 1
    assert registry.calls == ["readiness.doctor"]


def test_status_blocker_prints_report_and_exits_precondition() -> None:
    registry = _Registry(blocker=True)
    app = build_click_app(registry, SimpleNamespace())

    result = CliRunner().invoke(app, ["status", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 4
    assert payload["ok"] is False
    assert payload["error"]["code"] == "precondition_failed"
    assert payload["error"]["details"]["status"] == "blocked"
    assert payload["error"]["details"]["findings"] == [
        {
            "check_id": "database",
            "area": "core",
            "title": "Database",
            "status": "failed",
            "detail": "",
            "remediation": "Start PostgreSQL.",
        }
    ]
    assert registry.calls == ["readiness.status"]


def test_status_blocker_plain_prints_check_reason_and_recovery() -> None:
    app = build_click_app(_Registry(blocker=True), SimpleNamespace())

    result = CliRunner().invoke(app, ["status", "--plain"])

    assert result.exit_code == 4
    assert "failed: database — Database" in result.output
    assert "Next: Start PostgreSQL." in result.output


def test_doctor_plain_groups_findings_by_contract_area() -> None:
    app = build_click_app(_Registry(), SimpleNamespace())

    result = CliRunner().invoke(app, ["doctor", "--plain"])

    assert result.exit_code == 0, result.output
    assert "Core readiness" in result.output
    assert "Optional features" in result.output
    assert "Recommendations" in result.output
    assert "Extension" not in result.output


class _PhaseAwareRegistry:
    async def invoke(self, key, _value, **_kwargs):
        assert key == "readiness.doctor"
        return (
            CoreReadinessService(
                probe=_probe(
                    [
                        {
                            "id": "docker_daemon",
                            "category": "core",
                            "title": "Docker daemon",
                            "status": "fail",
                            "detail": "Docker daemon is not reachable.",
                            "remediation": "Start Docker Engine.",
                        },
                        {
                            "id": "installation_resume",
                            "category": "core",
                            "title": "Installation resume",
                            "status": "fail",
                            "detail": (
                                "Failed step: start_runtime. Log: .flow-steward/logs/install-1.log."
                            ),
                            "remediation": "Resolve the failure and rerun ./install.sh.",
                        },
                    ]
                )
            )
            .check()
            .model_dump(mode="json")
        )

    async def choices(self, _key):
        return ()


def test_doctor_json_preserves_phase_findings_in_diagnostic_order() -> None:
    app = build_click_app(_PhaseAwareRegistry(), SimpleNamespace())

    result = CliRunner().invoke(app, ["doctor", "--json"])

    payload = json.loads(result.output)
    findings = payload["error"]["details"]["findings"]
    assert result.exit_code == 4
    assert [finding["check_id"] for finding in findings] == [
        "docker_daemon",
        "installation_resume",
    ]
    assert ".flow-steward/logs/install-1.log" in findings[1]["detail"]


def test_doctor_plain_preserves_resume_command_and_exact_log() -> None:
    app = build_click_app(_PhaseAwareRegistry(), SimpleNamespace())

    result = CliRunner().invoke(app, ["doctor", "--plain"])

    assert result.exit_code == 4
    assert "failed: installation_resume" in result.output
    assert ".flow-steward/logs/install-1.log" in result.output
    assert "Next: Resolve the failure and rerun ./install.sh." in result.output
