"""
Typed tool artifact model for registry-backed tools.

The artifact is intentionally about tool identity + contract + implementation
kind. Runtime-specific dispatch continues to live outside this module.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from core.application.registry.builder_metadata import normalize_builder_metadata
from core.application.registry.catalog_categories import DEFAULT_CATALOG_CATEGORY

_BUILTIN_MCP_CONNECTOR_BY_SERVER: dict[str, str] = {
    "fs": "builtin_fs_mcp",
}


def _json_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        return value


def _normalize_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return _json_clone(value)
    return {}


def _normalize_mcp_connector_alias_shape(implementation: dict[str, Any]) -> dict[str, Any]:
    payload = dict(implementation or {})
    kind = str(payload.get("kind") or payload.get("type") or "").strip().lower()
    connector_id = str(payload.get("connector_id") or "").strip()
    if kind != "mcp" or connector_id:
        return payload
    server = str(payload.get("server") or "").strip().lower()
    fallback_connector_id = _BUILTIN_MCP_CONNECTOR_BY_SERVER.get(server)
    if fallback_connector_id:
        payload["connector_id"] = fallback_connector_id
    return payload


class ToolParameter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    type: str = "string"
    required: bool = True
    description: str = ""
    values: list[str] | None = None
    default: Any = None

    @field_validator("name", "type", "description", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    def schema_fragment(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type or "string"}
        if self.description:
            schema["description"] = self.description
        if self.values:
            schema["enum"] = list(self.values)
        if self.default is not None:
            schema["default"] = _json_clone(self.default)
        return schema


class ToolFunction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    # What the step editor shows for this operation; without them it could only show
    # the technical name, which it never does (it said "Unavailable action" instead).
    display_name: str = ""
    description: str = ""
    parameters: list[ToolParameter] = Field(default_factory=list)
    returns: dict[str, Any] = Field(default_factory=dict)
    artifact_policy_operation_id: str = ""

    @field_validator(
        "name", "display_name", "description", "artifact_policy_operation_id", mode="before"
    )
    @classmethod
    def _normalize_name(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("returns", mode="before")
    @classmethod
    def _normalize_returns(cls, value: Any) -> dict[str, Any]:
        return _normalize_schema(value)

    @property
    def input_schema(self) -> dict[str, Any]:
        properties = {
            param.name: param.schema_fragment() for param in self.parameters if param.name
        }
        required = [param.name for param in self.parameters if param.name and param.required]
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        returns = _normalize_schema(self.returns)
        if not returns:
            return {"type": "object", "properties": {}, "required": []}
        if any(key in returns for key in ("properties", "items", "additionalProperties")):
            return returns
        return_name = str(returns.get("name") or "").strip()
        if return_name:
            return_type = str(returns.get("type") or "string").strip() or "string"
            required = [return_name] if bool(returns.get("required", True)) else []
            return {
                "type": "object",
                "properties": {
                    return_name: {
                        "type": return_type,
                        "description": str(returns.get("description") or "").strip(),
                    }
                },
                "required": required,
            }
        return returns

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name}
        if self.display_name:
            payload["display_name"] = self.display_name
        if self.description:
            payload["description"] = self.description
        payload.update(
            {
                "parameters": [param.model_dump(mode="python") for param in self.parameters],
                "returns": _json_clone(self.returns),
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
            }
        )
        if self.artifact_policy_operation_id:
            payload["artifact_policy_operation_id"] = self.artifact_policy_operation_id
        return payload


class ToolInterface(BaseModel):
    model_config = ConfigDict(extra="ignore")

    function_name: str | None = None
    functions: list[ToolFunction] = Field(default_factory=list)
    parameters: list[ToolParameter] = Field(default_factory=list)
    returns: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("function_name", mode="before")
    @classmethod
    def _normalize_function_name(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("returns", "input_schema", "output_schema", mode="before")
    @classmethod
    def _normalize_schema_payloads(cls, value: Any) -> dict[str, Any]:
        return _normalize_schema(value)

    @property
    def all_function_names(self) -> list[str]:
        if self.function_name:
            return [self.function_name]
        return [function.name for function in self.functions if function.name]

    @property
    def operations(self) -> list[dict[str, Any]]:
        if self.functions:
            return [function.to_dict() for function in self.functions]
        function = ToolFunction(
            name=self.function_name or "invoke",
            parameters=list(self.parameters),
            returns=_json_clone(self.returns),
        )
        return [function.to_dict()]

    def resolved_input_schema(self) -> dict[str, Any]:
        if self.input_schema:
            return _json_clone(self.input_schema)
        if self.functions:
            properties = {
                operation["name"]: operation["input_schema"] for operation in self.operations
            }
            required = [operation["name"] for operation in self.operations if operation["name"]]
            return {"type": "object", "properties": properties, "required": required}
        return ToolFunction(
            name=self.function_name or "invoke",
            parameters=list(self.parameters),
            returns=_json_clone(self.returns),
        ).input_schema

    def resolved_output_schema(self) -> dict[str, Any]:
        if self.output_schema:
            return _json_clone(self.output_schema)
        if self.functions:
            properties = {
                operation["name"]: operation["output_schema"] for operation in self.operations
            }
            required = [operation["name"] for operation in self.operations if operation["name"]]
            return {"type": "object", "properties": properties, "required": required}
        return ToolFunction(
            name=self.function_name or "invoke",
            parameters=list(self.parameters),
            returns=_json_clone(self.returns),
        ).output_schema

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ToolInterface:
        return cls.model_validate(data or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "functions": [function.to_dict() for function in self.functions],
            "parameters": [param.model_dump(mode="python") for param in self.parameters],
            "returns": _json_clone(self.returns),
            "input_schema": self.resolved_input_schema(),
            "output_schema": self.resolved_output_schema(),
            "operations": self.operations,
        }
        if self.function_name:
            payload["function_name"] = self.function_name
        return payload


class ToolImplementation(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: str = Field(default="internal", validation_alias=AliasChoices("kind", "type"))
    module: str | None = None
    class_name: str | None = Field(
        default=None, validation_alias=AliasChoices("class_name", "class")
    )
    server: str | None = None
    base_url: str | None = None
    auth: dict[str, Any] | None = None
    command: str | None = None
    working_dir: str | None = None
    entrypoint: str | None = None
    connector_id: str | None = None  # FK to mcp_connectors when kind=="mcp"

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return normalized or "internal"

    @field_validator(
        "module",
        "class_name",
        "server",
        "base_url",
        "command",
        "working_dir",
        "entrypoint",
        "connector_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("auth", mode="before")
    @classmethod
    def _normalize_auth(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return _json_clone(value)
        return None

    @property
    def type(self) -> str:
        return self.kind

    @model_validator(mode="after")
    def _validate_mcp_connector_identity(self) -> ToolImplementation:
        if self.kind == "mcp" and not self.connector_id:
            raise ValueError("connector_id is required for mcp tool implementations")
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ToolImplementation:
        return cls.model_validate(data or {})

    def to_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python", by_alias=False, exclude_none=True)
        payload["kind"] = self.kind
        payload["type"] = self.kind
        return payload


class ToolManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    artifact_schema_version: str = "tool_artifact_v1"
    tool_id: str
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = True
    category: str = DEFAULT_CATALOG_CATEGORY
    prompt: str = ""
    interface: ToolInterface = Field(default_factory=ToolInterface)
    implementation: ToolImplementation = Field(default_factory=ToolImplementation)
    config: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    builder_metadata_jsonb: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "tool_id", "version", "display_name", "description", "category", "prompt", mode="before"
    )
    @classmethod
    def _normalize_text_fields(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("config", mode="before")
    @classmethod
    def _normalize_config(cls, value: Any) -> dict[str, Any]:
        return _normalize_schema(value)

    @field_validator("builder_metadata_jsonb", mode="before")
    @classmethod
    def _normalize_builder_metadata(cls, value: Any) -> dict[str, Any]:
        return normalize_builder_metadata(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        normalized: list[str] = []
        for item in value:
            tag = str(item or "").strip()
            if tag and tag not in normalized:
                normalized.append(tag)
        return normalized

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> ToolManifest:
        interface_raw = row.get("interface") or {}
        implementation_raw = row.get("implementation") or {}
        config_raw = row.get("config") or {}

        if isinstance(interface_raw, str):
            interface_raw = json.loads(interface_raw)
        if isinstance(implementation_raw, str):
            implementation_raw = json.loads(implementation_raw)
        if isinstance(implementation_raw, dict):
            implementation_raw = _normalize_mcp_connector_alias_shape(implementation_raw)
        if isinstance(config_raw, str):
            config_raw = json.loads(config_raw)

        tags_raw = row.get("tags")
        if tags_raw is not None:
            tags = tags_raw
        elif isinstance(config_raw, dict):
            tags = config_raw.get("tags")
        else:
            tags = []
        payload = {
            "tool_id": row.get("tool_id"),
            "version": row.get("version", "1.0.0"),
            "display_name": row.get("display_name") or row.get("tool_id"),
            "description": row.get("description", ""),
            "enabled": row.get("enabled", True),
            "category": row.get("category") or DEFAULT_CATALOG_CATEGORY,
            "prompt": row.get("prompt", ""),
            "interface": interface_raw,
            "implementation": implementation_raw,
            "config": config_raw,
            "builder_metadata_jsonb": row.get("builder_metadata_jsonb") or {},
            "tags": tags,
        }
        return cls.model_validate(payload)

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.interface.resolved_input_schema()

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.interface.resolved_output_schema()

    @property
    def identity(self) -> dict[str, str]:
        return {"tool_id": self.tool_id, "version": self.version}

    def to_registry_payload(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "category": self.category,
            "prompt": self.prompt,
            "interface": self.interface.to_dict(),
            "implementation": self.implementation.to_dict(),
            "config": _json_clone(self.config),
            "builder_metadata_jsonb": normalize_builder_metadata(self.builder_metadata_jsonb),
            "tags": list(self.tags),
        }

    def catalog_entry(self, *, source: str | None = None) -> dict[str, Any]:
        entry = {
            "artifact_schema_version": self.artifact_schema_version,
            "tool_id": self.tool_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "category": self.category,
            "tags": list(self.tags),
            "identity": self.identity,
            "implementation_kind": self.implementation.kind,
            "implementation": self.implementation.to_dict(),
            "interface": {
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
                "operations": self.interface.operations,
            },
        }
        if source:
            entry["source"] = source
        if self.prompt:
            entry["prompt"] = self.prompt
        return entry

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_registry_payload()
        payload["artifact_schema_version"] = self.artifact_schema_version
        payload["identity"] = self.identity
        payload["input_schema"] = self.input_schema
        payload["output_schema"] = self.output_schema
        payload["operations"] = self.interface.operations
        return payload
