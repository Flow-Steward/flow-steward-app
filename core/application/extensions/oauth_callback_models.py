from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict


class OAuthGrantClaimUnavailable(RuntimeError):
    """Raised when a provider needs serialization the repository cannot provide."""


OAuthCallbackOutcome = Literal[
    "connected",
    "needs_attention",
    "authorization_required",
    "authorization_denied",
    "restart_required",
    "provider_unavailable",
]


class OAuthCallbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: OAuthCallbackOutcome
    connection_id: str | None = None
    connection_preserved: Literal[True] | None = None
    help_target: dict[str, str] | None = None
    return_page_id: str | None = None


class OAuthCallbackRepository(Protocol):
    def lock_oauth_install_for_update(self, **scope: Any) -> dict[str, Any] | None: ...

    def consume_state_for_callback(
        self,
        *,
        state_hash: str,
        account_id: Any,
        project_id: Any,
        extension_id: str,
        callback_id: str,
    ) -> dict[str, Any] | None: ...

    def load_state_for_callback(
        self,
        *,
        state_hash: str,
        account_id: Any,
        project_id: Any,
        extension_id: str,
        callback_id: str,
    ) -> dict[str, Any] | None: ...

    def list_connections_for_configuration(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> list[dict[str, Any]]: ...

    def oauth_provider_token_claim(
        self, *, configuration_id: str, subject: str
    ) -> AbstractContextManager[None]: ...

    def get_configuration_for_scope(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> dict[str, Any] | None: ...

    def get_configuration_for_scope_for_update(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> dict[str, Any] | None: ...

    def lock_connection(
        self,
        *,
        connection_id: str,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
        connection_type: str,
    ) -> dict[str, Any] | None: ...

    def create_oauth_connection(self, **values: Any) -> dict[str, Any] | None: ...

    def update_oauth_connection(self, **values: Any) -> dict[str, Any] | None: ...

    def mark_configuration_verified(self, **scope: Any) -> bool: ...

    def mark_configuration_invalid_client(self, **scope: Any) -> dict[str, Any] | None: ...

    def update_oauth_post_connect(self, **values: Any) -> dict[str, Any] | None: ...


class OAuthCallbackTokenStore(Protocol):
    def resolve_access_token(self, *, account_id: str, secret_refs: Any) -> str: ...

    def resolve_refresh_token(self, *, account_id: str, secret_refs: Any) -> str: ...

    def write_tokens(
        self,
        *,
        account_id: str,
        connection_id: str,
        token_response: dict[str, Any],
        existing_refresh_token: str,
    ) -> dict[str, Any]: ...
