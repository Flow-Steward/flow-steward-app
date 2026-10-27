"""Carry out the removal an uninstall has already resolved and confirmed.

Split out of `ExtensionUninstallService` to keep that file reviewable; the order
of the steps is the contract, so they live together here rather than being spread
back across the service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def remove_resolved_uninstall_target(
    context: Any,
    target: Any,
    *,
    persistence: Any,
    reconcile_committed_bundle_cleanup: Any,
    delete_runtime_install_records: Any,
    install_store: Any,
    plan: Any,
    require_archive_mutation_available: Callable[[], None],
) -> Any:
    """Remove the target; every extension uninstalls the same way."""
    require_archive_mutation_available()
    cleanup_intent, removed_data, state = persistence.remove_installed_target(context, target)
    result = reconcile_committed_bundle_cleanup(context, cleanup_intent, removed_data)
    delete_runtime_install_records(context, target.install_id)
    if not hasattr(install_store, "_conn") and hasattr(install_store, "uninstall"):
        install_store.uninstall(
            extension_id=context.extension_id,
            account_id=context.account_id,
        )
    if not state.affected_state_persisted:
        plan.apply_confirmed_uninstall_affected_state(
            plan=context.confirmed_plan,
            runtime_store=context.runtime_store,
            account_id=context.account_id,
        )
    return result


__all__ = ["remove_resolved_uninstall_target"]
