"""Workflow availability aggregation and launch gate enforcement."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from core.application.extensions.connection_viability import project_connection_is_viable
from core.application.extensions.install_availability_service import InstallAvailabilityService
from core.application.management.errors import ServiceConflictError
from core.application.registry.active_workflow_dependencies import workflow_registry_tool_issues

_WORKFLOW_REASON_TEXT = {
    "none": "",
    "workflow_disabled": "Workflow is disabled.",
    "blocked_dependency": "Workflow has blocked runtime dependencies.",
    "missing_project_connection": "Workflow requires a project connection that is missing.",
    "project_connection_disabled": "Required project connection is disabled.",
    "project_connection_health_failed": "Required project connection is unhealthy.",
    "workflow_not_found": "Workflow was not found.",
    "missing_or_unavailable_tool": "Workflow has a missing or unavailable registry tool dependency.",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_lower(value: Any) -> str:
    return _normalize_text(value).lower()


def _normalize_dependency_ref(value: Any) -> str:
    normalized = _normalize_text(value)
    if normalized.lower() in {"", "none", "null"}:
        return ""
    return normalized


def _row_to_dict(row: Any, description: Any | None = None) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if description is None:
        return {}
    cols = [col[0] if hasattr(col, "__getitem__") else col.name for col in description]
    return dict(zip(cols, row, strict=False))


class WorkflowAvailabilityService:
    """Resolve workflow availability and provide a shared launch gate."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        install_availability_service: InstallAvailabilityService | None = None,
        fetch_workflow_row: Callable[[str, str], dict[str, Any] | None] | None = None,
        list_dependencies: Callable[[str, str], list[dict[str, Any]]] | None = None,
        list_project_connections: Callable[[str, str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._install_availability_service = (
            install_availability_service or InstallAvailabilityService(get_logger=get_logger)
        )
        self._fetch_workflow_row = fetch_workflow_row
        self._list_dependencies = list_dependencies
        self._list_project_connections = list_project_connections

    def _table_exists(self, bl: Any, table_name: str) -> bool:
        if bl is None:
            return False
        if hasattr(bl, "_table_exists"):
            try:
                return bool(bl._table_exists(table_name))
            except Exception:
                return False
        conn = getattr(bl, "conn", None)
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (table_name,))
                row = cur.fetchone()
                if row is None:
                    return False
                if isinstance(row, dict):
                    return bool(next(iter(row.values()), None))
                return bool(row[0])
        except Exception:
            return False

    def _column_exists(self, bl: Any, table_name: str, column_name: str) -> bool:
        if bl is None:
            return False
        if hasattr(bl, "_column_exists"):
            try:
                return bool(bl._column_exists(table_name, column_name))
            except Exception:
                return False
        conn = getattr(bl, "conn", None)
        if conn is None:
            return False
        schema_name = "public"
        rel_name = str(table_name or "").strip()
        if "." in rel_name:
            schema_name, rel_name = rel_name.split(".", 1)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = %s
                       AND table_name = %s
                       AND column_name = %s
                     LIMIT 1
                    """,
                    (schema_name, rel_name, str(column_name or "").strip()),
                )
                return cur.fetchone() is not None
        except Exception:
            return False

    def _db_fetch_workflow_row(self, workflow_id: str, account_id: str) -> dict[str, Any] | None:
        bl = self._get_logger()
        if bl is None:
            return None
        if hasattr(bl, "get_workflow"):
            try:
                from core.application.registry.workflow_lookup import get_workflow_row

                workflow = get_workflow_row(bl, workflow_id, account_id=account_id)
                if isinstance(workflow, dict):
                    return dict(workflow)
                return None
            except Exception:
                pass
        conn = getattr(bl, "conn", None)
        if conn is None or not self._table_exists(bl, "registry_workflows"):
            return None

        normalized_workflow_id = _normalize_text(workflow_id)
        normalized_account_id = _normalize_text(account_id)
        conditions = ["workflow_id = %s"]
        params: list[Any] = [normalized_workflow_id]
        has_account_column = self._column_exists(bl, "registry_workflows", "account_id")
        if normalized_account_id and has_account_column:
            conditions.append("(account_id IS NULL OR account_id = %s)")
            params.append(normalized_account_id)
        if self._column_exists(bl, "registry_workflows", "deleted_at"):
            conditions.append("deleted_at IS NULL")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                  FROM registry_workflows
                 WHERE {" AND ".join(conditions)}
                 ORDER BY workflow_id
                 LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_dict(row, cur.description)

    def _db_list_dependencies(self, workflow_id: str, account_id: str) -> list[dict[str, Any]]:
        bl = self._get_logger()
        conn = getattr(bl, "conn", None) if bl is not None else None
        if conn is None or not self._table_exists(bl, "workflow_runtime_dependencies"):
            return []

        normalized_workflow_id = _normalize_text(workflow_id)
        normalized_account_id = _normalize_text(account_id)
        params: list[Any] = [normalized_workflow_id]
        account_clause = ""
        has_account_column = self._column_exists(bl, "workflow_runtime_dependencies", "account_id")
        if normalized_account_id and has_account_column:
            account_clause = " AND (account_id IS NULL OR account_id = %s)"
            params.append(normalized_account_id)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                  FROM workflow_runtime_dependencies
                 WHERE workflow_id = %s
                       {account_clause}
                 ORDER BY dependency_id
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
            description = cur.description
        return [_row_to_dict(row, description) for row in rows]

    def _db_list_project_connections(
        self,
        project_id: str,
        account_id: str,
    ) -> list[dict[str, Any]]:
        bl = self._get_logger()
        if bl is None:
            return []
        if hasattr(bl, "list_project_connections"):
            try:
                rows = bl.list_project_connections(str(project_id), enabled_only=False)
                normalized = [dict(item) for item in rows if isinstance(item, dict)]
                if account_id:
                    normalized = [
                        item
                        for item in normalized
                        if not _normalize_text(item.get("account_id"))
                        or _normalize_text(item.get("account_id")) == _normalize_text(account_id)
                    ]
                return normalized
            except Exception:
                pass

        conn = getattr(bl, "conn", None)
        if conn is None or not self._table_exists(bl, "project_connections"):
            return []
        params: list[Any] = [str(project_id)]
        account_clause = ""
        normalized_account_id = _normalize_text(account_id)
        has_account_column = self._column_exists(bl, "project_connections", "account_id")
        if normalized_account_id and has_account_column:
            account_clause = " AND account_id = %s"
            params.append(normalized_account_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                  FROM project_connections
                 WHERE project_id = %s
                       {account_clause}
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
            description = cur.description
        return [_row_to_dict(row, description) for row in rows]

    @staticmethod
    def _result(
        *,
        workflow_id: str,
        account_id: str,
        project_id: str | None,
        availability_status: str,
        reason_code: str,
        reason_text: str = "",
        owner_install_id: str = "",
        blocked_dependencies: list[str] | None = None,
        missing_connections: list[str] | None = None,
        visible_in_catalog: bool | None = None,
    ) -> dict[str, Any]:
        normalized_status = _normalize_lower(availability_status) or "unavailable"
        normalized_reason = _normalize_lower(reason_code) or "none"
        if visible_in_catalog is None:
            visible_in_catalog = normalized_status != "removed"
        return {
            "workflow_id": _normalize_text(workflow_id),
            "account_id": _normalize_text(account_id),
            "project_id": str(project_id) if project_id is not None else None,
            "owner_install_id": _normalize_text(owner_install_id),
            "availability_status": normalized_status,
            "availability_reason_code": normalized_reason,
            "availability_reason_text": _normalize_text(reason_text)
            or _WORKFLOW_REASON_TEXT.get(normalized_reason, ""),
            "new_runs_blocked": normalized_status != "available",
            "visible_in_catalog": bool(visible_in_catalog),
            "blocked_dependencies": list(blocked_dependencies or []),
            "missing_connections": list(missing_connections or []),
        }

    @staticmethod
    def _row_declares_blocked(row: dict[str, Any]) -> bool:
        row_status = _normalize_lower(row.get("availability_status"))
        if row_status in {"unavailable", "draining", "removed"}:
            return True
        return bool(row.get("new_runs_blocked"))

    def _resolve_dependency_install_id(
        self,
        dep: dict[str, Any],
        *,
        account_id: str,
    ) -> str:
        install_id = _normalize_dependency_ref(dep.get("install_id"))
        if install_id:
            return install_id
        if _normalize_lower(dep.get("dependency_kind")) != "published_block":
            return ""
        block_key = _normalize_dependency_ref(dep.get("block_key") or dep.get("dependency_ref"))
        if not block_key:
            return ""
        bl = self._get_logger()
        if bl is None:
            return ""
        if hasattr(bl, "get_published_block_contract"):
            try:
                row = bl.get_published_block_contract(block_key)
                if isinstance(row, dict):
                    return _normalize_text(row.get("source_install_id"))
            except Exception:
                return ""
        conn = getattr(bl, "conn", None)
        if conn is None or not self._table_exists(bl, "registry_published_block_contracts"):
            return ""
        has_account_column = self._column_exists(
            bl,
            "registry_published_block_contracts",
            "account_id",
        )
        normalized_account_id = _normalize_text(account_id)
        where_account = (
            " AND (account_id IS NULL OR account_id = %s)"
            if has_account_column and normalized_account_id
            else ""
        )
        params: tuple[Any, ...] = (
            (block_key, normalized_account_id)
            if has_account_column and normalized_account_id
            else (block_key,)
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT source_install_id
                  FROM registry_published_block_contracts
                 WHERE block_key = %s
                       {where_account}
                 LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
            if row is None:
                return ""
            if isinstance(row, dict):
                return _normalize_text(row.get("source_install_id"))
            return _normalize_text(row[0])

    def _resolve_connection_dependency(
        self,
        *,
        dependencies: list[dict[str, Any]],
        project_connections: list[dict[str, Any]],
    ) -> tuple[str, str]:
        for dep in dependencies:
            if _normalize_lower(dep.get("dependency_kind")) != "project_connection":
                continue
            if not bool(dep.get("required_for_execution", True)):
                continue
            dep_ref = _normalize_dependency_ref(dep.get("dependency_ref"))
            connection = None
            if dep_ref:
                connection = next(
                    (
                        row
                        for row in project_connections
                        if dep_ref
                        in {
                            _normalize_text(row.get("connection_id")),
                            _normalize_text(row.get("extension_install_id")),
                            _normalize_text(row.get("extension_id")),
                        }
                    ),
                    None,
                )
            elif project_connections:
                connection = project_connections[0]

            if connection is None:
                return "missing_project_connection", dep_ref
            if not bool(connection.get("enabled", True)):
                return "project_connection_disabled", dep_ref or _normalize_text(
                    connection.get("connection_id")
                )
            if not project_connection_is_viable(connection):
                return "project_connection_health_failed", dep_ref or _normalize_text(
                    connection.get("connection_id")
                )
        return "", ""

    def _resolve_workflow_availability(
        self,
        workflow_id: str,
        *,
        account_id: str,
        project_id: str | None,
        visited: set[str],
    ) -> dict[str, Any]:
        normalized_workflow_id = _normalize_text(workflow_id)
        if not normalized_workflow_id:
            return self._result(
                workflow_id="",
                account_id=account_id,
                project_id=project_id,
                availability_status="unavailable",
                reason_code="workflow_not_found",
            )
        if normalized_workflow_id in visited:
            return self._result(
                workflow_id=normalized_workflow_id,
                account_id=account_id,
                project_id=project_id,
                availability_status="unavailable",
                reason_code="blocked_dependency",
                blocked_dependencies=[normalized_workflow_id],
            )

        if self._fetch_workflow_row is not None:
            workflow_row = self._fetch_workflow_row(
                normalized_workflow_id, _normalize_text(account_id)
            )
        else:
            workflow_row = self._db_fetch_workflow_row(
                normalized_workflow_id, _normalize_text(account_id)
            )
        if not isinstance(workflow_row, dict):
            return self._result(
                workflow_id=normalized_workflow_id,
                account_id=account_id,
                project_id=project_id,
                availability_status="unavailable",
                reason_code="workflow_not_found",
            )

        owner_install_id = _normalize_text(
            workflow_row.get("source_install_id") or workflow_row.get("owner_install_id")
        )
        if not bool(workflow_row.get("enabled", True)):
            return self._result(
                workflow_id=normalized_workflow_id,
                account_id=account_id,
                project_id=project_id,
                owner_install_id=owner_install_id,
                availability_status="unavailable",
                reason_code="workflow_disabled",
            )

        if self._row_declares_blocked(workflow_row):
            reason_code = (
                _normalize_lower(workflow_row.get("availability_reason_code"))
                or "blocked_dependency"
            )
            reason_text = _normalize_text(workflow_row.get("availability_reason_text"))
            status = _normalize_lower(workflow_row.get("availability_status")) or "unavailable"
            return self._result(
                workflow_id=normalized_workflow_id,
                account_id=account_id,
                project_id=project_id,
                owner_install_id=owner_install_id,
                availability_status=status,
                reason_code=reason_code,
                reason_text=reason_text,
                visible_in_catalog=bool(
                    workflow_row.get("visible_in_catalog", status != "removed")
                ),
            )

        if owner_install_id:
            owner_availability = self._install_availability_service.get_install_availability(
                owner_install_id,
                account_id=_normalize_text(account_id),
            )
            owner_status = _normalize_lower(owner_availability.get("availability_status"))
            if owner_status and owner_status != "available":
                return self._result(
                    workflow_id=normalized_workflow_id,
                    account_id=account_id,
                    project_id=project_id,
                    owner_install_id=owner_install_id,
                    availability_status=owner_status,
                    reason_code=_normalize_lower(
                        owner_availability.get("availability_reason_code")
                    ),
                    reason_text=_normalize_text(owner_availability.get("availability_reason_text")),
                    visible_in_catalog=bool(owner_availability.get("visible_in_catalog", True)),
                )

        bl = self._get_logger()
        tool_issues = workflow_registry_tool_issues(
            bl,
            workflow_row,
            account_id=_normalize_text(account_id),
        )
        if tool_issues:
            blocked_tool_ids = sorted(
                {str(issue.get("tool_id") or "").strip() for issue in tool_issues}
            )
            return self._result(
                workflow_id=normalized_workflow_id,
                account_id=account_id,
                project_id=project_id,
                owner_install_id=owner_install_id,
                availability_status="unavailable",
                reason_code="missing_or_unavailable_tool",
                reason_text=(
                    "Workflow has a missing or unavailable registry tool dependency: "
                    + ", ".join(blocked_tool_ids)
                ),
                blocked_dependencies=blocked_tool_ids,
            )

        if self._list_dependencies is not None:
            dependencies = self._list_dependencies(
                normalized_workflow_id, _normalize_text(account_id)
            )
        else:
            dependencies = self._db_list_dependencies(
                normalized_workflow_id, _normalize_text(account_id)
            )
        dependencies = [dict(item) for item in dependencies if isinstance(item, dict)]

        for dep in dependencies:
            if not bool(dep.get("required_for_execution", True)):
                continue
            dep_kind = _normalize_lower(dep.get("dependency_kind"))
            dep_ref = _normalize_dependency_ref(dep.get("dependency_ref") or dep.get("block_key"))
            if dep_kind in {"extension_install", "published_block"}:
                dep_install_id = self._resolve_dependency_install_id(
                    dep,
                    account_id=_normalize_text(account_id),
                )
                if dep_install_id:
                    dep_availability = self._install_availability_service.get_install_availability(
                        dep_install_id,
                        account_id=_normalize_text(account_id),
                    )
                    if _normalize_lower(dep_availability.get("availability_status")) != "available":
                        return self._result(
                            workflow_id=normalized_workflow_id,
                            account_id=account_id,
                            project_id=project_id,
                            owner_install_id=owner_install_id,
                            availability_status="unavailable",
                            reason_code="blocked_dependency",
                            blocked_dependencies=[dep_ref or dep_install_id],
                        )
            elif dep_kind == "child_workflow":
                child_workflow_id = _normalize_dependency_ref(dep.get("dependency_ref"))
                if child_workflow_id == normalized_workflow_id:
                    return self._result(
                        workflow_id=normalized_workflow_id,
                        account_id=account_id,
                        project_id=project_id,
                        owner_install_id=owner_install_id,
                        availability_status="unavailable",
                        reason_code="blocked_dependency",
                        blocked_dependencies=[child_workflow_id],
                    )
                if child_workflow_id:
                    child_availability = self._resolve_workflow_availability(
                        child_workflow_id,
                        account_id=account_id,
                        project_id=project_id,
                        visited={*visited, normalized_workflow_id},
                    )
                    if (
                        _normalize_lower(child_availability.get("availability_status"))
                        != "available"
                    ):
                        return self._result(
                            workflow_id=normalized_workflow_id,
                            account_id=account_id,
                            project_id=project_id,
                            owner_install_id=owner_install_id,
                            availability_status="unavailable",
                            reason_code="blocked_dependency",
                            blocked_dependencies=[child_workflow_id],
                        )

        project_connection_dependencies = [
            dep
            for dep in dependencies
            if _normalize_lower(dep.get("dependency_kind")) == "project_connection"
            and bool(dep.get("required_for_execution", True))
        ]
        if project_connection_dependencies and project_id is not None:
            if self._list_project_connections is not None:
                project_connections = self._list_project_connections(
                    str(project_id), _normalize_text(account_id)
                )
            else:
                project_connections = self._db_list_project_connections(
                    str(project_id), _normalize_text(account_id)
                )
            reason_code, dep_ref = self._resolve_connection_dependency(
                dependencies=project_connection_dependencies,
                project_connections=[
                    dict(item) for item in project_connections if isinstance(item, dict)
                ],
            )
            if reason_code:
                return self._result(
                    workflow_id=normalized_workflow_id,
                    account_id=account_id,
                    project_id=project_id,
                    owner_install_id=owner_install_id,
                    availability_status="unavailable",
                    reason_code=reason_code,
                    missing_connections=[dep_ref] if dep_ref else [],
                )

        return self._result(
            workflow_id=normalized_workflow_id,
            account_id=account_id,
            project_id=project_id,
            owner_install_id=owner_install_id,
            availability_status="available",
            reason_code="none",
        )

    def get_workflow_availability(
        self,
        workflow_id: str,
        *,
        account_id: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return self._resolve_workflow_availability(
            _normalize_text(workflow_id),
            account_id=_normalize_text(account_id),
            project_id=str(project_id) if project_id is not None else None,
            visited=set(),
        )

    def batch_workflow_availability(
        self,
        workflow_ids: Iterable[str],
        *,
        account_id: str,
        project_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = {_normalize_text(item) for item in workflow_ids if _normalize_text(item)}
        if not normalized_ids:
            return {}
        return {
            workflow_id: self.get_workflow_availability(
                workflow_id,
                account_id=_normalize_text(account_id),
                project_id=project_id,
            )
            for workflow_id in sorted(normalized_ids)
        }

    def assert_can_start_workflow(
        self,
        workflow_id: str,
        *,
        account_id: str,
        project_id: str | None = None,
    ) -> None:
        availability = self.get_workflow_availability(
            workflow_id,
            account_id=_normalize_text(account_id),
            project_id=project_id,
        )
        if _normalize_lower(availability.get("availability_status")) == "available" and not bool(
            availability.get("new_runs_blocked")
        ):
            return
        reason = _normalize_text(availability.get("availability_reason_text"))
        if not reason:
            reason = (
                _normalize_lower(availability.get("availability_reason_code"))
                or "blocked_dependency"
            )
        raise ServiceConflictError(
            f"Workflow '{workflow_id}' cannot be started: {reason}",
            payload=availability,
        )

    def annotate_workflow_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        account_id: str,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        by_id = self.batch_workflow_availability(
            [str(row.get("workflow_id") or "") for row in rows if isinstance(row, dict)],
            account_id=_normalize_text(account_id),
            project_id=project_id,
        )
        annotated: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            payload = dict(row)
            workflow_id = _normalize_text(payload.get("workflow_id"))
            availability = by_id.get(workflow_id, {})
            if availability:
                payload["availability_status"] = availability.get(
                    "availability_status", "available"
                )
                payload["availability_reason_code"] = availability.get(
                    "availability_reason_code",
                    "none",
                )
                payload["availability_reason_text"] = availability.get(
                    "availability_reason_text",
                    "",
                )
                payload["new_runs_blocked"] = bool(availability.get("new_runs_blocked", False))
                payload["visible_in_catalog"] = bool(availability.get("visible_in_catalog", True))
                payload["owner_install_id"] = availability.get("owner_install_id", "")
            else:
                payload.setdefault("availability_status", "available")
                payload.setdefault("availability_reason_code", "none")
                payload.setdefault("availability_reason_text", "")
                payload.setdefault("new_runs_blocked", False)
                payload.setdefault("visible_in_catalog", True)
            annotated.append(payload)
        return annotated
