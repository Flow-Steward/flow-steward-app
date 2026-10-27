"""Project/tenant query policy for account-scoped Tool tombstones."""

from __future__ import annotations

from typing import Any

_TOOL_KIND_SQL = (
    "COALESCE(NULLIF(BTRIM(implementation->>'type'), ''), BTRIM(implementation->>'kind'), '')"
)
_ORDINARY_GLOBAL_SQL = f"(registry_tools.project_id IS NULL AND {_TOOL_KIND_SQL} <> 'mcp')"


def tool_trash_scope_predicates(scope: Any) -> tuple[list[str], list[Any]]:
    """Keep ordinary global Tools visible without making MCP Tools global.

    The caller retains the account predicate and admin authorization. Explicit
    project selection stays project-only; existing project-bound MCP discovery
    also requires its declared tenant, never the ordinary-global fallback.
    """
    clauses: list[str] = []
    params: list[Any] = []
    tenant_id = str(getattr(scope, "tenant_id", "") or "").strip()
    project_id = getattr(scope, "project_id", None)
    allowed_projects = frozenset(getattr(scope, "allowed_project_ids", ()) or ())
    if tenant_id:
        clauses.append(
            "".join(
                (
                    "(",
                    _ORDINARY_GLOBAL_SQL,
                    " OR (EXISTS (SELECT 1 FROM projects p "
                    "WHERE p.id = registry_tools.project_id AND p.tenant_id = %s) AND (",
                    _TOOL_KIND_SQL,
                    " <> 'mcp' OR BTRIM(implementation->>'owner_tenant_id') = %s)))",
                )
            )
        )
        params.extend([tenant_id, tenant_id])
    if project_id is not None:
        clauses.append("project_id = %s")
        params.append(str(project_id))
    elif allowed_projects:
        clauses.append(f"({_ORDINARY_GLOBAL_SQL} OR project_id = ANY(%s))")
        params.append(sorted(str(value) for value in allowed_projects))
    return clauses, params
