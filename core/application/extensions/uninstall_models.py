from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.management.scope import ScopeContext


@dataclass
class UninstallContext:
    extension_id: str
    scope: ScopeContext | None
    resolved_scope: ScopeContext
    plan_revision: str
    account_id: str
    bl: Any
    runtime_store: Any
    confirmed_plan: dict[str, Any]


@dataclass(frozen=True)
class UninstallTarget:
    extension: dict[str, Any]
    install_id: str
    release_id: str
    include_consumer_accounts: bool


@dataclass
class RemovalTransactionState:
    install_id: str
    release_id: str
    remove_shared_runtime_package: bool
    affected_state_persisted: bool
    cleanup_intent: Any = None
    #: What the uninstall found on disk that it did not delete: files already
    #: gone, or a folder kept on purpose. Reported in ``removed_data``.
    file_notes: tuple[str, ...] = ()
