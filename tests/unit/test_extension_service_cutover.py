from __future__ import annotations

import ast
from pathlib import Path

from tests.support.source_index import ParsedSource, parse_file

ROOT = Path(__file__).parents[2]
EXTENSIONS_ROOT = ROOT / "core" / "application" / "extensions"


def _imports_module(parsed: ParsedSource, module_name: str) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == module_name
        for node in parsed.nodes_of(ast.ImportFrom)
    )


def test_production_extension_service_constructors_are_static_and_typed() -> None:
    component_modules = sorted(
        {
            path.name
            for pattern in ("command_*.py", "query_*.py")
            for path in EXTENSIONS_ROOT.glob(pattern)
        }
        | {"service_support.py"}
    )
    for module_name in component_modules:
        source = (EXTENSIONS_ROOT / module_name).read_text(encoding="utf-8")
        tree = ast.parse(source)

        assert len(source.splitlines()) <= 500, module_name
        oversized = {
            node.name: node.end_lineno - node.lineno + 1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.end_lineno is not None
            and node.end_lineno - node.lineno + 1 > 80
        }
        assert oversized == {}, (module_name, oversized)

        assert "# mypy: ignore-errors" not in source, module_name
        assert not any(
            isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
            for node in ast.walk(tree)
        ), module_name

        assert not any(
            isinstance(node, ast.ClassDef) and node.name.endswith("Mixin") for node in tree.body
        ), module_name

        local_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        imported_classes = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not any(
            isinstance(node, ast.ClassDef)
            and any(
                isinstance(base, ast.Name)
                and base.id in imported_classes
                and base.id not in local_classes
                and base.id not in {"Protocol", "ABC", "HTTPRedirectHandler"}
                for base in node.bases
            )
            for node in tree.body
        ), f"cross-module implementation inheritance: {module_name}"

        support_imports = [
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "service_support"
            for alias in node.names
        ]
        assert all(name.startswith("_") for name in support_imports), (
            module_name,
            support_imports,
        )

    support_source = (EXTENSIONS_ROOT / "service_support.py").read_text(encoding="utf-8")
    assert "__all__ = [name for name in globals()" not in support_source
    assert "# ruff: noqa: F401" not in support_source

    for module_name in ("command_service.py", "query_service.py"):
        tree = ast.parse((EXTENSIONS_ROOT / module_name).read_text(encoding="utf-8"))
        service_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name.endswith("Service")
        )
        constructor = next(
            node
            for node in service_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        assert constructor.returns is not None, module_name
        assert all(arg.annotation is not None for arg in constructor.args.kwonlyargs), module_name


def test_first_party_core_consumers_do_not_construct_services_from_compatibility_facade() -> None:
    compatibility_module = "core.application.extensions.extension_services"
    offenders: list[str] = []

    for path in (ROOT / "core").rglob("*.py"):
        if path == EXTENSIONS_ROOT / "extension_services.py":
            continue
        parsed = parse_file(path)
        if _imports_module(parsed, compatibility_module) or any(
            isinstance(node, ast.ImportFrom)
            and node.module == "core.application.extensions"
            and any(alias.name == "extension_services" for alias in node.names)
            for node in parsed.nodes_of(ast.ImportFrom)
        ):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
    assert not (EXTENSIONS_ROOT / "extension_services.py").exists()


def test_extension_code_uses_public_lifecycle_policies() -> None:
    private_module = "core.application.management.command_services_common"
    offenders = []

    for path in EXTENSIONS_ROOT.rglob("*.py"):
        if _imports_module(parse_file(path), private_module):
            offenders.append(path.name)

    assert offenders == []


def test_retired_management_query_service_and_test_only_command_proxies_are_absent() -> None:
    management_root = ROOT / "core" / "application" / "management"
    assert not (management_root / "query_services_extension.py").exists()

    command_tree = ast.parse((EXTENSIONS_ROOT / "command_service.py").read_text(encoding="utf-8"))
    command_service = next(
        node
        for node in command_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExtensionCommandService"
    )
    retired_proxies = {
        "_install_record_payload",
        "_materialize_dataset_execution_bridges",
        "_rollback_install_release_failure",
        "_remove_dataset_execution_bridges",
        "_cleanup_release_if_unreferenced",
        "_cleanup_release_artifacts",
    }
    assert (
        not {node.name for node in command_service.body if isinstance(node, ast.FunctionDef)}
        & retired_proxies
    )


def test_query_components_depend_on_narrow_typed_ports() -> None:
    component_modules = (
        "query_detail.py",
        "query_detail_projection.py",
        "query_marketplace_data.py",
    )
    for module_name in component_modules:
        source = (EXTENSIONS_ROOT / module_name).read_text(encoding="utf-8")
        assert "ExtensionQueryList" not in source, module_name

    ports_tree = ast.parse((EXTENSIONS_ROOT / "query_ports.py").read_text(encoding="utf-8"))
    for protocol in (
        node
        for node in ports_tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
    ):
        for method in (node for node in protocol.body if isinstance(node, ast.FunctionDef)):
            assert method.returns is not None, (protocol.name, method.name)
            assert not (isinstance(method.returns, ast.Name) and method.returns.id == "Any"), (
                protocol.name,
                method.name,
            )

    list_tree = ast.parse((EXTENSIONS_ROOT / "query_list.py").read_text(encoding="utf-8"))
    list_class = next(
        node
        for node in list_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExtensionQueryList"
    )
    for method_name in {"logger", "install_store", "runtime_store"}:
        method = next(
            node
            for node in list_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        assert method.returns is not None
        assert not (isinstance(method.returns, ast.Name) and method.returns.id == "Any"), (
            method_name
        )


def test_extension_query_service_has_no_local_composition_roots() -> None:
    for relative_path in (
        "core/entrypoints/api/integrations_connectors.py",
        "core/entrypoints/api/integrations_extension_routes.py",
        "core/entrypoints/api/integrations_mcp_routes.py",
        "core/entrypoints/api/integrations_request_helpers.py",
        "core/entrypoints/api/route_bindings.py",
        "core/entrypoints/cli/runtime.py",
        "core/application/extensions/binding_context.py",
        "core/application/management/query_service_runtime.py",
        "core/infrastructure/runtime/http_app_request_helpers.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "ExtensionQueryService(" not in source, relative_path

    management_runtime = (ROOT / "core/application/management/query_service_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "def create_extension_query_service(" not in management_runtime
    assert (
        "from core.application.extensions.query_service import create_extension_query_service"
    ) in management_runtime


def test_extension_lifecycle_routes_use_the_identity_store_connection() -> None:
    """Extension stores are composed on the identity-store transaction authority."""
    route_tree = ast.parse(
        (ROOT / "core/entrypoints/api/route_bindings.py").read_text(encoding="utf-8")
    )
    router_call = next(
        node
        for node in ast.walk(route_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_integrations_connectors_router"
    )
    logger_keyword = next(
        keyword for keyword in router_call.keywords if keyword.arg == "get_logger"
    )

    assert isinstance(logger_keyword.value, ast.Name)
    assert logger_keyword.value.id == "get_identity_store"


def test_project_connection_routes_use_the_integration_store_connection() -> None:
    """Project connections need project, connection, and extension methods together."""
    route_tree = ast.parse(
        (ROOT / "core/entrypoints/api/route_bindings.py").read_text(encoding="utf-8")
    )
    router_call = next(
        node
        for node in ast.walk(route_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_integrations_connectors_router"
    )
    integration_keyword = next(
        keyword for keyword in router_call.keywords if keyword.arg == "get_integration_store"
    )

    assert isinstance(integration_keyword.value, ast.Name)
    assert integration_keyword.value.id == "get_integration_store"


def test_notification_dispatch_uses_an_integration_store_command_service() -> None:
    route_tree = ast.parse(
        (ROOT / "core/entrypoints/api/route_bindings.py").read_text(encoding="utf-8")
    )
    router_call = next(
        node
        for node in ast.walk(route_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_project_integrations_router"
    )
    service_keyword = next(
        keyword for keyword in router_call.keywords if keyword.arg == "notification_command_service"
    )
    logger_keyword = next(
        keyword
        for keyword in ast.walk(service_keyword.value)
        if isinstance(keyword, ast.keyword) and keyword.arg == "get_logger"
    )

    assert isinstance(logger_keyword.value, ast.Name)
    assert logger_keyword.value.id == "get_integration_store"
