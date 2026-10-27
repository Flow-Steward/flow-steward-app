from __future__ import annotations

import ast
import inspect
from pathlib import Path

from core.application.extensions.binding_service import ExtensionBindingService


def test_extension_binding_service_is_composed_in_the_owning_package() -> None:
    source = inspect.getsource(ExtensionBindingService)

    assert "Mixin" not in source
    assert "self._archive_install_service" in source
    assert "self._uninstall_service" in source
    assert "self._state_service" in source
    assert "self._operation_service" in source


def test_legacy_extension_binding_modules_are_removed() -> None:
    management_root = Path(__file__).parents[2] / "core" / "application" / "management"

    assert not (management_root / "command_services_extension.py").exists()
    assert not (management_root / "command_services_extension_core_mixin.py").exists()
    assert not (management_root / "command_services_extension_ops_mixin.py").exists()


def test_extension_services_expose_a_static_typed_dependency_graph() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    extracted_modules = (
        "archive_install_service.py",
        "archive_new_install_service.py",
        "archive_update_service.py",
        "binding_cleanup_service.py",
        "binding_context.py",
        "binding_projection_service.py",
        "extension_state_service.py",
        "managed_release_service.py",
        "operation_service.py",
        "uninstall_plan_service.py",
        "uninstall_service.py",
    )

    for module_name in extracted_modules:
        source = (extensions_root / module_name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "# mypy: ignore-errors" not in source, module_name
        assert not any(
            isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
            for node in ast.walk(tree)
        ), module_name
        service_classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and (node.name.endswith("Service") or node.name == "ExtensionLifecycleContext")
        ]
        assert service_classes, module_name
        for service_class in service_classes:
            constructor = next(
                (
                    node
                    for node in service_class.body
                    if isinstance(node, ast.FunctionDef) and node.name == "__init__"
                ),
                None,
            )
            assert constructor is not None, (module_name, service_class.name)
            assert not constructor.args.args[1:], (module_name, service_class.name)
            assert constructor.args.kwonlyargs, (module_name, service_class.name)
            assert all(arg.annotation is not None for arg in constructor.args.kwonlyargs), (
                module_name,
                service_class.name,
            )

        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "core.application.management.command_services_common"
            for node in ast.walk(tree)
        ), module_name

    wiring_source = (extensions_root / "binding_service.py").read_text(encoding="utf-8")
    assert not (extensions_root / "binding_component.py").exists()
    forbidden_dynamic_routing = ("__getattr__", "__getattribute__", "__mro__")
    for marker in forbidden_dynamic_routing:
        assert marker not in wiring_source
    assert "ExtensionBindingComponent" not in wiring_source


def test_production_extension_services_do_not_route_through_compatibility_facade() -> None:
    root = Path(__file__).parents[2]
    extensions_root = root / "core" / "application" / "extensions"
    binding_source = (extensions_root / "binding_context.py").read_text(encoding="utf-8")
    binding_service_source = (extensions_root / "binding_service.py").read_text(encoding="utf-8")

    retired_legacy_modules = (
        "extension_services.py",
        "extension_services_common.py",
        "extension_services_command_core.py",
        "extension_services_command_install.py",
        "extension_services_command_lifecycle.py",
        "extension_services_query_list.py",
        "extension_services_query_marketplace.py",
        "extension_services_query_detail.py",
    )
    assert [name for name in retired_legacy_modules if (extensions_root / name).exists()] == []
    assert "core.application.extensions.extension_services" not in binding_source
    assert "core.application.extensions.command_service" in binding_source
    assert "extension_query_service_factory" in binding_source
    assert "ExtensionQueryService(" not in binding_source
    assert "ExtensionQueryService(" not in binding_service_source

    for module_name, class_name in (
        ("command_service.py", "ExtensionCommandService"),
        ("query_service.py", "ExtensionQueryService"),
    ):
        source = (extensions_root / module_name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        service = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        assert "# mypy: ignore-errors" not in source, module_name
        assert not any(
            isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
            for node in ast.walk(tree)
        ), module_name
        assert not any(
            isinstance(base, ast.Name) and base.id.endswith("Mixin") for base in service.bases
        ), module_name

    facade_imports = []
    for path in (root / "core").rglob("*.py"):
        if "core.application.extensions.extension_services" in path.read_text(encoding="utf-8"):
            facade_imports.append(str(path.relative_to(root)))
    assert facade_imports == []


def test_extension_application_package_owns_lifecycle_policies() -> None:
    root = Path(__file__).parents[2]
    extensions_root = root / "core" / "application" / "extensions"
    private_management_imports = []

    for path in extensions_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "core.application.management.command_services_common" in source:
            private_management_imports.append(str(path.relative_to(root)))

    assert private_management_imports == []


def test_extension_facade_does_not_own_private_component_dependencies() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    wiring_tree = ast.parse((extensions_root / "binding_service.py").read_text(encoding="utf-8"))
    facade = next(
        node
        for node in wiring_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExtensionBindingService"
    )
    service_modules = (
        "archive_install_service.py",
        "archive_new_install_service.py",
        "archive_update_service.py",
        "binding_cleanup_service.py",
        "binding_context.py",
        "binding_projection_service.py",
        "extension_state_service.py",
        "managed_release_service.py",
        "operation_service.py",
        "uninstall_plan_service.py",
        "uninstall_service.py",
    )

    assert not any(
        isinstance(node, ast.Call)
        and any(isinstance(argument, ast.Name) and argument.id == "self" for argument in node.args)
        for node in ast.walk(wiring_tree)
    )
    assert {
        node.name
        for node in facade.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_")
    } == {"__init__"}
    assert not any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Attribute)
        and isinstance(node.value.value.value, ast.Name)
        and node.value.value.value.id == "self"
        and node.value.attr.startswith("_")
        for node in ast.walk(wiring_tree)
    )
    for module_name in service_modules:
        source = (extensions_root / module_name).read_text(encoding="utf-8")
        assert "ExtensionBindingComponent" not in source, module_name
        assert "Callable[..., Any]" not in source, module_name


def test_extension_components_collaborate_through_public_typed_ports() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    service_modules = sorted(
        {
            *(path.name for path in extensions_root.glob("command_*.py")),
            *(path.name for path in extensions_root.glob("query_*.py")),
            *(path.name for path in extensions_root.glob("uninstall_*.py")),
            "archive_install_service.py",
            "archive_new_install_service.py",
            "archive_update_service.py",
            "binding_cleanup_service.py",
            "binding_projection_service.py",
            "extension_state_service.py",
            "managed_release_service.py",
        }
    )

    for module_name in service_modules:
        tree = ast.parse((extensions_root / module_name).read_text(encoding="utf-8"))
        private_component_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("_")
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.value.attr.startswith("_")
        ]
        assert not private_component_calls, module_name

    context_tree = ast.parse((extensions_root / "binding_context.py").read_text(encoding="utf-8"))
    context_class = next(
        node
        for node in context_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExtensionLifecycleContext"
    )
    ports = {
        node.name: node
        for node in context_class.body
        if isinstance(node, ast.FunctionDef) and node.name in {"command_service", "query_service"}
    }
    assert set(ports) == {"command_service", "query_service"}
    for method in ports.values():
        assert method.returns is not None
        assert not (isinstance(method.returns, ast.Name) and method.returns.id == "Any")


def test_extension_public_collaborator_methods_preserve_explicit_signatures() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    service_modules = (
        "archive_new_install_service.py",
        "binding_cleanup_service.py",
        "extension_state_service.py",
        "managed_release_service.py",
        "uninstall_plan_service.py",
    )

    for module_name in service_modules:
        tree = ast.parse((extensions_root / module_name).read_text(encoding="utf-8"))
        for service_class in (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name.endswith("Service")
        ):
            for method in (
                node
                for node in service_class.body
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
            ):
                assert method.args.vararg is None, (module_name, method.name)
                assert method.args.kwarg is None, (module_name, method.name)


def test_extension_command_adapter_is_retired_from_clean_cutover() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    offenders = []
    for path in extensions_root.glob("*.py"):
        if "ExtensionCommandServiceAdapter" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []


def test_canonical_extension_graph_has_strict_function_annotations() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    graph_modules = sorted(
        {
            *extensions_root.glob("command_*.py"),
            *extensions_root.glob("query_*.py"),
            *extensions_root.glob("uninstall_*.py"),
            extensions_root / "binding_context.py",
            extensions_root / "lifecycle_ports.py",
            extensions_root / "service_support.py",
        }
    )
    missing: list[tuple[str, str, str]] = []
    for path in graph_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "__init__":
                continue
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            untyped = [
                argument.arg
                for argument in arguments
                if argument.arg not in {"self", "cls"} and argument.annotation is None
            ]
            if node.args.vararg is not None and node.args.vararg.annotation is None:
                untyped.append(f"*{node.args.vararg.arg}")
            if node.args.kwarg is not None and node.args.kwarg.annotation is None:
                untyped.append(f"**{node.args.kwarg.arg}")
            if untyped:
                missing.append((path.name, node.name, f"arguments: {', '.join(untyped)}"))
            if node.returns is None:
                missing.append((path.name, node.name, "return"))
    assert missing == []


def test_extension_operation_event_routing_is_injected_without_runtime_private_lookup() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    support_source = (extensions_root / "operation_support.py").read_text(encoding="utf-8")
    operation_source = (extensions_root / "operation_service.py").read_text(encoding="utf-8")

    assert "sys.modules" not in support_source
    assert "core.infrastructure.runtime" not in support_source
    assert "http_app" not in support_source
    assert "ExtensionEventCommandServiceFactory" in operation_source


def test_extension_orchestrators_are_split_into_reviewable_steps() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    limits = {
        ("uninstall_plan_service.py", "preview_extension_uninstall"): 120,
        ("uninstall_service.py", "_uninstall_extension_locked"): 140,
        ("operation_service.py", "execute_action"): 120,
        ("archive_update_service.py", "update_simple_extension_bundle"): 120,
        ("archive_update_models.py", "rollback_update"): 100,
    }

    for (module_name, function_name), line_limit in limits.items():
        tree = ast.parse((extensions_root / module_name).read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        )
        assert function.end_lineno is not None
        assert function.end_lineno - function.lineno + 1 <= line_limit, (
            module_name,
            function_name,
            function.end_lineno - function.lineno + 1,
        )


def test_actual_archive_and_uninstall_workflows_are_reviewable_module_wide() -> None:
    extensions_root = Path(__file__).parents[2] / "core" / "application" / "extensions"
    workflow_modules = sorted(
        path.name
        for path in extensions_root.glob("*.py")
        if path.name.startswith(("archive_", "uninstall_"))
    )

    for module_name in workflow_modules:
        path = extensions_root / module_name
        assert path.exists(), module_name
        source = path.read_text(encoding="utf-8")
        assert len(source.splitlines()) <= 500, module_name
        assert "# mypy: ignore-errors" not in source, module_name
        tree = ast.parse(source)
        oversized = {
            node.name: node.end_lineno - node.lineno + 1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.end_lineno is not None
            and node.end_lineno - node.lineno + 1 > 80
        }
        assert oversized == {}, (module_name, oversized)


def test_extension_archive_update_has_typed_phase_and_rollback_ownership() -> None:
    service_source = (
        Path(__file__).parents[2]
        / "core"
        / "application"
        / "extensions"
        / "archive_update_service.py"
    ).read_text(encoding="utf-8")
    models_source = (
        Path(__file__).parents[2]
        / "core"
        / "application"
        / "extensions"
        / "archive_update_models.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(models_source)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    for class_name in {"UpdateRequest", "PreparedUpdate", "UpdateRollbackState"}:
        assert class_name in classes
        assert any(
            (isinstance(decorator, ast.Name) and decorator.id == "dataclass")
            or (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "dataclass"
            )
            for decorator in classes[class_name].decorator_list
        )

    rollback_owner = classes["UpdateRollbackState"]
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "rollback_update"
        for node in rollback_owner.body
    )
    assert "UpdateRollbackState" in service_source
