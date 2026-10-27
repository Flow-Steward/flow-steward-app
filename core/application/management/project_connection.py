"""Project Connection model — project-scoped activation and configuration of extensions/connectors.

A ``project_connection`` ties an installed extension (or MCP connector) to a
specific project with its own config, secret refs, routes, and workflow mappings.
One extension install supports many project connections; each connection is
isolated to its project and account.

Naming convention: all new identifiers use ``connection`` terminology.
The term ``binding`` is not used in new code.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _generate_connection_id() -> str:
    return f"conn_{uuid.uuid4().hex[:16]}"


def normalize_secret_refs_payload(value: Any) -> dict[str, Any]:
    """Normalize connection secret refs into the canonical object shape."""
    if not value:
        return {}
    if isinstance(value, dict):
        payload = dict(value)
        refs_value = payload.get("refs")
        if isinstance(refs_value, dict):
            payload["refs"] = {
                str(key or "").strip(): str(ref or "").strip()
                for key, ref in refs_value.items()
                if str(key or "").strip() and str(ref or "").strip()
            }
            return payload
        if all(not isinstance(item, (dict, list)) for item in payload.values()):
            refs = {
                str(key or "").strip(): str(ref or "").strip()
                for key, ref in payload.items()
                if str(key or "").strip() and str(ref or "").strip()
            }
            return {"refs": refs} if refs else {}
        payload["refs"] = {}
        return payload
    if isinstance(value, list):
        refs = {}
        for item in value:
            secret_name = str(item or "").strip()
            if secret_name:
                refs[secret_name] = secret_name
        return {"refs": refs} if refs else {}
    return {}


class ProjectConnection(BaseModel):
    """Project-scoped association between a project and an extension/connector."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(default_factory=_generate_connection_id)
    project_id: str
    extension_id: str | None = None
    account_id: str
    display_name: str = ""
    connection_kind: str = "extension"  # "extension" | "mcp_connector" | "extension_runtime"
    connection_type: str = ""
    environment: str = ""
    credential_revision: int = 1
    extension_install_id: str = ""
    oauth_configuration_id: str | None = None
    oauth_provider_id: str | None = None
    oauth_extension_release_id: str | None = None
    oauth_authorization_revision: int | None = None
    oauth_requested_scopes: list[str] = Field(default_factory=list)
    oauth_granted_scopes: list[str] = Field(default_factory=list)
    oauth_provider_security_fingerprint: str | None = None
    oauth_provider_revocation_fingerprint: str | None = None
    runtime_mode: str | None = None  # "local" | "remote_https"
    remote_base_url: str = ""
    health_status: str = ""
    runtime_status: str = ""
    last_seen_ok_at: float | None = None
    last_error: str = ""
    enabled: bool = True
    connection_config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, Any] = Field(default_factory=dict)
    connection_routes: list[dict[str, Any]] = Field(default_factory=list)
    ingress_config: dict[str, Any] = Field(default_factory=dict)
    workflow_mappings: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @field_validator("project_id", mode="before")
    @classmethod
    def _normalize_project_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("project_id is required")
        return normalized

    @field_validator("connection_kind", mode="before")
    @classmethod
    def _normalize_connection_kind(cls, value: Any) -> str:
        normalized = str(value or "extension").strip().lower() or "extension"
        if normalized not in {"extension", "mcp_connector", "extension_runtime"}:
            raise ValueError(
                "connection_kind must be extension, mcp_connector, or extension_runtime"
            )
        return normalized

    @field_validator(
        "extension_id",
        "oauth_configuration_id",
        "oauth_provider_id",
        "oauth_extension_release_id",
        "oauth_provider_security_fingerprint",
        "oauth_provider_revocation_fingerprint",
        mode="before",
    )
    @classmethod
    def _normalize_optional_identifier(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("runtime_mode", mode="before")
    @classmethod
    def _validate_runtime_mode(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"local", "remote_https"}:
            raise ValueError("runtime_mode must be local or remote_https")
        return normalized

    @field_validator(
        "display_name",
        "connection_type",
        "environment",
        "extension_install_id",
        "remote_base_url",
        "last_error",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("oauth_authorization_revision", mode="before")
    @classmethod
    def _normalize_oauth_authorization_revision(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        revision = int(value)
        if revision < 1:
            raise ValueError("oauth_authorization_revision must be at least 1")
        return revision

    @field_validator("oauth_requested_scopes", "oauth_granted_scopes", mode="before")
    @classmethod
    def _normalize_oauth_scopes(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        scopes: list[str] = []
        for item in value:
            scope = str(item or "").strip()
            if scope and scope not in scopes:
                scopes.append(scope)
        return scopes

    @field_validator("credential_revision", mode="before")
    @classmethod
    def _normalize_credential_revision(cls, value: Any) -> int:
        try:
            return max(1, int(value or 1))
        except (TypeError, ValueError):
            return 1

    @field_validator("secret_refs", mode="before")
    @classmethod
    def _normalize_secret_refs(cls, value: Any) -> dict[str, Any]:
        return normalize_secret_refs_payload(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        tags: list[str] = []
        for item in value:
            tag = str(item or "").strip()
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    @field_validator("runtime_mode", mode="before")
    @classmethod
    def _normalize_runtime_mode(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized and normalized not in {"local", "remote_https"}:
            raise ValueError("runtime_mode must be local or remote_https")
        return normalized

    @model_validator(mode="after")
    def _validate_connection_contract(self) -> ProjectConnection:
        oauth_identity_values = (
            self.oauth_configuration_id,
            self.oauth_provider_id,
            self.oauth_extension_release_id,
            self.oauth_authorization_revision,
            self.oauth_provider_security_fingerprint,
            self.oauth_provider_revocation_fingerprint,
        )
        has_oauth_metadata = bool(
            any(value is not None for value in oauth_identity_values)
            or self.oauth_requested_scopes
            or self.oauth_granted_scopes
        )
        if has_oauth_metadata:
            required_oauth_values = {
                "oauth_configuration_id": self.oauth_configuration_id,
                "extension_id": self.extension_id,
                "extension_install_id": self.extension_install_id,
                "oauth_provider_id": self.oauth_provider_id,
                "oauth_extension_release_id": self.oauth_extension_release_id,
                "oauth_authorization_revision": self.oauth_authorization_revision,
                "oauth_provider_security_fingerprint": self.oauth_provider_security_fingerprint,
                "oauth_provider_revocation_fingerprint": (
                    self.oauth_provider_revocation_fingerprint
                ),
            }
            missing = [name for name, value in required_oauth_values.items() if not value]
            if missing:
                raise ValueError(
                    "OAuth connections require exact configuration metadata: " + ", ".join(missing)
                )
        if (
            self.connection_kind in {"extension", "mcp_connector"}
            and not str(self.extension_id or "").strip()
        ):
            raise ValueError("extension_id is required for extension and mcp_connector connections")
        if self.connection_kind == "extension_runtime":
            if not str(self.extension_install_id or "").strip():
                raise ValueError(
                    "extension_install_id is required for extension_runtime connections"
                )
            if not str(self.runtime_mode or "").strip():
                raise ValueError("runtime_mode is required for extension_runtime connections")
            if (
                str(self.runtime_mode) == "remote_https"
                and not str(self.remote_base_url or "").strip()
            ):
                raise ValueError(
                    "remote_base_url is required for remote_https extension_runtime connections"
                )
        return self
