from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from core.application.management.scope import ScopeContext


class ExtensionOperationHandlerMode(Protocol):
    def __call__(
        self,
        extension: dict[str, Any] | None,
        *,
        operation_type: str,
        operation_id: str,
    ) -> str: ...


@dataclass
class _ActionExecutionContext:
    extension_id: str
    action_id: str
    body: Any
    scope: ScopeContext | None
    resolved_scope: ScopeContext
    extension: dict[str, Any]
    bl: Any
    target: dict[str, Any]
    input_payload: dict[str, Any]
    audit: dict[str, Any]
    runtime_ctx: dict[str, Any]
    direct_action_idempotency_key: str = ""
    direct_action_intent: dict[str, Any] = field(default_factory=dict)
    authorized_project_ids: set[str] = field(default_factory=set)
    external_effect: dict[str, Any] = field(default_factory=dict)
    external_effect_id: str = ""
    external_effect_claim_token: str = ""
    prepared_grants: Any = None
    suppressed_test_mode: bool = False
    #: Every connection type the operation declares.
    required_connection_type_ids: frozenset[str] = frozenset()
    required_oauth_scopes: frozenset[str] = frozenset()
    browser_bootstrap: bool = False
