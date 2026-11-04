"""Stored secrets in workspace data connections, at the admin HTTP boundary (ADR 0057)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from core.application.management.errors import ServiceError


def requested_secret_choices(
    value: Any,
    schema: dict,
    secret_values: dict[str, str],
    existing_refs: dict[str, str],
) -> dict[str, str]:
    """The stored secrets chosen per secret field, after the connector's secret check.

    A chosen secret stands for its field, so a typed value for it is dropped and the
    required check counts the choice; stopping without a new value leaves the field
    empty. Both maps change in place.
    """
    choices = _choices(value, schema=schema)
    for name, secret_id in choices.items():
        if secret_id:
            secret_values.pop(name, None)
        elif name not in secret_values:
            existing_refs.pop(name, None)
    chosen = {name: secret_id for name, secret_id in choices.items() if secret_id}
    from core.entrypoints.admin.datasets_helpers import _validate_connection_secrets

    _validate_connection_secrets(
        schema=schema, secret_values={**secret_values, **chosen}, existing_refs=existing_refs
    )
    return choices


def _choices(value: Any, *, schema: dict) -> dict[str, str]:
    """Stored secrets chosen for the connector's secret fields (ADR 0057)."""
    if not isinstance(value, dict):
        return {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    known = set(properties) if isinstance(properties, dict) else set()
    choices: dict[str, str] = {}
    for name, secret_id in value.items():
        field = str(name or "").strip()
        if not field:
            continue
        if known and field not in known:
            raise HTTPException(status_code=400, detail=f"Unknown secret field '{field}'")
        choices[field] = str(secret_id or "").strip()
    return choices


def with_secret_choices(db: Any, scope: Any, rows: list[dict]) -> list[dict]:
    """Each row with the stored secrets it uses, by field; names only, never values."""
    cursor = getattr(db, "_cursor", None)
    if not callable(cursor) or not rows:
        return rows
    from core.application.registry.data_connection_catalog import secret_choices_by_connection

    with cursor() as cur:
        choices = secret_choices_by_connection(
            cur,
            account_id=str(getattr(scope, "account_id", "") or ""),
            connection_ids=[str(row.get("connection_id") or "") for row in rows],
        )
    return [
        {**row, "secret_choices": choices.get(str(row.get("connection_id") or ""), {})}
        for row in rows
    ]


def save_error(exc: Exception) -> HTTPException:
    """A refused data connection save: a service error keeps its status, a bad value is 400."""
    if isinstance(exc, ServiceError):
        return HTTPException(status_code=exc.status_code, detail=exc.payload)
    return HTTPException(status_code=400, detail=str(exc))
