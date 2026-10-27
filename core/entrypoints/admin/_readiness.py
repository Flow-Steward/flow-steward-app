# mypy: ignore-errors
from __future__ import annotations

from fastapi import HTTPException

from core.application.registry.contract_validation import format_validation_error_detail


def enforce_enabled_save_validation(
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    validation: dict,
    final_enabled: bool,
) -> None:
    if not final_enabled:
        return
    if bool((validation or {}).get("valid", False)):
        return
    raise HTTPException(
        status_code=400,
        detail=format_validation_error_detail(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            validation=validation,
        ),
    )
