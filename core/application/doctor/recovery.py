"""Recovery classification of persisted lifecycle facts for Doctor."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.doctor.checks import InterruptedOperation
from core.application.lifecycle.contracts import LifecycleOperationKind


def interrupted_journals(
    journals: Iterable[Mapping[str, Any]], *, profile: str, profile_switch_pending: bool
) -> list[InterruptedOperation]:
    found = []
    for journal in journals:
        kind = str(journal.get("kind") or "")
        config_apply = kind == LifecycleOperationKind.CONFIG_APPLY.value
        found.append(
            InterruptedOperation(
                operation_id=str(journal.get("operation_id") or ""),
                kind=kind,
                status=str(journal.get("status") or ""),
                step=str(journal.get("current_step") or journal.get("failed_step") or ""),
                recovery_command=(
                    "./install.sh"
                    if config_apply and profile_switch_pending
                    else str(journal.get("recovery_command") or "./flow-steward status")
                ),
                continuable=config_apply and profile == "scalable",
            )
        )
    return found
