"""Core's scoped roles seam reaches a platform module end to end, without Docker.

``tests/fixtures/platform_modules/reference_scoped_seams`` is a test-only stub: one
read-only route over ``container.scoped_roles`` behind the ``scoped_roles``
entitlement, a module route rule and a ``container.feature_endpoints`` registration
(ADR 0076 section 8, ADR 0082). It is built with the prototype builder, activated
with the real activator next to the reference entitlements module, and registered
through Core's ``load_platform_modules``. The port is a recording fake written
against the shared contract; the port itself is tested in
``test_scoped_role_ports.py``.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import platform
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.permissions import (
    ALL_PERMISSION_KEYS,
    OPERATOR_PERMISSIONS,
    VIEWER_PERMISSIONS,
)
from core.application.auth.route_permissions import (
    ModuleRoutePermissionRegistry,
    route_allowed,
)
from core.application.release.platform_modules import PreparedModuleRef
from core.application.reports.ui_page_registry import register_core_settings_pages
from core.application.settings.platform_contracts import build_platform_contracts
from core.entrypoints.admin.deps import require_entitlement
from core.entrypoints.api.platform_module_loading import (
    MODULE_SERVICE_ERROR_TYPES,
    load_platform_modules,
)
from core.infrastructure.platform_modules.loader import (
    PreparedPlatformModule,
    activate_prepared_platform_modules,
)
from core.infrastructure.release.platform_module_activator import FilesystemModuleActivator
from core.infrastructure.runtime.app_factory import RuntimeContainer, build_runtime_container
from core.infrastructure.runtime.http_app_lifecycle import _register_core_exception_handlers
from tests.support.platform_module_builder import BuiltModule, build_staged_artifact

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "platform_modules"
SOURCE = FIXTURES / "reference_scoped_seams"
MODULE_ID = "acme.reference-scoped-seams"
BASE = f"/api/modules/{MODULE_ID}/scoped-roles"
GRANTS = f"{BASE}/grants"
FEATURE = "scoped_roles"
ENTITLEMENTS_FILE = "reference-entitlements.json"
MODULE_IMPORT_ROOTS = ("fs_reference_scoped_seams", "fs_reference_entitlements")
CACHE_TAG = sys.implementation.cache_tag
MACHINE = platform.machine()

ROLE_PERMISSIONS = {
    "admin": ALL_PERMISSION_KEYS,
    "operator": OPERATOR_PERMISSIONS,
    "viewer": VIEWER_PERMISSIONS,
}

# --- recording fakes of Core's ports (the shared contract) --------------------------

GRANT = {
    "user_id": "u-1",
    "username": "name-u-1",
    "display_name": "Name u-1",
    "project_id": "p-1",
    "project_name": "Project 1",
    "role": "viewer",
}


class FakeScopedRoles:
    """``container.scoped_roles`` as the contract names it (read side only)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.writes: list[Any] = []

    def list_grants(
        self, account_id: str, /, *, user_id: str | None = None, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        del project_id
        self.calls.append((account_id, user_id))
        return [dict(GRANT)]

    def replace_user_grants(self, *args: Any, **kwargs: Any) -> None:
        self.writes.append((args, kwargs))

    def lift_limit(self, *args: Any, **kwargs: Any) -> None:
        self.writes.append((args, kwargs))


class FakeFeatureEndpoints:
    """``container.feature_endpoints``: records what a module registers."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []

    def register(self, feature: str, base_path: str) -> None:
        self.registered.append((feature, base_path))

    def snapshot(self) -> dict[str, str]:
        return dict(self.registered)


# --- process isolation and building ---------------------------------------------------


def _forget_module_imports() -> None:
    for name in list(sys.modules):
        if name.split(".", 1)[0] in MODULE_IMPORT_ROOTS:
            del sys.modules[name]
    importlib.invalidate_caches()


@pytest.fixture
def isolated_imports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    _forget_module_imports()
    yield
    _forget_module_imports()


def _build(source: Path, root: Path) -> BuiltModule:
    return build_staged_artifact(source, root, python_cache_tag=CACHE_TAG, machine=MACHINE)


def _ref(built: BuiltModule) -> PreparedModuleRef:
    return PreparedModuleRef(id=built.id, version=built.version, digest=built.digest)


class NoRestart:
    def module_roles(self) -> tuple[str, ...]:
        return ("fs-app",)

    def restart_role(self, role: str) -> None:
        del role


def _write_entitlements(root: Path, accounts: dict[str, list[str]]) -> None:
    path = root / ENTITLEMENTS_FILE
    previous = path.stat().st_mtime_ns if path.exists() else 1_700_000_000_000_000_000
    temporary = root / f".{ENTITLEMENTS_FILE}.tmp"
    payload = {
        "accounts": {account: {"features": features} for account, features in accounts.items()},
        "default": {"features": []},
    }
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)
    stamp = previous + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _require_authenticated_user(request: Any, required_roles: Any = None) -> dict[str, Any]:
    del required_roles
    user = getattr(request.state, "auth_user", None)
    if not isinstance(user, dict):
        raise PermissionError("Authentication required")
    return user


def _container(app: FastAPI) -> RuntimeContainer:
    contracts = build_platform_contracts()
    base = build_runtime_container(
        app=app,
        config={},
        auth_helpers={
            "require_authenticated_user": _require_authenticated_user,
            "require_entitlement": require_entitlement,
        },
        service_factories={
            "authorization_service": lambda: contracts["authorization_service"],
            "entitlement_service": lambda: contracts["entitlement_service"],
        },
        registries={
            "settings_pages": {},
            "registered_pages": {},
            "navigation_items": {},
            "platform_contracts": contracts,
            "module_route_permissions": ModuleRoutePermissionRegistry(),
        },
        metadata={"edition": "community"},
    )
    container = dataclasses.replace(
        base, scoped_roles=FakeScopedRoles(), feature_endpoints=FakeFeatureEndpoints()
    )
    register_core_settings_pages(container.registries)
    return container


def _install_test_auth(app: FastAPI, container: RuntimeContainer) -> None:
    """Sign in from headers and apply Core's route decision, module rules included."""

    @app.middleware("http")
    async def _sign_in(request, call_next):
        account = request.headers.get("x-test-account")
        role = request.headers.get("x-test-role", "admin")
        request.state.auth_user = (
            {
                "id": f"id-{role}",
                "username": f"{role}@{account}",
                "account_id": account,
                "roles": [role],
            }
            if account
            else None
        )
        rules = container.registries["module_route_permissions"].rules()
        if account and not route_allowed(
            request.method, request.url.path, ROLE_PERMISSIONS[role], rules
        ):
            return JSONResponse({"error": "route not permitted"}, status_code=403)
        return await call_next(request)


@dataclass
class Stand:
    root: Path
    app: FastAPI
    container: RuntimeContainer
    modules: tuple[PreparedPlatformModule, ...]
    client: TestClient
    loaded: tuple[str, ...] = field(default=())

    @property
    def scoped_roles(self) -> FakeScopedRoles:
        return self.container.scoped_roles

    def get(self, path: str, *, account: str | None, role: str = "admin"):
        headers = {"x-test-account": account, "x-test-role": role} if account else {}
        return self.client.get(path, headers=headers)


@pytest.fixture
def built(tmp_path: Path) -> BuiltModule:
    root = tmp_path / "runtime" / "platform-modules"
    root.mkdir(parents=True)
    return _build(SOURCE, root)


@pytest.fixture
def stand(built: BuiltModule, isolated_imports: None, monkeypatch: pytest.MonkeyPatch) -> Stand:
    root = built.path.parents[2]
    entitlements = _build(FIXTURES / "reference_entitlements", root)
    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(root))
    FilesystemModuleActivator(
        root, restarter=NoRestart(), cache_tag=CACHE_TAG, machine=MACHINE
    ).activate((_ref(entitlements), _ref(built)))
    app = FastAPI()
    container = _container(app)
    app.state.runtime_container = container
    _register_core_exception_handlers(app, service_error_types=MODULE_SERVICE_ERROR_TYPES)
    _install_test_auth(app, container)
    modules = activate_prepared_platform_modules("app", root=root)
    loaded = load_platform_modules(app, container, modules, module_names=())
    return Stand(root, app, container, modules, TestClient(app), loaded)


# --- the seams ------------------------------------------------------------------------


def test_the_stub_loads_through_core_with_one_route_rule_and_no_page(stand: Stand) -> None:
    registrations = {
        record["id"]: record for record in stand.container.metadata["platform_module_registrations"]
    }

    assert "fs_reference_scoped_seams" in stand.loaded
    assert registrations[MODULE_ID] == {
        "id": MODULE_ID,
        "version": "1.0.0",
        "pages": [],
        "route_rules": 1,
    }


def test_the_module_registers_its_endpoint_with_core(stand: Stand) -> None:
    assert stand.container.feature_endpoints.registered == [(FEATURE, BASE)]


def test_the_route_is_refused_without_the_entitlement(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [], "acc-b": [FEATURE]})

    refused = stand.get(GRANTS, account="acc-a")

    assert refused.status_code == 403
    assert refused.json()["code"] == "feature_not_entitled"
    assert refused.json()["details"] == {"feature": FEATURE}
    assert stand.get(GRANTS, account=None).status_code == 401
    assert stand.scoped_roles.calls == []


def test_the_port_gets_the_principals_account_never_the_requests(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [FEATURE]})

    response = stand.get(f"{GRANTS}?account_id=acc-b&user_id=u-1", account="acc-a")

    assert response.status_code == 200
    assert response.json() == {"grants": [GRANT]}
    assert stand.scoped_roles.calls == [("acc-a", "u-1")]


def test_cores_route_rule_applies_to_the_module_route(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [FEATURE]})

    for role in ("viewer", "operator"):
        assert stand.get(GRANTS, account="acc-a", role=role).status_code == 403
    assert stand.client.put(
        GRANTS,
        headers={"x-test-account": "acc-a"},
        json={"user_id": "u-1", "grants": [{"project_id": "p-1", "role": "viewer"}]},
    ).status_code in {403, 405}
    assert stand.scoped_roles.calls == []
    assert stand.scoped_roles.writes == []
