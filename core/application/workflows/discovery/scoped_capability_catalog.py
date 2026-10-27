from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.management.scope import normalize_scope
from core.application.registry.projected_artifact_rows import (
    projection_aware_catalog_row,
    projection_aware_catalog_rows,
)
from core.application.workflows.capability_visibility import (
    capability_available_for_execution,
    capability_visible_by_policy,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _scope_value(scope: Mapping[str, Any] | Any, key: str) -> str:
    if isinstance(scope, Mapping):
        return _text(scope.get(key))
    return _text(getattr(scope, key, ""))


def _normalize_status(value: Any) -> str:
    return _text(value).lower()


def _row_scope_kind(row: Mapping[str, Any]) -> str:
    explicit = _normalize_status(row.get("scope_kind"))
    if explicit:
        return explicit
    if _text(row.get("project_id")):
        return "project"
    if _text(row.get("tenant_id")):
        return "tenant"
    if _text(row.get("account_id")):
        return "account"
    return "system"


def _row_available(
    row: Mapping[str, Any], *, capability_type: str, assigned_skill_execution: bool = False
) -> bool:
    capability_id = _capability_row_id(capability_type, row)
    policy = (
        capability_available_for_execution
        if assigned_skill_execution and capability_type == "skill"
        else capability_visible_by_policy
    )
    return policy(
        capability_id,
        capability=row,
    )


def _request_tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", _text(value).lower()) if token}


def _summary_match_score(request_text: str, row: Mapping[str, Any]) -> tuple[int, int]:
    tokens = _request_tokens(request_text)
    if not tokens:
        return (0, 0)
    fields = [
        _text(row.get("display_name")),
        _text(row.get("description")),
        _text(row.get("description_preview")),
        _text(row.get("category")),
        _text(row.get("impl_type")),
        _text(
            row.get("tool_id")
            or row.get("skill_id")
            or row.get("agent_id")
            or row.get("workflow_id")
        ),
    ]
    lowered_fields = [field.lower() for field in fields if field]
    direct_hits = sum(1 for token in tokens if any(token in field for field in lowered_fields))
    exact_token_hits = len(tokens & _request_tokens(" ".join(fields)))
    return (direct_hits, exact_token_hits)


def _summary_sort_key(request_text: str, row: Mapping[str, Any]) -> tuple[int, int, str, str]:
    direct_hits, exact_token_hits = _summary_match_score(request_text, row)
    return (
        -direct_hits,
        -exact_token_hits,
        _text(row.get("type")),
        _text(row.get("display_name")),
    )


def _capability_row_id(capability_type: str, row: Mapping[str, Any]) -> str:
    id_key = {
        "workflow": "workflow_id",
        "agent": "agent_id",
        "skill": "skill_id",
        "tool": "tool_id",
    }.get(capability_type, "id")
    return _text(row.get(id_key) or row.get("id"))


@dataclass(frozen=True)
class ScopedCapabilityRef:
    capability_type: str
    capability_id: str
    operation_id: str = ""


class ScopedCapabilityCatalog:
    def __init__(self, db: Any) -> None:
        self.db = db

    def _list_summary_rows(
        self,
        getter_name: str,
        *,
        enabled_only: bool,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
    ) -> list[dict[str, Any]]:
        getter = getattr(self.db, getter_name, lambda **_: [])
        scope_obj = normalize_scope(scope, project_id=project_id)
        account_id = _scope_value(scope_obj, "account_id") or None
        tenant_id = _scope_value(scope_obj, "tenant_id") or None
        normalized_project_id = _text(project_id) or _scope_value(scope_obj, "project_id") or None
        try:
            rows = getter(
                enabled_only=enabled_only,
                account_id=account_id,
                tenant_id=tenant_id,
                project_id=normalized_project_id,
            )
        except TypeError:
            try:
                rows = getter(
                    enabled_only=enabled_only,
                    account_id=account_id,
                    project_id=normalized_project_id,
                )
            except TypeError:
                rows = getter(enabled_only=enabled_only)
        return [dict(row) for row in (rows or []) if isinstance(row, Mapping)]

    def list_visible_capability_summaries(
        self,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
        request_text: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        rows.extend(self.workflow_summaries(scope=scope, project_id=project_id))
        rows.extend(self.agent_summaries(scope=scope, project_id=project_id))
        rows.extend(self.skill_summaries(scope=scope, project_id=project_id))
        rows.extend(self.tool_summaries(scope=scope, project_id=project_id))
        rows.sort(key=lambda item: _summary_sort_key(request_text, item))
        return rows[: max(0, int(limit or 0))]

    def workflow_summaries(
        self,
        *,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
    ) -> list[dict[str, Any]]:
        rows = self._list_summary_rows(
            "list_workflows_summary",
            enabled_only=True,
            scope=scope,
            project_id=project_id,
        )
        project_workflow_ids = self._project_workflow_ids(project_id)
        return [
            {**row, "type": "workflow"}
            for row in rows
            if self._row_visible(
                row,
                capability_type="workflow",
                scope=scope,
                project_id=project_id,
                project_workflow_ids=project_workflow_ids,
            )
        ]

    def agent_summaries(
        self,
        *,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
    ) -> list[dict[str, Any]]:
        rows = self._list_summary_rows(
            "list_agents_summary",
            enabled_only=True,
            scope=scope,
            project_id=project_id,
        )
        account_id = _scope_value(normalize_scope(scope, project_id=project_id), "account_id")
        rows = projection_aware_catalog_rows(
            self.db, "agent", "agent_id", rows, account_id=account_id, enabled_only=True
        )
        return [
            {**row, "type": "agent"}
            for row in rows
            if self._row_visible(row, capability_type="agent", scope=scope, project_id=project_id)
        ]

    def skill_summaries(
        self,
        *,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
    ) -> list[dict[str, Any]]:
        rows = self._list_summary_rows(
            "list_skills_summary",
            enabled_only=True,
            scope=scope,
            project_id=project_id,
        )
        account_id = _scope_value(normalize_scope(scope, project_id=project_id), "account_id")
        rows = projection_aware_catalog_rows(
            self.db, "skill", "skill_id", rows, account_id=account_id, enabled_only=True
        )
        return [
            {**row, "type": "skill"}
            for row in rows
            if self._row_visible(row, capability_type="skill", scope=scope, project_id=project_id)
        ]

    def tool_summaries(
        self,
        *,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
    ) -> list[dict[str, Any]]:
        rows = self._list_summary_rows(
            "list_tools_summary",
            enabled_only=True,
            scope=scope,
            project_id=project_id,
        )
        account_id = _scope_value(normalize_scope(scope, project_id=project_id), "account_id")
        from core.application.extensions.extension_projection_v2 import (
            installed_extension_grounding_tools,
        )

        rows_by_id = {_text(row.get("tool_id")): row for row in rows if _text(row.get("tool_id"))}
        for tool in installed_extension_grounding_tools(self.db, account_id=account_id):
            tool_id = _text(tool.get("tool_id"))
            if tool_id and tool_id not in rows_by_id:
                rows_by_id[tool_id] = {
                    **tool,
                    "description_preview": _text(tool.get("description"))[:120],
                }
        rows = list(rows_by_id.values())
        return [
            {**row, "type": "tool"}
            for row in rows
            if self._row_visible(row, capability_type="tool", scope=scope, project_id=project_id)
        ]

    def hydrate_visible_capabilities(
        self,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
        capability_refs: Iterable[ScopedCapabilityRef | Mapping[str, Any]],
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        hydrated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for ref in capability_refs:
            normalized = self._normalize_ref(ref)
            if not normalized.capability_id:
                continue
            try:
                row = self.assert_capability_visible(
                    scope=scope,
                    project_id=project_id,
                    capability_ref=normalized,
                )
            except ValueError:
                continue
            hydrated[
                (normalized.capability_type, normalized.capability_id, normalized.operation_id)
            ] = row
        return hydrated

    def assert_capability_visible(
        self,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
        capability_ref: ScopedCapabilityRef | Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._assert_capability_visible(scope, project_id, capability_ref)

    def assert_assigned_skill_available(
        self,
        *,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
        skill_id: str,
    ) -> dict[str, Any]:
        """Resolve an assigned runtime skill without requiring discovery visibility.

        Require explicit account context before normalization can supply a default.
        Assignment itself remains the caller's responsibility.
        """
        if not _scope_value(scope, "account_id"):
            raise ValueError("Selected skill is unavailable in the current scope.")
        return self._assert_capability_visible(
            scope,
            project_id,
            ScopedCapabilityRef("skill", skill_id),
            assigned_skill_execution=True,
        )

    def _assert_capability_visible(
        self,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
        capability_ref: ScopedCapabilityRef | Mapping[str, Any],
        *,
        assigned_skill_execution: bool = False,
    ) -> dict[str, Any]:
        normalized = self._normalize_ref(capability_ref)
        capability_type = normalized.capability_type
        capability_id = normalized.capability_id
        if capability_type == "workflow":
            row = getattr(self.db, "get_workflow", lambda *_: None)(capability_id)
            project_workflow_ids = self._project_workflow_ids(project_id)
        elif capability_type in {"agent", "skill"}:
            account_id = _scope_value(normalize_scope(scope, project_id=project_id), "account_id")
            manual_row = getattr(self.db, f"get_{capability_type}", lambda *_: None)(capability_id)
            row = projection_aware_catalog_row(
                self.db,
                capability_type,
                f"{capability_type}_id",
                capability_id,
                manual_row,
                account_id=account_id,
            )
            project_workflow_ids = None
        elif capability_type == "tool":
            getter = getattr(self.db, "get_tool", lambda *_: None)
            row = getter(capability_id) or getter(capability_id.removeprefix("connector."))
            if not isinstance(row, Mapping):
                from core.application.extensions.extension_projection_v2 import (
                    installed_extension_grounding_tools,
                )

                account_id = _scope_value(
                    normalize_scope(scope, project_id=project_id), "account_id"
                )
                row = next(
                    (
                        tool
                        for tool in installed_extension_grounding_tools(
                            self.db, account_id=account_id
                        )
                        if _text(tool.get("tool_id")) == capability_id
                    ),
                    None,
                )
            project_workflow_ids = None
        else:
            raise ValueError("Unknown capability type.")
        if not isinstance(row, Mapping):
            raise ValueError("Selected capability is not visible in the current scope.")
        payload = dict(row)
        if not self._row_visible(
            payload,
            capability_type=capability_type,
            scope=scope,
            project_id=project_id,
            project_workflow_ids=project_workflow_ids,
            assigned_skill_execution=assigned_skill_execution,
        ):
            raise ValueError("Selected capability is not visible in the current scope.")
        return payload

    def _normalize_ref(
        self,
        capability_ref: ScopedCapabilityRef | Mapping[str, Any],
    ) -> ScopedCapabilityRef:
        if isinstance(capability_ref, ScopedCapabilityRef):
            return capability_ref
        capability_type = _normalize_status(
            capability_ref.get("type")
            or capability_ref.get("capability_type")
            or capability_ref.get("kind")
        )
        capability_id = _text(
            capability_ref.get("id")
            or capability_ref.get("capability_id")
            or capability_ref.get("workflow_id")
            or capability_ref.get("agent_id")
            or capability_ref.get("skill_id")
            or capability_ref.get("tool_id")
            or capability_ref.get("connector_id")
        )
        if capability_type == "connector":
            capability_type = "tool"
        operation_id = _text(
            capability_ref.get("operation_id") or capability_ref.get("function_name")
        )
        return ScopedCapabilityRef(
            capability_type=capability_type,
            capability_id=capability_id,
            operation_id=operation_id,
        )

    def _project_workflow_ids(self, project_id: str | None) -> set[str]:
        normalized_project_id = _text(project_id)
        if not normalized_project_id or not hasattr(self.db, "list_project_workflows"):
            return set()
        try:
            rows = (
                self.db.list_project_workflows(project_id=normalized_project_id, enabled_only=True)
                or []
            )
        except TypeError:
            rows = self.db.list_project_workflows(project_id=normalized_project_id) or []
        return {
            _text((row or {}).get("workflow_id"))
            for row in rows
            if isinstance(row, Mapping) and _text((row or {}).get("workflow_id"))
        }

    def _row_visible(
        self,
        row: dict[str, Any],
        *,
        capability_type: str,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
        project_workflow_ids: set[str] | None = None,
        assigned_skill_execution: bool = False,
    ) -> bool:
        if not _row_available(
            row,
            capability_type=capability_type,
            assigned_skill_execution=assigned_skill_execution,
        ):
            return False
        scope_obj = normalize_scope(scope, project_id=project_id)
        if (
            capability_type == "workflow"
            and scope_obj.allowed_workflow_ids
            and _capability_row_id(capability_type, row) not in scope_obj.allowed_workflow_ids
        ):
            return False
        scope_account_id = _scope_value(scope_obj, "account_id")
        scope_tenant_id = _scope_value(scope_obj, "tenant_id")
        normalized_project_id = _text(project_id) or _scope_value(scope_obj, "project_id")
        row_account_id = _text(row.get("account_id"))
        row_tenant_id = _text(row.get("tenant_id"))
        row_project_id = _text(row.get("project_id"))
        scope_kind = _row_scope_kind(row)

        if row_account_id and scope_account_id and row_account_id != scope_account_id:
            return False
        if row_tenant_id and scope_tenant_id and row_tenant_id != scope_tenant_id:
            return False
        if row_tenant_id and not scope_tenant_id:
            return False
        if row_project_id and normalized_project_id and row_project_id != normalized_project_id:
            return False
        if row_project_id and not normalized_project_id:
            return False
        if scope_kind in {"system", "public"}:
            return True
        if scope_kind == "project":
            if row_project_id and normalized_project_id == row_project_id:
                return True
            if capability_type == "workflow":
                return _capability_row_id(capability_type, row) in (project_workflow_ids or set())
            return False
        if scope_kind == "tenant":
            return bool(row_tenant_id and scope_tenant_id and row_tenant_id == scope_tenant_id)
        if scope_kind == "account":
            return bool(row_account_id and scope_account_id and row_account_id == scope_account_id)
        if row_project_id:
            return normalized_project_id == row_project_id
        if row_account_id:
            return scope_account_id == row_account_id
        return False
