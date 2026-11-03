from __future__ import annotations

import ast
import builtins
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "core/entrypoints/cli/scripts/lint.sh"


@pytest.mark.parametrize("group", ["all", "independent", "frontend-contract"])
def test_partition_command_preserves_selection_and_never_injects_empty_argv(
    tmp_path: Path, group: str
) -> None:
    (tmp_path / "tests/unit").mkdir(parents=True)
    wrapper = tmp_path / "core/entrypoints/cli/scripts/run-core-only-tests.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/bin/bash\nset -eu\n"
        '[[ "$1" == --coverage-export-dir ]]\nmkdir -p "$2"\nshift 2\n'
        'if [[ "$1" == --without-assets ]]; then echo without-assets >> "$TRACE"; shift; fi\n'
        'exec "$@"\n'
    )
    wrapper.chmod(0o755)
    interpreter = tmp_path / "fake-python"
    interpreter.write_text(
        "#!/bin/bash\nset -eu\n"
        'if [[ "$1" == -m ]]; then echo tests/unit/test_selected.py; exit; fi\n'
        'printf "%s\\n" "$@" >> "$TRACE"\n'
    )
    interpreter.chmod(0o755)
    source = SCRIPT.read_text()
    body = (
        "run_check_unit_tests_with_coverage() {"
        + source.split("run_check_unit_tests_with_coverage() {", 1)[1].split(
            "\nassert_audit_test_isolation()", 1
        )[0]
    )
    harness = (
        "set -euo pipefail\n"
        f"ROOT_DIR={shlex.quote(str(tmp_path))}\n"
        f"PYTHON_BIN={shlex.quote(str(interpreter))}\n"
        "TARGETS=(); REQUESTED_CHECKS=(tests); quality_owner_tier=3\n"
        "assert_audit_test_isolation() { :; }\n"
        'ensure_frontend_shell() { echo shell >> "$TRACE"; }\n'
        'run_step() { shift; "$@"; }\n' + body + f"\nrun_check_unit_tests_with_coverage {group}\n"
    )
    trace = tmp_path / "trace"
    result = subprocess.run(
        ("bash", "-c", harness),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "TRACE": str(trace)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = trace.read_text().splitlines()
    assert "" not in arguments
    assert ("without-assets" in arguments) == (group == "independent")
    assert ("shell" in arguments) == (group != "independent")
    assert ("tests/unit" in arguments) == (group == "all")
    assert ("tests/unit/test_selected.py" in arguments) == (group != "all")


def test_independent_workspace_has_no_real_asset_or_source_checkout_escape(tmp_path: Path) -> None:
    wrapper = tmp_path / "core/entrypoints/cli/scripts/run-core-only-tests.sh"
    wrapper.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / wrapper.relative_to(tmp_path), wrapper)
    assets = tmp_path / "assets/frontend/dist/index.html"
    assets.parent.mkdir(parents=True)
    assets.write_text("real source shell")
    result = subprocess.run(
        (
            "bash",
            str(wrapper),
            "--without-assets",
            "python",
            "-c",
            "import os; from pathlib import Path; "
            "assert not Path('assets').exists(); "
            "assert Path(os.environ['PYTEST_SOURCE_REPO_ROOT']) == Path.cwd()",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("loaded", [False, True])
def test_singleton_cleanup_clears_loaded_modules_without_importing_dormant_ones(loaded) -> None:
    tree = ast.parse((ROOT / "tests/conftest.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_reset_singletons"
    )
    function.decorator_list = []
    bus = SimpleNamespace(MessageBus=SimpleNamespace(_instances={"stale": object()}))
    registry = SimpleNamespace(_instance=object())
    modules = (
        {
            "core.application.orchestration.message_bus": bus,
            "core.application.registry.registry_manager": registry,
        }
        if loaded
        else {}
    )
    imports = []

    def unexpected_import(*args, **kwargs):
        imports.append(args[0])
        raise AssertionError("cleanup must not import dormant code")

    namespace = {
        "sys": SimpleNamespace(modules=modules),
        "__builtins__": {**vars(builtins), "__import__": unexpected_import},
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "singleton-fixture", "exec"),
        namespace,
    )
    fixture = namespace["_reset_singletons"]()
    next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)
    assert imports == []
    if loaded:
        assert bus.MessageBus._instances == {}
        assert registry._instance is None
