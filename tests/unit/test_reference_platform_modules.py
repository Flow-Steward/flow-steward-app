"""The reference platform modules end to end, without Docker (ADR 0082 prototype).

Both modules are built from ``tests/fixtures/platform_modules`` with the prototype
builder, activated with the real activator, verified and put on ``sys.path`` by
the real loader and registered through Core's ``load_platform_modules``
into a FastAPI app whose runtime container carries the same registration points
``core/entrypoints/api/app_factory.py`` builds. Only the database pool and the
auth middleware are stand-ins: an in-memory store that understands the notes
module's four statements, and a middleware that signs a request in from test
headers and applies Core's route permission decision with the module rules.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import itertools
import json
import logging
import os
import platform
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
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
    route_permission,
)
from core.application.release.platform_modules import (
    PreparedModuleRef,
    module_descriptor_digest,
    staged_module_refs,
)
from core.application.reports.ui_page_registry import (
    SOURCE_MODULE_FIELD,
    register_core_settings_pages,
    resolve_registered_settings_pages,
)
from core.application.settings.platform_contracts import (
    CoreEntitlementService,
    build_platform_contracts,
)
from core.application.tooling.db.migration_runner import (
    module_migration_violations,
    module_schema_name,
)
from core.entrypoints.admin.deps import require_entitlement
from core.entrypoints.api.platform_module_loading import (
    MODULE_SERVICE_ERROR_TYPES,
    load_platform_modules,
)
from core.infrastructure.platform_modules.loader import (
    PreparedPlatformModule,
    activate_prepared_platform_modules,
    register_platform_modules_for_role,
)
from core.infrastructure.release.platform_module_activator import (
    FilesystemModuleActivator,
    FilesystemStagedModules,
    read_module_descriptor,
)
from core.infrastructure.runtime.app_factory import (
    RuntimeContainer,
    build_runtime_container,
)
from core.infrastructure.runtime.http_app_lifecycle import _register_core_exception_handlers
from tests.support.platform_module_builder import BuiltModule, build_staged_artifact

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "platform_modules"
NOTES_ID = "acme.reference-notes"
ENTITLEMENTS_ID = "acme.reference-entitlements"
NOTES_PREFIX = f"/api/modules/{NOTES_ID}"
NOTES_TABLE = f"{module_schema_name(NOTES_ID)}.notes"
FEATURE = "reference_notes"
ENTITLEMENTS_FILE = "reference-entitlements.json"
#: Top-level import names the reference modules bring; forgotten after each test.
MODULE_IMPORT_ROOTS = (
    "fs_reference_notes",
    "fs_reference_entitlements",
    "fs_reference_dep",
    "fs_reference_broken",
)
CACHE_TAG = sys.implementation.cache_tag
MACHINE = platform.machine()

ROLE_PERMISSIONS = {
    "admin": ALL_PERMISSION_KEYS,
    "operator": OPERATOR_PERMISSIONS,
    "viewer": VIEWER_PERMISSIONS,
}


# --- in-memory stand-in for the platform store ------------------------------------


_SELECT = re.compile(
    r"^SELECT (?P<cols>.+) FROM (?P<table>\S+) WHERE account_id = %s ORDER BY created_at, id$"
)
_INSERT = re.compile(
    r"^INSERT INTO (?P<table>\S+) \((?P<cols>[^)]+)\) VALUES \([^)]+\) RETURNING (?P<ret>.+)$"
)
_UPDATE = re.compile(
    r"^UPDATE (?P<table>\S+) SET (?P<sets>.+) WHERE account_id = %s AND id = %s "
    r"RETURNING (?P<ret>.+)$"
)
_DELETE = re.compile(r"^DELETE FROM (?P<table>\S+) WHERE account_id = %s AND id = %s RETURNING id$")


def _columns(text: str) -> list[str]:
    return [column.strip() for column in text.split(",")]


class FakeNotesStore:
    """Plays the ``db`` service factory's platform store for the notes table only.

    Rows come back as tuples in the selected column order, like a plain cursor.
    A statement it does not recognise fails the test instead of passing silently.
    """

    def __init__(self, table: str) -> None:
        self.table = table
        self.rows: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self._clock = itertools.count(1)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def run(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        statement = " ".join(sql.split())
        self.statements.append(statement)
        if match := _SELECT.match(statement):
            self._check_table(match["table"])
            (account_id,) = params
            return self._project(
                [row for row in self.rows if row["account_id"] == account_id], match["cols"]
            )
        if match := _INSERT.match(statement):
            self._check_table(match["table"])
            stamp = self._stamp()
            row = {"created_at": stamp, "updated_at": stamp, "pinned": None}
            row.update(zip(_columns(match["cols"]), params, strict=True))
            self.rows.append(row)
            return self._project([row], match["ret"])
        if match := _UPDATE.match(statement):
            self._check_table(match["table"])
            *values, account_id, note_id = params
            changes: dict[str, Any] = {}
            bound = iter(values)
            for assignment in _columns(match["sets"]):
                column, expression = (part.strip() for part in assignment.split("="))
                changes[column] = next(bound) if expression == "%s" else self._stamp()
            targets = [
                row for row in self.rows if row["account_id"] == account_id and row["id"] == note_id
            ]
            for row in targets:
                row.update(changes)
            return self._project(targets, match["ret"])
        if match := _DELETE.match(statement):
            self._check_table(match["table"])
            account_id, note_id = params
            kept = [
                row
                for row in self.rows
                if not (row["account_id"] == account_id and row["id"] == note_id)
            ]
            removed = [row for row in self.rows if row not in kept]
            self.rows = kept
            return [(row["id"],) for row in removed]
        raise AssertionError(f"unexpected statement: {statement}")

    def _check_table(self, table: str) -> None:
        assert table == self.table, f"statement on {table}, expected {self.table}"

    def _stamp(self) -> str:
        return f"2026-11-25T00:00:{next(self._clock):02d}+00:00"

    @staticmethod
    def _project(rows: list[dict[str, Any]], columns: str) -> list[tuple[Any, ...]]:
        names = _columns(columns)
        return [tuple(row[name] for name in names) for row in rows]


class _FakeCursor:
    def __init__(self, store: FakeNotesStore) -> None:
        self._store = store
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._result = self._store.run(sql, tuple(params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)


class NoRestart:
    def __init__(self) -> None:
        self.restarted: list[str] = []

    def module_roles(self) -> tuple[str, ...]:
        return ("fs-app",)

    def restart_role(self, role: str) -> None:
        self.restarted.append(role)


# --- process isolation -------------------------------------------------------------


def _forget_module_imports() -> None:
    for name in list(sys.modules):
        if name.split(".", 1)[0] in MODULE_IMPORT_ROOTS:
            del sys.modules[name]
    importlib.invalidate_caches()


def _drop_import_roots(root: Path) -> None:
    prefix = str(root.resolve())
    sys.path[:] = [entry for entry in sys.path if not entry.startswith(prefix)]
    _forget_module_imports()


@pytest.fixture
def isolated_imports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A private ``sys.path``, no bytecode in the staged trees, no leaked module imports.

    The loader refuses files the module list does not name, so a ``__pycache__``
    written into a staged tree by an import would break the next activation.
    """
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    _forget_module_imports()
    yield
    _forget_module_imports()


# --- building, activating and loading ----------------------------------------------


@dataclass(frozen=True)
class Built:
    root: Path
    notes_v1: BuiltModule
    notes_v2: BuiltModule
    entitlements: BuiltModule


def _ref(built: BuiltModule) -> PreparedModuleRef:
    return PreparedModuleRef(id=built.id, version=built.version, digest=built.digest)


def _build(source: Path, root: Path, **kwargs: Any) -> BuiltModule:
    return build_staged_artifact(
        source, root, python_cache_tag=CACHE_TAG, machine=MACHINE, **kwargs
    )


@pytest.fixture
def built(tmp_path: Path) -> Built:
    root = tmp_path / "runtime" / "platform-modules"
    root.mkdir(parents=True)
    return Built(
        root=root,
        notes_v1=_build(FIXTURES / "reference_notes" / "1.0.0", root),
        notes_v2=_build(FIXTURES / "reference_notes" / "1.1.0", root),
        entitlements=_build(FIXTURES / "reference_entitlements", root),
    )


def _write_entitlements(root: Path, accounts: dict[str, list[str]]) -> None:
    """Rewrite the file the entitlements module reads, as an operator would."""
    path = root / ENTITLEMENTS_FILE
    previous = path.stat().st_mtime_ns if path.exists() else 1_700_000_000_000_000_000
    temporary = root / f".{ENTITLEMENTS_FILE}.tmp"
    payload = {
        "accounts": {account: {"features": features} for account, features in accounts.items()},
        "default": {"features": []},
    }
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)
    # Strictly later than the previous version, whatever the filesystem's clock.
    stamp = previous + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _require_authenticated_user(request: Any, required_roles: Any = None) -> dict[str, Any]:
    del required_roles
    user = getattr(request.state, "auth_user", None)
    if not isinstance(user, dict):
        raise PermissionError("Authentication required")
    return user


def _container(app: FastAPI, store: FakeNotesStore) -> RuntimeContainer:
    contracts = build_platform_contracts()
    container = build_runtime_container(
        app=app,
        config={},
        auth_helpers={
            "require_authenticated_user": _require_authenticated_user,
            "require_entitlement": require_entitlement,
        },
        service_factories={
            "db": lambda: store,
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
    register_core_settings_pages(container.registries)
    return container


def _install_test_auth(app: FastAPI, container: RuntimeContainer) -> None:
    """Sign in from headers and apply Core's route decision, module rules included."""

    @app.middleware("http")
    async def _sign_in(request, call_next):
        account = request.headers.get("x-test-account")
        role = request.headers.get("x-test-role", "admin")
        request.state.auth_user = (
            {"username": f"{role}@{account}", "account_id": account, "roles": [role]}
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
    store: FakeNotesStore
    modules: tuple[PreparedPlatformModule, ...]
    loaded: tuple[str, ...]
    client: TestClient

    @property
    def contracts(self) -> dict[str, Any]:
        return self.container.registries["platform_contracts"]

    def call(self, method: str, path: str, *, account: str | None, role: str = "admin", **kw):
        headers = {"x-test-account": account, "x-test-role": role} if account else {}
        return self.client.request(method, f"{NOTES_PREFIX}{path}", headers=headers, **kw)

    def visible_pages(self, account: str, role: str = "admin") -> list[str]:
        pages = resolve_registered_settings_pages(
            self.container.registries,
            auth_user={"username": "u", "account_id": account, "roles": [role]},
            authorization_service=self.contracts["authorization_service"],
            entitlement_service=self.contracts["entitlement_service"],
        )
        return [page["page_key"] for page in pages]


def _load_stand(root: Path) -> Stand:
    store = FakeNotesStore(NOTES_TABLE)
    app = FastAPI()
    container = _container(app, store)
    app.state.runtime_container = container
    _register_core_exception_handlers(app, service_error_types=MODULE_SERVICE_ERROR_TYPES)
    _install_test_auth(app, container)
    modules = activate_prepared_platform_modules("app", root=root)
    loaded = load_platform_modules(app, container, modules, module_names=())
    return Stand(root, app, container, store, modules, loaded, TestClient(app))


def _activator(root: Path) -> FilesystemModuleActivator:
    return FilesystemModuleActivator(
        root, restarter=NoRestart(), cache_tag=CACHE_TAG, machine=MACHINE
    )


@pytest.fixture
def stand(built: Built, isolated_imports: None, monkeypatch: pytest.MonkeyPatch) -> Stand:
    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(built.root))
    _activator(built.root).activate((_ref(built.entitlements), _ref(built.notes_v1)))
    return _load_stand(built.root)


# --- the builder ---------------------------------------------------------------------


def test_builder_output_is_deterministic_and_matches_cores_digest(
    built: Built, tmp_path: Path
) -> None:
    again = _build(FIXTURES / "reference_notes" / "1.1.0", tmp_path / "second-root")

    assert again.digest == built.notes_v2.digest
    assert (again.path / "module.json").read_bytes() == (
        built.notes_v2.path / "module.json"
    ).read_bytes()
    for result in (built.notes_v1, built.notes_v2, built.entitlements):
        descriptor = read_module_descriptor(result.path)
        assert module_descriptor_digest(descriptor) == result.digest
        assert not list(result.path.rglob("__pycache__"))
        assert not list(result.path.rglob("*.pyc"))


def test_builder_refuses_to_overwrite_a_staged_version_unless_asked(built: Built) -> None:
    source = FIXTURES / "reference_notes" / "1.0.0"

    with pytest.raises(FileExistsError):
        _build(source, built.root)
    rebuilt = _build(source, built.root, replace=True)

    assert rebuilt.digest == built.notes_v1.digest


def test_the_overlay_version_carries_the_base_files_plus_its_delta(built: Built) -> None:
    v1 = set(read_module_descriptor(built.notes_v1.path)["files_sha256"])
    v2 = read_module_descriptor(built.notes_v2.path)["files_sha256"]

    assert v1 <= set(v2)
    assert set(v2) - v1 == {"db/migrations/20261125100000_add_pinned_flag.sql"}
    assert (
        v2["src/fs_reference_notes/_features.py"]
        != read_module_descriptor(built.notes_v1.path)["files_sha256"][
            "src/fs_reference_notes/_features.py"
        ]
    )
    assert {
        (ref.id, ref.version) for ref in staged_module_refs(FilesystemStagedModules(built.root))
    } == {
        (ENTITLEMENTS_ID, "1.0.0"),
        (NOTES_ID, "1.1.0"),
    }


# --- module migrations ---------------------------------------------------------------


def test_module_migrations_pass_the_static_guard_and_1_1_adds_exactly_one(built: Built) -> None:
    schema = module_schema_name(NOTES_ID)
    shipped = {}
    for result in (built.notes_v1, built.notes_v2):
        files = sorted((result.path / "db" / "migrations").glob("*.sql"))
        for path in files:
            assert module_migration_violations(path.read_text("utf-8"), schema_name=schema) == []
        shipped[result.version] = [path.name for path in files]

    assert schema == "mod_acme_reference__notes"
    assert len(shipped["1.1.0"]) == len(shipped["1.0.0"]) + 1
    assert shipped["1.1.0"][: len(shipped["1.0.0"])] == shipped["1.0.0"]


# --- loading -------------------------------------------------------------------------


def test_both_modules_are_loaded_under_their_identity(stand: Stand) -> None:
    registrations = {
        record["id"]: record for record in stand.container.metadata["platform_module_registrations"]
    }

    assert {(module.module_id, module.version) for module in stand.modules} == {
        (ENTITLEMENTS_ID, "1.0.0"),
        (NOTES_ID, "1.0.0"),
    }
    assert set(stand.loaded) == {"fs_reference_entitlements", "fs_reference_notes"}
    assert registrations[NOTES_ID]["version"] == "1.0.0"
    assert registrations[NOTES_ID]["pages"] == ["reference_notes"]
    assert registrations[NOTES_ID]["route_rules"] == 2
    assert registrations[ENTITLEMENTS_ID] == {
        "id": ENTITLEMENTS_ID,
        "version": "1.0.0",
        "pages": [],
        "route_rules": 0,
    }


def test_the_bundled_dependency_loads_from_the_module_site_tree(stand: Stand) -> None:
    dependency = importlib.import_module("fs_reference_dep")
    entitlements_dir = next(m.path for m in stand.modules if m.module_id == ENTITLEMENTS_ID)

    assert importlib.metadata.version("fs_reference_dep") == "1.0.0"
    assert Path(dependency.__file__).is_relative_to(entitlements_dir / "site")
    assert "fs_reference_dep" in sys.modules


def test_role_hooks_log_the_loaded_identity(
    built: Built, isolated_imports: None, caplog: pytest.LogCaptureFixture
) -> None:
    _activator(built.root).activate((_ref(built.entitlements), _ref(built.notes_v1)))
    caplog.set_level(logging.INFO, logger="fs_reference_notes")

    for role in ("worker", "control"):
        register_platform_modules_for_role(role, config={}, root=built.root)

    messages = [record.getMessage() for record in caplog.records]
    for role in ("worker", "control"):
        assert f"Platform module {NOTES_ID} 1.0.0 registered for role {role}" in messages


def test_the_control_role_registers_the_entitlement_source_for_the_retention_pass(
    built: Built, isolated_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(built.root))
    _activator(built.root).activate((_ref(built.entitlements), _ref(built.notes_v1)))
    service = CoreEntitlementService()

    register_platform_modules_for_role(
        "control", config={}, root=built.root, services={"entitlement_service": service}
    )

    # Missing file: a definite "nothing is entitled".
    assert service.entitlement_status("acc-a", "approval_journal") is False
    _write_entitlements(built.root, {"acc-a": ["approval_journal"]})
    assert service.entitlement_status("acc-a", "approval_journal") is True
    assert service.entitlement_status("acc-b", "approval_journal") is False
    # A malformed file is a failed source: access fails closed, the purge sees "unknown".
    path = built.root / ENTITLEMENTS_FILE
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    path.write_text("{not json", encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))
    assert service.is_entitled("acc-a", "approval_journal") is False
    assert service.entitlement_status("acc-a", "approval_journal") is None


# --- the page ------------------------------------------------------------------------


def test_the_notes_page_is_a_module_owned_resource_admin_page(stand: Stand) -> None:
    record = stand.container.registries["settings_pages"]["reference_notes"]
    surface = record["control_surface"]
    [resource] = surface["resources"]

    assert record[SOURCE_MODULE_FIELD] == NOTES_ID
    assert record["required_entitlement"] == FEATURE
    assert "reference_notes" in stand.container.registries["registered_pages"]
    assert surface["kind"] == "resource_admin"
    assert resource["list_path"] == f"{NOTES_PREFIX}/notes"
    assert resource["item_path"].startswith(f"{NOTES_PREFIX}/")
    assert [field["key"] for field in resource["fields"]] == ["title", "body"]


def test_the_page_follows_the_entitlement_file_without_a_reload(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [FEATURE]})

    assert "reference_notes" in stand.visible_pages("acc-a")
    assert "reference_notes" not in stand.visible_pages("acc-b")
    assert "reference_notes" not in stand.visible_pages("acc-a", role="viewer")

    _write_entitlements(stand.root, {"acc-a": [], "acc-b": [FEATURE]})

    assert "reference_notes" not in stand.visible_pages("acc-a")
    assert "reference_notes" in stand.visible_pages("acc-b")
    # The browser never sees which module registered a page.
    [page] = [
        page
        for page in resolve_registered_settings_pages(
            stand.container.registries,
            auth_user={"account_id": "acc-b", "roles": ["admin"]},
            entitlement_service=stand.contracts["entitlement_service"],
        )
        if page["page_key"] == "reference_notes"
    ]
    assert SOURCE_MODULE_FIELD not in page


# --- the API -------------------------------------------------------------------------


def test_the_api_refuses_until_the_entitlement_file_grants_the_feature(stand: Stand) -> None:
    missing_file = stand.call("GET", "/notes", account="acc-a")

    assert missing_file.status_code == 403
    assert missing_file.json()["code"] == "feature_not_entitled"
    assert missing_file.json()["details"] == {"feature": FEATURE}

    _write_entitlements(stand.root, {"acc-b": [FEATURE]})
    assert stand.call("GET", "/notes", account="acc-a").status_code == 403

    _write_entitlements(stand.root, {"acc-a": [FEATURE]})
    granted = stand.call("GET", "/notes", account="acc-a")
    assert granted.status_code == 200
    assert granted.json() == {"items": []}

    (stand.root / ENTITLEMENTS_FILE).write_text("{not json", encoding="utf-8")
    os.utime(stand.root / ENTITLEMENTS_FILE, ns=(1, 1))
    assert stand.call("GET", "/notes", account="acc-a").json()["code"] == "feature_not_entitled"


def test_an_anonymous_request_is_not_authenticated(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [FEATURE]})

    assert stand.call("GET", "/notes", account=None).status_code == 401


def test_the_contributed_route_rule_decides_writes(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [FEATURE]})
    rules = stand.container.registries["module_route_permissions"].rules()
    write_path = f"{NOTES_PREFIX}/notes"

    # Without the module's rules a write under its tree is unmapped, so denied.
    assert route_permission("POST", write_path) is None
    assert route_permission("POST", write_path, rules).permission == "extensions.use"
    assert route_permission("DELETE", f"{write_path}/n-1", rules).permission == "extensions.use"

    refused = stand.call("POST", "/notes", account="acc-a", role="viewer", json={"title": "x"})
    assert refused.status_code == 403
    assert stand.store.rows == []
    assert stand.call("GET", "/notes", account="acc-a", role="viewer").status_code == 200

    created = stand.call("POST", "/notes", account="acc-a", role="operator", json={"title": "x"})
    assert created.status_code == 201
    note_id = created.json()["item"]["id"]
    assert (
        stand.call("DELETE", f"/notes/{note_id}", account="acc-a", role="viewer").status_code == 403
    )
    assert (
        stand.call("DELETE", f"/notes/{note_id}", account="acc-a", role="operator").status_code
        == 200
    )


def test_one_account_never_sees_or_changes_another_accounts_note(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [FEATURE], "acc-b": [FEATURE]})
    created = stand.call("POST", "/notes", account="acc-a", json={"title": "A's", "body": "only"})
    note_id = created.json()["item"]["id"]

    assert created.status_code == 201
    assert stand.call("GET", "/notes", account="acc-b").json() == {"items": []}
    assert (
        stand.call("PUT", f"/notes/{note_id}", account="acc-b", json={"title": "B's"}).status_code
        == 404
    )
    assert stand.call("DELETE", f"/notes/{note_id}", account="acc-b").status_code == 404

    [note] = stand.call("GET", "/notes", account="acc-a").json()["items"]
    assert (note["id"], note["title"], note["body"]) == (note_id, "A's", "only")
    assert "pinned" not in note
    updated = stand.call("PUT", f"/notes/{note_id}", account="acc-a", json={"title": "A2"})
    assert updated.json()["item"]["title"] == "A2"
    filtered = [sql for sql in stand.store.statements if not sql.startswith("INSERT")]
    assert filtered
    assert all("account_id = %s" in sql for sql in filtered)


def test_version_1_0_refuses_the_field_that_1_1_adds(stand: Stand) -> None:
    _write_entitlements(stand.root, {"acc-a": [FEATURE]})

    response = stand.call("POST", "/notes", account="acc-a", json={"title": "x", "pinned": True})

    assert response.status_code == 422


# --- upgrading the module ------------------------------------------------------------


def test_activating_1_1_changes_the_installed_set_and_adds_the_pinned_field(
    built: Built, stand: Stand
) -> None:
    activator = _activator(built.root)
    before = activator.installed_set()
    assert before == (_ref(built.entitlements), _ref(built.notes_v1))

    # A role restart: the process starts again with no module on its path.
    _drop_import_roots(built.root)
    activator.activate((_ref(built.entitlements), _ref(built.notes_v2)))

    after = activator.installed_set()
    assert after != before
    assert after == (_ref(built.entitlements), _ref(built.notes_v2))

    upgraded = _load_stand(built.root)
    _write_entitlements(built.root, {"acc-a": [FEATURE]})
    record = upgraded.container.registries["settings_pages"]["reference_notes"]
    fields = record["control_surface"]["resources"][0]["fields"]
    assert {"key": "pinned", "label": "Pinned", "type": "checkbox", "default": False} in fields
    assert {(m.module_id, m.version) for m in upgraded.modules} >= {(NOTES_ID, "1.1.0")}

    created = upgraded.call("POST", "/notes", account="acc-a", json={"title": "x", "pinned": True})
    assert created.status_code == 201
    assert created.json()["item"]["pinned"] is True


# --- a module that fails to load -----------------------------------------------------

BROKEN_ID = "acme.reference-broken"
BROKEN_FEATURE = "reference_broken"


def test_a_module_whose_register_fails_is_skipped_and_the_app_keeps_running(
    built: Built, isolated_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(built.root))
    broken = _build(FIXTURES / "reference_broken", built.root)
    _activator(built.root).activate((_ref(built.entitlements), _ref(broken), _ref(built.notes_v1)))

    stand = _load_stand(built.root)

    assert set(stand.loaded) == {"fs_reference_entitlements", "fs_reference_notes"}
    [failure] = stand.container.metadata["platform_module_failures"]
    assert (failure["id"], failure["version"], failure["status"]) == (BROKEN_ID, "1.0.0", "failed")
    # It does not declare the entitlement source capability, so its source is refused.
    assert failure["load_error"].startswith("EntitlementSourceRefusedError:")
    assert f"'{BROKEN_ID}' does not declare" in failure["load_error"]
    # The route it mounted before failing is gone, and so is its rule; no source stayed.
    assert stand.client.get(f"/api/modules/{BROKEN_ID}/items").status_code == 404
    assert not any(
        getattr(route, "path", "").startswith(f"/api/modules/{BROKEN_ID}")
        for route in stand.app.routes
    )
    assert stand.container.registries["module_route_permissions"].rule_count(BROKEN_ID) == 0
    entitlements = stand.contracts["entitlement_service"]
    assert entitlements.is_entitled("acc-a", BROKEN_FEATURE) is False
    assert entitlements.source_owner == ENTITLEMENTS_ID
    assert BROKEN_ID not in {
        record["id"] for record in stand.container.metadata["platform_module_registrations"]
    }
    # The other modules work.
    _write_entitlements(stand.root, {"acc-a": [FEATURE]})
    assert stand.call("GET", "/notes", account="acc-a").status_code == 200


def test_a_background_role_skips_the_failing_module_and_rolls_back_its_source(
    built: Built, isolated_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(built.root))
    broken = _build(FIXTURES / "reference_broken", built.root)
    _activator(built.root).activate((_ref(built.entitlements), _ref(broken)))
    service = CoreEntitlementService()

    modules = register_platform_modules_for_role(
        "control", config={}, root=built.root, services={"entitlement_service": service}
    )

    assert [module.module_id for module in modules] == [ENTITLEMENTS_ID]
    # The source is rolled back, so access stays denied; a failed module makes
    # the answer unknown rather than a definite no, so retention keeps data.
    assert service.is_entitled("acc-a", BROKEN_FEATURE) is False
    assert service.entitlement_status("acc-a", BROKEN_FEATURE) is None
    assert service.loading_degraded


# --- one designated entitlement source and the module ports (ADR 0082 §3b) ---------


def _declared_broken_source(tmp_path: Path) -> Path:
    """A copy of the broken module that declares the entitlement source capability."""
    import shutil

    source = tmp_path / "declared-broken"
    shutil.copytree(FIXTURES / "reference_broken", source)
    descriptor = json.loads((source / "module-source.json").read_text("utf-8"))
    descriptor["provides"] = ["entitlement_source"]
    (source / "module-source.json").write_text(json.dumps(descriptor), encoding="utf-8")
    return source


def test_the_entitlements_module_is_the_declared_source(stand: Stand, built: Built) -> None:
    descriptor = read_module_descriptor(built.entitlements.path)
    notes = read_module_descriptor(built.notes_v1.path)

    assert descriptor["provides"] == ["entitlement_source"]
    assert "provides" not in notes
    assert {m.module_id: m.provides for m in stand.modules} == {
        ENTITLEMENTS_ID: frozenset({"entitlement_source"}),
        NOTES_ID: frozenset(),
    }
    assert stand.contracts["entitlement_service"].source_owner == ENTITLEMENTS_ID


def test_a_set_with_two_declared_entitlement_sources_is_refused(
    built: Built, tmp_path: Path
) -> None:
    from core.application.lifecycle.errors import LifecycleError

    second = _build(_declared_broken_source(tmp_path), built.root)
    activator = _activator(built.root)

    with pytest.raises(LifecycleError) as refused:
        activator.activate((_ref(built.entitlements), _ref(second)))

    assert refused.value.code == "release_module_set_rejected"
    assert "exactly one entitlement source" in refused.value.cause
    assert activator.installed_set() == ()


# --- module dependencies (ADR 0082, 2026-11-27 amendment) ---------------------------


def _requiring_broken_source(tmp_path: Path, requires: list[str]) -> Path:
    """A copy of the broken module that requires other modules."""
    import shutil

    source = tmp_path / "requiring-broken"
    shutil.copytree(FIXTURES / "reference_broken", source)
    descriptor = json.loads((source / "module-source.json").read_text("utf-8"))
    descriptor["requires"] = requires
    (source / "module-source.json").write_text(json.dumps(descriptor), encoding="utf-8")
    return source


def test_a_declared_dependency_is_pinned_and_an_undeclared_one_leaves_the_digest(
    built: Built, tmp_path: Path
) -> None:
    plain = read_module_descriptor(_build(FIXTURES / "reference_broken", tmp_path / "a").path)
    requiring = _build(_requiring_broken_source(tmp_path, [NOTES_ID]), tmp_path / "b")
    descriptor = read_module_descriptor(requiring.path)

    assert "requires" not in plain
    assert "requires" not in read_module_descriptor(built.notes_v1.path)
    assert descriptor["requires"] == [NOTES_ID]
    assert {key: value for key, value in descriptor.items() if key != "requires"} == plain
    assert requiring.digest == module_descriptor_digest(descriptor)
    assert requiring.digest != module_descriptor_digest(plain)


def test_a_set_missing_a_required_module_is_refused_at_install(
    built: Built, tmp_path: Path
) -> None:
    from core.application.lifecycle.errors import LifecycleError

    requiring = _build(_requiring_broken_source(tmp_path, [f"{NOTES_ID}>=1.0.0"]), built.root)
    activator = _activator(built.root)

    with pytest.raises(LifecycleError) as refused:
        activator.activate((_ref(requiring),))

    assert refused.value.code == "release_module_set_rejected"
    assert f"requires {NOTES_ID}" in refused.value.cause
    assert activator.installed_set() == ()

    activator.activate((_ref(requiring), _ref(built.notes_v1)))
    assert [ref.id for ref in activator.installed_set()] == [BROKEN_ID, NOTES_ID]


def test_a_declared_source_that_fails_to_load_is_rolled_back(
    built: Built, isolated_imports: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(built.root))
    declared = _build(_declared_broken_source(tmp_path), built.root)
    _activator(built.root).activate((_ref(declared), _ref(built.notes_v1)))

    stand = _load_stand(built.root)

    [failure] = stand.container.metadata["platform_module_failures"]
    assert failure["id"] == BROKEN_ID
    # Its source registration was accepted, then the missing port failed the module.
    assert failure["load_error"].startswith("AttributeError:")
    assert "missing_port" in failure["load_error"]
    entitlements = stand.contracts["entitlement_service"]
    assert entitlements.loading_checkpoint() == ()
    assert entitlements.source_owner is None
    assert entitlements.is_entitled("acc-a", BROKEN_FEATURE) is False


def test_the_control_role_hands_the_installation_id_to_modules(
    built: Built, isolated_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(built.root))
    _activator(built.root).activate((_ref(built.entitlements), _ref(built.notes_v1)))
    service = CoreEntitlementService()
    seen: list[Any] = []

    def _spy(context: Any) -> None:
        seen.append(context["installation_id"])

    def _import(name: str) -> Any:
        imported = importlib.import_module(name)
        if name == "fs_reference_notes":
            monkeypatch.setattr(imported, "register_control", _spy)
        return imported

    register_platform_modules_for_role(
        "control",
        config={},
        root=built.root,
        services={"entitlement_service": service},
        installation_id="11111111-2222-3333-4444-555555555555",
        import_module=_import,
    )

    assert seen == ["11111111-2222-3333-4444-555555555555"]
    assert service.source_owner == ENTITLEMENTS_ID
