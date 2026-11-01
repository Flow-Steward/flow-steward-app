"""The ``llm_provider`` block of the extension descriptor.

``runtime.extension_contract_v2.llm_provider`` declares an LLM provider
extension: its supplier, the secrets and settings it asks for, the defaults of
those settings, and the operations the host calls. Field-name helpers keep
provider secrets out of the plaintext connection config. The descriptor module
re-exports everything here, so callers keep importing from it.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

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
        "auth_header",
        "bearer_token",
        "client_secret",
        "client_certificate",
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
        "signing_key",
        "token",
    }
)
_LLM_SECRET_FIELD_SUFFIXES = (
    "_access_key",
    "_api_key",
    "_authorization",
    "_auth_header",
    "_credential",
    "_credentials",
    "_client_certificate",
    "_password",
    "_private_key",
    "_secret",
    "_secret_key",
    "_signing_key",
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


def is_safe_llm_connection_config_name(value: Any) -> bool:
    """Whether a declared plaintext setting name is safe at any boundary."""
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
        and value == canonical_llm_field_name(value)
        and value not in {"prototype", "constructor", "remote_base_url", "headers"}
        and not is_llm_secret_field_name(value)
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


class LlmConnectionStringSchema(BaseModel):
    """The bounded string-only subset supported by the host."""

    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["string"]
    title: str | None = Field(default=None, min_length=1, max_length=128)
    minLength: int = Field(default=0, ge=0, le=4096)
    maxLength: int = Field(default=4096, ge=0, le=4096)
    enum: list[str] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def reject_null_metadata(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("schema metadata must not be null")
        return value

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.title is not None and not self.title.strip():
            raise ValueError("schema title must not be blank")
        if self.minLength > self.maxLength:
            raise ValueError("schema minimum exceeds maximum")
        if self.enum is not None and (
            len(set(self.enum)) != len(self.enum)
            or any(
                not item.strip() or len(item) > 256 or not self.accepts(item) for item in self.enum
            )
        ):
            raise ValueError("schema enum contains invalid values")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}

    def accepts(self, value: str) -> bool:
        return self.minLength <= len(value) <= self.maxLength and (
            self.enum is None or value in self.enum
        )


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
    connection_config_schema: dict[str, LlmConnectionStringSchema] = Field(default_factory=dict)
    provider_secret_help: dict[str, str] = Field(default_factory=dict)
    provider_secret_formats: dict[str, Literal["json_object"]] = Field(default_factory=dict)
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
        if len(declared_config) > 32 or len(set(declared_config)) != len(declared_config):
            raise ValueError("llm_provider connection config has too many or duplicate fields")
        if any(not is_safe_llm_connection_config_name(field) for field in declared_config):
            raise ValueError(
                "llm_provider connection config fields must use safe canonical snake_case names"
            )
        if set(self.connection_config_schema) - set(declared_config):
            raise ValueError("llm_provider connection_config_schema names undeclared settings")
        for field, default in self.connection_config_defaults.items():
            schema = self.connection_config_schema.get(field)
            if len(default) > 4096 or (schema and not schema.accepts(default)):
                raise ValueError("llm_provider connection config default violates schema")
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
        undeclared_formats = sorted(set(self.provider_secret_formats) - declared_secrets)
        if undeclared_formats:
            raise ValueError(
                "llm_provider provider_secret_formats names undeclared provider secrets: "
                + ", ".join(undeclared_formats)
            )
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
