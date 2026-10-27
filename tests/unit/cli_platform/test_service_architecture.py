"""Architecture guardrails for schema-driven CLI service bindings."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.service_registry import SERVICE_FACTORIES

ROOT = Path(__file__).parents[3]
SCHEMA_CLI_PATHS = (
    ROOT / "core/entrypoints/cli/platform",
    ROOT / "core/entrypoints/cli/runtime.py",
    ROOT / "core/entrypoints/cli/click_app.py",
)
FORBIDDEN_ENTRYPOINT_PREFIXES = (
    "core.entrypoints.api",
    "core.entrypoints.admin",
    "core.entrypoints.ws",
    "core.entrypoints.mcp",
)
FORBIDDEN_LEGACY_CLI_MODULES = {
    "core.entrypoints.cli.users",
    "core.entrypoints.cli.extensions",
    "core.entrypoints.cli.projects",
    "core.entrypoints.cli.workflows",
    "core.entrypoints.cli.tasks",
    "core.entrypoints.cli.jobs",
    "core.entrypoints.cli.reports",
    "core.entrypoints.cli.tooling",
}
FORBIDDEN_IMPORT_PREFIXES = (*FORBIDDEN_ENTRYPOINT_PREFIXES, "requests", "httpx", "urllib")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in SCHEMA_CLI_PATHS:
        if path.is_file():
            files.append(path)
        else:
            files.extend(sorted(path.rglob("*.py")))
    return files


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def test_every_public_service_ref_has_exactly_one_declared_binding() -> None:
    public_refs = [spec.service_ref for spec in PUBLIC_COMMAND_SPECS]

    assert len(public_refs) == len(set(public_refs))
    assert set(SERVICE_FACTORIES) == set(public_refs)


def test_schema_driven_cli_surface_has_no_forbidden_transport_or_parser_patterns() -> None:
    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = _module_name(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _forbidden_import(alias.name):
                        violations.append(f"{module}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if _forbidden_import(imported) or imported in FORBIDDEN_LEGACY_CLI_MODULES:
                    violations.append(f"{module}:{node.lineno}: from {imported} import ...")
            elif isinstance(node, ast.Call):
                call_name = _call_name(node.func)
                if call_name in {
                    "runpy.run_module",
                    "argparse.ArgumentParser",
                    "argparse.REMAINDER",
                } or call_name.endswith(".parse_known_args"):
                    violations.append(f"{module}:{node.lineno}: call {call_name}")
            elif (
                isinstance(node, ast.ClassDef)
                and node.name.startswith("Cli")
                and node.name.endswith("Service")
            ):
                violations.append(f"{module}:{node.lineno}: class {node.name}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _looks_like_sql(node.value)
            ):
                violations.append(f"{module}:{node.lineno}: SQL literal")

    assert violations == []


def test_admin_workflow_entrypoint_does_not_read_persistence_stores_directly() -> None:
    source = (ROOT / "core/entrypoints/admin/workflows_definition.py").read_text(encoding="utf-8")

    assert "job_admin_store" not in source
    assert "job_thread_store" not in source
    assert "db.conn" not in source


def _forbidden_import(module: str) -> bool:
    return module in FORBIDDEN_LEGACY_CLI_MODULES or any(
        module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _looks_like_sql(value: str) -> bool:
    return bool(
        re.search(
            r"^\s*(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE)\b",
            value,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    )
