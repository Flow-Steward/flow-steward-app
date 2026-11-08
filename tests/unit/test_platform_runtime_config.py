"""Restricted host configuration transport preserves the canonical CLI contract."""

import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("operation", "arguments"),
    (
        ("config-show", ["config", "show", "--json"]),
        ("config-show", ["config", "--help"]),
        ("config-validate", ["--plain", "config", "validate"]),
        (
            "config-set",
            ["config", "set", "--public-host", "fs.xenonled.eu", "--public-port", "443"],
        ),
        ("config-apply", ["config", "apply", "--confirm", "--non-interactive"]),
    ),
)
def test_config_operation_delegates_original_arguments_to_canonical_click(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    arguments: list[str],
) -> None:
    from core.entrypoints.cli import platform_runtime
    from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime

    click_module = importlib.import_module("core.entrypoints.cli.lifecycle_click_app")
    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(click_module._runtime, "_runtime", None)
    calls: list[list[str]] = []

    def invoke(**kwargs: object) -> int:
        assert isinstance(click_module._runtime._runtime, CompactPlatformRuntime)
        calls.append(kwargs["args"])
        return 7

    monkeypatch.setattr(click_module.click_app, "main", invoke)

    assert platform_runtime.main([operation, *arguments]) == 7
    assert calls == [arguments]


def test_config_unknown_options_are_rejected_by_canonical_click(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from core.entrypoints.cli import platform_runtime

    click_module = importlib.import_module("core.entrypoints.cli.lifecycle_click_app")
    monkeypatch.setattr(click_module._runtime, "_runtime", click_module._runtime._runtime)
    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", str(tmp_path))

    assert platform_runtime.main(["config-show", "config", "show", "--unknown-setting"]) == 2
    output = capsys.readouterr()
    assert "No such option: --unknown-setting" in output.err
    assert "Usage:" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize(
    "arguments",
    (
        ["config-show", "config", "set", "--public-host", "fs.xenonled.eu"],
        ["config-apply", "release", "upgrade", "--confirm"],
    ),
)
def test_config_operation_mismatch_is_refused_before_runtime_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]
) -> None:
    from core.entrypoints.cli import platform_runtime

    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", str(tmp_path))

    def unexpected_import(name: str):
        pytest.fail(f"Refused configuration transport imported runtime: {name}")

    monkeypatch.setattr(platform_runtime.importlib, "import_module", unexpected_import)

    with pytest.raises(SystemExit) as raised:
        platform_runtime.main(arguments)
    assert raised.value.code == 2


def test_config_transport_requires_host_installation_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.entrypoints.cli import platform_runtime

    monkeypatch.delenv("FS_CONFIGURATION_STATE_ROOT", raising=False)

    with pytest.raises(SystemExit) as raised:
        platform_runtime.main(["config-show", "config", "show"])
    assert raised.value.code == 2


def test_provider_container_keeps_configuration_host_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from core.entrypoints.cli import compact_container_cli

    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")

    assert compact_container_cli.main(["config", "show"]) == 2
    assert "requires host Docker" in capsys.readouterr().err
