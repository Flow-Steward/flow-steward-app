from __future__ import annotations

import os
import subprocess
import sys


def test_production_asgi_lifespan_starts_without_pytest_facade_sync() -> None:
    script = """
from fastapi.testclient import TestClient
from dataclasses import replace
from unittest.mock import patch
from unittest.mock import MagicMock
from core.entrypoints.api.app import app
from core.infrastructure.runtime.serve_request_runtime import route_registered

fake_db = MagicMock()
fake_db.conn = None
fake_db.dsn = ""
fake_db.list_extensions.return_value = []
fake_db.sync_extension_scan_records.return_value = ([], [])
app.state.runtime_container.service_factories["db"] = lambda: fake_db
app.state.lifecycle_runtime_bindings = replace(
    app.state.lifecycle_runtime_bindings,
    get_logger_only=lambda: fake_db,
)

assert app.state.runtime_container is not None
assert app.state.auth_users_router is not None
assert route_registered(app.router, "/health", "GET")
with patch("core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn"):
    with TestClient(app, raise_server_exceptions=False) as client:
        assert len(app.routes) > 500
        response = client.get("/health")
        assert response.status_code == 200, response.text
"""
    env = dict(os.environ)
    env.update(
        {
            "CONTROL_LOOPS_ENABLED": "0",
            "FS_MEMORY_VECTOR_BACKEND": "null",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    assert "is not defined" not in completed.stderr


def test_production_protected_routes_do_not_crash_without_pytest_facade_sync() -> None:
    script = """
from fastapi.testclient import TestClient
from dataclasses import replace
from unittest.mock import patch
from unittest.mock import MagicMock
from core.entrypoints.api.app import app
from core.infrastructure.runtime.http_app_auth_runtime import (
    _local_auth_bootstrap_required,
)

fake_db = MagicMock()
fake_db.conn = None
fake_db.dsn = ""
fake_db.list_extensions.return_value = []
fake_db.sync_extension_scan_records.return_value = ([], [])
app.state.runtime_container.service_factories["db"] = lambda: fake_db
app.state.lifecycle_runtime_bindings = replace(
    app.state.lifecycle_runtime_bindings,
    get_logger_only=lambda: fake_db,
)

with patch("core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn"):
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ("/api/projects", "/api/jobs", "/api/auth/me"):
            response = client.get(path)
            assert response.status_code != 500, (path, response.text)

assert _local_auth_bootstrap_required(get_logger_only_fn=lambda: None) is False
"""
    env = dict(os.environ)
    env.update(
        {
            "CONTROL_LOOPS_ENABLED": "0",
            "FS_LOCAL_AUTH_ENABLED": "1",
            "FS_MEMORY_VECTOR_BACKEND": "null",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    assert "is not defined" not in completed.stderr


def test_create_app_keeps_lifecycle_bindings_per_app() -> None:
    script = """
from core.entrypoints.api.app_factory import create_app

app1 = create_app()
app2 = create_app()
app1.state.local_auth_enabled_override = lambda: False
app2.state.local_auth_enabled_override = lambda: True

bindings1 = app1.state.lifecycle_runtime_bindings
bindings2 = app2.state.lifecycle_runtime_bindings
assert bindings1 is not bindings2
assert bindings1.local_auth_enabled() is False
assert bindings2.local_auth_enabled() is True
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=dict(os.environ),
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr


def test_production_extension_binding_uses_canonical_event_ingest() -> None:
    script = """
from core.application.management.command_services import EventCommandService
from core.entrypoints.api.app import app

factories = app.state.runtime_container.service_factories
extension_service = factories["extension_binding_service"]()
event_factory = extension_service._event_command_service_factory
assert event_factory is not None
assert isinstance(event_factory(), EventCommandService)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=dict(os.environ),
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr


def test_production_extension_binding_configures_query_service_for_uninstall_plans() -> None:
    script = """
from unittest.mock import MagicMock

from core.entrypoints.api.app import app

factories = app.state.runtime_container.service_factories
extension_service = factories["extension_binding_service"]()
query_service = extension_service._context.query_service(
    MagicMock(),
    install_store=MagicMock(),
    runtime_store=MagicMock(),
)

assert query_service.__class__.__name__ == "ExtensionQueryService"
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=dict(os.environ),
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
