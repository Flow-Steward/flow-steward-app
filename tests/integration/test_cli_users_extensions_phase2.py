"""Phase 2 users/extensions public CLI contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.user_admin.contracts import (
    PublicUserView,
    UserActiveUpdateInput,
    UserAuditContext,
    UserCreateInput,
    UserListView,
    UserMutationResultView,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
    UserUpdateInput,
)

_API_USER_ID = "00000000-0000-0000-0000-000000000101"
_CLI_USER_ID = "00000000-0000-0000-0000-000000000102"


def _clear_user_login_failures(_username: str, *, bl: Any = None) -> None:
    _ = bl


class _Registry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def invoke(self, key: str, value: object, **_kwargs: object) -> dict[str, Any]:
        self.calls.append((key, value))
        if key == "users.list":
            return {"users": [], "total": 0}
        if key.startswith("users."):
            return {"status": "ok", "user": {}}
        if key == "extensions.list":
            return {"extensions": []}
        if key == "extensions.show":
            return {"extension_id": getattr(value, "extension_id", "")}
        if key in {"extensions.enable", "extensions.disable", "extensions.install"}:
            return {"extension": {}, "installed_path": ""}
        if key == "extensions.uninstall":
            return {"extension_id": getattr(value, "extension_id", ""), "state": "uninstalled"}
        if key == "extensions.create":
            return {
                "status": "created",
                "extension_id": getattr(value, "extension_id", ""),
                "path": "extensions/acme",
                "files_created": 1,
            }
        if key in {
            "extensions.validate",
            "extensions.test",
            "extensions.bundle",
            "extensions.lock-deps",
        }:
            return {
                "status": "ok",
                "extension_id": getattr(value, "extension_id", ""),
                "exit_code": 0,
                "output": "",
            }
        raise AssertionError(f"Unexpected key: {key}")

    async def choices(self, _key: str) -> tuple[object, ...]:
        return ()


def _app(registry: _Registry | None = None) -> tuple[object, _Registry]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    registry = registry or _Registry()
    return build_click_app(registry, SimpleNamespace()), registry


def _users_router(
    *,
    query_service: object,
    command_service: object,
    api_error_response: object,
) -> object:
    from core.entrypoints.api.auth_users import create_auth_users_router

    admin = {
        "id": "u_admin",
        "username": "admin@example.com",
        "roles": ["admin"],
        "account_id": "default",
        "account_ids": ["default"],
    }
    return create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: object(),
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda *_args, **_kwargs: dict(admin),
        request_auth_user_payload=lambda _request: dict(admin),
        api_error_response=api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=lambda *_args: None,
        get_user_by_username=lambda *_args: None,
        normalize_requested_roles=lambda roles: list(roles or []),
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: value,
        auth_user_column_exists=lambda *_args: True,
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="authentication required",
        err_database_not_configured="database missing",
        err_auth_tables_not_available="auth tables missing",
        err_admin_role_required="admin required",
        err_local_auth_disabled="local auth disabled",
        err_invalid_json_body="invalid json",
        auth_csrf_cookie_name="csrf",
        user_query_service=query_service,
        user_command_service=command_service,
    )


def test_users_and_extensions_groups_expose_exact_phase2_leaves() -> None:
    app, _registry = _app()
    runner = CliRunner()

    users = runner.invoke(app, ["users", "--help"])
    extensions = runner.invoke(app, ["extensions", "--help"])

    assert users.exit_code == 0, users.output
    assert extensions.exit_code == 0, extensions.output
    for command in (
        "list",
        "create",
        "set-password",
        "force-reset",
        "unlock",
        "set-active",
        "set-roles",
    ):
        assert command in users.output
    assert "issue-bootstrap-token" in users.output
    assert "force-password-reset" not in users.output
    for command in (
        "list",
        "show",
        "install",
        "uninstall",
        "enable",
        "disable",
        "create",
        "validate",
        "test",
        "bundle",
        "lock-deps",
    ):
        assert command in extensions.output


def test_legacy_users_extensions_spellings_are_rejected_without_service_calls() -> None:
    app, registry = _app()
    runner = CliRunner()

    for args in (
        ["ext"],
        ["tooling"],
        ["tooling", "extensions", "validate"],
        ["users", "force-password-reset"],
        ["extensions", "deps"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, (args, result.output)

    assert registry.calls == []


def test_users_api_and_cli_delegate_to_same_application_service_methods() -> None:
    from core.entrypoints.api.auth_users import create_auth_users_router
    from core.entrypoints.cli.platform.service_bindings import users

    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[UserCreateInput, UserAuditContext]] = []

        def create_user(
            self,
            command: UserCreateInput,
            **kwargs: Any,
        ) -> UserMutationResultView:
            self.calls.append((command, kwargs["audit_context"]))
            return UserMutationResultView(
                status="created",
                user=PublicUserView.model_validate(
                    {
                        "id": f"u_{len(self.calls)}",
                        "username": command.email,
                        "email": command.email,
                        "password_hash": "must-not-cross-boundary",
                        "roles": command.roles,
                    }
                ),
            )

    service = RecordingService()
    admin = {
        "id": "u_admin",
        "username": "admin@example.com",
        "roles": ["admin"],
        "account_id": "default",
        "account_ids": ["default"],
    }
    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: object(),
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda *_args, **_kwargs: dict(admin),
        request_auth_user_payload=lambda _request: dict(admin),
        api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=lambda *_args: None,
        get_user_by_username=lambda *_args: None,
        normalize_requested_roles=lambda roles: list(roles or []),
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: value,
        auth_user_column_exists=lambda *_args: True,
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="authentication required",
        err_database_not_configured="database missing",
        err_auth_tables_not_available="auth tables missing",
        err_admin_role_required="admin required",
        err_local_auth_disabled="local auth disabled",
        err_invalid_json_body="invalid json",
        auth_csrf_cookie_name="csrf",
        user_query_service=SimpleNamespace(),
        user_command_service=service,
    )
    app = FastAPI()
    app.include_router(router)
    api_response = TestClient(app).post(
        "/api/auth/users",
        json={
            "email": "api@example.com",
            "password": "Password123!",
            "roles": ["viewer"],
        },
    )

    cli_result = asyncio.run(
        users.users_create(
            SimpleNamespace(user_command_service=service),
            UserCreateInput(
                email="cli@example.com",
                password="Password123!",
                roles=["viewer"],
                actor="operator@example.com",
            ),
        )
    )

    assert api_response.status_code == 200
    assert "password_hash" not in api_response.json()["user"]
    assert "password_hash" not in cli_result.model_dump(mode="json")["user"]
    assert [type(command) for command, _context in service.calls] == [
        UserCreateInput,
        UserCreateInput,
    ]
    assert service.calls[0][1] == UserAuditContext(
        actor="admin@example.com",
        source="api",
    )
    assert service.calls[1][1] == UserAuditContext(
        actor="operator@example.com",
        source="cli",
    )


def test_users_api_keeps_input_validation_at_client_boundary() -> None:
    class NoCallCommandService:
        def __init__(self) -> None:
            self.calls = 0

        def create_user(self, *_args: Any, **_kwargs: Any) -> UserMutationResultView:
            self.calls += 1
            raise AssertionError("invalid transport input must not reach the service")

    safe_errors: list[Exception] = []
    command_service = NoCallCommandService()
    router = _users_router(
        query_service=SimpleNamespace(),
        command_service=command_service,
        api_error_response=lambda exc: (
            safe_errors.append(exc)
            or JSONResponse({"error": "safe internal error"}, status_code=500)
        ),
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).post(
        "/api/auth/users",
        json={"email": "not-an-email", "password": "Password123!"},
    )

    assert response.status_code == 400
    assert "not-an-email" not in str(response.json())
    assert command_service.calls == 0
    assert safe_errors == []


def test_users_api_routes_internal_output_validation_through_safe_500_handler() -> None:
    class InvalidOutputCommandService:
        def create_user(self, *_args: Any, **_kwargs: Any) -> UserMutationResultView:
            return UserMutationResultView.model_validate({"status": "created", "user": {}})

    safe_errors: list[Exception] = []
    router = _users_router(
        query_service=SimpleNamespace(),
        command_service=InvalidOutputCommandService(),
        api_error_response=lambda exc: (
            safe_errors.append(exc)
            or JSONResponse({"error": "safe internal error"}, status_code=500)
        ),
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).post(
        "/api/auth/users",
        json={"email": "valid@example.com", "password": "Password123!"},
    )

    assert response.status_code == 500
    assert response.json() == {"error": "safe internal error"}
    assert len(safe_errors) == 1


def test_users_api_and_cli_share_list_and_existing_user_mutation_services() -> None:
    from core.entrypoints.cli.platform.service_bindings import users

    class RecordingQueryService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def list_users(self, **kwargs: Any) -> UserListView:
            self.calls.append(dict(kwargs))
            return UserListView(users=[], total=0, limit=25, offset=0)

    class RecordingCommandService:
        def __init__(self) -> None:
            self.calls: list[tuple[UserSelectorInput, UserAuditContext]] = []

        def force_password_reset(
            self, command: UserSelectorInput, **kwargs: Any
        ) -> UserMutationResultView:
            self.calls.append((command, kwargs["audit_context"]))
            return UserMutationResultView(
                status="password_reset_required",
                user=PublicUserView(id=str(command.user_id), username="user@example.com"),
                revoked_sessions=1,
            )

    query_service = RecordingQueryService()
    command_service = RecordingCommandService()
    router = _users_router(
        query_service=query_service,
        command_service=command_service,
        api_error_response=lambda _exc: JSONResponse(
            {"error": "safe internal error"}, status_code=500
        ),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    api_list = client.get("/api/auth/users")
    cli_list = asyncio.run(users.users_list(SimpleNamespace(user_query_service=query_service), {}))
    api_reset = client.post(f"/api/auth/users/{_API_USER_ID}/force-password-reset")
    cli_reset = asyncio.run(
        users.users_force_reset(
            SimpleNamespace(user_command_service=command_service),
            UserSelectorInput(user_id=_CLI_USER_ID, actor="operator@example.com"),
        )
    )

    assert api_list.status_code == 200
    assert cli_list.total == 0
    assert api_reset.status_code == 200
    assert cli_reset.status == "password_reset_required"
    assert len(query_service.calls) == 2
    assert len(command_service.calls) == 2
    assert command_service.calls[0][1].source == "api"
    assert command_service.calls[1][1].source == "cli"


def test_users_api_and_cli_cover_every_existing_user_mutation_boundary() -> None:
    from core.entrypoints.cli.platform.service_bindings import users

    class RecordingCommandService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, dict[str, Any]]] = []

        def _record(
            self, method: str, command: object, kwargs: dict[str, Any]
        ) -> UserMutationResultView:
            self.calls.append((method, command, dict(kwargs)))
            return UserMutationResultView(
                status=method,
                user=PublicUserView(
                    id=str(getattr(command, "user_id", "")),
                    username="user@example.com",
                ),
                revoked_sessions=1,
            )

        def update_user(self, command: UserUpdateInput, **kwargs: Any) -> UserMutationResultView:
            return self._record("update_user", command, kwargs)

        def set_password(
            self, command: UserPasswordUpdateInput, **kwargs: Any
        ) -> UserMutationResultView:
            return self._record("set_password", command, kwargs)

        def set_active(
            self, command: UserActiveUpdateInput, **kwargs: Any
        ) -> UserMutationResultView:
            return self._record("set_active", command, kwargs)

        def set_roles(self, command: UserRolesUpdateInput, **kwargs: Any) -> UserMutationResultView:
            return self._record("set_roles", command, kwargs)

        def unlock_user(self, command: UserSelectorInput, **kwargs: Any) -> UserMutationResultView:
            return self._record("unlock_user", command, kwargs)

    command_service = RecordingCommandService()
    router = _users_router(
        query_service=SimpleNamespace(),
        command_service=command_service,
        api_error_response=lambda _exc: JSONResponse(
            {"error": "safe internal error"}, status_code=500
        ),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    runtime = SimpleNamespace(user_command_service=command_service)

    cases = (
        (
            "set_password",
            {"password": "Password123!", "must_change_password": True},
            users.users_set_password,
            UserPasswordUpdateInput(
                user_id=_CLI_USER_ID,
                password="Password123!",
                must_change_password=True,
                actor="operator@example.com",
            ),
            {"password": "Password123!", "must_change_password": True},
        ),
        (
            "set_active",
            {"is_active": False},
            users.users_set_active,
            UserActiveUpdateInput(
                user_id=_CLI_USER_ID,
                is_active=False,
                actor="operator@example.com",
            ),
            {"is_active": False},
        ),
        (
            "set_roles",
            {"roles": ["admin"]},
            users.users_set_roles,
            UserRolesUpdateInput(
                user_id=_CLI_USER_ID,
                roles=["admin"],
                actor="operator@example.com",
            ),
            {"roles": ["admin"]},
        ),
    )

    for method, api_body, cli_binding, cli_command, expected_api_fields in cases:
        api_response = client.put(f"/api/auth/users/{_API_USER_ID}", json=api_body)
        cli_result = asyncio.run(cli_binding(runtime, cli_command))
        api_method, api_command, api_kwargs = command_service.calls[-2]
        cli_method, recorded_cli_command, cli_kwargs = command_service.calls[-1]

        assert api_response.status_code == 200
        assert api_response.json()["user"]["id"] == _API_USER_ID
        assert cli_result.user.id == _CLI_USER_ID
        assert api_method == "update_user"
        assert isinstance(api_command, UserUpdateInput)
        assert api_command.user_id == _API_USER_ID
        for field, expected in expected_api_fields.items():
            assert getattr(api_command, field) == expected
        assert api_kwargs["allowed_account_ids"] == ["default"]
        assert api_kwargs["audit_context"] == UserAuditContext(
            actor="admin@example.com", source="api"
        )
        assert cli_method == method
        assert recorded_cli_command == cli_command
        assert "allowed_account_ids" not in cli_kwargs
        assert cli_kwargs["audit_context"] == UserAuditContext(
            actor="operator@example.com", source="cli"
        )

    api_unlock = client.post(f"/api/auth/users/{_API_USER_ID}/unlock")
    cli_unlock = asyncio.run(
        users.users_unlock(
            runtime,
            UserSelectorInput(user_id=_CLI_USER_ID, actor="operator@example.com"),
        )
    )
    api_method, api_command, api_kwargs = command_service.calls[-2]
    cli_method, cli_command, cli_kwargs = command_service.calls[-1]

    assert api_unlock.status_code == 200
    assert api_unlock.json()["user_id"] == _API_USER_ID
    assert cli_unlock.user.id == _CLI_USER_ID
    assert api_method == cli_method == "unlock_user"
    assert isinstance(api_command, UserSelectorInput)
    assert isinstance(cli_command, UserSelectorInput)
    assert api_kwargs["allowed_account_ids"] == ["default"]
    assert api_kwargs["audit_context"].source == "api"
    assert "allowed_account_ids" not in cli_kwargs
    assert cli_kwargs["audit_context"].source == "cli"
