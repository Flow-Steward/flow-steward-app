"""Where host settings meet the operator: the refusal, the CLI output, ./install.sh."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.application.extensions import url_safety
from core.application.installer import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.application.installer.service import _checked_operator_settings
from core.application.lifecycle.configuration_models import ConfigSetRequest
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.operator_settings import (
    HOST_SETTINGS_DOCS_URL,
    operator_setting_specs,
    settable_names,
    typed_setting_fields,
)
from core.application.tooling.ops.generate_host_settings_doc import (
    render_host_settings_markdown,
)
from core.entrypoints.cli.platform.renderers import _config_show_lines
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

PRIVATE = "FS_ALLOW_PRIVATE_REMOTE_URLS"
COMPACT_COMMAND = f"./install.sh --setting {PRIVATE}=1"
SCALABLE_COMMAND = f"flow-steward config set {PRIVATE}=1"


@pytest.mark.parametrize(
    ("profile", "command"), (("compact", COMPACT_COMMAND), ("scalable", SCALABLE_COMMAND))
)
def test_a_refused_private_address_gives_the_command_for_this_installation(
    monkeypatch: pytest.MonkeyPatch, profile: str, command: str
) -> None:
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", profile)
    monkeypatch.setattr(url_safety, "_allow_private_remote_urls", lambda: False)

    with pytest.raises(ValueError) as provider:
        url_safety.assert_safe_provider_url(
            "http://10.0.0.5:11434", purpose="Base URL", allow_private_addresses=False
        )
    with pytest.raises(ValueError) as remote:
        url_safety.assert_safe_remote_http_url("http://localhost:5001", purpose="Docling base_url")

    for refusal in (provider.value, remote.value):
        assert command in str(refusal)


def test_an_extension_that_blocks_a_private_address_gets_the_same_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    detail = "Docling base_url host 'docling-serve' is not allowed"

    blocked = url_safety.with_private_address_hint(
        detail, [{"code": "network_blocked", "message": detail}]
    )
    other = url_safety.with_private_address_hint(detail, [{"code": "invalid_connection"}])

    assert blocked.startswith(detail) and COMPACT_COMMAND in blocked
    assert other == detail


def test_config_show_reads_as_sections_and_ends_with_the_command_and_the_page() -> None:
    lines = _config_show_lines(
        {
            "active": {
                "public_host": "flow.example.test",
                "environment": "production",
                "operator_settings": {PRIVATE: "1"},
            },
            "pending_diff": {"public_port": {"active": 8099, "pending": 443}},
            "protected_setting_states": [{"name": "FS_SECRET_KEY", "state": "configured"}],
        }
    )
    text = "\n".join(lines)

    assert lines[0] == "Settings"
    assert "public_host  flow.example.test" in text.split("Host settings", 1)[0]
    assert f"{PRIVATE}  1" in text.split("Host settings set by the operator", 1)[1]
    assert "public_port  8099 -> 443" in text
    assert "environment  production" in text.split("Fixed at installation", 1)[1]
    assert "FS_SECRET_KEY  configured" in text
    assert lines[-2].startswith("Change a setting: flow-steward config set NAME=VALUE")
    assert lines[-1] == f"All settings: {HOST_SETTINGS_DOCS_URL}"
    assert "{" not in text


def _accepted(name: str, value: str) -> bool:
    try:
        ConfigSetRequest.model_validate({"assignments": [f"{name}={value}"]})
    except ValidationError:
        return False
    return True


def _valid_value(name: str) -> str:
    field = typed_setting_fields().get(name)
    if field is not None:
        return str(field.choices[0].value if field.choices else (field.examples or ("1",))[0])
    spec = operator_setting_specs()[name]
    return spec.allowed_values[-1] if spec.allowed_values else "1"


def test_one_name_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every name config show calls changeable, the page lists, or an error names is
    one `config set NAME=VALUE` accepts as printed."""
    active = DeploymentConfig(operator_settings={PRIVATE: "1"}).model_dump(mode="json")
    show = "\n".join(_config_show_lines({"active": active}))
    shown = {
        line.split()[0]
        for section in ("Settings", "Host settings set by the operator")
        for line in show.split(f"{section}\n", 1)[1].split("\n\n", 1)[0].splitlines()
    }
    page = {
        line.removeprefix("### `").removesuffix("`")
        for line in render_host_settings_markdown().splitlines()
        if line.startswith("### `")
    }
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "scalable")
    error = url_safety.private_addresses_hint().split("config set ", 1)[1].split("=", 1)[0]

    assert PRIVATE in shown and "public_host" in shown
    assert page == set(settable_names())
    for name in {*shown, *page, error}:
        assert _accepted(name, _valid_value(name)), name


def test_the_committed_page_is_the_generated_one() -> None:
    root = Path(__file__).resolve().parents[3]
    committed = (root / "docs/configuration/host-settings.md").read_text(encoding="utf-8")

    assert committed == render_host_settings_markdown()


def test_install_sh_refuses_a_setting_it_cannot_set() -> None:
    with pytest.raises(InstallerFailure) as refused:
        _checked_operator_settings({"FS_SECRET_KEY": "x"})

    assert refused.value.code == "installer_setting_invalid"
    assert "flow-steward config settings" not in refused.value.cause


class _Runtime:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        self.commands.append(tuple(command))
        return ProcessOutcome(command=tuple(command), returncode=0)

    def recreated(self) -> bool:
        return any("up" in command for command in self.commands)


@pytest.fixture
def installed(tmp_path: Path):
    (tmp_path / "infra/docker").mkdir(parents=True)
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (tmp_path / ".env.prod").write_text(
        "POSTGRES_PASSWORD=kept-secret\nFS_WORKER_PROCESSES=2\n", encoding="utf-8"
    )
    (state / "compose.env").write_text("FS_COMPOSE_PROJECT_NAME=fs-compact\n", encoding="utf-8")
    runtime = _Runtime()
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(state),
        process=runtime,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
    )
    return tmp_path, gateway, runtime


def _compact(**fields) -> InstallConfig:
    return InstallConfig(environment="production", deployment_profile="compact", **fields)


def test_install_sh_setting_writes_the_key_records_it_and_restarts_the_runtime(installed) -> None:
    root, gateway, runtime = installed
    log = gateway.state_store.open_log(f"settings-{uuid4().hex}")

    applied = gateway.set_operator_settings(_compact(), changes={PRIVATE: "1"}, log=log)

    assert applied.operator_settings == {PRIVATE: "1"}
    env = (root / ".env.prod").read_text(encoding="utf-8")
    assert f"{PRIVATE}=1" in env and "POSTGRES_PASSWORD=kept-secret" in env
    assert f'{PRIVATE} = "1"' in (root / ".flow-steward/stack.toml").read_text("utf-8")
    assert runtime.recreated()

    reset = gateway.set_operator_settings(applied, changes={PRIVATE: ""}, log=log)
    assert reset.operator_settings == {}
    assert f"{PRIVATE}=0" in (root / ".env.prod").read_text(encoding="utf-8")


def test_the_same_setting_again_is_no_change_and_no_restart(installed) -> None:
    _root, gateway, runtime = installed
    log = gateway.state_store.open_log(f"settings-{uuid4().hex}")

    unchanged = gateway.set_operator_settings(
        _compact(operator_settings={PRIVATE: "1"}), changes={PRIVATE: "1"}, log=log
    )

    assert unchanged.operator_settings == {PRIVATE: "1"}
    assert not runtime.recreated()
