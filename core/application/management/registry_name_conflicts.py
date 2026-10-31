"""One rule for identifier and display-name conflicts of agents, skills and workflows.

Every writer that creates one of these objects, or renames it, calls
:func:`require_free_display_name` inside the transaction that saves the object.
Agent and skill creates turn a taken identifier into :func:`id_conflict_error`.
Both raise :class:`ServiceConflictError` with one payload shape, so the admin
UI, the HTTP API and MCP report the same conflict.

Visibility is the caller's: a conflict names only objects the caller may see,
their where-used rows are filtered by the caller's access to each row, and a
suggested name or identifier is a hint that nothing reserves.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from core.application.management.errors import ServiceConflictError, ServiceForbiddenError
from core.application.management.guards import require_workflow_access
from core.application.registry.active_workflow_dependencies import enabled_workflow_dependencies
from core.application.registry.identifier_keys import unique_text_key
from core.application.registry.projected_artifact_rows import projection_aware_catalog_rows
from core.application.workflows.workflow_hook_contract import is_hook_storage
from core.application.workflows.workflow_where_used import workflow_consumers

from .command_services_agent_rules import row_in_account_scope

RegistryEntityType = Literal["agent", "skill", "workflow"]

NAME_CONFLICT_CODE = "registry_name_conflict"
ID_CONFLICT_CODE = "registry_id_conflict"

_ID_KEYS: dict[str, str] = {"agent": "agent_id", "skill": "skill_id", "workflow": "workflow_id"}
_ENTITY_LABELS: dict[str, str] = {"agent": "Agent", "skill": "Skill", "workflow": "Workflow"}
_MATCH_LIMIT = 10
_USED_BY_LIMIT = 25
_SUGGESTION_ATTEMPTS = 1000
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_display_name(value: Any) -> str:
    """Compare names case-insensitively, trimmed, with inner whitespace collapsed."""
    return " ".join(str(value or "").split()).casefold()


def display_name_lock_key(account_id: Any, entity_type: str, normalized_name: str) -> str:
    """The advisory-lock key that serializes writers of one name in one account."""
    return f"registry_display_name:{_text(account_id)}:{entity_type}:{normalized_name}"


def effective_display_name(data: Mapping[str, Any] | None, entity_id: str) -> str:
    """The name the registry store saves: an omitted ``display_name`` becomes the id."""
    return str((data or {}).get("display_name", entity_id) or "")


def workflow_row_visible(scope: Any, workflow: Mapping[str, Any]) -> bool:
    """Whether the caller may see this workflow row (workflow, project and account limits)."""
    workflow_id = _text(workflow.get("workflow_id") or workflow.get("id"))
    if workflow_id:
        try:
            require_workflow_access(scope, workflow_id)
        except ServiceForbiddenError:
            return False
    project_id = _text(workflow.get("project_id"))
    allowed_project_ids = getattr(scope, "allowed_project_ids", None)
    if project_id and allowed_project_ids and project_id not in allowed_project_ids:
        return False
    row_account_id = _text(workflow.get("account_id"))
    scope_account_id = _text(getattr(scope, "account_id", ""))
    return not (row_account_id and scope_account_id and row_account_id != scope_account_id)


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in (value or []) if isinstance(row, Mapping)]


def _call_rows(db: Any, method: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    reader = getattr(db, method, None)
    return _rows(reader(*args, **kwargs)) if callable(reader) else []


def visible_registry_rows(db: Any, entity_type: str, scope: Any) -> list[dict[str, Any]]:
    """Every live object of *entity_type* the caller sees, extension projections included."""
    account_id = _text(getattr(scope, "account_id", ""))
    if entity_type == "agent":
        rows = projection_aware_catalog_rows(
            db, "agent", "agent_id", _call_rows(db, "list_agents"), account_id=account_id
        )
        return [row for row in rows if row_in_account_scope(row, scope)]
    if entity_type == "skill":
        rows = projection_aware_catalog_rows(
            db,
            "skill",
            "skill_id",
            _call_rows(db, "list_skills", account_id=account_id),
            account_id=account_id,
        )
        return [row for row in rows if row_in_account_scope(row, scope)]
    if entity_type == "workflow":
        manual = [row for row in _call_rows(db, "list_workflows") if not is_hook_storage(row)]
        rows = projection_aware_catalog_rows(
            db, "workflow", "workflow_id", manual, account_id=account_id
        )
        return [row for row in rows if workflow_row_visible(scope, row)]
    raise ValueError(f"Unsupported registry entity type: {entity_type}")


def _row_id(entity_type: str, row: Mapping[str, Any]) -> str:
    return _text(row.get(_ID_KEYS[entity_type]) or row.get("id"))


def _row_names(entity_type: str, row: Mapping[str, Any]) -> list[str]:
    value = row.get("display_name")
    if isinstance(value, Mapping):
        names = [str(item) for item in value.values() if isinstance(item, str)]
    else:
        names = [str(value or "")]
    return [name for name in names if name.strip()] or [_row_id(entity_type, row)]


def _origin(row: Mapping[str, Any]) -> str:
    """Where the object comes from: an extension, the platform, or the caller's account.

    Ownership decides it, not ``scope_kind``: agents and skills are stored with
    the column default ``'system'`` even when a user creates them in an account
    (the store gives a system row without project or install the default
    account). Only a row without an account is platform-global, and only an
    extension projection carries ``source_install_id``.
    """
    if _text(row.get("source_install_id")):
        return "extension"
    if not _text(row.get("account_id")):
        return "system"
    return "account"


def _timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


class _ConflictView:
    """Caller-scoped presentation of matching objects and their dependents."""

    def __init__(self, db: Any, entity_type: str, scope: Any, rows: list[dict[str, Any]]):
        self.db = db
        self.entity_type = entity_type
        self.scope = scope
        self.rows = rows
        self._visible: dict[str, dict[str, dict[str, Any]]] = {entity_type: self._by_id(rows)}

    def _by_id(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        return {_row_id(self.entity_type, row): dict(row) for row in rows}

    def visible(self, entity_type: str) -> dict[str, dict[str, Any]]:
        if entity_type not in self._visible:
            rows = visible_registry_rows(self.db, entity_type, self.scope)
            self._visible[entity_type] = {_row_id(entity_type, row): row for row in rows}
        return self._visible[entity_type]

    def _dependent(self, entity_type: str, entity_id: str, **extra: Any) -> dict[str, Any] | None:
        row = self.visible(entity_type).get(entity_id)
        if row is None:
            return None
        return {
            "entity_type": entity_type,
            "id": entity_id,
            "display_name": _row_names(entity_type, row)[0],
            "enabled": bool(row.get("enabled", False)),
            **extra,
        }

    def used_by(self, entity_id: str) -> list[dict[str, Any]]:
        if self.entity_type == "agent":
            # The disable guard's query: enabled workflows that run this agent.
            candidates = [
                self._dependent("workflow", workflow_id)
                for workflow_id in enabled_workflow_dependencies(self.db, agent_ids=[entity_id])
            ]
        elif self.entity_type == "skill":
            reader = getattr(self.db, "get_agents_using_skill", None)
            agent_ids = reader(entity_id) if callable(reader) else []
            candidates = [self._dependent("agent", _text(agent_id)) for agent_id in agent_ids or []]
        else:
            candidates = self._workflow_consumers(entity_id)
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, Any]] = []
        for item in candidates:
            if item is None or (item["entity_type"], item["id"]) in seen:
                continue
            seen.add((item["entity_type"], item["id"]))
            result.append(item)
        return result

    def _workflow_consumers(self, workflow_id: str) -> list[dict[str, Any] | None]:
        transaction = getattr(self.db, "_transaction", None)
        if not callable(transaction):
            return []
        with transaction() as cur:
            consumers = workflow_consumers(
                cur, account_id=getattr(self.scope, "account_id", ""), workflow_id=workflow_id
            )
        return [
            self._dependent("workflow", consumer.caller_workflow_id, relation=consumer.kind)
            for consumer in consumers
        ]

    def match(self, row: Mapping[str, Any]) -> dict[str, Any]:
        entity_id = _row_id(self.entity_type, row)
        used_by = self.used_by(entity_id)
        return {
            "id": entity_id,
            "display_name": _row_names(self.entity_type, row)[0],
            "enabled": bool(row.get("enabled", False)),
            "updated_at": _timestamp(row.get("updated_at")),
            "origin": _origin(row),
            "used_by": used_by[:_USED_BY_LIMIT],
            "used_by_truncated": len(used_by) > _USED_BY_LIMIT,
        }

    def matches(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "matches": [self.match(row) for row in rows[:_MATCH_LIMIT]],
            "matches_truncated": len(rows) > _MATCH_LIMIT,
        }


def suggest_display_name(display_name: str, taken: Iterable[str]) -> str | None:
    """The first ``"<name> 2"``, ``"<name> 3"``… nobody visible uses; only a hint."""
    base = " ".join(str(display_name or "").split())
    used = {normalize_display_name(name) for name in taken}
    for number in range(2, _SUGGESTION_ATTEMPTS + 2):
        candidate = f"{base} {number}"
        if normalize_display_name(candidate) not in used:
            return candidate
    return None


def _suggest_id(requested_id: str, taken: set[str]) -> str | None:
    try:
        return unique_text_key(requested_id, lambda candidate: candidate in taken)
    except ValueError:
        return None


def _lock_display_name(db: Any, *, entity_type: str, account_id: Any, normalized: str) -> None:
    transaction = getattr(db, "_transaction", None)
    if not callable(transaction):
        return
    has_active_transaction = getattr(db, "_has_active_transaction", None)
    if callable(has_active_transaction) and has_active_transaction() is False:
        # A lock taken in its own short transaction would be gone before the save.
        raise RuntimeError("The display-name check must run inside the saving transaction.")
    with transaction() as cur:
        cur.execute(_LOCK_SQL, (display_name_lock_key(account_id, entity_type, normalized),))


def require_free_display_name(
    db: Any,
    *,
    entity_type: RegistryEntityType,
    display_name: str,
    scope: Any,
    entity_id: str = "",
    current_display_name: str | None = None,
) -> None:
    """Refuse creating, or renaming into, a name a visible object of the same type has.

    Call it inside the transaction that saves the object, before the save. It
    takes ``pg_advisory_xact_lock`` keyed by account, type and normalized name,
    so a concurrent writer of the same name waits for this transaction and then
    sees its row. A save that keeps the current name (after normalization) is
    never refused, so existing duplicates do not block editing other fields.
    """
    normalized = normalize_display_name(display_name)
    if not normalized:
        return
    if current_display_name is not None and normalize_display_name(current_display_name) == (
        normalized
    ):
        return
    _lock_display_name(
        db,
        entity_type=entity_type,
        account_id=getattr(scope, "account_id", ""),
        normalized=normalized,
    )
    rows = visible_registry_rows(db, entity_type, scope)
    excluded_id = _text(entity_id)
    matches = [
        row
        for row in rows
        if _row_id(entity_type, row) != excluded_id
        and any(normalize_display_name(name) == normalized for name in _row_names(entity_type, row))
    ]
    if not matches:
        return
    requested = " ".join(str(display_name).split())
    label = _ENTITY_LABELS[entity_type]
    message = f"{label} name '{requested}' is already used. Choose another name."
    view = _ConflictView(db, entity_type, scope, rows)
    raise ServiceConflictError(
        message,
        payload={
            "error": NAME_CONFLICT_CODE,
            "code": NAME_CONFLICT_CODE,
            "message": message,
            "entity_type": entity_type,
            "field": "display_name",
            "display_name": requested,
            **view.matches(matches),
            "suggested_display_name": suggest_display_name(
                requested, (name for row in rows for name in _row_names(entity_type, row))
            ),
        },
    )


def id_conflict_error(
    db: Any,
    *,
    entity_type: Literal["agent", "skill"],
    entity_id: str,
    scope: Any,
    taken_ids: Callable[[], Iterable[str]] = tuple,
) -> ServiceConflictError:
    """The 409 for an explicitly requested identifier that is already taken.

    Only an object the caller may see is named; an id held by another account
    or by an object in Trash yields the same conflict without a match.
    """
    rows = visible_registry_rows(db, entity_type, scope)
    requested_id = _text(entity_id)
    visible_match = [row for row in rows if _row_id(entity_type, row) == requested_id]
    taken = {requested_id, *(_row_id(entity_type, row) for row in rows)}
    taken.update(_text(value) for value in taken_ids())
    label = _ENTITY_LABELS[entity_type]
    message = f"{label} ID '{requested_id}' is already taken. Choose another ID."
    view = _ConflictView(db, entity_type, scope, rows)
    return ServiceConflictError(
        message,
        payload={
            "error": ID_CONFLICT_CODE,
            "code": ID_CONFLICT_CODE,
            "message": message,
            "entity_type": entity_type,
            "field": _ID_KEYS[entity_type],
            "id": requested_id,
            **view.matches(visible_match),
            "suggested_id": _suggest_id(requested_id, taken),
        },
    )


def require_free_id(
    db: Any,
    *,
    entity_type: Literal["agent", "skill"],
    entity_id: str,
    scope: Any,
    exists: Callable[[str], bool],
    taken_ids: Callable[[], Iterable[str]] = tuple,
) -> None:
    """Refuse a create whose id is taken before any work; the insert re-checks it atomically."""
    requested_id = _text(entity_id)
    projected = any(
        _row_id(entity_type, row) == requested_id
        for row in visible_registry_rows(db, entity_type, scope)
    )
    if projected or exists(requested_id):
        raise id_conflict_error(
            db, entity_type=entity_type, entity_id=requested_id, scope=scope, taken_ids=taken_ids
        )


__all__ = [
    "ID_CONFLICT_CODE",
    "NAME_CONFLICT_CODE",
    "display_name_lock_key",
    "effective_display_name",
    "id_conflict_error",
    "normalize_display_name",
    "require_free_display_name",
    "require_free_id",
    "suggest_display_name",
    "visible_registry_rows",
    "workflow_row_visible",
]
