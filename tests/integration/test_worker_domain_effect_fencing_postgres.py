"""Real-PostgreSQL fencing for worker-owned domain-event effects."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from core.application.jobs.workflow_knowledge_sync_requests import (
    WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
)
from core.application.memory.runtime import curate_completed_job_to_memory
from core.application.workflows import workflow_knowledge_sync as sync_mod
from core.application.workflows.knowledge_projection import (
    WorkflowKnowledgeDocument,
    WorkflowKnowledgeEdge,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
)
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.infrastructure.db.platform_logger_jobs_mixin import (
    TERMINAL_MEMORY_CURATION_EVENT_TYPE,
)
from core.infrastructure.db.platform_stores import PlatformWorkerStore
from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore
from core.infrastructure.runtime.worker.terminal_curation import process_terminal_curation_batch
from core.infrastructure.runtime.worker.workflow_knowledge_sync import (
    process_workflow_knowledge_sync_batch,
)

pytestmark = pytest.mark.integration


def _worker_store(dsn: str) -> PlatformWorkerStore:
    return PlatformWorkerStore(dsn)


def _close_store(store: PlatformWorkerStore) -> None:
    connection = getattr(store, "_direct_conn", None)
    if connection is not None:
        connection.close()


def _seed_job(store: PlatformWorkerStore) -> tuple[str, str]:
    project_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    with store.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"worker-fence-{project_id[:8]}", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            """
            INSERT INTO jobs (
                id, account_id, tenant_id, project_id, task_id, task_summary,
                status, final_report, meta
            ) VALUES (%s, %s, %s, %s, %s, %s, 'completed', %s, '{}'::jsonb)
            """,
            (
                job_id,
                DEFAULT_ACCOUNT_ID,
                DEFAULT_TENANT_ID,
                project_id,
                f"task-{job_id[:8]}",
                "Fence terminal memory curation",
                "Terminal curation completed successfully.",
            ),
        )
    return project_id, job_id


def _force_claim_available(dsn: str, event_id: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE domain_event_outbox
               SET claim_expires_at = now() - interval '1 second',
                   available_at = now() - interval '1 second'
             WHERE event_id = %s
            """,
            (event_id,),
        )


def _claim_row_locked(dsn: str, event_id: str) -> bool:
    import psycopg

    try:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SET lock_timeout = '150ms'")
            cur.execute(
                """
                UPDATE domain_event_outbox
                   SET claim_expires_at = now() - interval '1 second',
                       available_at = now() - interval '1 second'
                 WHERE event_id = %s
                """,
                (event_id,),
            )
    except psycopg.errors.LockNotAvailable:
        return True
    return False


def _run_in_thread(action: Callable[[], None]) -> tuple[threading.Thread, list[BaseException]]:
    failures: list[BaseException] = []

    def _target() -> None:
        try:
            action()
        except BaseException as exc:  # the assertion reports the worker failure explicitly
            failures.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, failures


def test_terminal_curation_rolls_back_lost_claim_before_second_worker_reclaims(
    disposable_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_DB_POOL_ENABLED", "0")
    worker_a = _worker_store(disposable_db.dsn)
    worker_b = _worker_store(disposable_db.dsn)
    try:
        _project_id, job_id = _seed_job(worker_a)
        event_id = f"job-terminal-memory:{job_id}"
        worker_a.enqueue_domain_event(
            event_id=event_id,
            event_type=TERMINAL_MEMORY_CURATION_EVENT_TYPE,
            account_id=DEFAULT_ACCOUNT_ID,
            aggregate_type="job",
            aggregate_id=job_id,
            job_id=job_id,
            payload={"terminal_status": "completed"},
        )
        effect_written = threading.Event()
        release_failure = threading.Event()

        def _curate_then_fail(*, job_id: str, logger_instance: Any) -> list[str]:
            curate_completed_job_to_memory(
                job_id=job_id,
                logger_instance=logger_instance,
            )
            effect_written.set()
            assert release_failure.wait(timeout=3)
            raise RuntimeError("worker-a interrupted after canonical memory write")

        thread, failures = _run_in_thread(
            lambda: process_terminal_curation_batch(
                event_store=worker_a,
                memory_store=worker_a,
                curate_terminal_job=_curate_then_fail,
                worker_id="worker-a",
            )
        )
        assert effect_written.wait(timeout=3)
        try:
            claim_locked_during_effect = _claim_row_locked(disposable_db.dsn, event_id)
        finally:
            release_failure.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert failures == []

        _force_claim_available(disposable_db.dsn, event_id)
        assert (
            process_terminal_curation_batch(
                event_store=worker_b,
                memory_store=worker_b,
                curate_terminal_job=curate_completed_job_to_memory,
                worker_id="worker-b",
            )
            == 1
        )

        with worker_b.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM memory_records
                      WHERE source_type = 'job_completion' AND source_id = %s),
                    (SELECT COUNT(*) FROM platform_action_log
                      WHERE action_type = 'memory_write_candidate' AND job_id = %s),
                    (SELECT COUNT(*) FROM domain_event_ledger WHERE event_id = %s),
                    (SELECT status FROM domain_event_outbox WHERE event_id = %s)
                """,
                (job_id, job_id, event_id, event_id),
            )
            record_count, audit_count, ledger_count, outbox_status = cur.fetchone()

        assert claim_locked_during_effect is True
        assert (record_count, audit_count, ledger_count, outbox_status) == (1, 1, 1, "published")
    finally:
        _close_store(worker_a)
        _close_store(worker_b)


def _projection(prefix: str) -> WorkflowKnowledgeProjection:
    left = f"entity:{prefix}:left"
    right = f"entity:{prefix}:right"
    return WorkflowKnowledgeProjection(
        nodes=[
            WorkflowKnowledgeNode(id=left, kind="workflow", label=f"{prefix} left"),
            WorkflowKnowledgeNode(id=right, kind="step", label=f"{prefix} right"),
        ],
        edges=[
            WorkflowKnowledgeEdge(
                id=f"edge:{prefix}",
                kind="contains",
                source_id=left,
                target_id=right,
            )
        ],
        documents=[
            WorkflowKnowledgeDocument(
                id=f"doc:{prefix}",
                entity_id=left,
                entity_kind="workflow",
                title=f"{prefix} workflow",
                text=f"{prefix} workflow projection",
            )
        ],
    )


class _ProjectionSource:
    def __init__(self, *, digest: str, projection: WorkflowKnowledgeProjection) -> None:
        self.digest = digest
        self.projection = projection


def _install_projection_effect_ledger(store: PlatformWorkerStore) -> None:
    with store.conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE test_workflow_projection_effects (
                operation text NOT NULL,
                entity_id text NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE FUNCTION test_log_workflow_projection_effect() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO test_workflow_projection_effects (operation, entity_id)
                VALUES (TG_OP, COALESCE(NEW.entity_id, OLD.entity_id));
                RETURN COALESCE(NEW, OLD);
            END
            $$
            """
        )
        cur.execute(
            """
            CREATE TRIGGER test_workflow_projection_effect
            AFTER INSERT OR DELETE ON platform_workflow_knowledge_nodes
            FOR EACH ROW EXECUTE FUNCTION test_log_workflow_projection_effect()
            """
        )


def test_workflow_projection_retry_is_idempotent_without_a_long_claim_transaction(
    disposable_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_DB_POOL_ENABLED", "0")
    monkeypatch.setattr(
        sync_mod,
        "catalog_sync_digest",
        lambda db, **_kwargs: db.digest,
    )
    monkeypatch.setattr(
        sync_mod,
        "build_workflow_catalog_projection",
        lambda db, **_kwargs: db.projection,
    )
    worker_a = _worker_store(disposable_db.dsn)
    worker_b = _worker_store(disposable_db.dsn)
    knowledge_a = PostgresAgeWorkflowKnowledgeStore(
        dsn=disposable_db.dsn,
        connection=worker_a.conn,
    )
    knowledge_b = PostgresAgeWorkflowKnowledgeStore(
        dsn=disposable_db.dsn,
        connection=worker_b.conn,
    )
    try:
        knowledge_a.reset_graph()
        _install_projection_effect_ledger(worker_a)
        event_id = f"workflow-knowledge-sync:{uuid.uuid4()}"
        payload = {
            "idempotency_key": event_id,
            "scopes": [
                {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "tenant_id": DEFAULT_TENANT_ID,
                    "ensure_indexes": True,
                    "include_capability_catalog": False,
                }
            ],
        }
        worker_a.enqueue_domain_event(
            event_id=event_id,
            event_type=WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
            account_id=DEFAULT_ACCOUNT_ID,
            aggregate_type="workflow_knowledge_catalog",
            aggregate_id="workflow-knowledge-catalog",
            payload=payload,
        )
        source_a = _ProjectionSource(digest="digest-a", projection=_projection("stale"))
        source_b = _ProjectionSource(digest="digest-a", projection=_projection("stale"))
        effect_written = threading.Event()
        release_failure = threading.Event()

        def _sync_then_fail(scope: dict[str, Any], **_kwargs) -> dict[str, Any]:
            sync_mod.sync_workflow_knowledge_catalog_if_stale(
                store=knowledge_a,
                db=source_a,
                account_id=str(scope["account_id"]),
                tenant_id=str(scope["tenant_id"]),
                include_capability_catalog=False,
            )
            effect_written.set()
            assert release_failure.wait(timeout=3)
            raise RuntimeError("worker-a interrupted after canonical SQL+AGE projection")

        thread, failures = _run_in_thread(
            lambda: process_workflow_knowledge_sync_batch(
                event_store=worker_a,
                status_store=worker_a,
                db=source_a,
                knowledge_store=knowledge_a,
                run_sync=_sync_then_fail,
                worker_id="worker-a",
            )
        )
        if not effect_written.wait(timeout=10):
            release_failure.set()
            thread.join(timeout=3)
            with worker_b.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, last_error, claim_owner, claim_token, claim_version,
                           claim_expires_at, available_at, now()
                      FROM domain_event_outbox
                     WHERE event_id = %s
                    """,
                    (event_id,),
                )
                row = cur.fetchone()
                cur.execute(
                    """
                    SELECT state, wait_event_type, wait_event, left(query, 240)
                      FROM pg_stat_activity
                     WHERE datname = current_database()
                       AND pid <> pg_backend_pid()
                     ORDER BY pid
                    """
                )
                activity = list(cur.fetchall())
            pytest.fail(
                "workflow projection did not reach the interruption hook: "
                f"outbox={row!r}; "
                f"thread_alive={thread.is_alive()}; failures={failures!r}; "
                f"activity={activity!r}"
            )
        try:
            claim_locked_during_effect = _claim_row_locked(disposable_db.dsn, event_id)
        finally:
            release_failure.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert failures == []

        _force_claim_available(disposable_db.dsn, event_id)

        def _sync_current(scope: dict[str, Any], **_kwargs) -> dict[str, Any]:
            return sync_mod.sync_workflow_knowledge_catalog_if_stale(
                store=knowledge_b,
                db=source_b,
                account_id=str(scope["account_id"]),
                tenant_id=str(scope["tenant_id"]),
                include_capability_catalog=False,
            )

        assert (
            process_workflow_knowledge_sync_batch(
                event_store=worker_b,
                status_store=worker_b,
                db=source_b,
                knowledge_store=knowledge_b,
                run_sync=_sync_current,
                worker_id="worker-b",
            )
            == 1
        )

        with worker_b.conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation, entity_id
                  FROM test_workflow_projection_effects
                 ORDER BY operation, entity_id
                """
            )
            effects = list(cur.fetchall())
            cur.execute(
                """
                SELECT entity_id
                  FROM platform_workflow_knowledge_nodes
                 WHERE account_id = %s AND tenant_id = %s AND project_scope = ''
                 ORDER BY entity_id
                """,
                (DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID),
            )
            sql_nodes = [str(row[0]) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT COUNT(*) FROM platform_workflow_knowledge_edges
                 WHERE account_id = %s AND tenant_id = %s AND project_scope = ''
                """,
                (DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID),
            )
            sql_edge_count = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*) FROM domain_event_ledger WHERE event_id = %s",
                (event_id,),
            )
            ledger_count = int(cur.fetchone()[0])
            cur.execute(
                "SELECT status FROM domain_event_outbox WHERE event_id = %s",
                (event_id,),
            )
            outbox_status = str(cur.fetchone()[0])
        graph_rows = knowledge_b._run_cypher(
            """
            MATCH (n:Entity)
            WHERE n.account_id = $account_id
              AND n.tenant_id = $tenant_id
              AND n.project_scope = ''
            RETURN n.id AS entity_id
            """,
            columns=["entity_id"],
            params={"account_id": DEFAULT_ACCOUNT_ID, "tenant_id": DEFAULT_TENANT_ID},
        )
        graph_nodes = sorted(str(row["entity_id"]) for row in graph_rows)

        expected_nodes = ["entity:stale:left", "entity:stale:right"]
        assert claim_locked_during_effect is False
        assert effects == [("INSERT", node_id) for node_id in expected_nodes]
        assert sql_nodes == expected_nodes
        assert graph_nodes == expected_nodes
        assert sql_edge_count == 1
        assert (
            knowledge_b.get_scope_sync_digest(
                account_id=DEFAULT_ACCOUNT_ID,
                tenant_id=DEFAULT_TENANT_ID,
            )
            == "digest-a"
        )
        assert (ledger_count, outbox_status) == (1, "published")
    finally:
        _close_store(worker_a)
        _close_store(worker_b)
