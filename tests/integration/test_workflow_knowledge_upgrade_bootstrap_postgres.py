from __future__ import annotations

import psycopg
import pytest

from core.application.tooling.db.upgrade_db import upgrade_database
from core.infrastructure.db.workflow_knowledge_store import (
    PostgresAgeWorkflowKnowledgeStore,
)
from tests import _disposable_db

_GRAPH_NAME = "platform_workflow_graph"
_REQUIRED_GRAPH_OBJECTS = {
    "Entity",
    "RELATES_TO",
    "idx_entity_id",
    "idx_relates_to_edge_id",
}


def _graph_relation_oids(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return dict(
            conn.execute(
                """
                SELECT cls.relname, cls.oid
                  FROM pg_class AS cls
                  JOIN pg_namespace AS namespace ON namespace.oid = cls.relnamespace
                 WHERE namespace.nspname = %s
                   AND cls.relkind = ANY(%s)
                """,
                (_GRAPH_NAME, ["r", "i"]),
            ).fetchall()
        )


@pytest.mark.integration
def test_canonical_upgrade_bootstraps_age_graph_without_rebuilding_existing_state() -> None:
    """A fresh-install upgrade must create AGE state once and preserve it thereafter."""

    reason = _disposable_db.unavailable_reason()
    if reason:
        pytest.skip(f"disposable canonical PostgreSQL unavailable: {reason}")

    database = _disposable_db.start_disposable_db()
    try:
        with psycopg.connect(database.dsn, autocommit=True) as conn:
            graph_before = conn.execute(
                "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
                (_GRAPH_NAME,),
            ).fetchone()
        assert graph_before is None

        with _disposable_db.redirected_to(database):
            first = upgrade_database(
                dsn=database.dsn,
                author="workflow-knowledge-bootstrap-test",
                maintenance_confirmed=True,
            )

            store = PostgresAgeWorkflowKnowledgeStore(dsn=database.dsn)
            first_status = store.runtime_status()
            assert first_status["graph"] == {
                "available": True,
                "extension_available": True,
                "extension_installed": True,
                "labels_ready": True,
                "error": "",
            }

            created = store._run_cypher(
                """
                CREATE (n:Entity {id: '__upgrade_sentinel__', kind: 'upgrade_test'})
                RETURN n.id AS sentinel_id
                """,
                columns=["sentinel_id"],
            )
            assert created == [{"sentinel_id": "__upgrade_sentinel__"}]

            object_oids_before = _graph_relation_oids(database.dsn)
            assert set(object_oids_before) >= _REQUIRED_GRAPH_OBJECTS

            second = upgrade_database(
                dsn=database.dsn,
                author="workflow-knowledge-bootstrap-test",
                maintenance_confirmed=True,
            )

            sentinel = store._run_cypher(
                """
                MATCH (n:Entity {id: '__upgrade_sentinel__'})
                RETURN n.id AS sentinel_id
                """,
                columns=["sentinel_id"],
            )
            assert sentinel == [{"sentinel_id": "__upgrade_sentinel__"}]
            object_oids_after = _graph_relation_oids(database.dsn)
            assert object_oids_after == object_oids_before
            assert store.runtime_status()["graph"]["labels_ready"] is True

        assert first["schema_migrations"]["pending"] == []
        assert second["schema_migrations"]["applied"] == []
    finally:
        _disposable_db.stop_disposable_db(database.container)
