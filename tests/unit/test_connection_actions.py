from __future__ import annotations

from typing import Any

import pytest


def _platform_connection() -> dict[str, Any]:
    return {
        "runtime_kind": "platform",
        "connector_ref_jsonb": {"connector_id": "platform.postgres"},
        "connection_config_jsonb": {"host": "db.internal", "database": "flow_steward"},
    }


def _mariadb_connection() -> dict[str, Any]:
    return {
        "runtime_kind": "local_extension",
        "connector_ref_jsonb": {"install_id": "mariadb-install"},
        "connection_config_jsonb": {"host": "db.internal", "database": "client"},
    }


def test_platform_postgres_action_does_not_require_extension_runtime(monkeypatch) -> None:
    from core.application.datasets import connection_actions

    def fail_if_called(*_args: Any, **_kwargs: Any) -> tuple[None, None]:
        raise AssertionError("platform connector must not resolve an extension runtime")

    monkeypatch.setattr(connection_actions, "resolve_extension_runtime_record", fail_if_called)
    monkeypatch.setattr(
        connection_actions,
        "run_platform_connection_action",
        lambda _connection, _action, **_kwargs: {
            "ok": True,
            "result": {"connected": True},
            "errors": [],
        },
    )

    assert connection_actions.execute_connection_action(
        _platform_connection(), "test_connection"
    ) == {"ok": True, "result": {"connected": True}, "errors": []}


def test_platform_postgres_introspection_uses_the_infrastructure_adapter(monkeypatch) -> None:
    from core.infrastructure.db import platform_connection_actions

    captured: dict[str, str] = {}

    def introspect(dsn: str, *, schema_name: str) -> dict[str, Any]:
        captured.update(dsn=dsn, schema_name=schema_name)
        return {
            "schema": schema_name,
            "tables": [{"name": "events", "columns": [], "primary_key": [], "indexes": []}],
        }

    monkeypatch.setattr(platform_connection_actions, "introspect_postgres_schema", introspect)
    response = platform_connection_actions.run_platform_connection_action(
        {
            "connection_config_jsonb": {
                "host": "db.internal",
                "database": "flow_steward",
                "user": "reader",
                "schema": "analytics",
            }
        },
        "introspect_schema",
        secret_refs={"values": {"password": "fixture-password"}},  # pragma: allowlist secret
    )

    assert response["ok"] is True
    assert response["result"]["schema_snapshot"]["resources"] == [
        {"resource_id": "analytics.events", "columns": [], "primary_key": [], "indexes": []}
    ]
    assert (
        captured
        == {
            "dsn": "postgresql://reader:fixture-password@db.internal:5432/flow_steward",  # pragma: allowlist secret
            "schema_name": "analytics",
        }
    )


def test_mariadb_action_resolves_its_pinned_extension_runtime(monkeypatch) -> None:
    from core.application.datasets import connection_actions

    extension = {"extension_key": "acme.client-mariadb"}

    monkeypatch.setattr(
        connection_actions,
        "resolve_extension_runtime_record_by_install_id",
        lambda install_id, **_kwargs: (
            (extension, None) if install_id == "mariadb-install" else (None, None)
        ),
    )
    monkeypatch.setattr(
        connection_actions,
        "run_extension_action",
        lambda resolved, **_kwargs: (
            {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "result": {"schema_snapshot": {"resources": [{"resource_id": "orders"}]}},
                    "errors": [],
                },
            }
            if resolved == extension
            else {}
        ),
    )

    response = connection_actions.execute_connection_action(
        _mariadb_connection(), "introspect_schema"
    )

    assert response["ok"] is True
    assert response["result"]["schema_snapshot"]["resources"][0]["resource_id"] == "orders"


def test_mariadb_action_resolves_uuid_pinned_install_without_package_id_lookup(monkeypatch) -> None:
    from core.application.datasets import connection_actions

    install_id = "e8e80d3d-2904-4730-9298-675c44a77677"
    connection = {
        "runtime_kind": "local_extension",
        "connector_ref_jsonb": {
            "install_id": install_id,
            "extension_id": "acme.dataset-connector",
        },
    }
    extension = {"extension_key": "acme.dataset-connector"}

    monkeypatch.setattr(
        connection_actions,
        "resolve_extension_runtime_record_by_install_id",
        lambda candidate, **_kwargs: (extension, None) if candidate == install_id else (None, None),
    )
    monkeypatch.setattr(
        connection_actions,
        "resolve_extension_runtime_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pinned install must not be treated as a package id")
        ),
    )
    monkeypatch.setattr(
        connection_actions,
        "run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "response": {"ok": True, "result": {"connected": True}, "errors": []},
        },
    )

    response = connection_actions.execute_connection_action(connection, "test_connection")

    assert response == {"ok": True, "result": {"connected": True}, "errors": []}


@pytest.mark.parametrize(
    "prebuilt",
    [
        "alloydb-omni",
        "alloydb-postgres",
        "clickhouse",
        "cloud-sql-mssql",
        "cloud-sql-mysql",
        "cloud-sql-postgres",
        "mariadb",
        "mssql",
        "mysql",
        "oceanbase",
        "postgres",
        "singlestore",
        "snowflake",
        "spanner",
        "spanner-postgres",
        "sqlite",
    ],
)
def test_every_published_toolbox_connector_variant_resolves_the_pinned_uuid_install(
    monkeypatch, prebuilt: str
) -> None:
    """Resolution is connector-agnostic; the catalog decides supported variants."""
    from core.application.datasets import connection_actions

    install_id = "e8e80d3d-2904-4730-9298-675c44a77677"
    extension = {"extension_key": "acme.dataset-connector"}
    calls: list[str] = []

    monkeypatch.setattr(
        connection_actions,
        "resolve_extension_runtime_record_by_install_id",
        lambda candidate, **_kwargs: (extension, None) if candidate == install_id else (None, None),
    )
    monkeypatch.setattr(
        connection_actions,
        "resolve_extension_runtime_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a UUID install must not be resolved as a package id")
        ),
    )
    monkeypatch.setattr(
        connection_actions,
        "run_extension_action",
        lambda _extension, **kwargs: (
            calls.append(kwargs["input_payload"]["connection_config"]["prebuilt"])
            or {
                "status": "succeeded",
                "response": {"ok": True, "result": {"connected": True}, "errors": []},
            }
        ),
    )

    response = connection_actions.execute_connection_action(
        {
            "runtime_kind": "local_extension",
            "connector_ref_jsonb": {
                "install_id": install_id,
                "extension_id": "acme.dataset-connector",
                "local_connector_id": f"toolbox_{prebuilt.replace('-', '_')}",
            },
            "connection_config_jsonb": {"prebuilt": prebuilt},
        },
        "test_connection",
    )

    assert response["ok"] is True
    assert calls == [prebuilt]


def test_invalid_connector_binding_returns_safe_test_failure(monkeypatch) -> None:
    from core.application.datasets import connection_actions

    monkeypatch.setattr(
        connection_actions,
        "resolve_extension_runtime_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("internal identifier error")),
    )

    response = connection_actions.execute_connection_action(
        {
            "runtime_kind": "local_extension",
            "connector_ref_jsonb": {"extension_id": "conn_mariadb_connection"},
        },
        "test_connection",
    )

    assert response == {
        "ok": False,
        "result": {},
        "errors": [
            {
                "code": "connector_binding_invalid",
                "message": "The selected data connector is invalid. Save the connection again.",
            }
        ],
    }


def test_unknown_connection_runtime_returns_safe_error() -> None:
    from core.application.datasets.connection_actions import execute_connection_action

    assert execute_connection_action({"runtime_kind": "unknown"}, "test_connection") == {
        "ok": False,
        "result": {},
        "errors": [
            {
                "code": "connector_runtime_unavailable",
                "message": "Data connector is unavailable.",
            }
        ],
    }
