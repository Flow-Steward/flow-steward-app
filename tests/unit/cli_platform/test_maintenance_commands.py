"""Closed public maintenance command contract tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

from click.testing import CliRunner

from core.application.maintenance.contracts import (
    MaintenanceChangeResult,
    MaintenanceStatusResult,
)
from core.application.management.command_specs.contracts import (
    InteractivePolicy,
    OperationType,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _MaintenanceService:
    calls: list[str] = field(default_factory=list)

    def status(self) -> MaintenanceStatusResult:
        self.calls.append("status")
        return MaintenanceStatusResult(status="disabled")

    def enable(self, **_kwargs: object) -> MaintenanceChangeResult:
        self.calls.append("enable")
        return MaintenanceChangeResult(status="enabled", changed=True)

    def disable(self) -> MaintenanceChangeResult:
        self.calls.append("disable")
        return MaintenanceChangeResult(status="disabled", changed=True)


@dataclass
class _Runtime:
    maintenance_service: _MaintenanceService = field(default_factory=_MaintenanceService)


def _app() -> tuple[Any, _Runtime]:
    runtime = _Runtime()
    return build_click_app(ServiceRegistry(cast(Any, runtime)), SimpleNamespace()), runtime


def test_maintenance_specs_are_exact_and_have_no_reason_input() -> None:
    specs = {
        spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS if spec.path[0] == "maintenance"
    }

    assert tuple(specs) == (
        "maintenance.status",
        "maintenance.enable",
        "maintenance.disable",
    )
    assert specs["maintenance.status"].operation_type is OperationType.READ
    assert specs["maintenance.status"].interactive_policy is InteractivePolicy.IMMEDIATE
    for command_id in ("maintenance.enable", "maintenance.disable"):
        assert specs[command_id].operation_type is OperationType.CHANGE
        assert specs[command_id].interactive_policy is InteractivePolicy.ALWAYS_REVIEW
        assert specs[command_id].fields == ()
        assert "reason" not in specs[command_id].input_model.model_fields


def test_maintenance_help_exposes_only_three_commands_and_no_reason() -> None:
    app, _runtime = _app()
    runner = CliRunner()

    group = runner.invoke(app, ["maintenance", "--help"])
    enable = runner.invoke(app, ["maintenance", "enable", "--help"])

    assert group.exit_code == 0, group.output
    assert all(command in group.output for command in ("status", "enable", "disable"))
    assert "--reason" not in enable.output


def test_status_is_immediate_and_enable_disable_require_confirm() -> None:
    app, runtime = _app()
    runner = CliRunner()

    status = runner.invoke(app, ["maintenance", "status", "--json"])
    refused_enable = runner.invoke(
        app,
        ["maintenance", "enable", "--non-interactive", "--plain"],
    )
    enabled = runner.invoke(
        app,
        ["maintenance", "enable", "--non-interactive", "--confirm", "--json"],
    )
    disabled = runner.invoke(
        app,
        ["maintenance", "disable", "--non-interactive", "--confirm", "--json"],
    )

    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["result"]["status"] == "disabled"
    assert refused_enable.exit_code == 10
    assert enabled.exit_code == 0, enabled.output
    assert disabled.exit_code == 0, disabled.output
    assert runtime.maintenance_service.calls == ["status", "enable", "disable"]
