# ruff: noqa: F403, F405
from __future__ import annotations

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_services_tool import (
    _tool_row_in_scope as _command_tool_row_in_scope,
)
from core.application.registry.projected_artifact_rows import (
    projected_catalog_row_matches,
    projection_aware_catalog_row,
    projection_aware_catalog_rows,
)

from .query_services_common import *  # NOSONAR


def _tool_row_in_scope(row: Any, scope: ScopeContext | None) -> bool:
    """True if *row* is visible to *scope* under account **and** project isolation.

    ``registry_tools`` carries optional ``account_id`` and ``project_id``:
    * An account-owned row (non-empty ``account_id``) must match the caller's account.
    * For a project-restricted principal (non-empty ``allowed_project_ids``), a
      project-bound row (non-empty ``project_id``) must be in the allowed set;
      account-global rows (``project_id`` NULL) stay visible account-wide.

    Non-dict rows (test doubles) carry no ownership data and are treated as in-scope.
    """
    if not isinstance(row, dict):
        return True
    row_account = str(row.get("account_id") or "").strip()
    if row_account:
        scope_account = str(getattr(scope, "account_id", "") or "").strip()
        if not scope_account or row_account != scope_account:
            return False
    allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset()) or frozenset()
    if allowed_project_ids:
        row_project = str(row.get("project_id") or "").strip()
        if row_project and row_project not in {str(p).strip() for p in allowed_project_ids}:
            return False
    return True


class ToolQueryService:
    def __init__(
        self,
        *,
        get_db: Callable[[], Any],
        get_registry: Callable[[], Any],
        get_tool_validation: Callable[[Any, str], dict[str, Any]],
    ):
        self._get_db = get_db
        self._get_registry = get_registry
        self._get_tool_validation = get_tool_validation

    def _require_tool_in_scope(self, db: Any, tool_id: str, scope: ScopeContext | None) -> dict:
        tool = projection_aware_catalog_row(
            db,
            "tool",
            "tool_id",
            tool_id,
            db.get_tool(tool_id),
            account_id=str(getattr(scope, "account_id", "") or DEFAULT_ACCOUNT_ID),
        )
        if not tool or not _tool_row_in_scope(tool, scope):
            raise ServiceNotFoundError(f"Tool '{tool_id}' not found")
        return tool

    @staticmethod
    def _record(payload: Any) -> ToolRecordView:
        if isinstance(payload, dict):
            row = dict(payload)
        else:
            try:
                row = dict(payload)
            except Exception:
                row = {}
        return ToolRecordView.model_validate(row)

    def get_health(self, *, scope: ScopeContext | None = None) -> ToolsHealthView:
        _ = _service_scope(scope)
        try:
            registry = self._get_registry()
            resolver = registry.tool_resolver
            return ToolsHealthView(
                registered=dict(resolver.list_registered() or {}),
                errors=list(resolver.get_errors() or []),
            )
        except Exception as exc:
            logger.warning("tool query health failed: %s", exc)
            return ToolsHealthView(registered={}, errors=[str(exc)])

    def list_tools(
        self,
        *,
        enabled_only: bool = False,
        category: str = "",
        search: str = "",
        scope: ScopeContext | None = None,
    ) -> ToolsListView:
        resolved = _service_scope(scope)
        db = self._get_db()
        normalized_search = str(search or "").strip()
        normalized_category = str(category or "").strip()
        durable_rows = db.list_tools(enabled_only=enabled_only) or []
        assignable_tool_ids = {
            str(row.get("tool_id") or "").strip()
            for row in durable_rows
            if isinstance(row, dict)
            and str(row.get("tool_id") or "").strip()
            and not str(row.get("tool_id") or "").strip().startswith("std.")
            and not row.get("deleted_at")
            and _command_tool_row_in_scope(row, resolved)
        }
        rows = projection_aware_catalog_rows(
            db,
            "tool",
            "tool_id",
            durable_rows,
            account_id=resolved.account_id,
            enabled_only=enabled_only,
        )
        if normalized_category:
            rows = [
                row
                for row in rows
                if isinstance(row, dict) and row.get("category") == normalized_category
            ]
        if normalized_search:
            # search_entities returns a generic cross-entity projection
            # (entity_id/display_name/description), not tool rows. Returning it
            # directly changed the response schema under ?search= — 11 fields
            # disappeared and tool_id came back null — and silently discarded
            # enabled_only and category. Use it only to select which tools match,
            # then serve the canonical rows.
            matched_ids = {
                str(row.get("tool_id") or row.get("entity_id") or "").strip()
                for row in (db.search_entities(normalized_search, entity_type="tool") or [])
                if isinstance(row, dict)
            }
            matched_ids.discard("")
            rows = [
                row
                for row in rows
                if isinstance(row, dict)
                and (
                    str(row.get("tool_id") or "").strip() in matched_ids
                    or projected_catalog_row_matches(
                        row,
                        normalized_search,
                        fields=("tool_id", "display_name", "description", "category"),
                    )
                )
            ]
        rows = [row for row in rows if _command_tool_row_in_scope(row, resolved)]
        for row in rows:
            tool_id = str(row.get("tool_id") or "").strip()
            assignable = tool_id in assignable_tool_ids
            connector_only = tool_id.startswith("std.")
            row["agent_relationship_assignable"] = assignable
            if assignable:
                reason_code, reason_text = "none", ""
            elif connector_only:
                reason_code = "tool_connector_only"
                reason_text = (
                    "Standard Tool Actions are available only through connector workflow steps"
                )
            else:
                reason_code = "tool_not_assignable_in_current_scope"
                reason_text = "This tool is unavailable for assignment in the current project or tenant scope."
            row["agent_relationship_reason_code"] = reason_code
            row["agent_relationship_reason_text"] = reason_text
        return ToolsListView(tools=[self._record(row) for row in rows])

    def get_tool(self, tool_id: str, *, scope: ScopeContext | None = None) -> ToolRecordView:
        resolved = _service_scope(scope)
        db = self._get_db()
        tool = self._require_tool_in_scope(db, tool_id, resolved)
        return self._record(tool)

    def get_contract_validation(
        self, tool_id: str, *, scope: ScopeContext | None = None
    ) -> ToolValidationView:
        resolved = _service_scope(scope)
        db = self._get_db()
        self._require_tool_in_scope(db, tool_id, resolved)
        return ToolValidationView(
            tool_id=tool_id, validation=self._get_tool_validation(db, tool_id)
        )

    def list_agents_using_tool(
        self, tool_id: str, *, scope: ScopeContext | None = None
    ) -> list[str]:
        resolved = _service_scope(scope)
        db = self._get_db()
        self._require_tool_in_scope(db, tool_id, resolved)
        rows = db.get_agents_using_tool(tool_id) or []
        return list(rows)

    def list_history(
        self,
        tool_id: str,
        *,
        limit: int = 50,
        scope: ScopeContext | None = None,
    ) -> ToolHistoryEntriesView:
        resolved = _service_scope(scope)
        db = self._get_db()
        self._require_tool_in_scope(db, tool_id, resolved)
        rows = db.get_entity_history(entity_type="tool", entity_id=tool_id, limit=limit) or []
        return ToolHistoryEntriesView(entries=[self._record(row) for row in rows])

    def get_revision(
        self, tool_id: str, revision: int, *, scope: ScopeContext | None = None
    ) -> ToolRecordView:
        resolved = _service_scope(scope)
        db = self._get_db()
        self._require_tool_in_scope(db, tool_id, resolved)
        entry = db.get_entity_revision(entity_type="tool", entity_id=tool_id, revision=revision)
        if not entry:
            raise ServiceNotFoundError(f"Revision {revision} not found for tool '{tool_id}'")
        return self._record(entry)

    def get_diff(
        self,
        tool_id: str,
        *,
        from_entry_id: str | None = None,
        to_entry_id: str | None = None,
        scope: ScopeContext | None = None,
    ) -> ToolDiffView:
        resolved = _service_scope(scope)
        db = self._get_db()
        self._require_tool_in_scope(db, tool_id, resolved)
        try:
            payload = db.diff_entity_history(
                entity_type="tool",
                entity_id=tool_id,
                from_entry_id=from_entry_id,
                to_entry_id=to_entry_id,
            )
        except KeyError as exc:
            raise ServiceNotFoundError(str(exc)) from exc
        return ToolDiffView.model_validate(payload or {})


__all__ = [
    "ToolQueryService",
]
