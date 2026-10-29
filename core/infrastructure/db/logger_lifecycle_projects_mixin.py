import time
import uuid
from contextlib import contextmanager
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

# Sorting is a whitelist, not a passthrough: the key names the API accepts are
# mapped to expressions here and nothing else can reach the ORDER BY clause.
_PROJECT_SORT_EXPRESSIONS = {
    "name": "p.name",
    "job_count": "COALESCE(j.job_count, 0)",
}


def _project_jobs_predicate(has_jobs: str | None, *, project_column: str) -> str:
    """Translate the has-jobs filter into an EXISTS clause taking no parameters.

    `project_column` is the caller's own table alias, never request data: the two
    call sites pass the literals "p.id" and "projects.id".
    """
    normalized = str(has_jobs or "").strip().lower()
    if normalized in {"yes", "true", "1", "with"}:
        return f"EXISTS (SELECT 1 FROM jobs fj WHERE fj.project_id = {project_column} AND fj.deleted_at IS NULL)"  # nosec B608 -- alias is an internal literal.
    if normalized in {"no", "false", "0", "without"}:
        return f"NOT EXISTS (SELECT 1 FROM jobs fj WHERE fj.project_id = {project_column} AND fj.deleted_at IS NULL)"  # nosec B608 -- alias is an internal literal.
    return ""


def _projects_order_by(sort: str | None, direction: str | None) -> str:
    """Name remains the default and the tiebreaker, so pages never overlap."""
    expression = _PROJECT_SORT_EXPRESSIONS.get(str(sort or "").strip().lower())
    if not expression:
        return "ORDER BY p.name"
    descending = str(direction or "").strip().lower() in {"desc", "descending"}
    keyword = "DESC" if descending else "ASC"
    if expression == "p.name":
        return f"ORDER BY p.name {keyword}"
    return f"ORDER BY {expression} {keyword}, p.name ASC"


#: The workflows a project lists: its enabled bindings that were not removed,
#: the same ones GET /api/projects/{id}/workflows answers as workflow_ids.
_PROJECT_WORKFLOW_IDS_SELECT = (
    "COALESCE((SELECT array_agg(b.workflow_id ORDER BY b.workflow_id)"
    " FROM project_workflows b"
    " WHERE b.project_id = p.id AND b.enabled = true AND b.deleted_at IS NULL),"
    " ARRAY[]::text[]) AS workflow_ids"
)


class PlatformLoggerLifecycleProjectsMixin:
    conn: Any
    _column_exists: Any
    _table_exists: Any
    _lifecycle_starts: dict[str, float]

    def list_active_project_ids(self) -> list[str]:
        """Return canonical non-deleted project identities for background services."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM projects WHERE deleted_at IS NULL ORDER BY id")
            return [str(row[0]) for row in cur.fetchall()]

    def list_recent_registry_changelog(
        self,
        *,
        entity_types: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return bounded raw changelog rows for application-owned background policy."""

        normalized_types = tuple(
            sorted({str(entity_type or "").strip() for entity_type in entity_types if entity_type})
        )
        if not normalized_types:
            return []
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, entity_type, entity_id, created_at, snapshot
                  FROM registry_changelog
                 WHERE entity_type = ANY(%s)
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (list(normalized_types), max(1, int(limit or 1))),
            )
            return [dict(row) for row in (cur.fetchall() or [])]

    @staticmethod
    def _project_description_value(row: dict[str, Any]) -> str:
        return str(row.get("description") or "").strip()

    @classmethod
    def _hydrate_project_row(cls, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row) if isinstance(row, dict) else None
        if payload is None:
            try:
                payload = dict(row)
            except Exception:
                return None
        payload["description"] = cls._project_description_value(payload)
        return payload

    def start_lifecycle(self):
        lifecycle_id = uuid.uuid4().hex
        self._lifecycle_starts[lifecycle_id] = time.perf_counter()
        return lifecycle_id

    def end_lifecycle(self, lifecycle_id):
        started = self._lifecycle_starts.pop(lifecycle_id, None)
        if started is None:
            return None
        return int((time.perf_counter() - started) * 1000)

    @contextmanager
    def lifecycle(self):
        lifecycle_id = self.start_lifecycle()
        try:
            yield lifecycle_id
        finally:
            self.end_lifecycle(lifecycle_id)

    def get_action_history(self, project_id, task_id=None, limit=50):
        """History for project, optionally by task_id, most recent first"""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            if task_id:
                cur.execute(
                    "SELECT * FROM platform_action_log WHERE project_id=%s AND task_id=%s ORDER BY timestamp DESC LIMIT %s",
                    (project_id, task_id, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM platform_action_log WHERE project_id=%s ORDER BY timestamp DESC LIMIT %s",
                    (project_id, limit),
                )
            return cur.fetchall()

    def get_task_statistics(self, project_id, task_id):
        """Count cycles, iterations, time stats for task"""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
            SELECT MIN(timestamp) AS started_at, MAX(timestamp) AS finished_at, COUNT(*) AS steps,
                   MAX(cycle_no) AS cycles, MAX(iteration_no) AS iterations
              FROM platform_action_log
             WHERE project_id=%s AND task_id=%s
            """,
                (project_id, task_id),
            )
            return cur.fetchone()

    def get_projects(self, *, account_id: str | None = None, include_trashed: bool = False) -> list:
        """Return all projects ordered by name, optionally filtered by account."""
        conditions: list[str] = []
        params: list[Any] = []
        if not include_trashed:
            conditions.append("p.deleted_at IS NULL")
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            conditions.append("p.account_id = %s")
            params.append(normalized_account)
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        workspace_join = (
            "LEFT JOIN workspaces w ON w.id = p.workspace_id"
            if self._table_exists("workspaces")
            else ""
        )
        workspace_select = (
            "COALESCE(w.display_name, '') AS workspace_name,"
            " COALESCE(w.workspace_key, '') AS workspace_key,"
            if self._table_exists("workspaces")
            else "'' AS workspace_name, '' AS workspace_key,"
        )
        default_workflow_join = (
            """
                  LEFT JOIN project_workflows pw
                    ON pw.project_id = p.id
                   AND pw.is_default = true
                   AND pw.enabled = true
                   AND pw.deleted_at IS NULL
            """
            if self._table_exists("project_workflows")
            else ""
        )
        default_workflow_select = (
            "COALESCE(pw.workflow_id, '') AS operator_default_workflow_id,"
            f" {_PROJECT_WORKFLOW_IDS_SELECT}"
            if self._table_exists("project_workflows")
            else "'' AS operator_default_workflow_id, ARRAY[]::text[] AS workflow_ids"
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                       p.*,
                       {workspace_select}
                       {default_workflow_select},
                       COALESCE(j.job_count, 0) AS job_count
                  FROM projects p
                  LEFT JOIN (
                        SELECT project_id, COUNT(id) AS job_count
                          FROM jobs
                         WHERE deleted_at IS NULL
                         GROUP BY project_id
                  ) j ON j.project_id = p.id
                  {workspace_join}
                  {default_workflow_join}
                 {where_sql}
                 ORDER BY p.name
                """,
                tuple(params),
            )
            return [
                payload
                for row in cur.fetchall()
                if (payload := self._hydrate_project_row(row)) is not None
            ]

    def list_projects(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        search: str | None = None,
        account_id: str | None = None,
        workspace_id: str | None = None,
        has_jobs: str | None = None,
        sort: str | None = None,
        direction: str | None = None,
        include_trashed: bool = False,
    ) -> list[dict]:
        """Return a paginated project list, ordered by name unless sorted."""
        conditions: list[str] = []
        params: list[Any] = []
        if not include_trashed:
            conditions.append("p.deleted_at IS NULL")
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            conditions.append("p.account_id = %s")
            params.append(normalized_account)
        normalized_workspace = str(workspace_id or "").strip()
        if normalized_workspace:
            conditions.append("p.workspace_id = %s")
            params.append(normalized_workspace)
        normalized_search = str(search or "").strip()
        if normalized_search:
            like = f"%{normalized_search}%"
            conditions.append("p.name ILIKE %s")
            params.append(like)
        jobs_predicate = _project_jobs_predicate(has_jobs, project_column="p.id")
        if jobs_predicate:
            conditions.append(jobs_predicate)
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order_sql = _projects_order_by(sort, direction)
        workspace_join = (
            "LEFT JOIN workspaces w ON w.id = p.workspace_id"
            if self._table_exists("workspaces")
            else ""
        )
        workspace_select = (
            "COALESCE(w.display_name, '') AS workspace_name,"
            " COALESCE(w.workspace_key, '') AS workspace_key,"
            if self._table_exists("workspaces")
            else "'' AS workspace_name, '' AS workspace_key,"
        )
        default_workflow_join = (
            """
                  LEFT JOIN project_workflows pw
                    ON pw.project_id = p.id
                   AND pw.is_default = true
                   AND pw.enabled = true
                   AND pw.deleted_at IS NULL
            """
            if self._table_exists("project_workflows")
            else ""
        )
        default_workflow_select = (
            "COALESCE(pw.workflow_id, '') AS operator_default_workflow_id,"
            f" {_PROJECT_WORKFLOW_IDS_SELECT}"
            if self._table_exists("project_workflows")
            else "'' AS operator_default_workflow_id, ARRAY[]::text[] AS workflow_ids"
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                       p.*,
                       {workspace_select}
                       {default_workflow_select},
                       COALESCE(j.job_count, 0) AS job_count
                  FROM projects p
                  LEFT JOIN (
                        SELECT project_id, COUNT(id) AS job_count
                          FROM jobs
                         WHERE deleted_at IS NULL
                         GROUP BY project_id
                  ) j ON j.project_id = p.id
                  {workspace_join}
                  {default_workflow_join}
                  {where_sql}
                 {order_sql}
                 LIMIT %s OFFSET %s
                """,
                (*params, max(1, min(int(limit or 25), 500)), max(0, int(offset or 0))),
            )
            return [
                payload
                for row in cur.fetchall()
                if (payload := self._hydrate_project_row(row)) is not None
            ]

    def count_projects(
        self,
        *,
        search: str | None = None,
        account_id: str | None = None,
        workspace_id: str | None = None,
        has_jobs: str | None = None,
        include_trashed: bool = False,
    ) -> int:
        """Return total project count for paginated project queries."""
        conditions: list[str] = []
        params: list[Any] = []
        if not include_trashed:
            conditions.append("deleted_at IS NULL")
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            conditions.append("account_id = %s")
            params.append(normalized_account)
        normalized_workspace = str(workspace_id or "").strip()
        if normalized_workspace:
            conditions.append("workspace_id = %s")
            params.append(normalized_workspace)
        normalized_search = str(search or "").strip()
        if normalized_search:
            like = f"%{normalized_search}%"
            conditions.append("name ILIKE %s")
            params.append(like)
        jobs_predicate = _project_jobs_predicate(has_jobs, project_column="projects.id")
        if jobs_predicate:
            conditions.append(jobs_predicate)
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM projects {where_sql}",
                tuple(params),
            )
            row = cur.fetchone()
            return int(row[0] if row else 0)

    def get_project(self, project_id: Any, *, include_trashed: bool = False) -> dict | None:
        """Return a single project by id."""
        try:
            normalized_project_id = str(uuid.UUID(str(project_id or "").strip()))
        except (AttributeError, TypeError, ValueError):
            return None
        conditions = ["id = %s"]
        params: list[Any] = [normalized_project_id]
        if not include_trashed and self._column_exists("projects", "deleted_at"):
            conditions.append("deleted_at IS NULL")
        where_sql = " AND ".join(conditions)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM projects WHERE {where_sql}", tuple(params))
            return self._hydrate_project_row(cur.fetchone())
