from __future__ import annotations

import ast
from pathlib import Path

from tests.support.source_index import parse_file


def _source_package(path: Path) -> str:
    path_parts = path.resolve().parts
    try:
        core_index = path_parts.index("core")
    except ValueError:
        return ""
    return ".".join(path_parts[core_index:-1])


def _resolve_import_from_module(path: Path, node: ast.ImportFrom) -> str:
    module = str(node.module or "")
    if node.level == 0:
        return module

    package_parts = _source_package(path).split(".")
    parent_depth = node.level - 1
    if not package_parts or parent_depth >= len(package_parts):
        return module

    base_parts = package_parts[:-parent_depth] if parent_depth else package_parts
    return ".".join([*base_parts, *module.split(".")]) if module else ".".join(base_parts)


def _imports_under(path: Path, package: str) -> list[str]:
    def _is_package_or_descendant(target: str) -> bool:
        return target == package or target.startswith(f"{package}.")

    imported: list[str] = []
    for node in parse_file(path).nodes:
        if isinstance(node, ast.Import):
            imported.extend(
                alias.name for alias in node.names if _is_package_or_descendant(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from_module(path, node)
            if _is_package_or_descendant(module):
                imported.append(module)
                continue
            for alias in node.names:
                imported_target = f"{module}.{alias.name}" if module else alias.name
                if _is_package_or_descendant(imported_target):
                    imported.append(imported_target)
    return imported


def _relative_infrastructure_source(tmp_path: Path, name: str, statement: str) -> Path:
    source = tmp_path / "core" / "infrastructure" / "runtime" / f"{name}.py"
    source.parent.mkdir(parents=True)
    source.write_text(statement, encoding="utf-8")
    return source


def test_imports_under_detects_policy_module_imported_from_parent(tmp_path: Path) -> None:
    source = tmp_path / "aliased_policy_import.py"
    source.write_text(
        "from core.application.extensions import extension_scan_lifecycle as lifecycle\n",
        encoding="utf-8",
    )

    assert _imports_under(source, "core.application.extensions.extension_scan_lifecycle") == [
        "core.application.extensions.extension_scan_lifecycle"
    ]


def test_imports_under_ignores_unrelated_parent_package_symbol(tmp_path: Path) -> None:
    source = tmp_path / "unrelated_parent_symbol.py"
    source.write_text(
        "from core.application.extensions import extension_scan_lifecycle_settings\n",
        encoding="utf-8",
    )

    assert _imports_under(source, "core.application.extensions.extension_scan_lifecycle") == []


def test_imports_under_detects_relative_policy_module_imported_from_parent(
    tmp_path: Path,
) -> None:
    source = _relative_infrastructure_source(
        tmp_path,
        "relative_policy_parent",
        "from ...application.extensions import extension_scan_lifecycle as lifecycle\n",
    )

    assert _imports_under(source, "core.application.extensions.extension_scan_lifecycle") == [
        "core.application.extensions.extension_scan_lifecycle"
    ]


def test_imports_under_detects_relative_policy_submodule_import(tmp_path: Path) -> None:
    source = _relative_infrastructure_source(
        tmp_path,
        "relative_policy_submodule",
        "from ...application.extensions.extension_scan_lifecycle "
        "import sync_extension_scan_with_llm_lifecycle\n",
    )

    assert _imports_under(source, "core.application.extensions.extension_scan_lifecycle") == [
        "core.application.extensions.extension_scan_lifecycle"
    ]


def test_imports_under_ignores_relative_unrelated_parent_package_symbol(tmp_path: Path) -> None:
    source = _relative_infrastructure_source(
        tmp_path,
        "relative_unrelated_parent_symbol",
        "from ...application.extensions import extension_scan_lifecycle_settings\n",
    )

    assert _imports_under(source, "core.application.extensions.extension_scan_lifecycle") == []


def test_extension_lifecycle_policy_is_composed_by_app_scoped_service() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    offenders = {
        str(path.relative_to(repo_root)): _imports_under(
            path, "core.application.extensions.extension_scan_lifecycle"
        )
        for path in (repo_root / "core/infrastructure").rglob("*.py")
        if _imports_under(path, "core.application.extensions.extension_scan_lifecycle")
    }

    assert offenders == {}
    service = repo_root / "core/application/extensions/extension_state_service.py"
    policy_imports = _imports_under(service, "core.application.extensions.extension_scan_lifecycle")
    assert set(policy_imports) == {"core.application.extensions.extension_scan_lifecycle"}

    entrypoint = repo_root / "core/entrypoints/api/app_factory.py"
    entrypoint_source = entrypoint.read_text(encoding="utf-8")
    assert "build_extension_binding_service_factory" in entrypoint_source
    assert '"extension_binding_service"' in entrypoint_source


def test_application_lifecycle_coordinator_has_no_infrastructure_imports() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    coordinator = repo_root / "core/application/extensions/extension_scan_lifecycle.py"

    assert _imports_under(coordinator, "core.infrastructure") == []


def test_infrastructure_startup_uses_injected_lifecycle_callback() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    startup_runtime = repo_root / "core/infrastructure/runtime/app_lifecycle_runtime.py"

    assert (
        _imports_under(startup_runtime, "core.application.extensions.extension_scan_lifecycle")
        == []
    )
    source = startup_runtime.read_text(encoding="utf-8")
    assert "sync_extension_scan=" in source


def test_application_owns_extension_scan_llm_lifecycle_coordination() -> None:
    from core.application.extensions.extension_scan_lifecycle import (
        sync_extension_scan_with_llm_lifecycle,
    )

    assert callable(sync_extension_scan_with_llm_lifecycle)


def test_release_membership_lock_identity_contract_is_application_owned() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    install_repository = repo_root / "core/application/extensions/install.py"
    postgres_lock_adapter = (
        repo_root / "core/infrastructure/db/extension_release_membership_lock.py"
    )

    assert (
        _imports_under(
            install_repository,
            "core.infrastructure.db.extension_release_membership_lock",
        )
        == []
    )
    assert _imports_under(
        postgres_lock_adapter,
        "core.application.extensions.release_membership_lock",
    ) == ["core.application.extensions.release_membership_lock"]


def test_model_catalog_uses_application_owned_persistence_repository() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    model_catalog = repo_root / "core/application/settings/model_catalog.py"

    assert _imports_under(model_catalog, "core.infrastructure.db.model_provider_store") == []
