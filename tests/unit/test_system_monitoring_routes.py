from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
    "psycopg_runtime",
    "langgraph",
    "langchain_core",
}


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


def _assert_frontend_shell_response(resp) -> str:
    assert resp.status_code == 200
    text = resp.text
    assert "Flow Steward" in text or "AI Orchestrator" not in text
    assert '<div id="root"></div>' in text
    assert "/app/assets/index-" in text
    return text


@pytest.fixture
def client():
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app import create_app

        current_app = create_app()
        current_app.state.local_auth_enabled_override = lambda: False
        current_app.state.external_auth_enabled_override = lambda: False
        yield TestClient(current_app, raise_server_exceptions=False)
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="Could not create TestClient")


@pytest.fixture
def api_client():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core.entrypoints.api.system_monitoring import create_api_system_monitoring_router

        app = FastAPI()
        app.include_router(
            create_api_system_monitoring_router(
                local_auth_enabled=lambda: False,
                external_auth_enabled=lambda: False,
                require_authenticated_user=lambda *_args, **_kwargs: {},
                auth_admin_roles={"admin"},
                components_query=lambda _bl: _stored_components(),
            )
        )
        yield TestClient(app, raise_server_exceptions=False)
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="Could not create API TestClient")


@pytest.fixture
def api_client_with_bool_external_flag():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core.entrypoints.api.system_monitoring import create_api_system_monitoring_router

        app = FastAPI()
        app.include_router(
            create_api_system_monitoring_router(
                local_auth_enabled=lambda: False,
                external_auth_enabled=False,
                require_authenticated_user=lambda *_args, **_kwargs: {},
                auth_admin_roles={"admin"},
                components_query=lambda _bl: _stored_components(),
            )
        )
        yield TestClient(app, raise_server_exceptions=False)
    except Exception as exc:
        _skip_only_missing_optional_dep(
            exc, context="Could not create bool external auth API client"
        )


def test_monitoring_page_renders(client):
    response = client.get("/monitoring")
    _assert_frontend_shell_response(response)


def _stored_components(*, stale=False, unavailable=False):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from core.application.monitoring.contracts import RuntimeInstallationContext, StatusRecord
    from core.application.monitoring.queries import RuntimeStatusQueryService

    now = datetime.now(UTC)
    checked = now - timedelta(seconds=181 if stale else 1)
    context = RuntimeInstallationContext(installation_id="test", deployment_profile="scalable")
    row = StatusRecord.model_validate(
        {
            "schema_version": 1,
            "installation_id": "test",
            "deployment_profile": "scalable",
            "service_type": "image-indexer",
            "instance_id": uuid4(),
            "incarnation_id": uuid4(),
            "sequence": 1,
            "observed_at": checked,
            "received_at": checked,
            "id": uuid4(),
            "source": "self",
            "payload": {
                "type": "runtime_status",
                "version": 1,
                "lifecycle": "running",
                "readiness": {"state": "not_ready", "checked_at": checked, "evidence": "probe"},
                "health": {"state": "unhealthy", "checked_at": checked, "evidence": "probe"},
            },
        }
    )

    class Reader:
        def load(self, _context):
            if unavailable:
                raise ConnectionError("private database driver details")
            return [row]

    return RuntimeStatusQueryService(repository=Reader()).get_components(context, now=now)


def _component_api_client(projection, queries):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.entrypoints.api.system_monitoring import create_api_system_monitoring_router

    def authorized(request, *, required_roles):
        assert required_roles == {"admin"}
        if request.headers.get("x-test-role") != "admin":
            raise PermissionError
        return {"roles": ["admin"]}

    def query(_bl):
        queries.append(True)
        return projection

    app = FastAPI()
    app.include_router(
        create_api_system_monitoring_router(
            local_auth_enabled=lambda: True,
            external_auth_enabled=False,
            require_authenticated_user=authorized,
            auth_admin_roles={"admin"},
            components_query=query,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("stale", [False, True])
def test_stored_unhealthy_or_stale_report_is_http_200_without_discovery(stale):
    queries = []
    client = _component_api_client(_stored_components(stale=stale), queries)
    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=object()),
        patch(
            "core.entrypoints.api.system_monitoring.deps.compute_readiness",
            return_value={"ready": False, "checks": {"postgres": {"ok": False}}},
        ),
        patch("core.entrypoints.api.system_monitoring.build_monitoring_meta", return_value={}),
        patch(
            "core.infrastructure.monitoring.docker_containers.list_monitoring_containers",
            side_effect=AssertionError("Docker read forbidden"),
        ),
        patch(
            "core.infrastructure.runtime.monitoring.heartbeat_transport.RabbitHeartbeatPublisher.publish",
            side_effect=AssertionError("publish forbidden"),
        ),
        patch(
            "core.infrastructure.runtime.monitoring.heartbeat_transport.RabbitHeartbeatConsumer.run",
            side_effect=AssertionError("consume forbidden"),
        ),
    ):
        response = client.get("/api/system/monitoring/summary", headers={"x-test-role": "admin"})
    assert response.status_code == 200
    body = response.json()
    assert body["components"]["rows"][0]["service_type"] == "image-indexer"
    assert body["components"]["rows"][0]["freshness"] == ("stale" if stale else "fresh")
    assert body["components"]["counts"]["instances_evidenced_healthy"] == 0
    assert queries == [True]


def test_component_authorization_precedes_query():
    queries = []
    client = _component_api_client(_stored_components(), queries)
    assert client.get("/api/system/monitoring/summary").status_code == 403
    assert queries == []


def test_missing_component_dsn_never_opens_an_implicit_database():
    from uuid import uuid4

    from core.application.monitoring.system import query_runtime_components

    with (
        patch("core.application.monitoring.system.runtime_env") as values,
        patch("core.application.monitoring.system.resolve_postgres_dsn", return_value=""),
        patch("core.application.monitoring.system.RuntimeStatusRepository") as repository,
    ):
        values.copy.return_value = {
            "FS_INSTALLATION_ID": str(uuid4()),
            "FS_DEPLOYMENT_PROFILE": "scalable",
        }
        result = query_runtime_components(object())
    repository.assert_not_called()
    assert result is not None
    assert result.available is False
    assert result.counts is None


def test_unavailable_stored_report_is_not_empty_green():
    queries = []
    client = _component_api_client(_stored_components(unavailable=True), queries)
    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=object()),
        patch(
            "core.entrypoints.api.system_monitoring.deps.compute_readiness",
            return_value={"ready": False, "checks": {}},
        ),
        patch("core.entrypoints.api.system_monitoring.build_monitoring_meta", return_value={}),
    ):
        response = client.get("/api/system/monitoring/summary", headers={"x-test-role": "admin"})
    assert response.status_code == 503
    assert response.json()["components"]["available"] is False
    assert response.json()["components"]["counts"] is None
    assert "private" not in response.text


def test_monitoring_summary_api_returns_combined_payload(api_client):
    fake_bl = MagicMock()
    fake_bl.conn = MagicMock()
    fake_cursor = fake_bl.conn.cursor.return_value.__enter__.return_value
    fake_cursor.fetchone.side_effect = [
        [3],  # projects
        [4],  # pending
        [0],  # resume_pending
        [2],  # in_progress
        [1],  # awaiting_approval
        [0],  # awaiting_timer
        [0],  # awaiting_child
        [0],  # stuck
        [0],  # orphaned
        [2],  # active leases
        [0, 0, 0, 0],  # queue delivery summary
    ]
    fake_cursor.fetchall.return_value = []
    fake_bl._table_exists.return_value = True

    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=fake_bl),
        patch(
            "core.entrypoints.api.system_monitoring.deps.compute_readiness",
            return_value={
                "ready": True,
                "checks": {
                    "postgres": {"ok": True, "error": ""},
                    "rabbitmq": {
                        "ok": True,
                        "error": "",
                        "state": "broker_available",
                        "status_label": "Broker available",
                    },
                    "queue_delivery": {
                        "ok": True,
                        "state": "retrying_delivery",
                        "state_label": "Retrying delivery",
                        "retrying_delivery_jobs": 2,
                        "dead_lettered_jobs": 0,
                    },
                },
            },
        ),
        patch("core.entrypoints.api.system_monitoring.deps.get_ready_queue_depth", return_value=5),
        patch(
            "core.entrypoints.api.system_monitoring.deps.env_flag",
            side_effect=lambda _name, default: default,
        ),
        patch(
            "core.entrypoints.api.system_monitoring.deps.local_auth_enabled",
            return_value=False,
        ),
        patch(
            "core.infrastructure.monitoring.docker_containers.list_monitoring_containers",
            return_value={
                "available": True,
                "services": [{"service": "fs-app", "tone": "green"}],
            },
        ),
        patch(
            "core.entrypoints.api.system_monitoring.extension_runtime_target",
            return_value="linux-arm64",
        ),
    ):
        response = api_client.get("/api/system/monitoring/summary")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True
    assert body["meta"]["project_count"] == 3
    assert body["meta"]["ready_queue_depth"] == 5
    assert body["meta"]["extension_runtime_target"] == "linux-arm64"
    assert body["meta"]["queue_delivery"]["state"] == "idle"
    assert body["checks"]["queue_delivery"]["state"] == "retrying_delivery"
    assert "containers" not in body


def test_monitoring_summary_is_a_successful_response_when_the_deployment_is_not_ready(
    api_client,
):
    """A produced report is a 200, even when what it reports is unhealthy.

    Mirroring the readiness probe's 503 here broke the dashboard in exactly the
    situation it exists for: the browser client rejects non-2xx, so the page
    rendered the zero-defaults of an absent payload and showed "Failed: 0"
    while checks were failing.
    """
    fake_bl = MagicMock()
    fake_bl.conn = MagicMock()
    fake_cursor = fake_bl.conn.cursor.return_value.__enter__.return_value
    fake_cursor.fetchone.side_effect = [
        [3],  # projects
        [0],  # pending
        [0],  # resume_pending
        [0],  # in_progress
        [0],  # awaiting_approval
        [0],  # awaiting_timer
        [0],  # awaiting_child
        [1],  # stuck
        [0],  # orphaned
        [0],  # active leases
        [0, 0, 0, 0],  # queue delivery summary
    ]
    fake_cursor.fetchall.return_value = []
    fake_bl._table_exists.return_value = True

    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=fake_bl),
        patch(
            "core.entrypoints.api.system_monitoring.deps.compute_readiness",
            return_value={
                "ready": False,
                "checks": {
                    "postgres": {"ok": True, "error": ""},
                    "stuck_jobs": {"ok": False, "detail": "1 persisted stuck job(s)."},
                },
            },
        ),
        patch("core.entrypoints.api.system_monitoring.deps.get_ready_queue_depth", return_value=0),
        patch(
            "core.entrypoints.api.system_monitoring.deps.env_flag",
            side_effect=lambda _name, default: default,
        ),
        patch(
            "core.entrypoints.api.system_monitoring.deps.local_auth_enabled",
            return_value=False,
        ),
        patch(
            "core.infrastructure.monitoring.docker_containers.list_monitoring_containers",
            return_value={"available": True, "services": []},
        ),
        patch(
            "core.entrypoints.api.system_monitoring.extension_runtime_target",
            return_value="linux-arm64",
        ),
    ):
        response = api_client.get("/api/system/monitoring/summary")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is False, "the unhealthy state belongs in the body, not the status code"
    assert body["summary"]["checks_failed"] >= 1
    assert "stuck_jobs" in body["checks"]


def test_monitoring_summary_includes_runtime_target_when_database_is_not_ready(api_client):
    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=None),
        patch(
            "core.infrastructure.monitoring.docker_containers.list_monitoring_containers",
            return_value={"available": False, "services": []},
        ),
        patch(
            "core.entrypoints.api.system_monitoring.extension_runtime_target",
            return_value="linux-amd64",
        ),
    ):
        response = api_client.get("/api/system/monitoring/summary")

    assert response.status_code == 503
    assert response.json()["meta"] == {"extension_runtime_target": "linux-amd64"}


def test_monitoring_summary_accepts_bool_external_auth_flag(api_client_with_bool_external_flag):
    fake_bl = MagicMock()
    fake_bl.conn = MagicMock()
    fake_cursor = fake_bl.conn.cursor.return_value.__enter__.return_value
    fake_cursor.fetchone.side_effect = [
        [0],
        [0],
        [0],
        [0],
        [0],
        [0],
        [0],
        [0],
        [0],
        [0, 0, 0, 0],
    ]
    fake_cursor.fetchall.return_value = []
    fake_bl._table_exists.return_value = True

    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=fake_bl),
        patch(
            "core.entrypoints.api.system_monitoring.deps.compute_readiness",
            return_value={"ready": True, "checks": {}},
        ),
        patch("core.entrypoints.api.system_monitoring.deps.get_ready_queue_depth", return_value=0),
        patch(
            "core.entrypoints.api.system_monitoring.deps.env_flag",
            side_effect=lambda _name, default: default,
        ),
        patch(
            "core.entrypoints.api.system_monitoring.deps.local_auth_enabled",
            return_value=False,
        ),
        patch(
            "core.infrastructure.monitoring.docker_containers.list_monitoring_containers",
            return_value={"available": False, "services": []},
        ),
        patch(
            "core.entrypoints.api.system_monitoring.extension_runtime_target",
            return_value="linux-amd64",
        ),
    ):
        response = api_client_with_bool_external_flag.get("/api/system/monitoring/summary")

    assert response.status_code == 200
    assert response.json()["meta"]["external_auth_enabled"] is False


def test_monitoring_storage_api_returns_storage_payload(api_client):
    fake_bl = MagicMock()
    storage_payload = {
        "generated_at": "2026-05-11T18:00:00+00:00",
        "backend_matrix": {"vector": "postgres_pgvector"},
        "summary": {"orphan_memory_projects": 1},
        "memory": {"available": True, "rows": [], "orphan_rows": []},
        "vector": {
            "available": True,
            "backend": "postgres_pgvector",
            "rows": [],
            "orphan_rows": [],
        },
    }

    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=fake_bl),
        patch(
            "core.entrypoints.api.system_monitoring.build_storage_monitoring_payload",
            return_value=storage_payload,
        ),
    ):
        response = api_client.get("/api/system/monitoring/storage")

    assert response.status_code == 200
    body = response.json()
    assert body["backend_matrix"]["vector"] == "postgres_pgvector"
    assert body["summary"]["orphan_memory_projects"] == 1


def test_monitoring_storage_orphan_cleanup_api_returns_result(api_client):
    fake_bl = MagicMock()
    cleanup_payload = {
        "status": "deleted",
        "backend": "memory",
        "scope": "project-missing",
        "deleted": {"records": 2, "entities": 3},
    }

    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=fake_bl),
        patch(
            "core.entrypoints.api.system_monitoring.cleanup_orphan_storage_scope",
            return_value=cleanup_payload,
        ) as cleanup_mock,
    ):
        response = api_client.post(
            "/api/system/monitoring/storage/orphans/cleanup",
            json={
                "backend": "memory",
                "scope": "project-missing",
                "confirm": "DELETE_ORPHAN_STORAGE",
            },
        )

    assert response.status_code == 200
    assert response.json()["deleted"]["records"] == 2
    cleanup_mock.assert_called_once_with(
        fake_bl,
        backend="memory",
        scope="project-missing",
        confirm="DELETE_ORPHAN_STORAGE",
    )


def test_monitoring_storage_api_masks_internal_exception(api_client):
    fake_bl = MagicMock()
    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=fake_bl),
        patch(
            "core.entrypoints.api.system_monitoring.build_storage_monitoring_payload",
            side_effect=RuntimeError("postgres://user:secret@db/internal_table"),
        ),
    ):
        response = api_client.get("/api/system/monitoring/storage")

    assert response.status_code == 500
    assert response.json()["error"] == "Storage monitoring failed"
    assert response.json()["correlation_id"]
    assert "secret" not in response.text


def test_build_storage_monitoring_payload_handles_missing_embedding_column():
    import core.application.monitoring.system as system_monitoring

    fake_bl = MagicMock()
    fake_bl.conn = MagicMock()
    fake_bl._column_exists.side_effect = lambda table, column: (
        not (table == "memory_records" and column == "embedding")
    )
    fake_cursor = fake_bl.conn.cursor.return_value.__enter__.return_value
    fake_cursor.fetchall.side_effect = [
        [
            ("project-1", "Demo Project", None),
        ],
        [
            ("project-1", 4, 0, 2, 1),
        ],
    ]

    fake_config = SimpleNamespace(vector_backend="null")
    fake_workflow_knowledge_store = MagicMock()
    fake_workflow_knowledge_store.runtime_status.return_value = {
        "ok": False,
        "graph_name": "platform_workflow_graph",
        "graph": {"available": False, "error": "Apache AGE extension is missing."},
        "vector": {"available": False, "error": "pgvector extension is missing."},
    }

    with (
        patch(
            "core.infrastructure.config.memory_config.load_memory_runtime_config",
            return_value=fake_config,
        ),
        patch(
            "core.infrastructure.config.memory_config.redacted_memory_backend_matrix",
            return_value={"vector": "null"},
        ),
        patch(
            "core.infrastructure.db.workflow_knowledge_store.PostgresAgeWorkflowKnowledgeStore",
            return_value=fake_workflow_knowledge_store,
        ),
    ):
        payload = system_monitoring.build_storage_monitoring_payload(fake_bl)

    assert payload["summary"]["active_projects"] == 1
    assert payload["memory"]["embedding_column_available"] is False
    assert payload["memory"]["rows"][0]["embedded_records"] == 0
    assert payload["vector"]["configured"] is False
    assert payload["vector"]["rows"] == []
    assert payload["workflow_knowledge"]["available"] is False
    assert payload["workflow_knowledge"]["graph"]["error"] == "Apache AGE extension is missing."


def test_cleanup_orphan_storage_scope_rejects_memory_scope_with_project_row():
    import core.application.monitoring.system as system_monitoring
    from core.application.management.errors import ServiceConflictError

    fake_bl = MagicMock()
    fake_bl.conn = MagicMock()
    fake_cursor = fake_bl.conn.cursor.return_value.__enter__.return_value
    fake_cursor.fetchone.return_value = ("project-1", "Demo", None)

    with pytest.raises(ServiceConflictError):
        system_monitoring.cleanup_orphan_storage_scope(
            fake_bl,
            backend="memory",
            scope="project-1",
            confirm="DELETE_ORPHAN_STORAGE",
        )


def test_cleanup_orphan_storage_scope_has_no_graph_backend():
    import core.application.monitoring.system as system_monitoring
    from core.application.management.errors import ServiceValidationError

    fake_bl = MagicMock()
    fake_bl.conn = MagicMock()

    with pytest.raises(ServiceValidationError, match="Unsupported storage backend"):
        system_monitoring.cleanup_orphan_storage_scope(
            fake_bl,
            backend="graph",
            scope="project:project-1",
            confirm="DELETE_ORPHAN_STORAGE",
        )
    fake_bl.conn.cursor.assert_not_called()


def test_monitoring_service_specs_use_canonical_runtime_services():
    import importlib

    import core.infrastructure.monitoring.docker_containers as docker_containers

    reloaded = importlib.reload(docker_containers)
    try:
        services = {row["service"] for row in reloaded._MONITORING_SERVICE_SPECS}
        assert {"fs-app", "fs-worker", "fs-mcp", "pg-fs", "rabbitmq-fs"} <= services
    finally:
        importlib.reload(docker_containers)


def test_monitoring_service_detection_does_not_alias_old_brand_stack():
    import core.infrastructure.monitoring.docker_containers as docker_containers

    retired_token = "retiredcodename"
    legacy_app = f"{retired_token}-app"
    legacy_rabbit = f"rabbitmq-{retired_token}"
    assert (
        docker_containers.infer_compose_service(
            {
                "Labels": f"com.docker.compose.service={legacy_app}",
                "Names": f"flow-steward-{legacy_app}-1",
            }
        )
        == legacy_app
    )
    assert (
        docker_containers.infer_compose_service(
            {
                "Labels": f"com.docker.compose.service={legacy_rabbit}",
                "Names": f"flow-steward-{legacy_rabbit}-1",
            }
        )
        == legacy_rabbit
    )
    assert (
        docker_containers.infer_compose_service(
            {"Labels": "", "Names": f"flow-steward-{legacy_app}-1"}
        )
        == ""
    )


def test_compact_reports_container_discovery_as_inapplicable(monkeypatch):
    """AC17-R69: the Services panel blamed the operator for a deliberate absence.

    Compact runs every service as a process in one container and mounts no Docker socket, on
    purpose — mounting it would hand the runtime control of the host's Docker. The page
    reported "Docker socket UNAVAILABLE — Required for container-level service discovery",
    which reads as a misconfiguration to go and fix. The operator cannot, and should not.
    """
    from core.infrastructure.monitoring import docker_containers

    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    payload = docker_containers.list_monitoring_containers()

    assert payload["container_discovery_applicable"] is False
    assert "by design" in payload["error"]
    assert "not mounted into the app container" not in payload["error"]


def test_scalable_still_reports_a_missing_socket_as_a_problem(monkeypatch):
    """Where containers do exist, an absent socket is still a real fault."""
    from core.infrastructure.monitoring import docker_containers

    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "scalable")
    monkeypatch.setattr(docker_containers, "docker_socket_available", lambda: False)
    payload = docker_containers.list_monitoring_containers()

    assert payload["container_discovery_applicable"] is True
    assert payload["available"] is False
    assert "not mounted into the app container" in payload["error"]
