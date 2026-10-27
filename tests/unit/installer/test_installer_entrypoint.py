from __future__ import annotations

import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from core.application.installer import (
    InstallationKind,
    InstallConfig,
    InstallRunResult,
    ReleaseIdentity,
    ReleaseImageIdentity,
)
from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.application.readiness import ReadinessArea, ReadinessStatus
from core.application.release.activation import ReleaseHandoffJournal
from core.application.release.catalog import ReleaseVersion
from core.application.release.images import required_product_roles
from core.application.release.installed_origin import SourceInstallationOrigin
from core.application.release.service import ReleaseTransitionBinding
from core.entrypoints.cli import installer
from core.infrastructure.installer.process import ProcessOutcome, SubprocessExecutor
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

_REAL_RECORDED_INSTALLATION_VALUES = installer._recorded_installation_values
_REAL_PREPARE_OFFICIAL_RELEASE = installer._prepare_official_release


def test_compact_maintenance_factory_retains_installed_target_and_read_only_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / ".flow-steward"
    store = LifecycleConfigurationStore(tmp_path, state_root=state_root)
    store.write_active(
        DeploymentConfig(
            environment="production",
            deployment_profile="compact",
            file_storage="local_fs",
            data_placement="compact_volume",
        )
    )
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=test\n")
    observed = {}

    class Gateway:
        def __init__(self, **kwargs):
            observed["constructor"] = kwargs

        def compact_maintenance_operation(self, target, operation):
            observed["target"] = target
            observed["operation"] = operation
            return {
                "status": "enabled",
                "activated_at": "2026-10-08T11:00:00Z",
                "owner_kind": "manual",
            }

    monkeypatch.setattr(installer, "DockerLifecycleGateway", Gateway)
    active = store.read_active()
    assert active is not None
    client = installer._compact_maintenance_client(
        project_root=tmp_path,
        state_root=state_root,
        config=active,
        docker_host_project_root=Path("/host/owned"),
    )
    assert client._mutation_state.installation_lock_path == state_root / "install.lock"
    assert client.status().status == "enabled"
    assert observed["constructor"] == {
        "project_root": tmp_path,
        "docker_host_project_root": Path("/host/owned"),
    }
    target = observed["target"]
    assert target.project_name == "test"
    assert target.deployment_profile == "compact"
    assert target.compose_files == (
        "infra/docker/docker-compose.compact.yaml",
        "infra/docker/compact/ingress-bundled_traefik.yaml",
        "infra/docker/compact/tls-bundled-files.yaml",
    )
    assert observed["operation"] == "maintenance-status"
    assert not (state_root / "runtime/maintenance-state.json").exists()
    assert not (state_root / "operations").exists()
    assert not (state_root / "install.lock").exists()


@pytest.fixture(autouse=True)
def _stub_official_release_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command-contract tests offline; resolver behavior has focused tests below."""
    monkeypatch.setenv("FS_INSTALL_MODE", "")
    monkeypatch.setattr(installer, "_prepare_official_release", lambda _config, **_kwargs: None)
    monkeypatch.setattr(installer, "_recorded_installation_values", lambda _root: {})


@pytest.mark.parametrize("process_signal", (signal.SIGINT, signal.SIGTERM))
def test_installer_signal_context_overrides_and_restores_ignored_signal(process_signal) -> None:
    previous = signal.getsignal(process_signal)
    signal.signal(process_signal, signal.SIG_IGN)
    try:
        with installer._installer_signal_handlers():
            assert signal.getsignal(process_signal) is not signal.SIG_IGN
        assert signal.getsignal(process_signal) is signal.SIG_IGN
    finally:
        signal.signal(process_signal, previous)


class FakeService:
    def __init__(self) -> None:
        self.configs = []

    def inspect(self):
        return SimpleNamespace(kind=InstallationKind.FRESH)

    def install(self, config, explicit_fields=frozenset(), target_release=None):
        self.configs.append(config)
        self.explicit_fields = explicit_fields
        self.target_release = target_release
        return InstallRunResult(
            classification=InstallationKind.FRESH,
            status="installed",
            product_url="https://localhost:8099",
            operation_id="op-1",
            log_path=".flow-steward/logs/install-op-1.log",
            completed_steps=("preflight", "manifest"),
        )


class FailingService:
    def inspect(self):
        return SimpleNamespace(kind=InstallationKind.FRESH)

    def install(self, _config, explicit_fields=frozenset(), target_release=None):
        raise InstallerFailure(
            code="core_start_failed",
            cause="Authorization: Bearer top-secret-token",
            remediation="password=hunter2; retry",
            exit_code=8,
            log_path=".flow-steward/logs/install.log",
            retryable=True,
        )


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_default_installer_uses_plain_questions_and_cancels_without_install(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    from core.entrypoints.cli.platform.modes import TerminalFacts

    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(
        installer, "_terminal_facts", lambda: TerminalFacts(True, True, 120, 40, "xterm", False)
    )
    result = CliRunner().invoke(
        installer.installer_command, ["--deployment-profile", profile], input="\n" * 50
    )
    assert result.exit_code == 10, result.output
    assert "Environment" in result.output
    assert f"Deployment profile: {profile}" in result.output
    assert "Apply this change?" in result.output
    assert "\x1b[" not in result.output
    assert service.configs == []


def test_installer_full_screen_form_requires_explicit_tui_and_cancels_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform.modes import TerminalFacts
    from core.entrypoints.cli.tui import form_app

    service = FakeService()
    forms: list[dict[str, object]] = []

    class CancelledForm:
        def __init__(self, **kwargs: object) -> None:
            forms.append(kwargs)

        def run(self) -> form_app.FormOutcome:
            return form_app.FormOutcome(status="cancelled")

    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(
        installer, "_terminal_facts", lambda: TerminalFacts(True, True, 120, 40, "xterm", False)
    )
    monkeypatch.setattr(form_app, "CommandFormApp", CancelledForm)
    result = CliRunner().invoke(installer.installer_command, ["--tui"])
    assert result.exit_code == 10, result.output
    assert len(forms) == 1
    assert forms[0]["color_enabled"] is True
    assert service.configs == []


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_plain_installer_collects_external_traefik_inputs_before_review(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    from core.entrypoints.cli.platform.modes import TerminalFacts

    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(
        installer, "_terminal_facts", lambda: TerminalFacts(True, True, 120, 40, "xterm", False)
    )
    result = CliRunner().invoke(
        installer.installer_command,
        [
            "--environment",
            "production",
            "--deployment-profile",
            profile,
            "--ingress-mode",
            "external_traefik",
            "--tls-mode",
            "external_resolver",
            "--file-storage",
            "local_fs",
            "--worker-processes",
            "2",
            "--runtime-http-port",
            "8080",
            "--public-host",
            "cli.example.test",
            "--public-port",
            "443",
            "--proxy-trusted-ips",
            "127.0.0.1/32,::1/128",
            "--traefik-http-entrypoint",
            "web",
            "--traefik-https-entrypoint",
            "websecure",
            "--extension-installer-origin",
            "http://fs-extension-installer:8099",
            "--certificate-path",
            ".ssl/cert.pem",
            "--private-key-path",
            ".ssl/key.pem",
        ],
        input="traefik\nmyresolver\nn\n",
    )
    assert result.exit_code == 10, result.output
    assert "Existing Traefik network" in result.output
    assert "Traefik certificate resolver" in result.output
    assert "Apply this change?" in result.output
    assert service.configs == []


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_plain_installer_asks_for_public_domain_and_reviews_it_before_install(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    from core.entrypoints.cli.platform.modes import TerminalFacts

    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(
        installer, "_terminal_facts", lambda: TerminalFacts(True, True, 120, 40, "xterm", False)
    )
    result = CliRunner().invoke(
        installer.installer_command,
        [
            "--environment",
            "production",
            "--deployment-profile",
            profile,
            "--ingress-mode",
            "bundled_traefik",
            "--tls-mode",
            "self_signed",
            "--file-storage",
            "local_fs",
            "--worker-processes",
            "2",
            "--runtime-http-port",
            "8080",
            "--public-port",
            "443",
            "--redirect-http-port",
            "8080",
            "--proxy-trusted-ips",
            "127.0.0.1/32,::1/128",
            "--extension-installer-origin",
            "http://fs-extension-installer:8099",
            "--certificate-path",
            ".ssl/cert.pem",
            "--private-key-path",
            ".ssl/key.pem",
        ],
        input="cli.example.test\nn\n",
    )
    assert result.exit_code == 10, result.output
    assert "Public host (domain or IP) [localhost]:" in result.output
    assert "Public host (domain or IP): cli.example.test" in result.output
    assert service.configs == []


def _release_identity(version: str, marker: str, *, profile: str) -> ReleaseIdentity:
    digest = "sha256:" + marker * 64
    return ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha=marker * 40,
        registry="ghcr.io/flow-steward",
        profile=profile,
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision=marker * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles(profile)
        ),
    )


@pytest.mark.parametrize("discover", [True, False])
@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_fresh_production_install_builds_checkout_without_tag_or_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: str, discover: bool
) -> None:
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: tmp_path / "state")

    def forbidden(**_kwargs):
        pytest.fail("Local install must not query GitHub or a registry")

    # The tree has no VERSION and no tag: the real non-raising probe says "source".
    monkeypatch.setattr(installer, "GitHubReleaseDiscovery", forbidden)
    monkeypatch.setattr(installer, "OfficialReleaseResolver", forbidden)
    assert (
        _REAL_PREPARE_OFFICIAL_RELEASE(
            InstallConfig(environment="production", deployment_profile=profile), discover=discover
        )
        is None
    )
    assert installer._runtime_env.get("FS_INSTALL_MODE") == "source"


def test_private_installer_has_no_management_subcommands() -> None:
    result = CliRunner().invoke(installer.installer_command, ["configure"])

    assert result.exit_code == 2
    assert "unexpected extra argument" in result.output.lower()


def test_confirmed_production_install_activates_local_release_without_self_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "sha256:" + "b" * 64
    release = ReleaseIdentity(
        version="1.0.10",
        tag="v1.0.10",
        commit_sha="b" * 40,
        registry="ghcr.io/flow-steward",
        profile="compact",
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision="b" * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles("compact")
        ),
    )
    events: list[str] = []

    class RecordingService(FakeService):
        def install(self, config, explicit_fields=frozenset(), target_release=None):
            events.append("install")
            assert target_release == release
            return super().install(config, explicit_fields, target_release)

    class RecordingHandoff:
        def __init__(self, **_kwargs):
            pytest.fail("fresh local install constructed the release handoff")

        def install_exact(self, target, *, config, explicit_fields, timeout_seconds):
            raise AssertionError("unreachable")

    service = RecordingService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(
        installer,
        "_prepare_official_release",
        lambda _config, **_kwargs: events.append("resolve") or release,
    )
    monkeypatch.setattr(installer, "InstallerReleaseHandoff", RecordingHandoff)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--confirm", "--plain"],
    )

    assert result.exit_code == 0, result.output
    assert events == ["resolve", "install"]
    assert service.configs[0].environment == "production"


def test_resumable_fresh_release_reenters_exact_handoff_without_second_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = _release_identity("1.0.10", "b", profile="compact")
    state_root = tmp_path / "state"
    state = InstallerStateStore(state_root)
    config = InstallConfig(environment="production", deployment_profile="compact")
    state.stage_pending_release(release)
    state.save_release_handoff(
        ReleaseHandoffJournal(
            target=release,
            phase="checkout_applied",
            install_config=config,
        )
    )
    events: list[str] = []

    class ResumableService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.RESUMABLE)

    class RecordingHandoff:
        def __init__(self, **_kwargs):
            pass

        def install_exact(self, target, *, config, explicit_fields, timeout_seconds):
            assert target == release
            assert config.deployment_profile == "compact"
            assert explicit_fields == frozenset()
            assert timeout_seconds == 3600
            events.append("exact_handoff")
            return InstallRunResult(
                classification=InstallationKind.RESUMABLE,
                status="installed",
                product_url="https://localhost:8099",
            )

    service = ResumableService()
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state_root)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "_prepare_official_release", lambda *_args, **_kwargs: release)
    monkeypatch.setattr(installer, "InstallerReleaseHandoff", RecordingHandoff)
    monkeypatch.setattr(
        installer,
        "_recorded_installation_values",
        lambda _root: config.model_dump(mode="json"),
    )

    result = CliRunner().invoke(installer.installer_command, ["--non-interactive", "--plain"])

    assert result.exit_code == 0, result.output
    assert events == ["exact_handoff"]
    assert service.configs == []


def test_resumable_fresh_release_without_handoff_resumes_in_matching_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = _release_identity("1.0.10", "b", profile="compact")
    state_root = tmp_path / "state"
    state = InstallerStateStore(state_root)
    state.stage_pending_release(release)
    config = InstallConfig(environment="production", deployment_profile="compact")

    class ResumableService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.RESUMABLE)

    class ForbiddenHandoff:
        def __init__(self, **_kwargs):
            pytest.fail("matching fresh resume must not create a checkout handoff")

    service = ResumableService()
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state_root)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "_prepare_official_release", lambda *_args, **_kwargs: release)
    monkeypatch.setattr(installer, "InstallerReleaseHandoff", ForbiddenHandoff)
    monkeypatch.setattr(
        installer,
        "_recorded_installation_values",
        lambda _root: config.model_dump(mode="json"),
    )

    result = CliRunner().invoke(installer.installer_command, ["--non-interactive", "--plain"])

    assert result.exit_code == 0, result.output
    assert service.target_release == release


def test_resumable_fresh_release_rejects_checkout_drift_before_runtime_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = _release_identity("1.0.10", "b", profile="compact")
    state_root = tmp_path / "state"
    state = InstallerStateStore(state_root)
    state.stage_pending_release(release)
    observed: list[str] = []

    class LocalSource:
        def __init__(self, **_kwargs):
            pass

        def exact(self):
            return SimpleNamespace(
                version=ReleaseVersion.parse("1.0.9"),
                tag="v1.0.9",
                commit_sha="a" * 40,
            )

    class Resolver:
        def verify(self, target):
            observed.append("resolver")
            return target

    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state_root)
    monkeypatch.setattr(installer, "LocalCheckoutReleaseSource", LocalSource)
    monkeypatch.setattr(installer, "OfficialReleaseResolver", Resolver)
    monkeypatch.setattr(installer, "release_platform_for_machine", lambda _machine: "linux/amd64")
    monkeypatch.setattr(
        installer,
        "_create_service",
        lambda **_kwargs: observed.append("service") or FakeService(),
    )

    config = InstallConfig(environment="production", deployment_profile="compact")
    with pytest.raises(LifecycleError) as raised:
        _REAL_PREPARE_OFFICIAL_RELEASE(
            config,
            discover=False,
        )

    assert getattr(raised.value, "code", "") == "production_checkout_pending_mismatch"
    assert observed == []


def test_private_release_bootstrap_reuses_recorded_scalable_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    digest = "sha256:" + "c" * 64
    release = ReleaseIdentity(
        version="1.0.10",
        tag="v1.0.10",
        commit_sha="c" * 40,
        registry="ghcr.io/flow-steward",
        profile="scalable",
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision="c" * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles("scalable")
        ),
    )
    state_root = tmp_path / "state"
    state = InstallerStateStore(state_root)
    state.stage_pending_release(release)
    state.save_release_handoff(ReleaseHandoffJournal(target=release, phase="checkout_applied"))
    service = FakeService()
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state_root)
    monkeypatch.setattr(
        installer,
        "_recorded_installation_values",
        lambda _root: {
            "environment": "production",
            "deployment_profile": "scalable",
            "public_port": 9443,
        },
    )
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    exit_code = installer.run_pending_release_handoff()

    assert exit_code == 0
    assert service.configs[0].deployment_profile == "scalable"
    assert service.configs[0].public_port == 9443
    assert service.target_release == release


@pytest.mark.parametrize("origin_kind", ("official", "source"))
@pytest.mark.parametrize(
    "maintenance_phase", ("missing", "draining", "enabled", "unavailable", "invalid")
)
@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_private_upgrade_bootstrap_rechecks_maintenance_before_installer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    origin_kind: str,
    maintenance_phase: str,
    profile: str,
) -> None:
    target = _release_identity("1.0.10", "c", profile=profile)
    installed = _release_identity("1.0.9", "b", profile=profile)
    state_root = tmp_path / "state"
    state = InstallerStateStore(state_root)
    if origin_kind == "official":
        state.publish_installed_release(installed)
    else:
        origin = SourceInstallationOrigin(
            commit_sha="b" * 40,
            deployment_profile=profile,
            platform="linux/amd64",
            installation_id="source-stand",
            configuration_sha256="d" * 64,
            release_repository="Flow-Steward/flow-steward-app",
        )
        state.save_release_transition_binding(
            ReleaseTransitionBinding(
                installed_origin=origin,
                configuration_sha256=origin.configuration_sha256,
                release_repository=origin.release_repository,
                deployment_profile=origin.deployment_profile,
                platform=origin.platform,
                target_release=target,
            )
        )
    state.stage_pending_release(target)
    state.save_release_handoff(ReleaseHandoffJournal(target=target, phase="checkout_applied"))
    configuration = LifecycleConfigurationStore(tmp_path, state_root=state_root)
    configuration.write_active(
        DeploymentConfig(environment="production", deployment_profile=profile)
    )
    maintenance_path = state_root / "runtime/maintenance-state.json"
    if profile == "compact":
        compose_project = "upgrade-compact"
        configuration.compose_env_path.write_text(
            f"FS_COMPOSE_PROJECT_NAME={compose_project}\n", encoding="utf-8"
        )

        # The child runs on the host-side management surface, not in /data.
        # Only the existing runtime operator can observe its private marker.
        def run_operator(self, command, **_kwargs):
            assert command[command.index("-p") + 1] == compose_project
            assert command[-10:] == [
                "exec",
                "-T",
                "--user",
                "10007:10007",
                "compact-runtime",
                "python",
                "-m",
                "core.entrypoints.cli.platform_runtime",
                "maintenance-status",
                "--json",
            ]
            if maintenance_phase == "unavailable":
                return ProcessOutcome(tuple(command), 1, stderr="Runtime operator unavailable")
            if maintenance_phase == "invalid":
                return ProcessOutcome(tuple(command), 0, "not-json")
            result = {"status": "disabled" if maintenance_phase == "missing" else maintenance_phase}
            if maintenance_phase != "missing":
                result.update(activated_at="2026-10-07T14:00:00Z", owner_kind="manual")
            return ProcessOutcome(
                tuple(command),
                0,
                json.dumps(
                    {
                        "schema": "flow-steward.cli.v1",
                        "ok": True,
                        "command": "maintenance.status",
                        "result": result,
                    }
                ),
            )

        monkeypatch.setattr(SubprocessExecutor, "run", run_operator)
    if maintenance_phase != "missing" and profile != "compact":
        maintenance_path.parent.mkdir(parents=True, exist_ok=True)
        maintenance_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": maintenance_phase,
                    "owner_kind": "manual",
                    "owner_id": "operator-maintenance",
                    "activated_at": "2026-10-07T14:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    service = FakeService()
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state_root)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    exit_code = installer.run_pending_release_handoff()

    if maintenance_phase == "enabled":
        assert exit_code == 0
        assert len(service.configs) == 1
        assert service.target_release == target
    else:
        assert exit_code == 8
        assert service.configs == []


def test_noninteractive_installer_uses_validated_defaults_without_prompt(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--confirm", "--plain"],
    )

    assert result.exit_code == 0, result.output
    assert "Environment" not in result.output
    # AC17-R92: Compact is the default profile, which implies a production install and
    # local file storage. Most operators clone the repository and install with defaults.
    assert service.configs[0].deployment_profile == "compact"
    assert service.configs[0].environment == "production"
    assert service.configs[0].file_storage == "local_fs"
    assert "https://localhost:8099" in result.output


def test_noninteractive_compact_install_defaults_to_local_storage(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        [
            "--non-interactive",
            "--confirm",
            "--plain",
            "--environment",
            "production",
            "--deployment-profile",
            "compact",
            "--worker-processes",
            "4",
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.configs[0].file_storage == "local_fs"
    assert service.configs[0].worker_processes == 4
    # AC17-R157: what the operator typed reaches the service, not only the merged config
    assert service.explicit_fields == {"environment", "deployment_profile", "worker_processes"}


def test_installer_accepts_explicit_local_storage_for_scalable(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        [
            "--non-interactive",
            "--confirm",
            "--plain",
            "--environment",
            "production",
            "--deployment-profile",
            "scalable",
            "--file-storage",
            "local_fs",
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.configs[0].deployment_profile == "scalable"
    assert service.configs[0].file_storage == "local_fs"


def test_non_tty_never_prompts_even_without_non_interactive_flag(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(
        installer,
        "_terminal_facts",
        lambda: installer.TerminalFacts(
            stdin_isatty=False,
            stdout_isatty=False,
            columns=80,
            rows=24,
            term="dumb",
            no_color_env=False,
        ),
    )

    result = CliRunner().invoke(installer.installer_command, ["--confirm", "--plain"])

    assert result.exit_code == 0, result.output
    # The point of this test is that a non-TTY never prompts; the value it lands on is
    # the AC17-R92 default pair (compact implies production), not a development install.
    assert service.configs[0].environment == "production"


def test_json_installer_emits_one_document(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        [
            "--json",
            "--confirm",
            "--environment",
            "production",
            "--file-storage",
            "external_s3",
            "--s3-endpoint",
            "https://objects.example.test",
            "--s3-credentials-path",
            "/run/secrets/s3.json",
            "--tls-mode",
            "reverse_proxy",
            "--public-host",
            "flow.example.com",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    assert payload["command"] == "installer.run"
    assert payload["result"]["status"] == "installed"
    assert len(result.output.splitlines()) == 1


def test_installer_failure_output_is_redacted(monkeypatch) -> None:
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: FailingService())

    result = CliRunner().invoke(
        installer.installer_command,
        ["--json", "--confirm"],
    )

    assert result.exit_code == 8
    assert "top-secret-token" not in result.output
    assert "hunter2" not in result.output
    assert "[REDACTED]" in result.output


def test_json_failure_preserves_retryable_and_log_path(monkeypatch) -> None:
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: FailingService())

    result = CliRunner().invoke(installer.installer_command, ["--json", "--confirm"])

    payload = json.loads(result.output)
    assert payload["error"]["retryable"] is True
    assert payload["log_path"] == ".flow-steward/logs/install.log"


def test_initialization_failure_has_stable_json_contract_and_emergency_log(monkeypatch) -> None:
    def fail(**_kwargs):
        raise PermissionError("state directory denied")

    monkeypatch.setattr(installer, "_create_service", fail)

    result = CliRunner().invoke(installer.installer_command, ["--json", "--confirm"])

    payload = json.loads(result.output)
    assert result.exit_code == 70
    assert payload["error"]["code"] == "installer_initialization_failed"
    assert payload["log_path"]
    assert Path(payload["log_path"]).stat().st_mode & 0o777 == 0o600
    Path(payload["log_path"]).unlink()


def test_external_s3_requires_endpoint_and_credentials_before_service(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--confirm", "--plain", "--file-storage", "external_s3"],
    )

    assert result.exit_code == 3
    assert "s3_endpoint" in result.output
    assert service.configs == []


def test_noninteractive_installer_requires_explicit_confirmation(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(installer.installer_command, ["--non-interactive"])

    assert result.exit_code == 10
    assert service.configs == []


def test_repeat_noninteractive_install_classifies_before_confirmation(monkeypatch) -> None:
    class InstalledService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="already_installed",
                product_url="https://localhost:8099",
            )

    service = InstalledService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--plain"],
    )

    assert result.exit_code == 0, result.output
    assert "Flow Steward is already installed" in result.output
    assert "./flow-steward status" in result.output
    assert "./flow-steward doctor" in result.output
    assert '"status"' not in result.output
    assert len(service.configs) == 1


def test_rerun_on_an_existing_compact_install_resumes_its_recorded_profile(
    monkeypatch, tmp_path: Path
) -> None:
    """AC17-R93: a bare rerun used to fall through to the spec defaults.

    The R88 remediation tells an operator to run ./install.sh to get a replacement
    bootstrap link. On a Compact installation that rerun proposed the defaults
    (scalable/development) instead of the recorded configuration and then refused
    its own combination with "Compact deployment profile requires production
    environment", so the instruction could not be followed literally.
    """
    (tmp_path / ".env.prod").write_text(
        "FS_DEPLOYMENT_PROFILE=compact\nOPERATOR_VALUE=keep\n", encoding="utf-8"
    )

    class InstalledService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="already_installed",
                product_url="https://localhost:8099",
            )

    service = InstalledService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "_ROOT", tmp_path)

    result = CliRunner().invoke(installer.installer_command, ["--non-interactive", "--plain"])

    assert result.exit_code == 0, result.output
    assert len(service.configs) == 1
    assert service.configs[0].deployment_profile == "compact"
    assert service.configs[0].environment == "production"


def test_rerun_still_lets_an_explicit_flag_override_the_recorded_profile(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.prod").write_text("FS_DEPLOYMENT_PROFILE=compact\n", encoding="utf-8")

    class InstalledService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="already_installed",
                product_url="https://localhost:8099",
            )

    service = InstalledService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "_ROOT", tmp_path)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--plain", "--deployment-profile", "scalable"],
    )

    assert result.exit_code == 0, result.output
    assert service.configs[0].deployment_profile == "scalable"


def test_existing_compact_release_resolves_same_version_scalable_set_for_profile_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installed = _release_identity("1.0.10", "b", profile="compact")
    state_root = tmp_path / "state"
    InstallerStateStore(state_root).publish_installed_release(installed)
    target = _release_identity("1.0.10", "b", profile="scalable")
    observed: dict[str, object] = {}

    class Resolver:
        def probe(self, release, *, profile, platform):
            observed.update(release=release, profile=profile, platform=platform)
            return target

    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state_root)
    monkeypatch.setattr(installer.host_platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(installer, "OfficialReleaseResolver", Resolver)

    target = _REAL_PREPARE_OFFICIAL_RELEASE(
        InstallConfig(environment="production", deployment_profile="scalable"),
        discover=False,
    )

    assert target.version == installed.version
    assert target.commit_sha == installed.commit_sha
    assert target.profile == "scalable"
    assert observed["profile"] == "scalable"
    assert observed["platform"] == "linux/amd64"
    assert observed["release"].version == ReleaseVersion.parse(installed.version)


def test_rerun_on_a_fresh_tree_keeps_the_spec_defaults(monkeypatch, tmp_path: Path) -> None:
    service = FakeService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "_ROOT", tmp_path)

    assert installer._recorded_installation_values(tmp_path) == {}


def test_recorded_values_preserve_complete_active_deployment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = tmp_path / "state"
    active = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        public_host="installed.example.com",
        public_port=9443,
        worker_replicas=4,
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        file_storage="bundled",
    )
    LifecycleConfigurationStore(tmp_path, state_root=state_root).write_active(active)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state_root)

    recorded = _REAL_RECORDED_INSTALLATION_VALUES(tmp_path)

    assert InstallConfig.model_validate(recorded) == InstallConfig.model_validate(
        active.model_dump(mode="python")
    )


def test_guided_external_input_must_be_inside_repo_or_predeclared(monkeypatch) -> None:
    monkeypatch.setattr(installer, "_ROOT", Path("/repo"))
    values = {
        "file_storage": "external_s3",
        "s3_credentials_path": "/outside/s3.json",
    }

    with pytest.raises(ValueError, match="Guided S3 credentials"):
        installer._validate_container_paths(values, supplied_keys=set())

    installer._validate_container_paths(values, supplied_keys={"s3_credentials_path"})

    with pytest.raises(ValueError, match="Relative installer file paths"):
        installer._validate_container_paths(
            {"file_storage": "external_s3", "s3_credentials_path": "../s3.json"},
            supplied_keys={"s3_credentials_path"},
        )


@pytest.mark.parametrize("predeclared", (False, True))
def test_plain_tls_paths_enforce_launcher_mount_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, predeclared: bool
) -> None:
    from core.entrypoints.cli.platform.modes import TerminalFacts

    service = FakeService()
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(
        installer, "_terminal_facts", lambda: TerminalFacts(True, True, 120, 40, "xterm", False)
    )
    result = CliRunner().invoke(
        installer.installer_command,
        ["--certificate-path", "/outside/cert.pem", "--private-key-path", "/outside/key.pem"]
        if predeclared
        else [],
        input="\n" * 6
        + "3\n"
        + "\n" * 5
        + ("" if predeclared else "/outside/cert.pem\n/outside/key.pem\n")
        + "y\n",
    )
    if predeclared:
        assert result.exit_code == 0, result.output
        assert service.configs[0].certificate_path == "/outside/cert.pem"
    else:
        assert result.exit_code == 3, result.output
        assert "pass external absolute paths as ./install.sh options" in result.output
        assert "Apply this change?" not in result.output
        assert service.configs == []


def test_create_service_uses_configured_installer_state_directory(
    tmp_path: Path, monkeypatch
) -> None:
    configured_root = tmp_path / "custom-state"
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(configured_root))

    service = installer._create_service()

    assert service.state.root == configured_root
    assert service.gateway.state_store is service.state
    assert service.state.journal_path == configured_root / "install-state.json"


def test_installer_readiness_uses_active_config_from_configured_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "custom-state"
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(configured_root))
    store = LifecycleConfigurationStore(
        installer._ROOT,
        state_root=configured_root,
    )
    store.write_active(installer.InstallConfig(public_port=18443))
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=installer-entrypoint-test\n",
        encoding="utf-8",
    )
    readiness_configs: list[DeploymentConfig] = []

    def cli_readiness(
        _gateway: object, config: DeploymentConfig, *, timeout_seconds: float
    ) -> dict[str, object]:
        assert timeout_seconds == 60.0
        readiness_configs.append(config)
        return {
            "checks": [
                {"id": "database", "category": "core", "status": "pass"},
                {"id": "embedding_provider", "category": "ai", "status": "warn"},
            ]
        }

    monkeypatch.setattr(
        installer,
        "run_doctor",
        lambda _options: pytest.fail("installer must not run Doctor in the bootstrap process"),
        raising=False,
    )
    monkeypatch.setattr(
        installer.DockerInstallerGateway,
        "probe_dispatch_queue_depth",
        lambda _gateway, _config: pytest.fail("installer must not use the fs-app-only seam"),
        raising=False,
    )
    monkeypatch.setattr(
        installer.DockerInstallerGateway,
        "probe_cli_readiness",
        cli_readiness,
        raising=False,
    )
    report = installer._create_service().readiness_service.check()

    assert len(readiness_configs) == 1
    assert readiness_configs[0].public_port == 18443
    assert report.core_failed == 0
    assert report.optional_warnings == 1
    assert any(
        finding.area is ReadinessArea.OPTIONAL and finding.status is ReadinessStatus.WARNING
        for finding in report.findings
    )


def test_installer_readiness_keeps_real_core_doctor_failure_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "custom-state"
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(configured_root))
    store = LifecycleConfigurationStore(
        installer._ROOT,
        state_root=configured_root,
    )
    store.write_active(installer.InstallConfig())
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=installer-entrypoint-test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        installer.DockerInstallerGateway,
        "probe_cli_readiness",
        lambda _gateway, _config, *, timeout_seconds: {
            "checks": [{"id": "database", "category": "core", "status": "fail"}]
        },
        raising=False,
    )
    report = installer._create_service().readiness_service.check()

    assert report.status == "blocked"
    assert report.core_failed == 1
    assert [finding.check_id for finding in report.findings] == ["database"]


def test_installer_cli_readiness_probe_failure_is_a_core_readiness_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "custom-state"
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(configured_root))
    store = LifecycleConfigurationStore(
        installer._ROOT,
        state_root=configured_root,
    )
    store.write_active(installer.InstallConfig())
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=installer-entrypoint-test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        installer.DockerInstallerGateway,
        "probe_cli_readiness",
        lambda _gateway, _config, *, timeout_seconds: {
            "checks": [
                {
                    "id": "cli_runtime_readiness",
                    "category": "core",
                    "title": "CLI runtime readiness",
                    "status": "fail",
                    "detail": "The CLI runtime readiness probe failed.",
                    "remediation": "Verify the retained CLI runtime and rerun ./install.sh.",
                }
            ]
        },
        raising=False,
    )

    report = installer._create_service().readiness_service.check()

    assert report.status == "blocked"
    assert report.core_failed == 1
    assert [finding.check_id for finding in report.findings] == ["cli_runtime_readiness"]


def test_installer_readiness_without_active_config_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "custom-state"
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(configured_root))
    monkeypatch.setattr(
        installer.DockerInstallerGateway,
        "probe_cli_readiness",
        lambda _gateway, _config: pytest.fail("missing active config must not invoke readiness"),
        raising=False,
    )

    report = installer._create_service().readiness_service.check()

    assert report.status == "unknown"
    assert report.core_failed == 1
    assert [finding.check_id for finding in report.findings] == ["readiness_contract"]


@pytest.mark.parametrize("presentation", ("--plain", "--json"))
@pytest.mark.parametrize("already_installed", (False, True))
def test_setup_link_is_a_distinct_next_step_block_outside_serialized_output(
    monkeypatch: pytest.MonkeyPatch, presentation: str, already_installed: bool
) -> None:
    setup_url = "https://fs.example.test/bootstrap#setup-token=one-time-test-token"

    class BootstrapService(FakeService):
        def inspect(self):
            return SimpleNamespace(
                kind=InstallationKind.INSTALLED if already_installed else InstallationKind.FRESH
            )

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            return InstallRunResult(
                classification=InstallationKind.INSTALLED
                if already_installed
                else InstallationKind.FRESH,
                status="already_installed" if already_installed else "installed",
                product_url="https://fs.example.test",
                bootstrap_url="https://fs.example.test/bootstrap",
                bootstrap_setup_url=setup_url,
            )

    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: BootstrapService())
    result = CliRunner().invoke(
        installer.installer_command, [presentation, "--non-interactive", "--confirm"]
    )

    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    border_positions = [index for index, line in enumerate(lines) if line == "=" * 60]
    assert len(border_positions) == 2, result.stderr
    heading_position = lines.index("NEXT STEP: CREATE THE FIRST ADMINISTRATOR")
    link_position = lines.index(setup_url)
    assert border_positions[0] < heading_position < link_position < border_positions[1]
    assert lines[link_position - 1] == lines[link_position + 1] == ""
    assert result.stderr.count(setup_url) == 1
    assert "Do not share it" in result.stderr
    assert "\x1b[" not in result.stderr
    assert "one-time-test-token" not in result.stdout
    if presentation == "--json":
        payload = json.loads(result.stdout)
        assert payload["result"]["status"] == (
            "already_installed" if already_installed else "installed"
        )
        assert "bootstrap_setup_url" not in payload["result"]


def test_setup_link_goes_to_stderr_so_a_redirected_json_result_has_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC17-R46 follow-up: `install.sh --json > result.json` must not capture a token.

    The operator still needs a link that works from installer output alone, so the
    complete link is written to stderr while the serialized document stays clean.
    """
    from core.entrypoints.cli import installer as installer_module

    result = InstallRunResult(
        classification=InstallationKind.FRESH,
        status="installed",
        product_url="https://fs.example.eu:443",
        bootstrap_url="https://fs.example.eu:443/bootstrap",
        bootstrap_setup_url="https://fs.example.eu:443/bootstrap#setup-token=one-time-secret",
        bootstrap_instruction="Open the one-time setup link printed above.",
        log_path="/tmp/install.log",
    )

    assert "one-time-secret" not in json.dumps(result.model_dump(mode="json"))

    printed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        installer_module.click,
        "echo",
        lambda message="", err=False, **_kwargs: printed.append((str(message), bool(err))),
    )

    installer_module._announce_setup_link(result)

    assert any("one-time-secret" in message and err for message, err in printed), (
        "the working link must reach the operator on stderr"
    )
    assert not any("one-time-secret" in message and not err for message, err in printed), (
        "the token must never reach stdout"
    )


def test_reissued_setup_link_reaches_the_operator_on_an_already_installed_run(
    monkeypatch,
) -> None:
    """AC17-R50: re-issuing a token without printing it replaced a dead end with silence.

    The installed path re-issues an expired non-loopback setup token, and the mechanism was
    verified working end to end -- new token in the env file, app container recreated, the
    link creating an administrator. The operator never saw it: this branch returned right
    after "Flow Steward is already installed", so the announcement that the fresh-install
    path uses was never reached, while the link they were holding had just been invalidated.
    """

    class ReissuingService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="already_installed",
                product_url="https://host.docker.internal:8399",
                bootstrap_url="https://host.docker.internal:8399/bootstrap",
                bootstrap_setup_url=(
                    "https://host.docker.internal:8399/bootstrap#setup-token=replacement"
                ),
                bootstrap_instruction="Open the one-time setup link printed above.",
            )

    service = ReissuingService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--plain"],
    )

    assert result.exit_code == 0, result.output
    assert "Flow Steward is already installed" in result.output
    # the instruction reaches stdout, the live token only stderr -- `install.sh --json >
    # result.json` must never capture a live token
    assert "Open the one-time setup link printed above." in result.stdout
    assert "setup-token=replacement" not in result.stdout
    assert "setup-token=replacement" in result.stderr


def test_already_installed_run_stays_quiet_when_no_setup_link_exists(monkeypatch) -> None:
    """The negative control: nothing to hand over, nothing announced."""

    class InstalledService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="already_installed",
                product_url="https://localhost:8099",
            )

    service = InstalledService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--plain"],
    )

    assert result.exit_code == 0, result.output
    assert "one-time setup link" not in result.output
    assert "one-time setup link" not in result.stderr


def test_an_applied_update_is_said_in_a_sentence_and_kept_out_of_json(
    monkeypatch, tmp_path
) -> None:
    """AC17-R141: an update reported and not applied had nothing on screen to say so.

    The opposite has to be said just as plainly. The revisions are fields in the JSON
    result for machines; the sentence is for the operator and must not leak into a
    redirected `--json` stream.
    """
    old, new = "1" * 40, "2" * 40

    class UpdatingService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="installed",
                product_url="https://flow.example.test",
                updated_from_revision=old,
                updated_to_revision=new,
            )

    # The checkout's own .env describes the developer's machine, not this installation.
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: UpdatingService())

    plain = CliRunner().invoke(installer.installer_command, ["--non-interactive", "--plain"])
    as_json = CliRunner().invoke(installer.installer_command, ["--json"])

    assert plain.exit_code == 0, plain.output
    assert f"Updated from {old} to {new}." in plain.output
    assert as_json.exit_code == 0, as_json.output
    assert "Updated from" not in as_json.output
    payload = json.loads(as_json.output)
    assert payload["result"]["updated_to_revision"] == new


def test_what_an_installed_run_changed_is_said_on_screen(monkeypatch) -> None:
    """R157 acceptance: "Compact worker processes: 2 -> 1." reached only the log."""

    class ChangingService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="already_installed",
                product_url="https://localhost:8099",
                changes=("Compact worker processes: 2 -> 1.",),
            )

    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: ChangingService())

    result = CliRunner().invoke(installer.installer_command, ["--non-interactive", "--plain"])

    assert "Flow Steward is already installed" in result.output
    assert "Compact worker processes: 2 -> 1." in result.stdout


def test_an_installed_runs_warnings_are_said_on_screen(monkeypatch) -> None:
    """AC17-R162: a rollback left the newer images in place -- FS_COMPACT_IMAGE was set in
    the shell -- and the notice saying so reached the JSON only."""
    notice = "FS_COMPACT_IMAGE is set in the shell that ran ./install.sh, so ..."

    class WarningService(FakeService):
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.INSTALLED)

        def install(self, config, explicit_fields=frozenset(), target_release=None):
            self.configs.append(config)
            return InstallRunResult(
                classification=InstallationKind.INSTALLED,
                status="already_installed",
                product_url="https://localhost:8099",
                warnings=(notice,),
            )

    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: WarningService())

    result = CliRunner().invoke(installer.installer_command, ["--non-interactive", "--plain"])

    assert "Flow Steward is already installed" in result.output
    assert f"Warning: {notice}" in result.stdout


@pytest.mark.parametrize(
    "args,expected_exit,expected_error",
    [(["--help"], 0, ""), (["--not-an-installer-option"], 2, "No such option")],
)
def test_main_returns_parser_exit_and_restores_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    expected_exit: int,
    expected_error: str,
) -> None:
    def unexpected_install(**_kwargs):
        pytest.fail("help or invalid arguments must not invoke installation")

    def original_handler(_signum, _frame):
        pytest.fail("no process signal should be delivered by parser handling")

    monkeypatch.setattr(installer.installer_command, "callback", unexpected_install)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, original_handler)
        assert installer.main(args) == expected_exit
        for sig in previous:
            assert signal.getsignal(sig) is original_handler
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    output = capsys.readouterr()
    if expected_error:
        assert output.out == ""
        assert "Usage: install.sh" in output.err
        assert expected_error in output.err
    else:
        assert "Usage: install.sh" in output.out
        assert output.err == ""


@pytest.mark.parametrize(
    ("answered", "supplied", "expected"),
    [
        ("bundled", set(), "local_fs"),
        ("local_fs", set(), "local_fs"),
        ("external_s3", set(), "external_s3"),
        ("bundled", {"file_storage"}, "bundled"),
    ],
)
def test_compact_keeps_an_answered_file_storage_and_replaces_only_the_scalable_default(
    answered, supplied, expected
):
    values = {
        "deployment_profile": "compact",
        "environment": "production",
        "file_storage": answered,
        "s3_endpoint": "https://s3.example.test",
    }

    normalized = installer._with_defaults(values, supplied_keys=supplied)

    assert normalized["file_storage"] == expected
    assert normalized["s3_endpoint"] == "https://s3.example.test"
