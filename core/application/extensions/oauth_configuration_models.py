"""Input, output and dependency contracts for OAuth configuration use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
)
from core.application.extensions.oauth_lifecycle import (
    PreparedOAuthRevocation,
)

OAuthConfigurationStatus = Literal[
    "incomplete", "ready_for_authorization", "verified", "needs_attention"
]


class OAuthConfigurationSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: UUID | None = None
    display_name: str = Field(min_length=1, max_length=200)
    values: dict[str, str] = Field(default_factory=dict)
    secret_replacements: dict[str, str] = Field(default_factory=dict)

    @field_validator("configuration_id", mode="before")
    @classmethod
    def _normalize_configuration_id(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("display_name", mode="before")
    @classmethod
    def _normalize_display_name(cls, value: Any) -> str:
        return str(value or "").strip()


class OAuthConfigurationFieldView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    value: str | None = None
    secret_present: bool | None = None


class OAuthConfigurationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: str
    display_name: str
    status: OAuthConfigurationStatus
    fields: list[OAuthConfigurationFieldView]
    revision: int
    authorization_revision: int


class OAuthConfigurationConnectionImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    display_name: str = ""
    connection_type: str = ""


class OAuthBoundWorkflowStep(BaseModel):
    """A workflow step that would lose its connection."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_name: str
    group_name: str = ""
    step_name: str
    connection_id: str


class OAuthConfigurationImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: str | None
    expected_revision: int | None
    changed_fields: list[str]
    change_policies: list[str]
    connections: list[OAuthConfigurationConnectionImpact]
    requires_confirmation: bool
    confirmation_token: str | None = None
    # Deleting a setup deletes its connections; the steps bound to them stop
    # working. Nobody was told which ones, so a recreated connection left a
    # whole workflow unable to save.
    workflow_steps: list[OAuthBoundWorkflowStep] = Field(default_factory=list)


class OAuthConfigurationRepository(Protocol):
    @property
    def create_configuration(self) -> Callable[..., dict[str, Any] | None]: ...

    @property
    def list_configurations(self) -> Callable[..., list[dict[str, Any]]]: ...

    @property
    def get_configuration_for_scope(self) -> Callable[..., dict[str, Any] | None]: ...

    @property
    def get_configuration_for_scope_for_update(
        self,
    ) -> Callable[..., dict[str, Any] | None]: ...

    @property
    def update_configuration(self) -> Callable[..., dict[str, Any] | None]: ...

    @property
    def list_connections_for_configuration(self) -> Callable[..., list[dict[str, Any]]]: ...

    @property
    def list_connections_for_configuration_for_update(
        self,
    ) -> Callable[..., list[dict[str, Any]]]: ...

    @property
    def invalidate_pending_states(self) -> Callable[..., int]: ...

    @property
    def mark_connections_reauthorization_required(self) -> Callable[..., int]: ...

    @property
    def delete_connections_for_configuration(self) -> Callable[..., int]: ...

    @property
    def delete_configuration(self) -> Callable[..., bool]: ...

    @property
    def repin_connections_to_release(self) -> Callable[..., int]: ...


class OAuthConfigurationSecretStore(Protocol):
    def present(self, *, account_id: str, ref_key: str) -> bool: ...

    def write(self, *, account_id: str, ref_key: str, value: str) -> None: ...

    def delete(self, *, account_id: str, ref_key: str) -> None: ...


class _ResolvedProvider(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    extension_id: str
    release_id: str
    provider: OAuthProviderContract


class _PreparedChange(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    resolved: _ResolvedProvider
    existing: dict[str, Any] | None
    configuration_id: str
    non_secret_config: dict[str, str]
    secret_refs: dict[str, str]
    secret_replacements: dict[str, str]
    changed_fields: list[str]
    change_policies: list[str]
    connections: list[dict[str, Any]]


@dataclass(frozen=True)
class _PreparedDelete:
    extension_id: str
    extension_release_id: str
    provider_security_fingerprint: str
    provider_revocation_fingerprint: str
    blocked_revision: int
    connection_ids: tuple[str, ...]
    revocations: tuple[PreparedOAuthRevocation, ...]
