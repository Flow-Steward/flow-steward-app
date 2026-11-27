from __future__ import annotations

import ast
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _materialize_cli_copy_context(output: Path) -> None:
    """Materialize the CLI /app import tree from its actual COPY recipe."""
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text().replace("\\\n", " ")
    in_cli = False
    copies = 0
    for line in dockerfile.splitlines():
        words = line.strip().split(maxsplit=1)
        if not words or words[0] not in {"FROM", "COPY"}:
            continue
        tokens = shlex.split(line)
        if tokens[0] == "FROM":
            if in_cli:
                break
            in_cli = tokens[-2:] == ["AS", "cli"]
            continue
        if not in_cli or any(token.startswith("--from=") for token in tokens):
            continue
        operands = [token for token in tokens[1:] if not token.startswith("--")]
        destination_name = operands[-1]
        if destination_name.startswith("/"):
            # Interpreter bootstrap is outside /app; the exact-image probe covers it.
            if not destination_name.startswith("/app/"):
                continue
            destination_name = destination_name.removeprefix("/app/")
        destination = output / destination_name
        assert destination.resolve().is_relative_to(output.resolve())
        for pattern in operands[:-1]:
            sources = sorted(ROOT.glob(pattern))
            assert sources, pattern
            for source in sources:
                if source.is_dir():
                    shutil.copytree(
                        source,
                        destination,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"),
                    )
                else:
                    target = destination
                    if operands[-1] == "." or operands[-1].endswith("/"):
                        target = destination / source.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                copies += 1
    assert copies


def test_cli_copy_context_runs_retention_help_with_real_settings_store(tmp_path: Path) -> None:
    context = tmp_path / "cli"
    context.mkdir()
    _materialize_cli_copy_context(context)
    probe = """
import importlib, runpy, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
common = importlib.import_module('core.application.tooling.ops.retention_cleanup_common')
assert Path(common.system_settings_store.__file__).is_relative_to(root)
assert common.system_settings_store.normalize_retention_settings(
    {'action_logs_days': 7}
)['action_logs_days'] == 7
sys.argv = ['retention_cleanup', '--help']
runpy.run_module('core.application.tooling.ops.retention_cleanup', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(context)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout


def test_cli_copy_context_loads_real_database_bootstrap_dependencies(tmp_path: Path) -> None:
    context = tmp_path / "cli"
    context.mkdir()
    _materialize_cli_copy_context(context)
    probe = """
import importlib, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
stores = importlib.import_module('core.infrastructure.db.platform_stores')
helpers = importlib.import_module('core.infrastructure.db.logger_scheduled_providers_common')
assert helpers._as_dict({'enabled': True}) == {'enabled': True}
seed = importlib.import_module('core.application.tooling.db.bootstrap_seed')
assert seed.PlatformScheduleStore is stores.PlatformScheduleStore
assert seed.LOOKUP_SEED_PATH.is_file()
assert seed.REGISTRY_SEED_PATH.is_file()
for module in list(sys.modules.values()):
    name = str(getattr(module, '__name__', ''))
    path = getattr(module, '__file__', None)
    if name.startswith('core.') and path:
        assert Path(path).is_relative_to(root), (name, path)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(context)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module,args,expected",
    [
        ("core.entrypoints.cli.lifecycle_click_app", ["--help"], "database"),
        (
            "core.entrypoints.cli.lifecycle_click_app",
            ["database", "upgrade", "--help"],
            "maintenance",
        ),
        ("core.application.tooling.db.upgrade_db", ["--help"], "maintenance"),
        ("core.application.tooling.db.bootstrap_seed", ["--help"], "author"),
        ("core.entrypoints.cli.doctor", ["--help"], "fix"),
        (
            "core.application.tooling.ops.prune_workspace_artifacts",
            ["--dry-run", "--workspace-root", "unused-cli-probe-workspace"],
            '"dry_run": true',
        ),
    ],
)
def test_cli_copy_context_documented_readonly_command_paths(
    tmp_path: Path, module: str, args: list[str], expected: str
) -> None:
    context = tmp_path / "cli"
    context.mkdir()
    _materialize_cli_copy_context(context)
    probe = """
import runpy, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
module = sys.argv[2]
sys.argv = [module, *sys.argv[3:]]
runpy.run_module(module, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(context), module, *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert not (tmp_path / "unused-cli-probe-workspace").exists()


def test_output_contract_format_helper_does_not_load_runtime_schema_validator(tmp_path):
    # The CLI lock intentionally lacks jsonschema; the audit image has it.
    probe = """
import sys
sys.path.insert(0, sys.argv[1])
sys.modules["jsonschema"] = None
from core.application.registry.output_contract_fields import response_field_json_schema
assert response_field_json_schema("number") == {"type": "number"}
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(ROOT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ADR 0082: the management process imports these at startup or on the upgrade and
# platform-module switch paths. ``lifecycle_click_app --help`` above never imports
# them (the runtime is built lazily), so their closure is checked statically.
_MANAGEMENT_RELEASE_MODULES = (
    "core.entrypoints.cli.platform_runtime",
    "core.entrypoints.cli.compact_platform_runtime",
    "core.entrypoints.cli.lifecycle_runtime",
    "core.infrastructure.release.platform_module_activator",
    "core.infrastructure.platform_modules.loader",
    "core.application.release.platform_modules",
    "core.application.release.upgrade_workflow",
    "core.application.release.service",
)


def _module_source(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _executed_imports(body: list[ast.stmt]):
    """Yield import statements that run on import: not in functions or TYPE_CHECKING."""
    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            yield statement
        elif isinstance(statement, ast.ClassDef):
            yield from _executed_imports(statement.body)
        elif isinstance(statement, ast.If):
            test = statement.test
            type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if not type_checking:
                yield from _executed_imports(statement.body)
            yield from _executed_imports(statement.orelse)
        elif isinstance(statement, ast.Try):
            for block in (statement.body, statement.orelse, statement.finalbody):
                yield from _executed_imports(block)
            for handler in statement.handlers:
                yield from _executed_imports(handler.body)
        elif isinstance(statement, (ast.With, ast.For, ast.While)):
            yield from _executed_imports(statement.body)


def _first_party_targets(statement: ast.stmt, module: str, is_package: bool) -> list[str]:
    if isinstance(statement, ast.Import):
        return [alias.name for alias in statement.names if alias.name.startswith("core.")]
    assert isinstance(statement, ast.ImportFrom)
    base = statement.module or ""
    if statement.level:
        package = module if is_package else module.rpartition(".")[0]
        for _ in range(statement.level - 1):
            package = package.rpartition(".")[0]
        base = f"{package}.{base}" if base else package
    if not base.startswith("core."):
        return []
    return [base] + [
        f"{base}.{alias.name}"
        for alias in statement.names
        if _module_source(ROOT, f"{base}.{alias.name}") is not None
    ]


def test_cli_copy_context_carries_module_level_imports_of_release_modules(
    tmp_path: Path,
) -> None:
    context = tmp_path / "cli"
    context.mkdir()
    _materialize_cli_copy_context(context)
    missing: dict[str, str] = {}
    seen: set[str] = set()
    pending = [(module, "start") for module in _MANAGEMENT_RELEASE_MODULES]
    while pending:
        module, importer = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        parts = module.split(".")
        pending.extend((".".join(parts[:index]), importer) for index in range(2, len(parts)))
        source = _module_source(context, module)
        if source is None:
            if _module_source(ROOT, module) is not None and not (
                context.joinpath(*parts).is_dir()
                and (ROOT.joinpath(*parts) / "__init__.py").is_file()
            ):
                missing[module] = importer
            continue
        is_package = source.name == "__init__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
        for statement in _executed_imports(tree.body):
            for target in _first_party_targets(statement, module, is_package):
                pending.append((target, f"{module}:{statement.lineno}"))
    assert {"core.application.release.service", "core.entrypoints.cli.lifecycle_runtime"} <= seen
    assert not missing, (
        "the cli stage of infra/docker/Dockerfile does not copy modules the management "
        f"process imports at module level: {dict(sorted(missing.items()))}"
    )
