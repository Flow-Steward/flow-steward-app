"""Read contract fields without planner or session dependencies."""

from __future__ import annotations

from typing import Any

from .capability_contracts import contract_field_names


def _text(value: Any) -> str:
    return str(value or "").strip()


def _candidate_contract_field_names(detail: dict[str, Any] | None, *, kind: str) -> list[str]:
    if not isinstance(detail, dict):
        return []
    rows = detail.get(f"expected_{kind}s")
    if not isinstance(rows, list):
        rows = detail.get(f"_{kind}s")
    return contract_field_names(rows if isinstance(rows, list) else [])


def _workflow_contract_field_rows(
    detail: dict[str, Any] | None, *, kind: str
) -> list[dict[str, Any]]:
    if not isinstance(detail, dict):
        return []
    raw_settings = detail.get("settings")
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    raw_callable_settings = settings.get("callable")
    callable_settings = raw_callable_settings if isinstance(raw_callable_settings, dict) else {}
    if isinstance(callable_settings, dict):
        rows = callable_settings.get(f"expected_{kind}s") or []
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    if kind == "input":
        rows = (detail.get("interface_contract") or {}).get("expected_inputs") or []
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    else:
        rows = (detail.get("output_contract") or {}).get("expected_outputs") or []
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    rows = detail.get(f"expected_{kind}s")
    if not isinstance(rows, list):
        rows = detail.get(f"_{kind}s")
    return [row for row in (rows or []) if isinstance(row, dict)]


def _workflow_contract_field_names(detail: dict[str, Any] | None, *, kind: str) -> list[str]:
    rows = _workflow_contract_field_rows(detail, kind=kind)
    names = contract_field_names(rows)
    return names or _candidate_contract_field_names(detail, kind=kind)
