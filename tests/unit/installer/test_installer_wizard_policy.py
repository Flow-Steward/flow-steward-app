"""Installation choices that a first-time operator can complete without internal details."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from core.application.installer.contracts import InstallConfig
from core.application.installer.spec import INSTALLER_SPEC
from core.entrypoints.cli import installer
from core.entrypoints.cli.platform.renderers import PlainRenderer


def test_new_compact_install_keeps_default_seaweedfs() -> None:
    values = installer._with_defaults({"deployment_profile": "compact"})
    assert values["file_storage"] == "bundled"


def test_new_install_choices_offer_seaweedfs_and_external_s3_only() -> None:
    storage = next(field for field in INSTALLER_SPEC.fields if field.key == "file_storage")
    assert [choice.value for choice in storage.choices] == ["bundled", "external_s3"]
    assert "SeaweedFS" in storage.choices[0].label


@pytest.mark.parametrize(
    "route,expected", [("external_proxy", "reverse_proxy"), ("platform", "platform_managed")]
)
def test_simple_routes_derive_compatible_https_without_a_question(route, expected) -> None:
    from core.application.installer.wizard import InstallerWizardPolicy

    policy = InstallerWizardPolicy()
    values = policy.normalize({"ingress_mode": route}, frozenset({"ingress_mode"}))
    assert values["tls_mode"] == expected
    assert values["public_port"] == 443
    assert values["file_storage"] == "bundled"
    config = InstallConfig.model_validate(values)
    assert config.tls_mode == expected


def test_explicit_ports_and_invalid_explicit_tls_are_not_overwritten() -> None:
    from core.application.installer.wizard import InstallerWizardPolicy

    given = {"ingress_mode": "external_proxy", "tls_mode": "self_signed", "public_port": 8443}
    policy = InstallerWizardPolicy(locked_keys=frozenset(given))
    values = policy.normalize(given, frozenset(given))
    assert values["tls_mode"] == "self_signed"
    assert values["public_port"] == 8443
    with pytest.raises(ValueError):
        InstallConfig.model_validate(values)


def test_generated_defaults_follow_route_changes_but_deliberate_port_survives() -> None:
    from core.application.installer.wizard import InstallerWizardPolicy

    policy = InstallerWizardPolicy()
    values = policy.normalize({}, frozenset())
    values["ingress_mode"] = "external_proxy"
    values = policy.normalize(values, frozenset({"ingress_mode"}))
    assert (values["tls_mode"], values["public_port"]) == ("reverse_proxy", 443)
    values["public_port"] = 8443
    values["ingress_mode"] = "bundled_traefik"
    values = policy.normalize(values, frozenset({"ingress_mode", "public_port"}))
    assert values["tls_mode"] == "local"
    assert values["public_port"] == 8443


def test_existing_localfs_values_remain_readable_without_migration() -> None:
    values = installer._with_defaults(
        {"deployment_profile": "compact", "file_storage": "local_fs"},
        supplied_keys={"file_storage"},
    )
    assert values["file_storage"] == "local_fs"
    assert InstallConfig.model_validate(values).file_storage == "local_fs"


def test_plain_nginx_flow_asks_only_meaningful_decisions() -> None:
    from core.entrypoints.cli.installer_form import collect_plain

    @click.command()
    def command():
        values = collect_plain({}, renderer=PlainRenderer())
        config = InstallConfig.model_validate(values)
        click.echo("CONFIG=" + json.dumps(config.model_dump(mode="json")))

    # Environment, layout, website access, proxy host placement/kind defaults,
    # public hostname, storage; technical values come from supported defaults.
    result = CliRunner().invoke(command, input="\n\n3\n\n\nfs.example.test\n\ny\n")
    assert result.exit_code == 0, result.output
    prompts = result.output.split("CONFIG=", 1)[0]
    for technical in (
        "Trusted proxy CIDRs",
        "Extension installer origin",
        "Compact worker processes",
        "Certificate path",
        "Private key path",
        "Reverse-proxy MCP port",
    ):
        assert technical not in prompts
    assert "self_signed" not in prompts
    assert "reverse_proxy" not in prompts
    assert '"file_storage": "bundled"' in result.output
    assert '"public_port": 443' in result.output


def test_completion_contains_full_nginx_setup_before_secret_login_link(
    tmp_path, monkeypatch
) -> None:
    from types import SimpleNamespace

    from core.application.installer import InstallationKind, InstallRunResult
    from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

    class Service:
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.FRESH)

        def install(self, config, **_kwargs):
            LifecycleConfigurationStore(
                tmp_path, state_root=tmp_path / ".flow-steward"
            ).write_active(config)
            return InstallRunResult(
                classification=InstallationKind.FRESH,
                status="installed",
                bootstrap_setup_url="https://fs.example.test/bootstrap#setup-token=fixture-only",
            )

    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: Service())
    monkeypatch.setattr(installer, "_prepare_official_release", lambda *_args, **_kwargs: None)
    result = CliRunner().invoke(
        installer.installer_command,
        [
            "--non-interactive",
            "--confirm",
            "--ingress-mode",
            "external_proxy",
            "--public-host",
            "fs.example.test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "server {" in result.output
    assert "server_name fs.example.test;" in result.output
    assert "127.0.0.1:18099" in result.output
    assert result.output.index("server {") < result.output.index(
        "NEXT STEP: CREATE THE FIRST ADMINISTRATOR"
    )
    assert "fixture-only" not in result.stdout
    assert "fixture-only" in result.stderr


def test_proxy_help_does_not_extend_the_private_cross_version_handoff_contract():
    from core.application.installer.contracts import InstallRunResult

    assert "proxy_setup" not in InstallRunResult.model_fields


def test_proxy_rendering_failure_preserves_success_and_first_login(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from core.application.installer import InstallationKind, InstallRunResult
    from core.entrypoints.cli import installer_completion
    from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

    class Service:
        def inspect(self):
            return SimpleNamespace(kind=InstallationKind.FRESH)

        def install(self, config, **_kwargs):
            LifecycleConfigurationStore(
                tmp_path, state_root=tmp_path / ".flow-steward"
            ).write_active(config)
            return InstallRunResult(
                classification=InstallationKind.FRESH,
                status="installed",
                bootstrap_setup_url="https://fs.example.test/bootstrap#setup-token=fixture-only",
            )

    def fail(*_args, **_kwargs):
        raise RuntimeError("cannot render token=fixture-render-secret")

    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: Service())
    monkeypatch.setattr(installer, "_prepare_official_release", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer_completion, "render_proxy_setup", fail)
    result = CliRunner().invoke(
        installer.installer_command,
        [
            "--non-interactive",
            "--confirm",
            "--ingress-mode",
            "external_proxy",
            "--public-host",
            "fs.example.test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Warning:" in result.output
    assert "fixture-render-secret" not in result.output
    assert "NEXT STEP: CREATE THE FIRST ADMINISTRATOR" in result.output
    assert "fixture-only" not in result.stdout
    assert "fixture-only" in result.stderr


def test_completion_preserves_saved_public_https_port(tmp_path):
    from core.entrypoints.cli.installer_completion import proxy_setup_for
    from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="bundled",
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        public_host="fs.example.test",
        public_port=8443,
    )
    LifecycleConfigurationStore(tmp_path, state_root=tmp_path / ".flow-steward").write_active(
        config
    )
    setup = proxy_setup_for(tmp_path)
    assert setup is not None
    assert "--https-port 8443" in "\n".join(setup.instructions)
