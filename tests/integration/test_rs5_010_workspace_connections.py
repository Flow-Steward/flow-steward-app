from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from core.application.management.command_services import TrashLifecycleService
from core.application.management.scope import ScopeContext
from tests._db_env import resolve_test_db_dsn


class _WorkspaceDeleteStore:
    def __init__(self, dsn: str) -> None:
        self.conn = psycopg.connect(dsn, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name = %s
                       AND column_name = %s
                )
                """,
                (table_name, column_name),
            )
            return bool(cur.fetchone()[0])

    def list_datasets(self, **_kwargs):
        return []

    def list_workspace_data_connections(
        self, *, workspace_id: str | None = None, include_trashed: bool = False
    ) -> list[dict]:
        clauses = ["workspace_id = %s"]
        params: list[object] = [workspace_id]
        if not include_trashed:
            clauses.append("deleted_at IS NULL")
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT c.*, 0 AS linked_dataset_count
                  FROM workspace_data_connections c
                 WHERE {" AND ".join(clauses)}
                 ORDER BY connection_id
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


@pytest.mark.integration
def test_workspace_delete_rolls_back_connection_reassignment_on_later_failure(
    monkeypatch,
) -> None:
    if os.environ.get("FS_AUDIT_ISOLATED") != "1":
        pytest.skip("requires the isolated audit PostgreSQL")
    audit_dsn = resolve_test_db_dsn()
    if not audit_dsn:
        pytest.skip("isolated audit PostgreSQL DSN is not configured")
    account_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    source_workspace_id = str(uuid.uuid4())
    target_workspace_id = str(uuid.uuid4())
    connection_id = f"rs5-010-{uuid.uuid4()}"
    setup_conn = psycopg.connect(audit_dsn, autocommit=True)
    store = _WorkspaceDeleteStore(audit_dsn)
    try:
        with setup_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts (id, tenant_id, account_key, display_name)
                VALUES (%s, %s, %s, 'RS5-010 account')
                """,
                (account_id, tenant_id, f"rs5-010-{account_id}"),
            )
            cur.executemany(
                """
                INSERT INTO workspaces
                    (id, tenant_id, account_id, workspace_key, display_name)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        source_workspace_id,
                        tenant_id,
                        account_id,
                        f"source-{source_workspace_id}",
                        "RS5-010 source",
                    ),
                    (
                        target_workspace_id,
                        tenant_id,
                        account_id,
                        f"target-{target_workspace_id}",
                        "RS5-010 target",
                    ),
                ],
            )
            cur.execute(
                """
                INSERT INTO workspace_data_connections
                    (connection_id, tenant_id, account_id, workspace_id, display_name)
                VALUES (%s, %s, %s, %s, 'RS5-010 connection')
                """,
                (connection_id, tenant_id, account_id, source_workspace_id),
            )

        service = TrashLifecycleService(get_logger=lambda: store)
        original_move = service._move_workspace_data_connection_direct

        def move_then_fail(bl, *, connection_id: str, workspace_id: str) -> None:
            original_move(bl, connection_id=connection_id, workspace_id=workspace_id)
            raise RuntimeError("synthetic failure after connection mutation")

        monkeypatch.setattr(service, "_move_workspace_data_connection_direct", move_then_fail)
        scope = ScopeContext(
            account_id=account_id,
            tenant_id=tenant_id,
            actor_id="rs5-010-auditor",
            principal_type="admin",
            allowed_workspace_ids=frozenset({source_workspace_id, target_workspace_id}),
        )

        with pytest.raises(RuntimeError, match="synthetic failure"):
            service.trash_workspace_with_plan(
                source_workspace_id,
                deleted_by="rs5-010-auditor",
                project_actions=[],
                connection_actions=[
                    {
                        "connection_id": connection_id,
                        "action": "reassign",
                        "target_workspace_id": target_workspace_id,
                    }
                ],
                scope=scope,
            )

        with setup_conn.cursor() as cur:
            cur.execute(
                "SELECT workspace_id::text FROM workspace_data_connections WHERE connection_id = %s",
                (connection_id,),
            )
            assert cur.fetchone()[0] == source_workspace_id
            cur.execute(
                "SELECT deleted_at FROM workspaces WHERE id = %s",
                (source_workspace_id,),
            )
            assert cur.fetchone()[0] is None

        monkeypatch.setattr(service, "_move_workspace_data_connection_direct", original_move)
        result = service.trash_workspace_with_plan(
            source_workspace_id,
            deleted_by="rs5-010-auditor",
            project_actions=[],
            connection_actions=[
                {
                    "connection_id": connection_id,
                    "action": "delete",
                    "target_workspace_id": "",
                }
            ],
            scope=scope,
        )
        assert result["deleted_connection_ids"] == [connection_id]

        service.restore("workspace", source_workspace_id, scope=scope)
        service.restore("workspace_data_connection", connection_id, scope=scope)

        with setup_conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.deleted_at, c.deleted_at, c.workspace_id::text
                  FROM workspaces w
                  JOIN workspace_data_connections c ON c.workspace_id = w.id
                 WHERE w.id = %s AND c.connection_id = %s
                """,
                (source_workspace_id, connection_id),
            )
            assert cur.fetchone() == (None, None, source_workspace_id)
    finally:
        with setup_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM workspace_data_connections WHERE connection_id = %s",
                (connection_id,),
            )
            cur.execute(
                "DELETE FROM workspaces WHERE id = ANY(%s::uuid[])",
                ([source_workspace_id, target_workspace_id],),
            )
            cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        store.close()
        setup_conn.close()
