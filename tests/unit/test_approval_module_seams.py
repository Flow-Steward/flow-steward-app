"""Core's approval seams reach a platform module end to end, without Docker (ADR 0082).

``tests/fixtures/platform_modules/reference_approval_seams`` is a test-only stub: one
read-only route over ``container.approval_assignments`` behind the
``approval_assignees`` entitlement, a module route rule and a
``container.feature_endpoints`` registration. It is built with the prototype
builder, activated with the real activator next to the reference entitlements
module, and registered through Core's ``load_platform_modules``. The ports are
recording fakes written against the shared contract; the ports themselves are
tested in ``test_approval_assignees_read_side.py``.
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
SOURCE = FIXTURES / "reference_approval_seams"
MODULE_ID = "acme.reference-approval-seams"
STEPS = f"/api/modules/{MODULE_ID}/approval-steps"
APPROVERS = f"{STEPS}/wf-1/review/approvers"
ASSIGNEES = "approval_assignees"
ENTITLEMENTS_FILE = "reference-entitlements.json"
MODULE_IMPORT_ROOTS = ("fs_reference_approval_seams", "fs_reference_entitlements")
CACHE_TAG = sys.implementation.cache_tag
MACHINE = platform.machine()

ROLE_PERMISSIONS = {
    "admin": ALL_PERMISSION_KEYS,
    "operator": OPERATOR_PERMISSIONS,
    "viewer": VIEWER_PERMISSIONS,
}

# --- recording fakes of Core's ports (the shared contract) --------------------------


class FakeApprovalAssignments:
    """``container.approval_assignments`` as the contract names it (read side only)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def list_approvers(
        self, workflow_id: str, step_id: str, *, account_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append((workflow_id, step_id, account_id))
        if workflow_id == "wf-foreign":
            raise LookupError("Workflow 'wf-foreign' was not found")
        return [{"user_id": "u-1", "username": "name-u-1", "display_name": "Name u-1"}]


class FakeFeatureEndpoints:
    """``container.feature_endpoints``: records what a module registers."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []

    def register(self, feature: str, base_path: str) -> None:
        self.registered.append((feature, base_path))

    def snapshot(self) -> dict[str, str]:
        return dict(self.registered)


@dataclass
class PortsContainer(RuntimeContainer):
    """The runtime container with the two ports the stub reads."""

    approval_assignments: Any = None
    feature_endpoints: Any = None


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


def _container(app: FastAPI) -> PortsContainer:
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
    values = {item.name: getattr(base, item.name) for item in dataclasses.fields(base)}
    values.update(
        approval_assignments=FakeApprovalAssignments(),
        feature_endpoints=FakeFeatureEndpoints(),
    )
    container = PortsContainer(**values)
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
    container: PortsContainer
    modules: tuple[PreparedPlatformModule, ...]
    client: TestClient
    loaded: tuple[str, ...] = field(default=())

    @property
    def assignments(self) -> FakeApprovalAssignments:
        return self.container.approval_assignments

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

    assert "fs_reference_approval_seams" in stand.loaded
    assert registrations[MODULE_ID] == {
        "id": MODULE_ID,
        "version": "1.0.0",
        "pages": [],
        "route_rules": 1,
    }


def test_the_module_registers_its_endpoint_with_core(stand: Stand) -> None:
    assert stand.container.feature_endpoints.registered == [(ASSIGNEES, STEPS)]


def test_the_route_is_refused_without_the_entitlement(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [], "acc-b": [ASSIGNEES]})

    refused = stand.get(APPROVERS, account="acc-a")

    assert refused.status_code == 403
    assert refused.json()["code"] == "feature_not_entitled"
    assert refused.json()["details"] == {"feature": ASSIGNEES}
    assert stand.get(APPROVERS, account=None).status_code == 401
    assert stand.assignments.calls == []


def test_the_port_gets_the_principals_account_never_the_requests(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [ASSIGNEES]})

    own = stand.get(f"{APPROVERS}?account_id=acc-b", account="acc-a")
    foreign = stand.get(f"{STEPS}/wf-foreign/review/approvers", account="acc-a")

    assert own.status_code == 200
    assert own.json() == {
        "approvers": [{"user_id": "u-1", "username": "name-u-1", "display_name": "Name u-1"}]
    }
    assert foreign.status_code == 404
    assert stand.assignments.calls == [
        ("wf-1", "review", "acc-a"),
        ("wf-foreign", "review", "acc-a"),
    ]


def test_cores_route_rule_applies_to_the_module_route(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [ASSIGNEES]})

    for role in ("viewer", "operator"):
        assert stand.get(APPROVERS, account="acc-a", role=role).status_code == 403
    assert stand.client.put(
        APPROVERS, headers={"x-test-account": "acc-a"}, json={"user_ids": ["u-1"]}
    ).status_code in {403, 405}
    assert stand.assignments.calls == []
