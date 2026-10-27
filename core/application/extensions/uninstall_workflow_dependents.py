"""Saved workflow dependents for an exact extension install uninstall plan."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

CursorFactory = Callable[[], AbstractContextManager[Any]]


def collect_workflow_dependents(
    bl: Any,
    *,
    extension_id: str,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    extension_label: str = "",
) -> list[dict[str, Any]]:
    if not _dependency_tables_exist(bl):
        return []
    cursor_factory = _cursor_factory(bl)
    if cursor_factory is None:
        return []

    rows = _dependency_rows(
        cursor_factory,
        extension_id=extension_id,
        install_id=install_id,
        account_id=account_id,
        include_consumer_accounts=include_consumer_accounts,
        include_legacy=_legacy_tables_exist(bl),
    )
    return _affected_workflows(
        rows, extension_label=extension_label or extension_id, account_id=account_id
    )


def _cursor_factory(bl: Any) -> CursorFactory | None:
    cursor_factory = getattr(bl, "cursor", None)
    if not callable(cursor_factory):
        cursor_factory = getattr(getattr(bl, "conn", None), "cursor", None)
    return cursor_factory if callable(cursor_factory) else None


def _dependency_rows(
    cursor_factory: CursorFactory,
    *,
    extension_id: str,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    include_legacy: bool,
) -> list[dict[str, Any]]:
    with cursor_factory() as cur:
        rows = _exact_dependency_rows(
            cur,
            install_id=install_id,
            account_id=account_id,
            include_consumer_accounts=include_consumer_accounts,
        )
        if include_legacy:
            rows.extend(
                _legacy_dependency_rows(
                    cur,
                    extension_id=extension_id,
                    install_id=install_id,
                    account_id=account_id,
                    include_consumer_accounts=include_consumer_accounts,
                )
            )
    return rows


def _exact_dependency_rows(
    cur: Any,
    *,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT COALESCE(d.account_id::text, '') AS account_id,
               d.workflow_id,
               COALESCE(w.display_name, d.workflow_id) AS display_name,
               d.dependency_ref
          FROM workflow_runtime_dependencies d
          JOIN registry_workflows w ON w.workflow_id = d.workflow_id
         WHERE (%s OR d.account_id::text = %s)
           AND d.install_id = %s
           AND d.dependency_kind = 'extension_action'
           AND d.required_for_execution = true
         ORDER BY d.workflow_id, d.dependency_ref
        """,
        (include_consumer_accounts, account_id, install_id),
    )
    return [_row(item, cur.description) for item in (cur.fetchall() or [])]


def _legacy_dependency_rows(
    cur: Any,
    *,
    extension_id: str,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT COALESCE(c.account_id::text, '') AS account_id,
               s.owner_workflow_id AS workflow_id,
               COALESCE(w.display_name, s.owner_workflow_id) AS display_name,
               COALESCE(
                   s.override->'connector'->>'operation_id',
                   s.override->'connector'->>'action_id',
                   s.override->'connector'->>'function_name',
                   'saved_extension_action'
               ) AS dependency_ref
          FROM registry_workflow_steps s
          JOIN registry_workflows w ON w.workflow_id = s.owner_workflow_id
          JOIN project_connections c
            ON c.connection_id = COALESCE(
                s.connection_binding_jsonb->>'connection_id',
                s.override->'connector'->'connection_binding'->>'connection_id'
            )
         WHERE s.connector_id = %s
           AND (%s OR c.account_id::text = %s)
           AND c.extension_install_id = %s
           AND c.extension_id = %s
           AND NOT EXISTS (
               SELECT 1
                 FROM workflow_runtime_dependencies d
                WHERE d.workflow_id = s.owner_workflow_id
                  AND d.step_id = s.step_id
                  AND d.dependency_kind = 'extension_action'
                  AND d.install_id = c.extension_install_id
           )
         ORDER BY s.owner_workflow_id, dependency_ref
        """,
        (
            f"extension.{extension_id}",
            include_consumer_accounts,
            account_id,
            install_id,
            extension_id,
        ),
    )
    return [_row(item, cur.description) for item in (cur.fetchall() or [])]


def _affected_workflows(
    rows: list[dict[str, Any]], *, extension_label: str, account_id: str
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        key = (
            _text(row.get("account_id")),
            _text(row.get("workflow_id")),
            _text(row.get("display_name")) or _text(row.get("workflow_id")),
        )
        if not key[1]:
            continue
        grouped.setdefault(key, set()).add(_text(row.get("dependency_ref")))
    affected: list[dict[str, Any]] = []
    for (owner_account_id, workflow_id, display_name), actions in grouped.items():
        action_text = ", ".join(f"'{item}'" for item in sorted(filter(None, actions)))
        reason = (
            f"Uses {extension_label} action {action_text}; this workflow will stop until the "
            "extension is reinstalled or the step is replaced."
        )
        affected.append(
            {
                "kind": "workflow",
                "id": workflow_id,
                "display_name": display_name,
                "owner_install_id": "",
                "owner_account_id": owner_account_id or account_id,
                "action": "preserve_and_disable",
                "reason": reason,
            }
        )
    return affected


def _dependency_tables_exist(bl: Any) -> bool:
    table_exists = getattr(bl, "_table_exists", None)
    return callable(table_exists) and all(
        table_exists(table) for table in ("workflow_runtime_dependencies", "registry_workflows")
    )


def _legacy_tables_exist(bl: Any) -> bool:
    table_exists = getattr(bl, "_table_exists", None)
    return callable(table_exists) and all(
        table_exists(table)
        for table in (
            "registry_workflow_steps",
            "project_connections",
        )
    )


def _row(value: Any, description: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    columns = [str(item.name) for item in (description or [])]
    return dict(zip(columns, value, strict=False))


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["collect_workflow_dependents"]
