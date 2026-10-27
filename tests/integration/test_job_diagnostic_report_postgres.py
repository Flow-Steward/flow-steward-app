"""Real persisted diagnostics must survive both HTTP download renderers.

These fixtures investigate the native generic-500 report; they do not assume
that serialization or the redaction fallback caused that unrelated response.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.application.management.scope import ScopeContext
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    """Run before default_account or any fixture can write to a database."""
    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname == "flow_steward_audit"


@pytest.fixture(params=["failed", "completed"])
def persisted_diagnostic_report(request, pg_conn, default_account):
    from fastapi.testclient import TestClient

    from core.entrypoints.api.app_factory import create_app
    from core.infrastructure.db.platform_stores import (
        PlatformEventStore,
        PlatformJobStore,
        PlatformObservabilityStore,
        PlatformProjectStore,
    )

    project_id, job_id, log_id, message_id, event_id, error_id = (uuid4() for _ in range(6))
    timestamp = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    marker = "diagnostic-fixture-confidential-value"
    stores = []
    client = None
    try:
        dsn = resolve_test_db_dsn()
        named_stores = {}
        for name, store_type in (
            ("job_store", PlatformJobStore),
            ("project_store", PlatformProjectStore),
            ("event_store", PlatformEventStore),
            ("observability_store", PlatformObservabilityStore),
        ):
            store = store_type(dsn)
            stores.append(store)
            named_stores[name] = store

        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, "Diagnostic report fixture", default_account),
            )
            cur.execute(
                "INSERT INTO jobs "
                "(id, project_id, account_id, task_id, task_summary, status, "
                "started_at, finished_at, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    job_id,
                    project_id,
                    default_account,
                    f"diagnostic-{job_id}",
                    "Persisted diagnostic report fixture",
                    request.param,
                    timestamp,
                    timestamp,
                    json.dumps({"password": marker}),
                ),
            )
            cur.execute(
                "INSERT INTO platform_action_log "
                "(id, project_id, job_id, timestamp, agent, action_type, status, "
                "payload, cost_usd, tokens_total, model_used) "
                "VALUES (%s, %s, %s, %s, 'fixture', 'diagnostic_fixture', 'ok', "
                "%s::jsonb, %s, 3, 'fixture-model')",
                (
                    log_id,
                    project_id,
                    job_id,
                    timestamp,
                    json.dumps({"password": marker, "ordinal": 1}),
                    Decimal("0.000123"),
                ),
            )
            cur.execute(
                "INSERT INTO platform_agent_messages "
                "(id, job_id, timestamp, correlation_id, from_agent, to_agent, "
                "step_id, skill, status, summary, envelope) "
                "VALUES (%s, %s, %s, %s, 'fixture', 'orchestrator', "
                "'fixture', 'fixture', 'pass', 'Persisted fixture message', %s::jsonb)",
                (
                    message_id,
                    job_id,
                    timestamp,
                    f"diagnostic-{message_id}",
                    json.dumps(
                        {
                            "schema_version": "agent_message_v1",
                            "from_agent": "fixture",
                            "to_agent": "orchestrator",
                            "step_id": "fixture",
                            "skill": "fixture",
                            "status": "pass",
                            "correlation_id": f"diagnostic-{message_id}",
                            "summary": "Persisted fixture message",
                            "artifacts": {},
                            "errors": [],
                            "timestamps": {
                                "started_at": timestamp.isoformat(),
                                "completed_at": timestamp.isoformat(),
                                "published_at": timestamp.isoformat(),
                            },
                        }
                    ),
                ),
            )
            cur.execute(
                "INSERT INTO platform_events "
                "(id, job_id, timestamp, phase, agent, event_type, title, detail) "
                "VALUES (%s, %s, %s, 'fixture', 'fixture', 'diagnostic_fixture', "
                "'Persisted fixture event', '{}'::jsonb)",
                (event_id, job_id, timestamp),
            )
            cur.execute(
                "INSERT INTO job_errors "
                "(id, job_id, source, category, retryable, error_message, created_at) "
                "VALUES (%s, %s, 'system', 'contract', false, "
                "'Persisted fixture error', %s)",
                (error_id, job_id, timestamp),
            )
        pg_conn.commit()

        app = create_app()
        app.state.local_auth_enabled_override = lambda: False
        app.state.external_auth_enabled_override = lambda: False
        factories = app.state.runtime_container.service_factories
        # Preserve the production narrow-store composition. Overriding "db"
        # would activate the legacy aggregate compatibility branch instead.
        for name, store in named_stores.items():
            factories[name] = lambda selected=store: selected
        service = factories["job_query_service"]()
        # Deliberately do not enter TestClient's lifespan: no scheduler,
        # reconciliation, worker, or monitoring process should start here.
        client = TestClient(app, raise_server_exceptions=True)
        yield SimpleNamespace(
            client=client,
            service=service,
            project_id=str(project_id),
            job_id=str(job_id),
            log_id=str(log_id),
            message_id=str(message_id),
            event_id=str(event_id),
            error_id=str(error_id),
            account_id=str(default_account),
            status=request.param,
            marker=marker,
        )
    finally:
        try:
            pg_conn.rollback()
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM platform_action_log WHERE job_id = %s", (job_id,))
                cur.execute("DELETE FROM platform_events WHERE job_id = %s", (job_id,))
                cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
                cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            pg_conn.commit()
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                for store in reversed(stores):
                    if store._direct_conn is not None:
                        store._direct_conn.close()


def _assert_complete_report(payload, fixture):
    assert payload["schema_version"] == "flow-steward.job-diagnostic-report.v1"
    assert payload["job"]["id"] == fixture.job_id
    assert payload["job"]["project_id"] == fixture.project_id
    assert payload["job"]["status"] == fixture.status
    assert fixture.log_id in {row["id"] for row in payload["logs"]}
    assert fixture.message_id in {row["id"] for row in payload["messages"]}
    assert fixture.event_id in {row["id"] for row in payload["events"]}
    assert fixture.error_id in {row["id"] for row in payload["errors"]}
    assert fixture.marker not in json.dumps(payload, allow_nan=False)


def test_persisted_report_projection_is_json_safe(persisted_diagnostic_report):
    fixture = persisted_diagnostic_report
    payload = asyncio.run(
        fixture.service.get_job_diagnostic_report(
            fixture.job_id,
            scope=ScopeContext(account_id=fixture.account_id),
        )
    )
    # No default=str: distinguish projection failure from transport rendering.
    json.dumps(payload, allow_nan=False)
    _assert_complete_report(payload, fixture)


@pytest.mark.parametrize("download_token", [None, "a" * 32])
def test_persisted_report_http_download_is_complete(persisted_diagnostic_report, download_token):
    fixture = persisted_diagnostic_report
    response = fixture.client.get(
        f"/api/jobs/{fixture.job_id}/report",
        params={"download_token": download_token} if download_token else {},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="flow-steward-job-{fixture.job_id}-report.json"'
    )
    # TestClient consumes the entire stream; 200/cookie alone cannot prove that
    # the tokenized response got beyond its leading whitespace/header flush.
    _assert_complete_report(response.json(), fixture)
    if download_token:
        assert f"fs_job_report_download_{download_token}=1" in response.headers["set-cookie"]
