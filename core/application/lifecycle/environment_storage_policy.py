"""Artifact availability decisions shared by environment planning and execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import ExitCode

if TYPE_CHECKING:
    from core.application.lifecycle.environment_service import StorageInventory


def _storage_decision_required(inventory: StorageInventory) -> bool:
    return inventory.completeness != "complete" or (inventory.approximate_count or 0) > 0


def _artifact_decision_required(
    inventory: StorageInventory | None = None,
) -> LifecycleError:
    return LifecycleError(
        code="environment_artifact_decision_required",
        summary="An explicit artifact availability decision is required.",
        cause="The source storage contains artifacts or its inventory is unknown.",
        remediation="Choose already_copied or accept_unavailable after reviewing the inventory.",
        retryable=False,
        step="storage_decision",
        exit_code=ExitCode.VALIDATION,
        details={
            "source_storage_inventory": (
                inventory.model_dump(mode="json") if inventory is not None else None
            )
        },
    )
