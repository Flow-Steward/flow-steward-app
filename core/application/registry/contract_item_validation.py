"""Lightweight structured contract validation, without workflow execution imports."""

from __future__ import annotations

from typing import Any

from .workflow_contract import SECRET_FIELD_TYPES, normalize_field_type

ALLOWED_CONTRACT_TYPES: set[str] = {
    "text",
    "json",
    "list",
    "string",
    "paragraph",
    "number",
    "boolean",
    "select",
    "object",
    "array",
    "file",
}


def _issue(*, code: str, message: str, path: str = "", severity: str = "error") -> dict[str, str]:
    return {
        "code": str(code or "").strip(),
        "message": str(message or "").strip(),
        "path": str(path or "").strip(),
        "severity": str(severity or "error").strip().lower(),
    }


def _normalize_name(value: Any) -> str:
    return str(value or "").strip()


def _normalize_type(value: Any) -> str:
    normalized = normalize_field_type(value)
    return normalized or "paragraph"


def validate_contract_items(
    items: Any,
    *,
    path_prefix: str,
    default_required: bool,
    allow_source: bool = False,
    db: Any = None,
    require_structured_contract: bool = False,
    allow_secret_input: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate/normalize structured contract rows.

    Returns: (normalized_items, issues)
    """
    normalized: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    if items is None:
        return [], []
    if not isinstance(items, list):
        return [], [
            _issue(
                code="contract.items_not_list",
                message="Contract items must be a list",
                path=path_prefix,
            )
        ]

    for idx, raw in enumerate(items):
        row_path = f"{path_prefix}[{idx}]"
        if not isinstance(raw, dict):
            issues.append(
                _issue(
                    code="contract.item_not_object",
                    message="Contract item must be an object",
                    path=row_path,
                )
            )
            continue

        name = _normalize_name(raw.get("name"))
        if not name:
            issues.append(
                _issue(
                    code="contract.name_required",
                    message="Contract item name is required",
                    path=f"{row_path}.name",
                )
            )
            continue
        if any(ch.isspace() for ch in name):
            issues.append(
                _issue(
                    code="contract.name_whitespace",
                    message=f"Contract item '{name}' cannot contain whitespace",
                    path=f"{row_path}.name",
                )
            )
            continue
        if name in seen_names:
            issues.append(
                _issue(
                    code="contract.duplicate_name",
                    message=f"Duplicate contract item '{name}'",
                    path=f"{row_path}.name",
                )
            )
            continue
        seen_names.add(name)

        item_type = _normalize_type(raw.get("type"))
        allowed_types = (
            ALLOWED_CONTRACT_TYPES | SECRET_FIELD_TYPES
            if allow_secret_input
            else ALLOWED_CONTRACT_TYPES
        )
        if item_type not in allowed_types:
            issues.append(
                _issue(
                    code="contract.invalid_type",
                    message=(
                        f"Contract item '{name}' has unsupported type '{item_type}'. "
                        f"Allowed: {', '.join(sorted(allowed_types))}"
                    ),
                    path=f"{row_path}.type",
                )
            )
            continue

        normalized_item: dict[str, Any] = {
            "name": name,
            "type": item_type,
            "description": str(raw.get("description") or "").strip(),
            "required": bool(raw.get("required", default_required)),
        }
        contract_id = str(raw.get("contract_id") or "").strip()
        if contract_id:
            if item_type not in {"json", "list", "object", "array"}:
                issues.append(
                    _issue(
                        code="contract.contract_id_invalid_type",
                        message=(
                            f"Contract item '{name}' references data contract '{contract_id}', "
                            "but only object/array fields can attach a data contract"
                        ),
                        path=f"{row_path}.contract_id",
                    )
                )
                continue
            normalized_item["contract_id"] = contract_id
            if db is not None and hasattr(db, "get_data_contract"):
                contract = db.get_data_contract(contract_id)
                if not isinstance(contract, dict):
                    issues.append(
                        _issue(
                            code="contract.data_contract_missing",
                            message=f"Structured field '{name}' references missing data contract '{contract_id}'",
                            path=f"{row_path}.contract_id",
                        )
                    )
                    continue
                contract_kind = str(contract.get("kind") or "json").strip().lower()
                normalized_contract_kind = _normalize_type(contract_kind)
                if normalized_contract_kind != _normalize_type(item_type):
                    issues.append(
                        _issue(
                            code="contract.data_contract_kind_mismatch",
                            message=(
                                f"Structured field '{name}' declares type '{item_type}' but data contract "
                                f"'{contract_id}' is kind '{contract_kind}'"
                            ),
                            path=f"{row_path}.contract_id",
                        )
                    )
                    continue
        elif require_structured_contract and item_type in {"json", "list", "object", "array"}:
            issues.append(
                _issue(
                    code="contract.data_contract_required",
                    message=(
                        f"Structured field '{name}' must reference a data contract via contract_id"
                    ),
                    path=f"{row_path}.contract_id",
                )
            )
            continue
        if allow_source:
            normalized_item["source"] = str(raw.get("source") or "").strip()
        normalized.append(normalized_item)

    return normalized, issues
