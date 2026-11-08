"""An interrupted first install must retain the answers collected before preflight."""

from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from click.testing import CliRunner

from core.application.installer.contracts import (
    InstallationKind,
    InstallConfig,
    InstallJournal,
    InstallRunResult,
)
from core.entrypoints.cli import installer
from core.infrastructure.installer.state_store import InstallerStateStore


def _interrupted_configuration(root: Path, status: Literal["failed", "running"]) -> InstallConfig:
    config = InstallConfig(
        environment="production",
        deployment_profile="scalable",
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        file_storage="bundled",
        public_host="flow.example.test",
        public_port=443,
        reverse_proxy_backend_port=18199,
        reverse_proxy_mcp_port=18200,
        proxy_kind="apache",
        worker_replicas=3,
    )
    InstallerStateStore(root / ".flow-steward").save_journal(
        InstallJournal(
            operation_id="interrupted-preflight",
            project_identity=str(root),
            config=config,
            status=status,
            failed_step="preflight" if status == "failed" else None,
            last_error_code="port_conflict" if status == "failed" else None,
        )
    )
    return config


@pytest.mark.parametrize("status", ("failed", "running"))
def test_preflight_resume_recovers_answers_before_active_configuration_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: Literal["failed", "running"]
) -> None:
    monkeypatch.setattr(installer, "installer_state_root", lambda root: root / ".flow-steward")
    expected = _interrupted_configuration(tmp_path, status)
    assert not (tmp_path / ".flow-steward/stack.toml").exists()
    assert not (tmp_path / ".env.prod").exists()

    recovered = installer._recorded_installation_values(tmp_path)

    assert recovered["deployment_profile"] == "scalable"
    assert InstallConfig.model_validate(recovered) == expected


@pytest.mark.parametrize("override", ((), ("--public-host", "new.example.test")))
def test_standard_resume_keeps_profile_before_resolving_the_pending_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: tuple[str, ...]
) -> None:
    monkeypatch.setattr(installer, "installer_state_root", lambda root: root / ".flow-steward")
    expected = _interrupted_configuration(tmp_path, "failed")
    resolved: list[InstallConfig] = []
    installed: list[InstallConfig] = []

    class ResumableService:
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.RESUMABLE)

        def install(self, config, **_kwargs):
            installed.append(config)
            return InstallRunResult(
                classification=InstallationKind.RESUMABLE,
                status="installed",
            )

    def resolve(config, **_kwargs):
        resolved.append(config)
        return

    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: ResumableService())
    monkeypatch.setattr(installer, "_prepare_official_release", resolve)
    monkeypatch.setenv("FS_INSTALL_MODE", "")
    result = CliRunner().invoke(
        installer.installer_command, ["--plain", "--non-interactive", *override]
    )

    assert result.exit_code == 0, result.output
    expected = expected.model_copy(update={"public_host": "new.example.test"} if override else {})
    assert resolved == [expected]
    assert installed == [expected]
