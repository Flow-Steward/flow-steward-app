"""PostgreSQL ownership proof for project-scoped MCP tool projections."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest

from core.application.management.command_models import McpConnectorUpdateInput
from core.application.management.errors import ServiceConflictError
from core.application.management.mcp_connector_services import McpConnectorCommandService
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.infrastructure.db import psycopg_runtime

ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"
TENANT_ID = "00000000-0000-0000-0000-000000000001"
WORKSPACE_ID = "00000000-0000-0000-0000-000000000003"
PROJECT_A = "20000000-0000-0000-0000-000000000001"
PROJECT_B = "20000000-0000-0000-0000-000000000002"
TOOL_ID = "mcp.persistence.proof.list"
RACE_TOOL_ID = "mcp.persistence.proof.race"
ATOMIC_TOOL_ID = "mcp.persistence.proof.atomic"
SERVICE_ATOMIC_TOOL_ID = "mcp.mcp-persistence-service-atomic.records.list"
GENERIC_TOOL_ID = "project.persistence.generic"


@pytest.fixture(autouse=True)
def _cleanup_projected_persistence_rows(disposable_db):
    yield
    with psycopg.connect(disposable_db.dsn) as cleanup:
        tool_ids = [
            TOOL_ID,
            RACE_TOOL_ID,
            ATOMIC_TOOL_ID,
            SERVICE_ATOMIC_TOOL_ID,
            GENERIC_TOOL_ID,
        ]
        cleanup.execute(
            "DELETE FROM public.registry_changelog WHERE entity_type = 'tool' AND entity_id = ANY(%s)",
            (tool_ids,),
        )
        cleanup.execute("DELETE FROM public.registry_tools WHERE tool_id = ANY(%s)", (tool_ids,))
        cleanup.execute(
            "DELETE FROM public.mcp_connectors WHERE connector_id LIKE 'mcp-persistence-%'"
        )
        cleanup.execute(
            "DELETE FROM public.projects WHERE id = ANY(%s)",
            ([PROJECT_A, PROJECT_B],),
        )
        cleanup.commit()


def _seed_projects(conn: psycopg.Connection) -> None:
    for project_id, name in ((PROJECT_A, "MCP owner A"), (PROJECT_B, "MCP owner B")):
        conn.execute(
            """
            INSERT INTO public.projects
                (id, tenant_id, account_id, workspace_id, name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (project_id, TENANT_ID, ACCOUNT_ID, WORKSPACE_ID, name),
        )
    conn.commit()


def _manifest(project_id: str = PROJECT_A, *, tool_id: str = TOOL_ID) -> dict:
    service = McpConnectorCommandService(get_logger=lambda: object())
    return service._build_projected_tool_manifest(
        {
            "connector_id": "mcp-persistence-proof",
            "account_id": ACCOUNT_ID,
            "project_id": project_id,
            "tenant_id": TENANT_ID,
            "display_name": "Persistence proof",
        },
        {
            "remote_tool_name": "records.list",
            "projected_tool_id": tool_id,
            "input_schema": {},
            "output_schema": {},
        },
        enabled=True,
    )


def test_projected_mcp_tool_persists_and_reads_only_in_its_project(disposable_db) -> None:
    with psycopg.connect(disposable_db.dsn) as conn:
        _seed_projects(conn)

    store = RegistryDBStore(disposable_db.dsn)
    store.save_tool(_manifest(), author="system")

    with psycopg.connect(disposable_db.dsn) as conn:
        row = conn.execute(
            """
            SELECT account_id::text, project_id::text, scope_kind,
                   implementation, config
              FROM public.registry_tools
             WHERE tool_id = %s
            """,
            (TOOL_ID,),
        ).fetchone()
    assert row is not None
    account_id, project_id, scope_kind, implementation, config = row
    assert (account_id, project_id, scope_kind) == (ACCOUNT_ID, PROJECT_A, "project")
    assert implementation["owner_account_id"] == ACCOUNT_ID
    assert implementation["owner_project_id"] == PROJECT_A
    assert implementation["owner_tenant_id"] == TENANT_ID
    assert config["owner_project_id"] == PROJECT_A
    assert config["owner_tenant_id"] == TENANT_ID

    project_a_ids = {
        item["tool_id"]
        for item in store.list_tools_summary(account_id=ACCOUNT_ID, project_id=PROJECT_A)
    }
    project_b_ids = {
        item["tool_id"]
        for item in store.list_tools_summary(account_id=ACCOUNT_ID, project_id=PROJECT_B)
    }
    system_ids = {
        item["tool_id"] for item in store.list_tools_summary(account_id=ACCOUNT_ID, project_id=None)
    }
    assert TOOL_ID in project_a_ids
    assert TOOL_ID not in project_b_ids
    assert TOOL_ID not in system_ids


def test_projected_mcp_tool_upsert_cannot_change_ownership(disposable_db) -> None:
    with psycopg.connect(disposable_db.dsn) as conn:
        _seed_projects(conn)

    store = RegistryDBStore(disposable_db.dsn)
    store.save_tool(_manifest(), author="system")
    moved = deepcopy(_manifest(PROJECT_B))

    with pytest.raises(ValueError, match="ownership"):
        store.save_tool(moved, author="system")

    row = store.get_tool(TOOL_ID)
    assert row is not None
    assert str(row["account_id"]) == ACCOUNT_ID
    assert str(row["project_id"]) == PROJECT_A
    assert row["scope_kind"] == "project"


def test_ordinary_project_tool_update_derives_tenant_from_project(disposable_db) -> None:
    with psycopg.connect(disposable_db.dsn) as conn:
        _seed_projects(conn)

    store = RegistryDBStore(disposable_db.dsn)
    store.save_tool(
        {
            "tool_id": GENERIC_TOOL_ID,
            "version": "1.0.0",
            "display_name": "Ordinary project tool",
            "description": "Before update",
            "enabled": True,
            "category": "general",
            "prompt": "",
            "interface": {"functions": []},
            "implementation": {"type": "internal"},
            "config": {},
            "tags": [],
            "account_id": ACCOUNT_ID,
            "project_id": PROJECT_A,
            "tenant_id": TENANT_ID,
            "scope_kind": "project",
        },
        author="project-tool-test",
    )

    persisted = store.get_tool(GENERIC_TOOL_ID)
    assert persisted is not None
    assert "tenant_id" not in persisted
    persisted["description"] = "After update"
    store.save_tool(persisted, author="project-tool-test")

    updated = store.get_tool(GENERIC_TOOL_ID)
    assert updated is not None
    assert updated["description"] == "After update"


def test_concurrent_first_projection_cannot_overwrite_tool_ownership(disposable_db) -> None:
    with psycopg.connect(disposable_db.dsn) as conn:
        _seed_projects(conn)
        conn.execute("DELETE FROM public.registry_tools WHERE tool_id = %s", (RACE_TOOL_ID,))
        conn.commit()

    barrier = Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def _publish(project_id: str) -> None:
        store = RegistryDBStore(disposable_db.dsn)
        barrier.wait(timeout=5)
        try:
            store.save_tool(
                _manifest(project_id, tool_id=RACE_TOOL_ID),
                author="system",
            )
        except ValueError as exc:
            outcomes.append((project_id, str(exc)))
        else:
            outcomes.append((project_id, "saved"))

    threads = [
        Thread(target=_publish, args=(project_id,), daemon=True)
        for project_id in (PROJECT_A, PROJECT_B)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    winners = [project_id for project_id, outcome in outcomes if outcome == "saved"]
    conflicts = [outcome for _project_id, outcome in outcomes if "ownership" in outcome]
    assert len(winners) == 1
    assert len(conflicts) == 1

    winner = winners[0]
    with psycopg.connect(disposable_db.dsn) as conn:
        row = conn.execute(
            """
            SELECT project_id::text, scope_kind, implementation, config
              FROM public.registry_tools
             WHERE tool_id = %s
            """,
            (RACE_TOOL_ID,),
        ).fetchone()
    assert row is not None
    project_id, scope_kind, implementation, config = row
    assert project_id == winner
    assert scope_kind == "project"
    assert implementation["owner_project_id"] == winner
    assert implementation["owner_tenant_id"] == TENANT_ID
    assert config["owner_project_id"] == winner
    assert config["owner_tenant_id"] == TENANT_ID


def test_projection_joins_connector_transaction_and_never_exposes_or_keeps_partial_write(
    disposable_db,
) -> None:
    with psycopg.connect(disposable_db.dsn) as setup:
        _seed_projects(setup)
        setup.execute("DELETE FROM public.registry_tools WHERE tool_id = %s", (ATOMIC_TOOL_ID,))
        setup.commit()

    store = RegistryDBStore(disposable_db.dsn)
    writer = psycopg_runtime.connect(disposable_db.dsn)
    observer = psycopg.connect(disposable_db.dsn)
    try:

        def _raises_operation_259():
            store.save_tool(
                _manifest(PROJECT_A, tool_id=ATOMIC_TOOL_ID),
                author="system",
            )
            assert store.participates_in_transaction(writer)
            visible = observer.execute(
                "SELECT count(*) FROM public.registry_tools WHERE tool_id = %s",
                (ATOMIC_TOOL_ID,),
            ).fetchone()[0]
            assert visible == 0
            raise RuntimeError("fault after projection")

        with (
            pytest.raises(RuntimeError, match="fault after projection"),
            writer.transaction(),
            store.participate_in_external_transaction(writer),
        ):
            _raises_operation_259()

        persisted = observer.execute(
            "SELECT count(*) FROM public.registry_tools WHERE tool_id = %s",
            (ATOMIC_TOOL_ID,),
        ).fetchone()[0]
        assert persisted == 0
    finally:
        writer.close()
        observer.close()


def test_service_projection_rollback_never_deadlocks_or_publishes_cache_before_commit(
    disposable_db,
) -> None:
    connector_id = "mcp-persistence-service-atomic"
    with psycopg.connect(disposable_db.dsn) as setup:
        _seed_projects(setup)
        setup.execute(
            "DELETE FROM public.registry_tools WHERE tool_id = %s",
            (SERVICE_ATOMIC_TOOL_ID,),
        )
        setup.execute("DELETE FROM public.mcp_connectors WHERE connector_id = %s", (connector_id,))
        setup.execute(
            """INSERT INTO public.mcp_connectors
                   (connector_id, account_id, project_id, tenant_id, display_name,
                    base_url, auth_mode, discovered_tools_jsonb, allowlisted_tools,
                    last_sync_status)
                VALUES (%s, %s, %s, %s, 'Atomic service proof',
                        'https://mcp.example.test/mcp', 'none', %s::jsonb,
                        '[]'::jsonb, 'success')""",
            (
                connector_id,
                ACCOUNT_ID,
                PROJECT_A,
                TENANT_ID,
                psycopg.types.json.Jsonb(
                    [
                        {
                            "remote_tool_name": "records.list",
                            "projected_tool_id": SERVICE_ATOMIC_TOOL_ID,
                            "display_name": "Records list",
                            "input_schema": {},
                            "output_schema": {},
                            "annotations": {},
                        }
                    ]
                ),
            ),
        )
        setup.commit()

    writer = psycopg_runtime.connect(disposable_db.dsn)
    writer.autocommit = True
    observer = psycopg.connect(disposable_db.dsn, autocommit=True)
    store = RegistryDBStore(disposable_db.dsn)
    cache_visibility: list[int] = []

    def reload_all() -> None:
        cache_visibility.append(
            observer.execute(
                "SELECT count(*) FROM public.registry_tools WHERE tool_id = %s",
                (SERVICE_ATOMIC_TOOL_ID,),
            ).fetchone()[0]
        )

    manager = SimpleNamespace(db_store=store, reload_all=reload_all)
    logger = SimpleNamespace(conn=writer, cursor=writer.cursor)
    service = McpConnectorCommandService(get_logger=lambda: logger)
    scope = ScopeContext(
        account_id=ACCOUNT_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_A,
        actor_id="projection-atomicity",
        principal_type="operator",
    )
    update = McpConnectorUpdateInput(allowlisted_tools=[SERVICE_ATOMIC_TOOL_ID])
    try:
        original_update = service._update_connector_row

        def fail_after_connector_update(*args, **kwargs):
            original_update(*args, **kwargs)
            raise RuntimeError("fault after connector and projection writes")

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=manager,
            ),
            patch.object(
                service,
                "_update_connector_row",
                side_effect=fail_after_connector_update,
            ),
            pytest.raises(RuntimeError, match="fault after connector and projection writes"),
        ):
            service.update_connector(connector_id, update, scope=scope)

        assert cache_visibility == []
        assert (
            observer.execute(
                "SELECT count(*) FROM public.registry_tools WHERE tool_id = %s",
                (SERVICE_ATOMIC_TOOL_ID,),
            ).fetchone()[0]
            == 0
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=manager,
        ):
            service.update_connector(connector_id, update, scope=scope)
        assert cache_visibility == [1]
    finally:
        observer.execute(
            "DELETE FROM public.registry_tools WHERE tool_id = %s", (SERVICE_ATOMIC_TOOL_ID,)
        )
        observer.execute(
            "DELETE FROM public.mcp_connectors WHERE connector_id = %s", (connector_id,)
        )
        writer.close()
        observer.close()


def test_sync_cannot_overwrite_a_concurrent_connector_update(disposable_db) -> None:
    connector_id = "mcp-persistence-sync-update"
    with psycopg.connect(disposable_db.dsn) as setup:
        _seed_projects(setup)
        setup.execute("DELETE FROM public.mcp_connectors WHERE connector_id = %s", (connector_id,))
        setup.execute(
            """INSERT INTO public.mcp_connectors
                   (connector_id, account_id, project_id, tenant_id, display_name,
                    base_url, auth_mode)
                VALUES (%s, %s, %s, %s, 'Sync update race',
                        'https://mcp.example.test/mcp', 'none')""",
            (connector_id, ACCOUNT_ID, PROJECT_A, TENANT_ID),
        )
        setup.commit()

    sync_conn = psycopg_runtime.connect(disposable_db.dsn)
    update_conn = psycopg_runtime.connect(disposable_db.dsn)
    sync_conn.autocommit = True
    update_conn.autocommit = True
    sync_service = McpConnectorCommandService(
        get_logger=lambda: SimpleNamespace(conn=sync_conn, cursor=sync_conn.cursor)
    )
    update_service = McpConnectorCommandService(
        get_logger=lambda: SimpleNamespace(conn=update_conn, cursor=update_conn.cursor)
    )
    scope = ScopeContext(
        account_id=ACCOUNT_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_A,
        actor_id="sync-update-race",
        principal_type="operator",
    )
    sync_started = Event()
    release_sync = Event()
    sync_errors: list[BaseException] = []

    @contextmanager
    def blocking_rpc_context(*_args, **_kwargs):
        def rpc_post(_payload):
            sync_started.set()
            assert release_sync.wait(timeout=10)
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 1.0

        yield rpc_post

    def run_sync() -> None:
        try:
            sync_service.sync_connector(connector_id, scope=scope)
        except BaseException as exc:
            sync_errors.append(exc)

    thread = Thread(target=run_sync, daemon=True)
    try:
        with patch(
            "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
            blocking_rpc_context,
        ):
            thread.start()
            assert sync_started.wait(timeout=10)
            update_service.update_connector(
                connector_id,
                McpConnectorUpdateInput(base_url="https://93.184.216.34/changed-mcp"),
                scope=scope,
            )
            release_sync.set()
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(sync_errors) == 1
        assert isinstance(sync_errors[0], ServiceConflictError)
        persisted = update_conn.execute(
            "SELECT base_url, last_sync_status FROM public.mcp_connectors WHERE connector_id = %s",
            (connector_id,),
        ).fetchone()
        assert persisted == ("https://93.184.216.34/changed-mcp", "never")
    finally:
        release_sync.set()
        thread.join(timeout=1)
        update_conn.execute(
            "DELETE FROM public.mcp_connectors WHERE connector_id = %s", (connector_id,)
        )
        sync_conn.close()
        update_conn.close()
