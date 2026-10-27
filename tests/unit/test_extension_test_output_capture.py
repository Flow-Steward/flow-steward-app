from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.tooling.extensions import test_extension as test_extension_module


def test_extension_subprocess_output_is_captured_inside_the_service_envelope(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    def _run(_cmd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="Looking in links: /extension/dev-wheels\n",
            stderr="pip warning\n",
        )

    monkeypatch.setattr(test_extension_module.subprocess, "run", _run)
    output = __import__("io").StringIO()
    with __import__("contextlib").redirect_stdout(output):
        exit_code = test_extension_module._run(
            ["python", "-m", "pip", "install"],
            cwd=tmp_path,
        )

    assert exit_code == 0
    assert calls == [
        {
            "cwd": str(tmp_path),
            "env": None,
            "capture_output": True,
            "text": True,
        }
    ]
    assert "Looking in links" in output.getvalue()
    assert "pip warning" in output.getvalue()


def test_extension_smoke_and_pytest_use_temporary_venv_and_safe_environment(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FS_EXTENSION_TEST_RUNTIME", "1")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setenv("FS_DB_DSN", "must-not-leak")
    monkeypatch.setenv("FS_SECRET_KEY", "must-not-leak")
    monkeypatch.setenv("ACME_SANDBOX_DSN", "disposable-test-dsn")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    extension_dir = tmp_path / "acme_crm_sync"
    (extension_dir / "scripts").mkdir(parents=True)
    (extension_dir / "tests").mkdir()
    (extension_dir / "requirements-dev.txt").write_text("", encoding="utf-8")
    (extension_dir / "scripts" / "smoke_check.py").write_text("", encoding="utf-8")
    (extension_dir / "tests" / "test_scaffold.py").write_text("", encoding="utf-8")

    commands: list[tuple[list[str], dict[str, str] | None]] = []

    def _run(cmd, *, cwd, env=None):
        commands.append((list(cmd), env))
        return 0

    monkeypatch.setattr(test_extension_module, "_run", _run)
    monkeypatch.setattr(test_extension_module, "validate_extensions", lambda _request: None)
    builder_options: dict[str, object] = {}

    class _FakeBuilder:
        def __init__(self, **kwargs) -> None:
            builder_options.update(kwargs)

        def create(self, _path) -> None:
            return None

    monkeypatch.setattr(test_extension_module.venv, "EnvBuilder", _FakeBuilder)
    result = test_extension_module.test_extension(
        test_extension_module.ExtensionTestRequest(
            extension_id="acme.crm-sync",
            root=tmp_path,
            skip_validate=True,
            test_env=("ACME_SANDBOX_DSN", "FS_SECRET_KEY", "not a name"),
        )
    )

    standalone_python = commands[0][0][0]
    assert result.exit_code == 0
    assert builder_options == {
        "with_pip": True,
        "clear": True,
        "system_site_packages": True,
    }
    assert commands[1][0] == [standalone_python, "scripts/smoke_check.py"]
    assert commands[2][0] == [standalone_python, "-m", "pytest", "tests/test_scaffold.py"]
    assert commands[1][1]["LANG"] == "en_US.UTF-8"
    assert "FS_ALLOW_PRIVATE_REMOTE_URLS" not in commands[1][1]
    assert "FS_DB_DSN" not in commands[1][1]
    assert "FS_SECRET_KEY" not in commands[1][1]
    assert commands[1][1]["ACME_SANDBOX_DSN"] == "disposable-test-dsn"
    assert "FS_ALLOW_PRIVATE_REMOTE_URLS" not in commands[2][1]
    assert "FS_SECRET_KEY" not in commands[2][1]
    pytest_pythonpath = commands[2][1]["PYTHONPATH"].split(__import__("os").pathsep)
    assert pytest_pythonpath == [str(extension_dir)]


def test_extension_test_refuses_non_delegated_runtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FS_EXTENSION_TEST_RUNTIME", raising=False)
    extension_dir = tmp_path / "acme_crm_sync"
    extension_dir.mkdir()

    result = test_extension_module.test_extension(
        test_extension_module.ExtensionTestRequest(
            extension_id="acme.crm-sync",
            root=tmp_path,
            skip_validate=True,
        )
    )

    assert result.exit_code == 2
    assert "dedicated test runtime" in result.output


def test_the_runner_installs_a_bundle_from_its_own_vendored_wheels(
    monkeypatch, tmp_path: Path
) -> None:
    """Core owns *how* a bundle's wheels are installed, not *which* wheels exist.

    This used to assert on two real bundles and their vendored wheel filenames,
    which made the platform's own suite depend on particular extensions being
    present and on what they happen to pin. That knowledge belongs to each bundle,
    and now lives in its suite; what Core owns is this: the runner installs offline
    from the bundle's own `dev-wheels`, so a vendored wheel is what gets used.
    """
    extension_dir = tmp_path / "acme_crm_sync"
    (extension_dir / "dev-wheels").mkdir(parents=True)
    (extension_dir / "tests").mkdir()
    (extension_dir / "requirements-dev.txt").write_text(
        "./dev-wheels/acme_dep-1.0-cp312-cp312-macosx_11_0_arm64.whl\n", encoding="utf-8"
    )
    (extension_dir / "dev-wheels" / "acme_dep-1.0-cp312-cp312-macosx_11_0_arm64.whl").write_bytes(
        b""
    )
    (extension_dir / "tests" / "test_scaffold.py").write_text("", encoding="utf-8")

    commands: list[list[str]] = []
    monkeypatch.setenv("FS_EXTENSION_TEST_RUNTIME", "1")
    monkeypatch.setattr(
        test_extension_module, "_run", lambda cmd, *, cwd, env=None: commands.append(list(cmd)) or 0
    )
    monkeypatch.setattr(test_extension_module, "validate_extensions", lambda _request: None)

    class _FakeBuilder:
        def __init__(self, **_kwargs) -> None:
            return None

        def create(self, _path) -> None:
            return None

    monkeypatch.setattr(test_extension_module.venv, "EnvBuilder", _FakeBuilder)
    result = test_extension_module.test_extension(
        test_extension_module.ExtensionTestRequest(
            extension_id="acme.crm-sync", root=tmp_path, skip_validate=True
        )
    )

    assert result.exit_code == 0
    install = commands[0]
    assert install[1:5] == ["-m", "pip", "install", "--disable-pip-version-check"]
    assert "--no-index" in install, "an install that can reach the network is not offline"
    find_links = [
        install[index + 1] for index, part in enumerate(install) if part == "--find-links"
    ]
    assert str(extension_dir / "dev-wheels") in find_links
    assert install[-1] == str(extension_dir / "requirements-dev.txt")


def test_only_the_requested_names_are_passed(capsys) -> None:
    """The delegated runtime is a fresh container, so the caller declares what crosses.

    An extension's tests read an input Core has no name for — a disposable DSN, a
    sandbox URL. Without it they skip, and a skipped suite reads as a green one.
    The names arrive as repeated `--test-env` options: one invocation's input, not
    product configuration.
    """
    resolved = test_extension_module.requested_test_env_names(
        ["ACME_SANDBOX_DSN", "ACME_SANDBOX_DSN", "", "lower_case", "1BAD", "FS_DB_DSN", "OK_2"]
    )

    assert resolved == ["ACME_SANDBOX_DSN", "OK_2"]
    warnings = capsys.readouterr().out
    assert "lower_case" in warnings
    assert "1BAD" in warnings
    assert "FS_DB_DSN" in warnings


def test_a_platform_credential_is_never_passed_even_when_requested() -> None:
    """The delegated runtime executes third-party bundle code.

    Two rounds of review each found a name a hand-written list did not mention.
    `FS_EXTENSION_RUNTIME_SIGNING_SECRET` is a *registered* secret; the rest are
    **compatibility names Core reads but never registered**, which is why the
    manifest alone was not the platform's environment surface.
    """
    for reserved in (
        "FS_DB_DSN",
        "FS_SECRET_KEY",
        "FS_WORKER_MCP_BEARER_TOKEN",
        "FS_EXTENSION_RUNTIME_SIGNING_SECRET",
        "MCP_BEARER_TOKEN",
        "POSTGRES_DSN",
        "POSTGRES_PASSWORD",
        "RABBITMQ_PASSWORD",
        "RABBITMQ_URL",
        "SECRET_KEY",
        "AWS_SESSION_TOKEN",
    ):
        assert test_extension_module.requested_test_env_names([reserved]) == [], reserved


def test_every_name_the_platform_declares_or_reads_is_refused() -> None:
    """Not a sample — the whole surface, so no name is missed.

    The manifest is what the platform is configured with; the audit is what it
    actually reads, compatibility aliases included. A bundle gets neither.
    """
    manifest = json.loads(test_extension_module._ENV_MANIFEST_PATH.read_text(encoding="utf-8"))
    registered = [
        str(row["name"]) for row in manifest["variables"] if str(row.get("name") or "").strip()
    ]
    audit = json.loads(test_extension_module._ENV_AUDIT_PATH.read_text(encoding="utf-8"))
    audit_rows = audit if isinstance(audit, list) else audit["variables"]
    read = [
        str(row["variable_name"])
        for row in audit_rows
        if str(row.get("variable_name") or "").strip()
    ]

    assert len(registered) > 300, "check the register paths"
    assert len(read) > 600, "check the register paths"
    assert test_extension_module.requested_test_env_names(registered) == []
    assert test_extension_module.requested_test_env_names(read) == []


def test_the_surface_is_read_from_the_registers_not_restated() -> None:
    """A list beside a register stops covering the next name somebody adds."""
    surface = test_extension_module.platform_variable_names()
    assert "FS_EXTENSION_RUNTIME_SIGNING_SECRET" in surface
    assert "MCP_BEARER_TOKEN" in surface
    source = Path(test_extension_module.__file__).read_text(encoding="utf-8")
    assert "flow_steward_env_manifest.json" in source
    assert "flow-steward-env-audit.json" in source


def test_an_extension_owned_name_is_not_mistaken_for_a_platform_one() -> None:
    """The refusal must not swallow the input the option exists to pass."""
    assert test_extension_module.requested_test_env_names(
        ["ACME_SANDBOX_DSN", "FS_TOOLBOX_INTEGRATION_DSN"]
    ) == ["ACME_SANDBOX_DSN", "FS_TOOLBOX_INTEGRATION_DSN"]


@pytest.mark.parametrize("missing", ["_ENV_MANIFEST_PATH", "_ENV_AUDIT_PATH"])
def test_an_unreadable_register_passes_nothing(monkeypatch, tmp_path: Path, missing: str) -> None:
    """Without a register there is no way to tell a secret from an extension's input."""
    test_extension_module.platform_variable_names.cache_clear()
    monkeypatch.setattr(test_extension_module, missing, tmp_path / "absent.json")
    try:
        assert test_extension_module.requested_test_env_names(["ACME_SANDBOX_DSN"]) == []
    finally:
        test_extension_module.platform_variable_names.cache_clear()


def test_no_request_passes_nothing_extra() -> None:
    assert test_extension_module.requested_test_env_names(None) == []
    assert test_extension_module.requested_test_env_names([]) == []


def test_a_test_input_leaves_no_trace_in_product_configuration() -> None:
    """A test input is ephemeral, so it must not appear in the platform's config.

    The first attempt at this made it a global `FS_EXTENSION_TEST_ENV`, which the
    architecture gate correctly flagged: developer tooling input has no place in
    the runtime manifest a running product is configured from.
    """
    manifest = json.loads(test_extension_module._ENV_MANIFEST_PATH.read_text(encoding="utf-8"))
    registered = {str(row["name"]) for row in manifest["variables"]}
    assert "FS_EXTENSION_TEST_ENV" not in registered

    for path in sorted(Path(test_extension_module.__file__).parents[4].glob(".env.example*")):
        assert "FS_EXTENSION_TEST_ENV" not in path.read_text(encoding="utf-8"), path


def test_core_names_no_extension_specific_environment_variable() -> None:
    """Core must not learn one extension's variable name — that is the contract."""
    source = Path(test_extension_module.__file__).read_text(encoding="utf-8")
    assert "TOOLBOX" not in source.upper()
    for name in test_extension_module._STANDALONE_ENV_ALLOWLIST:
        assert not name.startswith("FS_"), f"{name} is a platform-specific name in the allowlist"


def _bundle_with_tests(tmp_path: Path, monkeypatch) -> tuple[Path, list[list[str]]]:
    extension_dir = tmp_path / "acme_crm_sync"
    (extension_dir / "tests").mkdir(parents=True)
    (extension_dir / "requirements-dev.txt").write_text("", encoding="utf-8")
    for name in ("test_a.py", "test_b.py"):
        (extension_dir / "tests" / name).write_text("", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setenv("FS_EXTENSION_TEST_RUNTIME", "1")
    monkeypatch.setattr(
        test_extension_module, "_run", lambda cmd, *, cwd, env=None: commands.append(list(cmd)) or 0
    )

    class _FakeBuilder:
        def __init__(self, **_kwargs) -> None:
            return None

        def create(self, _path) -> None:
            return None

    monkeypatch.setattr(test_extension_module.venv, "EnvBuilder", _FakeBuilder)
    return extension_dir, commands


def test_selected_targets_run_fail_fast_and_the_whole_suite_runs_to_the_end(
    monkeypatch, tmp_path: Path
) -> None:
    """A selection is a focused Tier 1 run; the whole suite keeps its full failure list."""
    _extension_dir, commands = _bundle_with_tests(tmp_path, monkeypatch)

    selected = test_extension_module.test_extension(
        test_extension_module.ExtensionTestRequest(
            extension_id="acme.crm-sync",
            root=tmp_path,
            skip_validate=True,
            test_paths=("tests/test_b.py::test_one",),
        )
    )
    whole = test_extension_module.test_extension(
        test_extension_module.ExtensionTestRequest(
            extension_id="acme.crm-sync", root=tmp_path, skip_validate=True
        )
    )

    assert selected.exit_code == whole.exit_code == 0
    pytest_runs = [cmd[1:] for cmd in commands if cmd[1:3] == ["-m", "pytest"]]
    assert pytest_runs == [
        ["-m", "pytest", "-x", "tests/test_b.py::test_one"],
        ["-m", "pytest", "tests/test_a.py", "tests/test_b.py"],
    ]


@pytest.mark.parametrize("target", ["../outside/test_x.py", "/etc/passwd", "tests/test_missing.py"])
def test_a_target_outside_the_bundle_fails_without_running_pytest(
    monkeypatch, tmp_path: Path, target: str
) -> None:
    _extension_dir, commands = _bundle_with_tests(tmp_path, monkeypatch)

    result = test_extension_module.test_extension(
        test_extension_module.ExtensionTestRequest(
            extension_id="acme.crm-sync", root=tmp_path, skip_validate=True, test_paths=(target,)
        )
    )

    assert result.exit_code == 1
    assert "not inside the extension bundle" in result.output
    assert not [cmd for cmd in commands if cmd[1:3] == ["-m", "pytest"]]
