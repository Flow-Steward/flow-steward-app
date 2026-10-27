from __future__ import annotations

import pytest
from fastapi import FastAPI

from core.infrastructure.runtime import app_factory as runtime_app_factory
from core.infrastructure.runtime.app_factory import (
    build_runtime_container,
    load_optional_private_modules,
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

    loaded = load_optional_private_modules(
        FastAPI(),
        build_runtime_container(app=FastAPI(), config={}),
        module_names=("private.bootstrap",),
    )

    assert loaded == ()


def test_load_optional_private_modules_calls_register(monkeypatch) -> None:
    calls: list[tuple[object, object]] = []

    class _BootstrapModule:
        @staticmethod
        def register(app, container) -> None:
            calls.append((app, container))

    monkeypatch.setattr("importlib.import_module", lambda _module_name: _BootstrapModule)

    app = FastAPI()
    container = build_runtime_container(app=app, config={})

    loaded = load_optional_private_modules(app, container, module_names=("private.bootstrap",))

    assert loaded == ("private.bootstrap",)
    assert calls == [(app, container)]


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

    loaded = load_optional_private_modules(
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

    loaded = load_optional_private_modules(
        FastAPI(),
        build_runtime_container(app=FastAPI(), config={}),
        logger=logger,
        module_names=("private.bootstrap",),
    )

    assert loaded == ()
    assert logger.messages
