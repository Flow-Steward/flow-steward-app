from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner

from core.entrypoints.cli.platform.click_adapter import build_click_app


class _Registry:
    async def invoke(self, key, _value, **_kwargs):
        assert key == "users.issue-bootstrap-token"
        return {
            "status": "issued",
            "output_path": ".flow-steward/secret-output/bootstrap-token",
            "expires_at": "2026-07-23T11:00:00Z",
            "header_name": "x-platform-setup-token",
        }

    async def choices(self, _key):
        return ()


def test_users_help_exposes_issue_bootstrap_token() -> None:
    app = build_click_app(_Registry(), SimpleNamespace())

    result = CliRunner().invoke(app, ["users", "--help"])

    assert result.exit_code == 0, result.output
    assert "issue-bootstrap-token" in result.output


def test_issue_bootstrap_token_json_never_contains_token_value() -> None:
    app = build_click_app(_Registry(), SimpleNamespace())

    result = CliRunner().invoke(
        app,
        ["users", "issue-bootstrap-token", "--json", "--confirm"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert payload["result"]["output_path"].endswith("bootstrap-token")
    assert "token_value" not in result.output
    assert "token_value" not in json.dumps(payload["result"]).lower()


def test_product_direct_invocation_refuses_before_composing_recovery():
    import pytest

    from core.application.management.errors import ServicePreconditionError
    from core.entrypoints.cli.runtime import CLIRuntime

    with pytest.raises(ServicePreconditionError, match="host command"):
        _ = CLIRuntime().bootstrap_token_service


def test_private_management_rejects_other_user_commands():
    import pytest

    from core.application.management.errors import ServicePreconditionError
    from core.entrypoints.cli.bootstrap_recovery import BootstrapRecoveryRegistry

    registry = BootstrapRecoveryRegistry(SimpleNamespace())
    with pytest.raises(ServicePreconditionError, match="only"):
        registry.service("users.list")


def test_private_management_help_and_confirmation_are_lazy():
    from core.entrypoints.cli.bootstrap_recovery import BootstrapRecoveryRegistry

    class Runtime:
        @property
        def bootstrap_token_service(self):
            raise AssertionError("composition must not happen")

    runtime = Runtime()
    app = build_click_app(BootstrapRecoveryRegistry(runtime), runtime)
    help_result = CliRunner().invoke(app, ["users", "issue-bootstrap-token", "--help"])
    assert help_result.exit_code == 0, help_result.output
    refused = CliRunner().invoke(
        app, ["users", "issue-bootstrap-token", "--json", "--non-interactive"]
    )
    assert refused.exit_code != 0
    assert "composition must not happen" not in refused.output


def test_private_management_preserves_typed_recovery_failure():
    from core.application.installer.errors import InstallerFailure
    from core.entrypoints.cli.bootstrap_recovery import BootstrapRecoveryRegistry

    class Service:
        def issue(self):
            raise InstallerFailure(
                code="bootstrap_recovery_needed",
                cause="Runtime compensation needs reconciliation.",
                remediation="Preserve the recovery snapshot.",
                exit_code=8,
                retryable=False,
            )

    runtime = SimpleNamespace(bootstrap_token_service=Service())
    app = build_click_app(BootstrapRecoveryRegistry(runtime), runtime)
    result = CliRunner().invoke(app, ["users", "issue-bootstrap-token", "--json", "--confirm"])
    payload = json.loads(result.output)
    assert result.exit_code == 9, result.output
    assert payload["error"]["code"] == "bootstrap_recovery_needed"
    assert payload["error"]["hint"] == "Preserve the recovery snapshot."
