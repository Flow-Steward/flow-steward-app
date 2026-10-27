from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _read_declarations(extension: dict[str, Any]) -> list[dict[str, Any]]:
    compiled = _as_dict(extension.get("compiled_manifest"))
    declaration = _as_dict(extension.get("project_state")) or _as_dict(
        compiled.get("project_state")
    )
    return [
        _as_dict(item) for item in declaration.get("reads") or [] if _as_dict(item).get("namespace")
    ]


def _read_key(read: dict[str, Any], input_payload: dict[str, Any]) -> str:
    static_key = str(read.get("state_key") or "").strip()
    if static_key:
        return static_key
    source_field = str(read.get("state_key_from_input") or "").strip()
    if not source_field:
        return ""
    candidate = input_payload.get(source_field)
    if candidate is None or isinstance(candidate, (dict, list, tuple, set)):
        return ""
    value = str(candidate).strip()
    if not value:
        return ""
    return f"{read.get('state_key_prefix') or ''!s}{value}"


def load_extension_project_state_for_target(
    bl: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str,
    connection_id: str,
    extension: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hydrate only the project-state reads declared by one extension."""
    if bl is None or not callable(getattr(bl, "cursor", None)):
        return {}
    if not account_id or not project_id or not extension_id:
        return {}
    reads = _read_declarations(_as_dict(extension))
    if not reads:
        return {}
    payload = _as_dict(input_payload)
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        from core.application.extensions.extension_project_state import get_state, list_state

        for read in reads:
            namespace = str(read.get("namespace") or "").strip()
            state_key = _read_key(read, payload)
            if state_key:
                project_row = get_state(
                    bl,
                    account_id=account_id,
                    project_id=project_id,
                    extension_id=extension_id,
                    extension_install_id=extension_install_id,
                    namespace=namespace,
                    state_key=state_key,
                    connection_id=None,
                )
                if project_row:
                    rows_by_key[(namespace, state_key)] = project_row
                if connection_id:
                    connection_row = get_state(
                        bl,
                        account_id=account_id,
                        project_id=project_id,
                        extension_id=extension_id,
                        extension_install_id=extension_install_id,
                        namespace=namespace,
                        state_key=state_key,
                        connection_id=connection_id,
                    )
                    if connection_row:
                        rows_by_key[(namespace, state_key)] = connection_row
                continue
            max_items = int(read.get("max_items") or 0)
            if max_items <= 0:
                continue
            project_rows = list_state(
                bl,
                account_id=account_id,
                project_id=project_id,
                extension_id=extension_id,
                extension_install_id=extension_install_id,
                namespace=namespace,
                connection_id=None,
                limit=max_items,
            )
            connection_rows: list[dict[str, Any]] = []
            if connection_id:
                connection_rows = list_state(
                    bl,
                    account_id=account_id,
                    project_id=project_id,
                    extension_id=extension_id,
                    extension_install_id=extension_install_id,
                    namespace=namespace,
                    connection_id=connection_id,
                    limit=max_items,
                )
            for row in [*project_rows, *connection_rows]:
                row_namespace = str(row.get("namespace") or "").strip()
                row_key = str(row.get("state_key") or "").strip()
                if row_namespace and row_key:
                    rows_by_key[(row_namespace, row_key)] = row
    except Exception as exc:
        logger.warning("failed to load extension project state for runtime target: %s", exc)
        return {}
    rows = list(rows_by_key.values())
    if not rows:
        return {}
    return {
        "items": [
            {
                "namespace": str(row.get("namespace") or "").strip(),
                "state_key": str(row.get("state_key") or "").strip(),
                "value": _as_dict(row.get("value_jsonb")),
                "revision": row.get("revision"),
                "connection_id": str(row.get("connection_id") or "").strip() or None,
            }
            for row in rows
        ]
    }


__all__ = ["load_extension_project_state_for_target"]
