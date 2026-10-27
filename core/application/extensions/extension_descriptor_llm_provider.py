"""The ``llm_provider`` block of the extension descriptor.

``runtime.extension_contract_v2.llm_provider`` declares an LLM provider
extension: its supplier, the secrets and settings it asks for, the defaults of
those settings, and the operations the host calls. Field-name helpers keep
provider secrets out of the plaintext connection config. The descriptor module
re-exports everything here, so callers keep importing from it.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.application.extensions.semver import require_nonempty_text

LLM_PROVIDER_CONTRACT_SCHEMA_VERSION = "llm_provider_extension_v1"
LLM_PROVIDER_CONNECTION_CONFIG_FIELDS = frozenset({"upstream_base_url"})
# An llm_provider runs where its install runs: as an ordinary extension
# subprocess, or behind a separately hosted remote HTTPS runtime.
LLM_PROVIDER_RUNTIME_MODES = frozenset({"subprocess", "remote_https"})
# Help shown under one settings field: a sentence or two, not documentation.
LLM_PROVIDER_FIELD_HELP_MAX_CHARS = 300


def canonical_llm_field_name(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or "").strip())
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


_LLM_SECRET_FIELD_NAMES = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "runtime_bearer_token",
        "secret",
        "secret_key",
        "set_cookie",
        "token",
    }
)
_LLM_SECRET_FIELD_SUFFIXES = (
    "_access_key",
    "_api_key",
    "_authorization",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_secret_key",
    "_token",
)


def is_llm_secret_field_name(value: Any) -> bool:
    """Classify credential-like config keys independently of a manifest."""
    name = canonical_llm_field_name(value)
    return bool(
        name
        and (
            name in _LLM_SECRET_FIELD_NAMES
            or any(name.endswith(suffix) for suffix in _LLM_SECRET_FIELD_SUFFIXES)
        )
    )


def llm_plaintext_secret_paths(value: Any, *, path: str = "connection_config") -> list[str]:
    """Return recursively discovered secret-like keys in plaintext config."""
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key or "").strip()
            item_path = f"{path}.{key}" if key else path
            if is_llm_secret_field_name(key):
                found.append(item_path)
            found.extend(llm_plaintext_secret_paths(item, path=item_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and is_llm_secret_field_name(item[0])
            ):
                found.append(f"{path}[{index}].{item[0]}")
            found.extend(llm_plaintext_secret_paths(item, path=f"{path}[{index}]"))
    return found


class LlmProviderOperationRef(BaseModel):
    """One LLM operation binding (a remote query_id or action_id)."""

    model_config = ConfigDict(extra="forbid")

    query_id: str = ""
    action_id: str = ""
    optional: bool = False


class LlmProviderOperations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    list_models: LlmProviderOperationRef
    chat: LlmProviderOperationRef
    structured_chat: LlmProviderOperationRef | None = None


class LlmProviderModelGrouping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = "upstream_owner"
    owner_key_field: str = "upstream_owner_key"
    owner_label_field: str = "upstream_owner_label"


class LlmProviderContractV1(BaseModel):
    """``runtime.extension_contract_v2.llm_provider`` — LLM provider extension contract.

    Optional; present only on LLM provider extensions. Parsed/validated only when
    the ``llm_provider`` block is present so non-LLM descriptors are unaffected.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = LLM_PROVIDER_CONTRACT_SCHEMA_VERSION
    supplier_key: str
    supplier_label: str
    runtime_mode: str = "remote_https"
    credential_provider_key: str = ""
    required_provider_secrets: list[str] = Field(default_factory=list)
    optional_provider_secrets: list[str] = Field(default_factory=list)
    required_connection_config: list[str] = Field(default_factory=list)
    optional_connection_config: list[str] = Field(default_factory=list)
    #: The value the extension uses when an optional setting is left empty, such
    #: as the provider's public Base URL. The host compares effective settings,
    #: so clearing a field that held its default is not a change.
    connection_config_defaults: dict[str, str] = Field(default_factory=dict)
    #: Plain-language help shown under a declared setting or provider secret in
    #: the provider settings form: what to enter and in which format. Plain
    #: text, at most LLM_PROVIDER_FIELD_HELP_MAX_CHARS characters.
    connection_config_help: dict[str, str] = Field(default_factory=dict)
    provider_secret_help: dict[str, str] = Field(default_factory=dict)
    operations: LlmProviderOperations
    model_grouping: LlmProviderModelGrouping = Field(default_factory=LlmProviderModelGrouping)
    required_capabilities_for_enablement: list[str] = Field(
        default_factory=lambda: ["chat", "tools", "structured_output"]
    )

    @field_validator("supplier_key", "supplier_label", mode="before")
    @classmethod
    def _validate_supplier_fields(cls, value: Any) -> str:
        return require_nonempty_text(value, "runtime.extension_contract_v2.llm_provider supplier")

    @model_validator(mode="after")
    def _reject_plaintext_secret_overlap(self) -> LlmProviderContractV1:
        secret_fields = {
            canonical_llm_field_name(field)
            for field in [*self.required_provider_secrets, *self.optional_provider_secrets]
            if str(field).strip()
        }
        declared_config = [*self.required_connection_config, *self.optional_connection_config]
        plaintext_fields = {
            canonical_llm_field_name(field) for field in declared_config if str(field).strip()
        }
        overlap = sorted(secret_fields & plaintext_fields)
        host_secret_fields = sorted(
            field for field in declared_config if is_llm_secret_field_name(field)
        )
        rejected = sorted(set(overlap + host_secret_fields))
        if rejected:
            raise ValueError(
                "llm_provider connection config must not declare provider secret fields: "
                + ", ".join(rejected)
            )
        unsupported_plaintext_fields = sorted(
            plaintext_fields - LLM_PROVIDER_CONNECTION_CONFIG_FIELDS
        )
        if unsupported_plaintext_fields:
            raise ValueError(
                "llm_provider v1 connection config is host-owned and supports only "
                "upstream_base_url; unsupported fields: " + ", ".join(unsupported_plaintext_fields)
            )
        noncanonical_fields = sorted(
            str(field).strip()
            for field in declared_config
            if str(field).strip() != canonical_llm_field_name(field)
        )
        if noncanonical_fields:
            raise ValueError(
                "llm_provider connection config fields must use canonical snake_case names: "
                + ", ".join(noncanonical_fields)
            )
        undeclared_defaults = sorted(
            str(field) for field in self.connection_config_defaults if field not in declared_config
        )
        if undeclared_defaults:
            raise ValueError(
                "llm_provider connection_config_defaults name undeclared settings: "
                + ", ".join(undeclared_defaults)
            )
        empty_defaults = sorted(
            str(field)
            for field, value in self.connection_config_defaults.items()
            if not str(value or "").strip()
        )
        if empty_defaults:
            raise ValueError(
                "llm_provider connection_config_defaults must not be empty: "
                + ", ".join(empty_defaults)
            )
        return self

    @model_validator(mode="after")
    def _validate_field_help(self) -> LlmProviderContractV1:
        declared_config = {
            *self.required_connection_config,
            *self.optional_connection_config,
        }
        declared_secrets = {*self.required_provider_secrets, *self.optional_provider_secrets}
        for block, declared, what in (
            ("connection_config_help", declared_config, "settings"),
            ("provider_secret_help", declared_secrets, "provider secrets"),
        ):
            _check_field_help(getattr(self, block), declared, block=block, what=what)
        return self


def _check_field_help(help_by_field: dict[str, str], declared: set[str], *, block: str, what: str):
    undeclared = sorted(str(field) for field in help_by_field if field not in declared)
    if undeclared:
        raise ValueError(f"llm_provider {block} names undeclared {what}: " + ", ".join(undeclared))
    empty = sorted(str(field) for field, text in help_by_field.items() if not str(text).strip())
    if empty:
        raise ValueError(f"llm_provider {block} must not be empty: " + ", ".join(empty))
    too_long = sorted(
        str(field)
        for field, text in help_by_field.items()
        if len(str(text).strip()) > LLM_PROVIDER_FIELD_HELP_MAX_CHARS
    )
    if too_long:
        raise ValueError(
            f"llm_provider {block} must be at most {LLM_PROVIDER_FIELD_HELP_MAX_CHARS} "
            "characters: " + ", ".join(too_long)
        )
