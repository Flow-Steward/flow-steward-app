from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from core.application.auth.route_permissions import (
    ModuleRoutePermissionRegistry,
    ModuleRouteRuleRegistrar,
    route_permission,
)
from core.application.reports.ui_page_registry import register_page
from core.application.settings.platform_module_views import ModuleContainerViolationError
from core.entrypoints.api.platform_module_loading import load_platform_modules
from core.infrastructure.platform_modules.loader import (
    PlatformModuleImportError,
    PreparedPlatformModule,
)
from core.infrastructure.runtime import app_factory as runtime_app_factory
from core.infrastructure.runtime.app_factory import (
    RuntimeContainer,
    build_runtime_container,
)


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args) -> None:
        self.messages.append(message % args if args else message)


def test_validate_runtime_service_ports_names_the_exact_missing_seam_and_methods() -> None:
    container = build_runtime_container(
        app=FastAPI(),
        config={},
        service_factories={"job_store": lambda: object()},
    )

    with pytest.raises(
        RuntimeError,
        match=(
            r"job trace projection -> job_store.*"
            r"build_job_trace, list_job_reaction_events"
        ),
    ):
        runtime_app_factory.validate_runtime_service_ports(
            container,
            (
                (
                    "job trace projection -> job_store",
                    "job_store",
                    ("build_job_trace", "list_job_reaction_events"),
                ),
            ),
        )


def test_validate_runtime_service_ports_accepts_a_real_concrete_capability() -> None:
    class _JobProjectionStore:
        def build_job_trace(self):
            return []

    store = _JobProjectionStore()
    container = build_runtime_container(
        app=FastAPI(),
        config={},
        service_factories={"job_trace_store": lambda: store},
    )

    resolved = runtime_app_factory.validate_runtime_service_ports(
        container,
        (("job trace projection", "job_trace_store", ("build_job_trace",)),),
    )

    assert resolved == {"job_trace_store": store}


def test_build_runtime_container_preserves_internal_bootstrap_state() -> None:
    app = FastAPI()
    container = build_runtime_container(
        app=app,
        config={"env": "test"},
        auth_helpers={"require_authenticated_user": lambda: "ok"},
        service_factories={"project_query_service": lambda: "service"},
        registries={"settings_pages": {}},
        metadata={"edition": "community"},
    )

    assert container.app is app
    assert container.config["env"] == "test"
    assert callable(container.auth_helpers["require_authenticated_user"])
    assert callable(container.service_factories["project_query_service"])
    assert container.registries["settings_pages"] == {}
    assert container.metadata["edition"] == "community"


def test_load_optional_private_modules_is_noop_when_package_missing(monkeypatch) -> None:
    def _missing(_module_name: str):
        raise ModuleNotFoundError(name="private.bootstrap")

    monkeypatch.setattr("importlib.import_module", _missing)

    loaded = load_platform_modules(
        FastAPI(),
        build_runtime_container(app=FastAPI(), config={}),
        module_names=("private.bootstrap",),
    )

    assert loaded == ()


def test_load_optional_private_modules_calls_register_with_the_core_container(monkeypatch) -> None:
    calls: list[tuple[object, object]] = []
    seen_route_key: list[bool] = []

    class _BootstrapModule:
        @staticmethod
        def register(app, container) -> None:
            calls.append((app, container))
            seen_route_key.append("module_route_permissions" in container.registries)

    monkeypatch.setattr("importlib.import_module", lambda _module_name: _BootstrapModule)

    app = FastAPI()
    shared_routes = ModuleRoutePermissionRegistry()
    container = build_runtime_container(
        app=app,
        config={},
        registries={"module_route_permissions": shared_routes, "settings_pages": {}},
    )

    loaded = load_platform_modules(app, container, module_names=("private.bootstrap",))

    assert loaded == ("private.bootstrap",)
    [(called_app, view)] = calls
    assert called_app is app
    # An env-listed module keeps its pre-ADR 0082 behaviour: it registers against
    # Core's own container. It has no verified identity, so the route rule
    # registration point is absent while it registers and restored afterwards.
    assert view is container
    assert seen_route_key == [False]
    assert "platform_module" not in view.metadata
    assert container.registries["module_route_permissions"] is shared_routes


def test_load_optional_private_modules_preserves_configured_order(monkeypatch) -> None:
    calls: list[str] = []

    class _BootstrapModule:
        def __init__(self, module_name: str) -> None:
            self.module_name = module_name

        def register(self, _app, container) -> None:
            calls.append(self.module_name)
            container.registries.setdefault(self.module_name, True)

    monkeypatch.setattr(
        "importlib.import_module",
        lambda module_name: _BootstrapModule(str(module_name)),
    )

    app = FastAPI()
    container = build_runtime_container(app=app, config={}, registries={})

    loaded = load_platform_modules(
        app,
        container,
        module_names=("private.alpha", "private.beta"),
    )

    assert loaded == ("private.alpha", "private.beta")
    assert calls == ["private.alpha", "private.beta"]
    assert container.registries["private.alpha"] is True
    assert container.registries["private.beta"] is True


def test_load_optional_private_modules_warns_when_entrypoint_missing(monkeypatch) -> None:
    monkeypatch.setattr("importlib.import_module", lambda _module_name: object())
    logger = _Logger()

    loaded = load_platform_modules(
        FastAPI(),
        build_runtime_container(app=FastAPI(), config={}),
        logger=logger,
        module_names=("private.bootstrap",),
    )

    assert loaded == ()
    assert logger.messages


def _prepared(module_id: str, import_name: str, version: str = "1.0.0") -> PreparedPlatformModule:
    return PreparedPlatformModule(
        module_id=module_id, version=version, import_name=import_name, path=Path("/unused")
    )


def _shared_container(app: FastAPI) -> RuntimeContainer:
    return build_runtime_container(
        app=app,
        config={},
        registries={
            "module_route_permissions": ModuleRoutePermissionRegistry(),
            "settings_pages": {},
            "registered_pages": {},
            "navigation_items": {},
        },
        metadata={"edition": "community"},
    )


def _module_file(import_name: str) -> str:
    """Where a prepared module built by ``_prepared`` is imported from."""
    return f"/unused/src/{import_name}/__init__.py"


def _modules(monkeypatch, registers: dict) -> None:
    class _Module:
        def __init__(self, register, module_name: str) -> None:
            self.register = register
            self.__file__ = _module_file(module_name)

    def _import(module_name: str):
        if module_name not in registers:
            raise ModuleNotFoundError(name=module_name)
        return _Module(registers[module_name], module_name)

    monkeypatch.setattr("importlib.import_module", _import)


def test_load_optional_private_modules_loads_prepared_modules_first(monkeypatch) -> None:
    calls: list[str] = []
    _modules(
        monkeypatch,
        {
            name: (lambda _app, _container, name=name: calls.append(name))
            for name in ("prepared_accounts", "prepared_ledger", "private.optional")
        },
    )

    loaded = load_platform_modules(
        FastAPI(),
        _shared_container(FastAPI()),
        module_names=("private.optional", "prepared_accounts"),
        prepared_modules=(
            _prepared("acme.accounts", "prepared_accounts"),
            _prepared("acme.ledger", "prepared_ledger"),
        ),
    )

    assert loaded == ("prepared_accounts", "prepared_ledger", "private.optional")
    assert calls == ["prepared_accounts", "prepared_ledger", "private.optional"]


def test_load_optional_private_modules_raises_when_a_prepared_module_is_missing(
    monkeypatch,
) -> None:
    _modules(monkeypatch, {})

    with pytest.raises(RuntimeError, match=r"Required platform module 'acme\.accounts'"):
        load_platform_modules(
            FastAPI(),
            build_runtime_container(app=FastAPI(), config={}),
            module_names=(),
            prepared_modules=(_prepared("acme.accounts", "prepared_accounts"),),
        )


def test_load_optional_private_modules_raises_when_a_prepared_entrypoint_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "importlib.import_module",
        lambda module_name: SimpleNamespace(__file__=_module_file(module_name)),
    )

    with pytest.raises(RuntimeError, match="missing callable entrypoint 'register'"):
        load_platform_modules(
            FastAPI(),
            build_runtime_container(app=FastAPI(), config={}),
            module_names=(),
            prepared_modules=(_prepared("acme.accounts", "prepared_accounts"),),
        )


def test_load_optional_private_modules_keeps_env_names_optional_next_to_prepared(
    monkeypatch,
) -> None:
    _modules(monkeypatch, {"prepared_accounts": lambda _app, _container: None})
    monkeypatch.setenv("FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES", "private.absent")

    loaded = load_platform_modules(
        FastAPI(),
        build_runtime_container(app=FastAPI(), config={}),
        prepared_modules=(_prepared("acme.accounts", "prepared_accounts"),),
    )

    assert loaded == ("prepared_accounts",)


# --- a prepared module's view is bound to its loaded identity (ADR 0082) ----------


def test_prepared_module_view_carries_an_immutable_identity(monkeypatch) -> None:
    seen: list[RuntimeContainer] = []
    _modules(monkeypatch, {"acme_ledger": lambda _app, view: seen.append(view)})
    app = FastAPI()
    container = _shared_container(app)

    load_platform_modules(
        app,
        container,
        module_names=(),
        prepared_modules=(_prepared("acme.ledger", "acme_ledger", version="2.3.0"),),
    )

    [view] = seen
    identity = view.metadata["platform_module"]
    assert dict(identity) == {"id": "acme.ledger", "version": "2.3.0"}
    with pytest.raises(TypeError):
        identity["id"] = "acme.reports"  # type: ignore[index]
    assert "platform_module" not in container.metadata
    assert view.metadata["edition"] == "community"


def test_prepared_module_registrar_forwards_the_loaded_module_id(monkeypatch) -> None:
    def _register(_app, view) -> None:
        view.registries["module_route_permissions"].register(
            methods=("POST",), pattern="exports", permission="jobs.write"
        )

    _modules(monkeypatch, {"acme_ledger": _register})
    app = FastAPI()
    container = _shared_container(app)

    load_platform_modules(
        app,
        container,
        module_names=(),
        prepared_modules=(_prepared("acme.ledger", "acme_ledger"),),
    )

    shared = container.registries["module_route_permissions"]
    [rule] = shared.rules()
    assert rule.pattern.pattern.startswith(re.escape("/api/modules/acme.ledger/"))
    assert route_permission("POST", "/api/modules/acme.ledger/exports", shared.rules()) is not None
    assert shared.rule_count("acme.ledger") == 1


def test_prepared_module_cannot_reach_the_shared_route_registry(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def _register(app, view) -> None:
        registrar = view.registries["module_route_permissions"]
        observed["registrar"] = registrar
        observed["app_container"] = app.state.runtime_container
        observed["registry_values"] = list(view.registries.values())
        observed["metadata_values"] = list(view.metadata.values())

    _modules(monkeypatch, {"acme_ledger": _register})
    app = FastAPI()
    container = _shared_container(app)
    app.state.runtime_container = container
    shared = container.registries["module_route_permissions"]

    load_platform_modules(
        app,
        container,
        module_names=(),
        prepared_modules=(_prepared("acme.ledger", "acme_ledger"),),
    )

    registrar = observed["registrar"]
    assert isinstance(registrar, ModuleRouteRuleRegistrar)
    assert not isinstance(registrar, ModuleRoutePermissionRegistry)
    assert not hasattr(registrar, "rules")
    assert not hasattr(registrar, "__dict__")
    assert all(
        getattr(registrar, slot) is not shared for slot in ModuleRouteRuleRegistrar.__slots__
    )
    assert all(value is not shared for value in observed["registry_values"])
    assert all(value is not shared for value in observed["metadata_values"])
    # While the module registers, app.state exposes only the module's own view.
    assert observed["app_container"] is not container
    assert app.state.runtime_container is container
    assert "module_id" not in inspect.signature(registrar.register).parameters
    with pytest.raises(TypeError):
        registrar.register(  # type: ignore[call-arg]
            "acme.reports", methods=("POST",), pattern="exports", permission="jobs.write"
        )


def test_two_modules_cannot_register_under_each_other_prefix(monkeypatch) -> None:
    def _ledger(_app, view) -> None:
        # Aiming at another module's tree only nests below the module's own prefix.
        view.registries["module_route_permissions"].register(
            methods=("POST",), pattern=r"\.\./acme\.reports/exports", permission="jobs.write"
        )
        view.registries["module_route_permissions"].register(
            methods=("POST",), pattern=r".*", permission="jobs.write"
        )

    def _reports(_app, view) -> None:
        view.registries["module_route_permissions"].register(
            methods=("GET",), pattern="summary", permission="reports.read"
        )

    _modules(monkeypatch, {"acme_ledger": _ledger, "acme_reports": _reports})
    app = FastAPI()
    container = _shared_container(app)

    load_platform_modules(
        app,
        container,
        module_names=(),
        prepared_modules=(
            _prepared("acme.ledger", "acme_ledger"),
            _prepared("acme.reports", "acme_reports"),
        ),
    )

    rules = container.registries["module_route_permissions"].rules()
    # No ledger rule decides a path in the reports tree.
    assert route_permission("POST", "/api/modules/acme.reports/exports", rules) is None
    assert (
        route_permission("GET", "/api/modules/acme.reports/summary", rules).permission
        == "reports.read"
    )
    assert container.metadata["platform_module_registrations"] == [
        {"id": "acme.ledger", "version": "1.0.0", "pages": [], "route_rules": 2},
        {"id": "acme.reports", "version": "1.0.0", "pages": [], "route_rules": 1},
    ]


def test_env_listed_module_has_no_route_rule_registrar(monkeypatch) -> None:
    def _register(_app, view) -> None:
        view.registries["module_route_permissions"].register(
            methods=("POST",), pattern="exports", permission="jobs.write"
        )

    _modules(monkeypatch, {"private.bootstrap": _register})
    app = FastAPI()
    container = _shared_container(app)

    with pytest.raises(KeyError, match="module_route_permissions"):
        load_platform_modules(app, container, module_names=("private.bootstrap",))

    assert container.registries["module_route_permissions"].rules() == ()


def test_an_environment_listed_module_may_replace_a_container_key_as_before(monkeypatch) -> None:
    replacement = {"entitlement_service": object()}

    def _register(_app, container) -> None:
        container.registries["platform_contracts"] = replacement
        container.service_factories.pop("db")
        container.registries["module_route_permissions"] = "ignored"
        register_page(
            container.registries,
            page_key="general",
            path="/settings/general",
            component_key="private.general",
            settings_tab=True,
        )

    _modules(monkeypatch, {"private.bootstrap": _register})
    app = FastAPI()
    container = _shared_container(app)
    container.registries["platform_contracts"] = {"entitlement_service": object()}
    container.service_factories["db"] = lambda: "core-db"
    container.registries["settings_pages"]["general"] = {
        "page_key": "general",
        "path": "/settings/general",
        "_core_definition": True,
    }
    shared_routes = container.registries["module_route_permissions"]

    loaded = load_platform_modules(app, container, module_names=("private.bootstrap",))

    assert loaded == ("private.bootstrap",)
    assert container.registries["platform_contracts"] is replacement
    assert "db" not in container.service_factories
    assert container.registries["settings_pages"]["general"]["component_key"] == "private.general"
    # The route rule registration point is Core's again after registration.
    assert container.registries["module_route_permissions"] is shared_routes


def test_an_environment_listed_module_still_cannot_register_a_host_rendered_page(
    monkeypatch,
) -> None:
    def _register(_app, container) -> None:
        register_page(
            container.registries,
            key="private_records",
            path="/private/records",
            control_surface={
                "kind": "resource_admin",
                "resources": [
                    {
                        "key": "records",
                        "title": "Records",
                        "list_path": "/api/private/records",
                        "id_field": "id",
                        "fields": [{"key": "name", "type": "text"}],
                    }
                ],
            },
        )

    _modules(monkeypatch, {"private.bootstrap": _register})
    app = FastAPI()
    container = _shared_container(app)

    with pytest.raises(ValueError, match="host renderer"):
        load_platform_modules(app, container, module_names=("private.bootstrap",))
    assert "private_records" not in container.registries["registered_pages"]


def test_module_additions_to_shared_registries_stay_visible_to_core(monkeypatch) -> None:
    def _register(_app, view) -> None:
        view.registries["external_auth_enabled"] = lambda: True
        view.registries["settings_pages"]["acme_notes"] = {"page_key": "acme_notes"}
        view.metadata["acme.notes.ready"] = True
        view.registries["module_route_permissions"] = "replaced"
        view.metadata["platform_module"] = {"id": "acme.other"}

    _modules(monkeypatch, {"acme_notes": _register})
    app = FastAPI()
    container = _shared_container(app)
    shared = container.registries["module_route_permissions"]

    load_platform_modules(
        app,
        container,
        module_names=(),
        prepared_modules=(_prepared("acme.notes", "acme_notes"),),
    )

    assert callable(container.registries["external_auth_enabled"])
    assert "acme_notes" in container.registries["settings_pages"]
    assert container.metadata["acme.notes.ready"] is True
    # The identity-bound entries never flow back into Core's container.
    assert container.registries["module_route_permissions"] is shared
    assert "platform_module" not in container.metadata


def test_pages_a_prepared_module_registers_are_recorded_and_stamped(monkeypatch) -> None:
    def _register(_app, view) -> None:
        register_page(
            view.registries,
            key="acme_notes",
            label="Notes",
            path="/modules/notes",
            control_surface={
                "kind": "resource_admin",
                "resources": [
                    {
                        "key": "notes",
                        "title": "Notes",
                        "list_path": "/api/modules/acme.notes/notes",
                        "id_field": "id",
                        "fields": [{"key": "name", "type": "text"}],
                    }
                ],
            },
        )

    _modules(monkeypatch, {"acme_notes": _register})
    app = FastAPI()
    container = _shared_container(app)

    load_platform_modules(
        app,
        container,
        module_names=(),
        prepared_modules=(_prepared("acme.notes", "acme_notes"),),
    )

    assert container.registries["registered_pages"]["acme_notes"]["source_module"] == "acme.notes"
    assert container.metadata["platform_module_registrations"] == [
        {"id": "acme.notes", "version": "1.0.0", "pages": ["acme_notes"], "route_rules": 0}
    ]


# --- a prepared module must be imported from its own src/ (ADR 0082) ---------------


@pytest.mark.parametrize(
    ("origin", "message"),
    [
        ("/unused-other/src/acme_ledger/__init__.py", "outside the module's own"),
        ("/opt/core/acme_ledger/__init__.py", "outside the module's own"),
        (None, "has no __file__"),
    ],
)
def test_prepared_module_imported_from_elsewhere_is_refused(monkeypatch, origin, message) -> None:
    called: list[str] = []
    imported = SimpleNamespace(register=lambda _app, _view: called.append("register"))
    if origin is not None:
        imported.__file__ = origin
    monkeypatch.setattr("importlib.import_module", lambda _module_name: imported)
    app = FastAPI()

    with pytest.raises(PlatformModuleImportError, match=message):
        load_platform_modules(
            app,
            _shared_container(app),
            module_names=(),
            prepared_modules=(_prepared("acme.ledger", "acme_ledger"),),
        )
    # Code from somewhere else never registers under the module's identity.
    assert called == []


# --- a module adds container entries but never replaces or removes Core's ----------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda view: view.registries.__setitem__("platform_contracts", {}),
            "replaced existing registries entry 'platform_contracts'",
        ),
        (
            lambda view: view.registries.pop("settings_pages"),
            "removed existing registries entry 'settings_pages'",
        ),
        (
            lambda view: view.service_factories.__setitem__("db", lambda: "fake"),
            "replaced existing service_factories entry 'db'",
        ),
        (
            lambda view: view.service_factories.pop("db"),
            "removed existing service_factories entry 'db'",
        ),
        (
            lambda view: view.auth_helpers.__setitem__("require_entitlement", lambda: None),
            "replaced existing auth_helpers entry 'require_entitlement'",
        ),
        (
            lambda view: view.metadata.__setitem__("edition", "private"),
            "replaced existing metadata entry 'edition'",
        ),
    ],
)
def test_module_replacing_or_removing_a_core_entry_is_refused(monkeypatch, mutate, message):
    def _register(_app, view) -> None:
        view.registries["acme.notes.added"] = True
        mutate(view)

    _modules(monkeypatch, {"acme_notes": _register})
    app = FastAPI()
    contracts = {"entitlement_service": object()}
    container = build_runtime_container(
        app=app,
        config={},
        auth_helpers={"require_entitlement": lambda: "core"},
        service_factories={"db": lambda: "core-db"},
        registries={
            "platform_contracts": contracts,
            "settings_pages": {},
            "module_route_permissions": ModuleRoutePermissionRegistry(),
        },
        metadata={"edition": "community"},
    )
    db_factory = container.service_factories["db"]

    with pytest.raises(
        ModuleContainerViolationError, match=re.escape(f"platform module 'acme.notes' {message}")
    ):
        load_platform_modules(
            app,
            container,
            module_names=(),
            prepared_modules=(_prepared("acme.notes", "acme_notes"),),
        )

    assert container.registries["platform_contracts"] is contracts
    assert container.service_factories["db"] is db_factory
    assert container.metadata["edition"] == "community"
    assert "settings_pages" in container.registries
    # Nothing from a refused registration is merged, not even its additions.
    assert "acme.notes.added" not in container.registries


def test_module_view_copies_factories_and_helpers_at_the_top_level(monkeypatch) -> None:
    seen: list[RuntimeContainer] = []

    def _register(_app, view) -> None:
        seen.append(view)
        view.service_factories["acme_notes_service"] = lambda: "notes"
        view.auth_helpers["acme_notes_guard"] = lambda: True

    _modules(monkeypatch, {"acme_notes": _register})
    app = FastAPI()
    container = build_runtime_container(
        app=app, config={}, service_factories={"db": lambda: "core-db"}
    )

    load_platform_modules(
        app,
        container,
        module_names=(),
        prepared_modules=(_prepared("acme.notes", "acme_notes"),),
    )

    [view] = seen
    assert view.service_factories is not container.service_factories
    assert view.auth_helpers is not container.auth_helpers
    # Additions reach Core once the module registered cleanly.
    assert container.service_factories["acme_notes_service"]() == "notes"
    assert container.auth_helpers["acme_notes_guard"]() is True
