"""Architecture checks for the first-release user-admin clean cutover."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, get_type_hints

ROOT = Path(__file__).resolve().parents[2]


def test_user_admin_is_owned_by_focused_auth_package_without_compatibility_modules() -> None:
    package = ROOT / "core" / "application" / "auth" / "user_admin"
    expected_modules = {
        "__init__.py",
        "command_service.py",
        "contracts.py",
        "notifications.py",
        "policies.py",
        "projection.py",
        "query_service.py",
        "repository.py",
    }

    assert package.is_dir()
    assert {path.name for path in package.glob("*.py")} == expected_modules
    assert not (ROOT / "core/application/auth/user_admin_repository.py").exists()
    assert not (ROOT / "core/application/management/user_admin_contracts.py").exists()
    assert not (ROOT / "core/application/management/user_admin_services.py").exists()

    from core.application.auth.user_admin import contracts

    assert not hasattr(contracts, "UserRecordView")


def test_auth_users_router_drops_obsolete_repository_dependencies() -> None:
    from core.entrypoints.api.auth_route_bindings import register_auth_routes
    from core.entrypoints.api.auth_users import create_auth_users_router

    router_parameters = inspect.signature(create_auth_users_router).parameters
    registration_parameters = inspect.signature(register_auth_routes).parameters

    for parameters in (router_parameters, registration_parameters):
        assert "sql_from_users" not in parameters
        assert "sql_delete_user_roles_by_user_id" not in parameters
        assert "err_user_not_found" not in parameters

    assert "sql_user_username_column" not in router_parameters
    assert "sql_update_user_password_by_id" not in router_parameters
    assert "auth_password_min_length" not in router_parameters
    assert "err_username_required" not in router_parameters


def test_dead_auth_route_constants_are_removed() -> None:
    from core.infrastructure.runtime import http_app_constants

    assert not hasattr(http_app_constants, "SQL_DELETE_USER_ROLES_BY_USER_ID")
    assert not hasattr(http_app_constants, "ERR_USER_NOT_FOUND")


def test_user_admin_repository_has_no_legacy_schema_fallback() -> None:
    source = (ROOT / "core/application/auth/user_admin/repository.py").read_text(encoding="utf-8")

    assert "auth_user_column_exists_fn" not in source
    assert "user_account_membership_tables_available" not in source


def test_user_command_service_constructor_has_only_typed_live_dependencies() -> None:
    from core.application.auth.user_admin.command_service import UserAdminCommandService

    signature = inspect.signature(UserAdminCommandService.__init__)

    assert "auth_user_column_exists_fn" not in signature.parameters
    assert signature.return_annotation is not inspect.Signature.empty
    assert all(
        parameter.annotation is not inspect.Parameter.empty
        for name, parameter in signature.parameters.items()
        if name != "self"
    )


def test_user_query_service_constructor_has_only_typed_live_dependencies() -> None:
    from core.application.auth.user_admin.query_service import UserAdminQueryService

    signature = inspect.signature(UserAdminQueryService.__init__)
    type_hints = get_type_hints(UserAdminQueryService.__init__)

    assert "auth_user_column_exists_fn" not in signature.parameters
    assert signature.return_annotation is not inspect.Signature.empty
    assert all(
        parameter.annotation is not inspect.Parameter.empty
        for name, parameter in signature.parameters.items()
        if name != "self"
    )
    assert all(annotation is not Any for name, annotation in type_hints.items() if name != "return")
