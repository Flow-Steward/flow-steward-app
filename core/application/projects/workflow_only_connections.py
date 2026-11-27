"""Project configuration a person limited to chosen workflows may see (ADR 0076 §8b).

In a project reached only through workflow grants, project-wide configuration is
shown read-only, and only the part a granted workflow needs; everything else is
hidden (fail closed). A connection is needed by a granted workflow when:

- one of its steps is bound to the connection (``workflow_runtime_dependencies``
  rows of kind ``project_connection``);
- one of its steps or Invokes calls the connection's extension install (any
  dependency row naming that ``install_id``);
- the granted workflow is a projected workflow of that install
  (``extension.<install>.<id>``, whose connection slots read the install's
  connections); or
- the connection maps an event to the granted workflow (``workflow_mappings``).

A dependency read that fails yields no needs: the person sees nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.auth.principal import coerce_optional_project_id
from core.application.management.scope import ScopeContext

logger = logging.getLogger(__name__)

#: ``(account_id, workflow_ids) -> dependency rows`` with ``dependency_kind``,
#: ``dependency_ref`` and ``install_id``.
DependencyLoader = Callable[[str, list[str]], Iterable[Mapping[str, Any]]]


@dataclass(frozen=True)
class WorkflowConnectionNeeds:
    """What the granted workflows of one workflow-only project need."""

    workflow_ids: frozenset[str]
    connection_refs: frozenset[str] = field(default_factory=frozenset)
    install_ids: frozenset[str] = field(default_factory=frozenset)

    def needs(self, connection: Mapping[str, Any] | None) -> bool:
        """Whether a granted workflow needs this connection."""
        row = connection or {}
        refs = {
            str(row.get(key) or "").strip()
            for key in ("connection_id", "connection_key", "binding_key")
        } - {""}
        if refs & self.connection_refs:
            return True
        install_id = str(row.get("extension_install_id") or "").strip()
        if install_id and install_id in self.install_ids:
            return True
        mappings = row.get("workflow_mappings")
        if isinstance(mappings, Mapping):
            mapped = {str(value or "").strip() for value in mappings.values()}
            if mapped & self.workflow_ids:
                return True
        return False

    def needs_install(self, install_id: Any) -> bool:
        """Whether a granted workflow calls this extension install."""
        return str(install_id or "").strip() in self.install_ids


def restricted_workflow_ids(scope: ScopeContext, project_id: Any) -> frozenset[str] | None:
    """The granted workflows when ``project_id`` is reached only through them, else ``None``."""
    normalized = coerce_optional_project_id(project_id)
    restricted = scope.project_workflow_ids or {}
    if normalized is None or normalized not in restricted:
        return None
    return frozenset(restricted[normalized])


def _projected_install_id(workflow_id: str) -> str:
    parts = workflow_id.split(".", 2)
    if len(parts) == 3 and parts[0] == "extension" and parts[1] and parts[2]:
        return parts[1]
    return ""


_DEPENDENCY_SQL = """SELECT dependency_kind, dependency_ref, install_id
                       FROM workflow_runtime_dependencies
                      WHERE account_id = %s AND workflow_id = ANY(%s)"""


def cursor_dependency_loader(cur: Any) -> DependencyLoader:
    """Read the granted workflows' dependencies with an open cursor."""

    def load(account_id: str, workflow_ids: list[str]) -> list[Mapping[str, Any]]:
        cur.execute(_DEPENDENCY_SQL, (account_id, workflow_ids))
        names = [column[0] for column in cur.description or []]
        return [dict(zip(names, row, strict=False)) for row in cur.fetchall() or []]

    return load


def _db_dependency_loader(bl: Any) -> DependencyLoader:
    def load(account_id: str, workflow_ids: list[str]) -> list[Mapping[str, Any]]:
        conn = getattr(bl, "conn", None)
        if conn is None:
            raise RuntimeError("database connection unavailable")
        with conn.cursor() as cur:
            return list(cursor_dependency_loader(cur)(account_id, workflow_ids))

    return load


def workflow_connection_needs(
    bl: Any,
    scope: ScopeContext,
    project_id: Any,
    *,
    account_id: str | None = None,
    load_dependencies: DependencyLoader | None = None,
) -> WorkflowConnectionNeeds | None:
    """The needs of a workflow-only project's granted workflows; ``None`` when not limited."""
    workflow_ids = restricted_workflow_ids(scope, project_id)
    if workflow_ids is None:
        return None
    install_ids = {install for wid in workflow_ids if (install := _projected_install_id(wid))}
    connection_refs: set[str] = set()
    account = str(account_id or scope.account_id or "").strip()
    loader = load_dependencies or _db_dependency_loader(bl)
    try:
        rows = list(loader(account, sorted(workflow_ids))) if workflow_ids else []
    except Exception:
        logger.warning("Workflow dependency read failed; hiding project configuration")
        return WorkflowConnectionNeeds(workflow_ids=workflow_ids)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        install_id = str(row.get("install_id") or "").strip()
        if install_id:
            install_ids.add(install_id)
        if str(row.get("dependency_kind") or "").strip().lower() == "project_connection":
            ref = str(row.get("dependency_ref") or "").strip()
            if ref:
                connection_refs.add(ref)
    return WorkflowConnectionNeeds(
        workflow_ids=workflow_ids,
        connection_refs=frozenset(connection_refs),
        install_ids=frozenset(install_ids),
    )


def connections_for_scope(
    bl: Any,
    scope: ScopeContext,
    project_id: Any,
    rows: Iterable[Any],
    *,
    account_id: str | None = None,
    load_dependencies: DependencyLoader | None = None,
) -> list[Any]:
    """``rows`` (connection mappings or views with ``dump()``) a limited person may see."""
    items = list(rows or [])
    needs = workflow_connection_needs(
        bl, scope, project_id, account_id=account_id, load_dependencies=load_dependencies
    )
    if needs is None:
        return items
    return [row for row in items if needs.needs(_as_mapping(row))]


def require_install_needed(
    bl: Any,
    scope: ScopeContext,
    project_id: Any,
    install_id: Any,
    *,
    account_id: str | None = None,
    load_dependencies: DependencyLoader | None = None,
) -> None:
    """Refuse an extension install's project data no granted workflow uses (not revealed)."""
    from core.application.management.errors import ServiceNotFoundError

    needs = workflow_connection_needs(
        bl, scope, project_id, account_id=account_id, load_dependencies=load_dependencies
    )
    if needs is not None and not needs.needs_install(install_id):
        raise ServiceNotFoundError("Extension install not found")


def _as_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    dump = getattr(row, "dump", None) or getattr(row, "model_dump", None)
    if callable(dump):
        payload = dump()
        if isinstance(payload, Mapping):
            return payload
    return {}


__all__ = [
    "WorkflowConnectionNeeds",
    "connections_for_scope",
    "cursor_dependency_loader",
    "require_install_needed",
    "restricted_workflow_ids",
    "workflow_connection_needs",
]
