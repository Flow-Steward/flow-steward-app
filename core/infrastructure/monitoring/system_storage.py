"""Infrastructure storage adapters for monitoring payload builders."""

from __future__ import annotations

from typing import Any


def active_lease_count(bl) -> int:
    lease_lister = getattr(bl, "list_project_execution_leases", None)
    if callable(lease_lister):
        try:
            rows = lease_lister(active_only=True)
            if isinstance(rows, list):
                return len([row for row in rows if isinstance(row, dict)])
        except Exception:
            pass
    try:
        with bl.conn.cursor() as cur:
            if bl._table_exists("project_execution_slots"):
                cur.execute("SELECT COUNT(*) FROM project_execution_slots WHERE expires_at > now()")
                return int((cur.fetchone() or [0])[0] or 0)
    except Exception:
        return 0
    return 0


def load_monitoring_counts(bl) -> dict[str, int]:
    with bl.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM projects")
        project_count = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
        pending_jobs = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'resume_pending'")
        resume_pending_jobs = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'in_progress'")
        in_progress_jobs = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'awaiting_approval'")
        awaiting_approval_jobs = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'awaiting_timer'")
        awaiting_timer_jobs = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'awaiting_child'")
        awaiting_child_jobs = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'stuck'")
        stuck_jobs = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'orphaned'")
        orphaned_jobs = int((cur.fetchone() or [0])[0] or 0)
    return {
        "project_count": project_count,
        "pending_jobs": pending_jobs,
        "resume_pending_jobs": resume_pending_jobs,
        "in_progress_jobs": in_progress_jobs,
        "awaiting_approval_jobs": awaiting_approval_jobs,
        "awaiting_timer_jobs": awaiting_timer_jobs,
        "awaiting_child_jobs": awaiting_child_jobs,
        "stuck_jobs": stuck_jobs,
        "orphaned_jobs": orphaned_jobs,
        "active_leases": active_lease_count(bl),
    }


class SystemHealthCountsRepository:
    """Infrastructure adapter for the application deep-health query port."""

    def load(self, logger: Any) -> dict[str, int]:
        return load_monitoring_counts(logger)


def load_memory_storage_usage(bl) -> dict[str, Any]:
    def _project_scope_state(project_row: dict[str, Any] | None) -> str:
        if not isinstance(project_row, dict):
            return "missing"
        return "trashed" if project_row.get("deleted_at") else "active"

    def _column_exists(table_name: str, column_name: str) -> bool:
        checker = getattr(bl, "_column_exists", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(table_name, column_name))
        except Exception:
            return False

    project_rows_by_id: dict[str, dict[str, Any]] = {}
    project_rows_by_name: dict[str, dict[str, Any]] = {}
    memory_rows: dict[str, dict[str, Any]] = {}
    orphan_memory_rows: list[dict[str, Any]] = []
    has_memory_embedding_column = _column_exists("memory_records", "embedding")
    with bl.conn.cursor() as cur:
        cur.execute("SELECT id::text, name, deleted_at FROM projects")
        for row in cur.fetchall() or []:
            project_id = str(row[0] or "").strip()
            project_name = str(row[1] or "").strip()
            project_row = {"id": project_id, "name": project_name, "deleted_at": row[2]}
            if project_id:
                project_rows_by_id[project_id] = project_row
            if project_name:
                project_rows_by_name[project_name] = project_row

        if has_memory_embedding_column:
            cur.execute(
                """
                WITH record_counts AS (
                    SELECT
                        project_id::text AS project_id,
                        COUNT(*) AS records,
                        COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS embedded_records
                    FROM memory_records
                    WHERE project_id IS NOT NULL
                    GROUP BY project_id
                ),
                entity_counts AS (
                    SELECT project_id::text AS project_id, COUNT(*) AS entities
                    FROM memory_entities
                    WHERE project_id IS NOT NULL
                    GROUP BY project_id
                ),
                edge_counts AS (
                    SELECT project_id::text AS project_id, COUNT(*) AS edges
                    FROM memory_edges
                    WHERE project_id IS NOT NULL
                    GROUP BY project_id
                ),
                project_scopes AS (
                    SELECT project_id FROM record_counts
                    UNION
                    SELECT project_id FROM entity_counts
                    UNION
                    SELECT project_id FROM edge_counts
                )
                SELECT
                    project_scopes.project_id,
                    COALESCE(record_counts.records, 0) AS records,
                    COALESCE(record_counts.embedded_records, 0) AS embedded_records,
                    COALESCE(entity_counts.entities, 0) AS entities,
                    COALESCE(edge_counts.edges, 0) AS edges
                FROM project_scopes
                LEFT JOIN record_counts USING (project_id)
                LEFT JOIN entity_counts USING (project_id)
                LEFT JOIN edge_counts USING (project_id)
                ORDER BY
                    COALESCE(record_counts.records, 0)
                    + COALESCE(entity_counts.entities, 0)
                    + COALESCE(edge_counts.edges, 0) DESC,
                    project_scopes.project_id ASC
                """
            )
        else:
            cur.execute(
                """
                WITH record_counts AS (
                    SELECT
                        project_id::text AS project_id,
                        COUNT(*) AS records,
                        0 AS embedded_records
                    FROM memory_records
                    WHERE project_id IS NOT NULL
                    GROUP BY project_id
                ),
                entity_counts AS (
                    SELECT project_id::text AS project_id, COUNT(*) AS entities
                    FROM memory_entities
                    WHERE project_id IS NOT NULL
                    GROUP BY project_id
                ),
                edge_counts AS (
                    SELECT project_id::text AS project_id, COUNT(*) AS edges
                    FROM memory_edges
                    WHERE project_id IS NOT NULL
                    GROUP BY project_id
                ),
                project_scopes AS (
                    SELECT project_id FROM record_counts
                    UNION
                    SELECT project_id FROM entity_counts
                    UNION
                    SELECT project_id FROM edge_counts
                )
                SELECT
                    project_scopes.project_id,
                    COALESCE(record_counts.records, 0) AS records,
                    COALESCE(record_counts.embedded_records, 0) AS embedded_records,
                    COALESCE(entity_counts.entities, 0) AS entities,
                    COALESCE(edge_counts.edges, 0) AS edges
                FROM project_scopes
                LEFT JOIN record_counts USING (project_id)
                LEFT JOIN entity_counts USING (project_id)
                LEFT JOIN edge_counts USING (project_id)
                ORDER BY
                    COALESCE(record_counts.records, 0)
                    + COALESCE(entity_counts.entities, 0)
                    + COALESCE(edge_counts.edges, 0) DESC,
                    project_scopes.project_id ASC
                """
            )
        for row in cur.fetchall() or []:
            project_id = str(row[0] or "").strip()
            project_row = project_rows_by_id.get(project_id)
            payload = {
                "project_id": project_id,
                "project_name": str((project_row or {}).get("name") or "").strip(),
                "project_state": _project_scope_state(project_row),
                "records": int(row[1] or 0),
                "embedded_records": int(row[2] or 0),
                "entities": int(row[3] or 0),
                "edges": int(row[4] or 0),
            }
            memory_rows[project_id] = payload
            if payload["project_state"] == "missing":
                orphan_memory_rows.append(payload)
    return {
        "embedding_column_available": has_memory_embedding_column,
        "memory_rows": memory_rows,
        "orphan_memory_rows": orphan_memory_rows,
        "project_rows_by_id": project_rows_by_id,
        "project_rows_by_name": project_rows_by_name,
    }
