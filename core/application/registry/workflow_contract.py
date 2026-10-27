"""Typed public workflow interface models.

The v1 storage location remains ``registry_workflows.settings.callable``.
These models normalize that payload into one public boundary contract shared by
UI, API, MCP, webhook, schedule, and parent-workflow invocation paths.
"""

from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WorkflowInputMode = Literal["fields", "json_schema", "passthrough"]
#: Derived from the contract: what a run of this workflow returns.
WorkflowOutputMode = Literal["none", "variables", "artifacts", "variables_and_artifacts"]
WorkflowTriggerChannel = Literal[
    "ui",
    "api",
    "mcp",
    "webhook",
    "schedule",
    "parent_workflow",
]
WorkflowFieldType = Literal[
    "string",
    "paragraph",
    "number",
    "boolean",
    "select",
    "object",
    "array",
    "file",
    "secret",
]

DEFAULT_TRIGGER_CHANNELS: tuple[WorkflowTriggerChannel, ...] = (
    "ui",
    "api",
    "mcp",
    "webhook",
    "schedule",
    "parent_workflow",
)
FIELD_TYPE_ALIASES: dict[str, str] = {
    "": "string",
    "str": "string",
    "text": "paragraph",
    "json": "object",
    "dict": "object",
    "map": "object",
    "list": "array",
    "tuple": "array",
    "set": "array",
    "integer": "number",
    "int": "number",
    "float": "number",
    "decimal": "number",
    "bool": "boolean",
}
CANONICAL_FIELD_TYPES: frozenset[str] = frozenset(
    {"string", "paragraph", "number", "boolean", "select", "object", "array", "file", "secret"}
)
#: Types whose value is a credential. A configured default for one of these is
#: stored as an encrypted reference, never as a value on the contract, and stays
#: opaque through the run until an authorized tool or connector boundary.
SECRET_FIELD_TYPES: frozenset[str] = frozenset({"secret"})
RESERVED_CONTRACT_NAMES: frozenset[str] = frozenset(
    {
        "sys",
        "inputs",
        "outputs",
        "workflow_data",
        "materialized_inputs",
        "task_id",
        "task_summary",
        "task_source",
        "mode",
        "files",
    }
)
# Common transport payload keys that may exist at runtime boundaries even when
# they are not authored contract fields. `task_type` remains transport-only.
RUNTIME_PAYLOAD_KEYS: frozenset[str] = RESERVED_CONTRACT_NAMES | frozenset(
    {"title", "description", "priority", "project_id", "task_type"}
)


def json_object(value: Any) -> dict[str, Any]:
    """Return a dict for dict or JSON-object strings; otherwise return empty."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def normalize_field_type(value: Any) -> str:
    """Normalize older and shorthand contract type names."""
    normalized = str(value or "string").strip().lower()
    return FIELD_TYPE_ALIASES.get(normalized, normalized)


def normalize_contract_name(value: Any) -> str:
    return str(value or "").strip()


def coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        rows = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        rows = [str(item or "").strip() for item in value]
    else:
        rows = []
    return [item for item in rows if item]


_SAMPLE_FILE_TEXT_KEYS = ("artifact_id", "filename", "content_type", "sha256")


def normalize_sample_file(value: Any) -> dict[str, Any] | None:
    """The stored description of a test-run file, or ``None`` without an artifact."""
    if not isinstance(value, dict):
        return None
    sample: dict[str, Any] = {
        key: str(value.get(key) or "").strip()[:255] for key in _SAMPLE_FILE_TEXT_KEYS
    }
    if not sample["artifact_id"]:
        return None
    try:
        size = int(value.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    sample["size_bytes"] = max(size, 0)
    return {key: item for key, item in sample.items() if item not in ("", None)}


def _configured_default_is_valid(field: WorkflowContractField) -> bool:
    value = field.default
    if field.type in {"string", "paragraph"}:
        return not isinstance(value, (dict, list, tuple, set))
    if field.type == "select":
        return not isinstance(value, (dict, list, tuple, set)) and str(value) in field.options
    if field.type == "number":
        if isinstance(value, bool):
            return False
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(parsed)
    if field.type == "boolean":
        if isinstance(value, (bool, int, float)):
            return True
        return str(value or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "0",
            "false",
            "no",
            "off",
        }
    if field.type == "object":
        if isinstance(value, dict):
            return True
        if isinstance(value, str):
            try:
                return isinstance(json.loads(value), dict)
            except Exception:
                return False
        return False
    if field.type == "array":
        if isinstance(value, (list, tuple, set)):
            return True
        if isinstance(value, str):
            try:
                return isinstance(json.loads(value), list)
            except Exception:
                return False
        return False
    if field.type == "file":
        return value not in (None, "", [], {})
    return False


class WorkflowContractIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    path: str = ""
    severity: Literal["error", "warning"] = "error"

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class WorkflowContractField(BaseModel):
    """A public workflow input or variable output field."""

    model_config = ConfigDict(extra="allow")

    name: str
    label: str = ""
    type: str = "string"
    required: bool = False
    description: str = ""
    options: list[str] = Field(default_factory=list)
    contract_id: str = ""
    #: A value fixed for this workflow revision. `None` means no default is
    #: configured; `False`, `0` and `""` are configured defaults, because they
    #: are values an operator can legitimately mean.
    default: Any = None
    #: For `type: secret`: the key of the encrypted reference holding the
    #: configured value. The value itself is never carried on the contract.
    default_secret_ref: str = ""
    #: For `type: file`: a file attached for test runs. A test run that does
    #: not pass this input gets a copy of it; real runs always pass their own.
    sample_file: dict[str, Any] | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> str:
        return normalize_contract_name(value)

    @field_validator("default_secret_ref", mode="before")
    @classmethod
    def _normalize_secret_ref(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("sample_file", mode="before")
    @classmethod
    def _normalize_sample_file(cls, value: Any) -> dict[str, Any] | None:
        return normalize_sample_file(value)

    @property
    def is_secret(self) -> bool:
        return self.type in SECRET_FIELD_TYPES

    @property
    def has_default(self) -> bool:
        """Whether an operator has configured a value for this field."""
        if self.is_secret:
            return bool(self.default_secret_ref)
        return self.default is not None

    @field_validator("label", "description", "contract_id", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> str:
        return normalize_field_type(value)

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_options(cls, value: Any) -> list[str]:
        return _string_list(value)

    def public_label(self) -> str:
        return self.label or self.name.replace("_", " ").replace("-", " ").title()

    def dump(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        if not payload.get("label"):
            payload.pop("label", None)
        if not payload.get("description"):
            payload.pop("description", None)
        if not payload.get("options"):
            payload.pop("options", None)
        if not payload.get("contract_id"):
            payload.pop("contract_id", None)
        # An unconfigured default is absent, not empty: emitting `""` would put
        # a field on every contract that never had one, and read as configured.
        # `exclude_none` already drops `default` when nothing is configured.
        if not payload.get("default_secret_ref"):
            payload.pop("default_secret_ref", None)
        if self.type != "file" or not payload.get("sample_file"):
            payload.pop("sample_file", None)
        # A secret's value is never carried here, and a rejected plaintext must
        # not survive into the stored definition either.
        if self.is_secret:
            payload.pop("default", None)
        return payload

    def public_dump(self) -> dict[str, Any]:
        """The editor's view: whether a default exists, and never a secret's value.

        A browser needs to render "configured" or "not configured" and offer
        replace/clear. It does not need the value, and it does not need the
        storage key either — knowing where a credential is kept is not part of
        editing a workflow.
        """
        payload = self.dump()
        payload["default_configured"] = self.has_default
        if self.is_secret:
            payload.pop("default", None)
            payload.pop("default_secret_ref", None)
        return payload


class WorkflowReturnedFileSource(BaseModel):
    """The step result a returned file is read from."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    step_name: str = ""
    result: str
    target: str


class WorkflowReturnedFile(BaseModel):
    """A file output, derived from a step result ticked "Return from workflow".

    It has no editable fields of its own: the key is saved with the step's
    result setting, and the file comes from that result on every run.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["file"] = "file"
    required: bool = False
    source: WorkflowReturnedFileSource
    origin: Literal["step", "legacy"] = "step"

    def dump(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        if self.origin == "step":
            payload.pop("origin", None)
        return payload


class ArtifactOutputHandle(BaseModel):
    """Flow Steward artifact/job-attachment handle exposed as workflow output evidence."""

    model_config = ConfigDict(extra="allow")

    artifact_id: str = ""
    filename: str = ""
    content_type: str = ""
    size_bytes: int | None = None
    sha256: str = ""
    download_url: str = ""
    retention_class: str = ""
    storage_ref: str = ""

    @field_validator(
        "artifact_id",
        "filename",
        "content_type",
        "sha256",
        "download_url",
        "retention_class",
        "storage_ref",
        mode="before",
    )
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    def dump(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        return {key: value for key, value in payload.items() if value not in ("", None)}


class WorkflowInterface(BaseModel):
    """Normalized public workflow boundary stored under ``settings.callable``."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    trigger_channels: list[WorkflowTriggerChannel] = Field(default_factory=list)
    input_mode: WorkflowInputMode = "fields"
    strict_inputs: bool = True
    expected_inputs: list[WorkflowContractField] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_mode: WorkflowOutputMode = "none"
    expected_outputs: list[WorkflowContractField] = Field(default_factory=list)
    #: File outputs, derived from the steps' "Return from workflow" settings.
    returned_files: list[WorkflowReturnedFile] = Field(default_factory=list)
    contract_id: str = ""
    settings_issues: list[WorkflowContractIssue] = Field(default_factory=list)

    @field_validator("enabled", "strict_inputs", mode="before")
    @classmethod
    def _normalize_bool(cls, value: Any) -> bool:
        return coerce_bool(value, default=bool(value))

    @field_validator("trigger_channels", mode="before")
    @classmethod
    def _normalize_channels(cls, value: Any) -> list[str]:
        rows = _string_list(value)
        return [item for item in rows if item in DEFAULT_TRIGGER_CHANNELS]

    @field_validator("input_mode", mode="before")
    @classmethod
    def _normalize_input_mode(cls, value: Any) -> str:
        normalized = str(value or "fields").strip().lower()
        return normalized if normalized in {"fields", "json_schema", "passthrough"} else "fields"

    @field_validator("output_mode", mode="before")
    @classmethod
    def _normalize_output_mode(cls, value: Any) -> str:
        normalized = str(value or "none").strip().lower()
        allowed = {"none", "variables", "artifacts", "variables_and_artifacts"}
        return normalized if normalized in allowed else "none"

    @field_validator("contract_id", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _derive_output_mode(self) -> WorkflowInterface:
        # What a run returns is what the contract lists; there is no separate
        # switch that could leave a ticked file out of the result.
        if self.expected_outputs and self.returned_files:
            self.output_mode = "variables_and_artifacts"
        elif self.expected_outputs:
            self.output_mode = "variables"
        elif self.returned_files:
            self.output_mode = "artifacts"
        else:
            self.output_mode = "none"
        return self

    @classmethod
    def from_settings(cls, settings: Any, *, steps: Any = None) -> WorkflowInterface:
        """The interface of a workflow with these *settings* and *steps*.

        Returned files exist only in the steps; without *steps* the interface
        has none. Pass the definition's steps wherever results are returned,
        mapped or described (see ``interface_from_definition``).
        """
        payload = json_object(settings)
        callable_raw = payload.get("callable")
        if callable_raw is None:
            return cls(enabled=False)

        settings_issues: list[WorkflowContractIssue] = []
        if not isinstance(callable_raw, dict):
            return cls(
                enabled=coerce_bool(callable_raw, default=True),
                settings_issues=[
                    WorkflowContractIssue(
                        code="workflow.interface.invalid_callable_shape",
                        message="settings.callable must be an object contract",
                        path="workflow.callable",
                    )
                ],
            )

        callable_obj = json_object(callable_raw)
        for retired_key in ("callable_expected_inputs", "callable_expected_outputs"):
            if retired_key in payload:
                settings_issues.append(
                    WorkflowContractIssue(
                        code="workflow.interface.retired_key_unsupported",
                        message=f"Unsupported settings.{retired_key}",
                        path=f"workflow.{retired_key}",
                    )
                )

        expected_outputs = [
            WorkflowContractField.model_validate(item)
            for item in _list_of_dicts(callable_obj.get("expected_outputs"))
        ]
        returned_files: list[WorkflowReturnedFile] = []
        if steps is not None:
            from core.application.registry.workflow_returned_files import derive_returned_files

            derived, derive_issues = derive_returned_files(
                steps,
                # Declared by hand before files were returned from steps.
                legacy_declarations=_list_of_dicts(
                    callable_obj.get("expected_artifacts") or callable_obj.get("artifacts")
                ),
                taken_names=[row.name for row in expected_outputs],
            )
            returned_files = [
                WorkflowReturnedFile.model_validate(row.contract_row()) for row in derived
            ]
            settings_issues.extend(
                WorkflowContractIssue.model_validate(issue) for issue in derive_issues
            )

        return cls(
            enabled=callable_obj.get("enabled", True),
            trigger_channels=callable_obj.get("trigger_channels") or [],
            input_mode=callable_obj.get("input_mode") or "fields",
            strict_inputs=callable_obj.get("strict_inputs", True),
            expected_inputs=[
                WorkflowContractField.model_validate(item)
                for item in _list_of_dicts(callable_obj.get("expected_inputs"))
            ],
            input_schema=json_object(
                callable_obj.get("input_schema")
                or callable_obj.get("json_schema")
                or callable_obj.get("schema")
            ),
            expected_outputs=expected_outputs,
            returned_files=returned_files,
            contract_id=callable_obj.get("contract_id") or "",
            settings_issues=settings_issues,
        )

    def dump(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "trigger_channels": list(self.trigger_channels),
            "input_mode": self.input_mode,
            "strict_inputs": bool(self.strict_inputs),
            "expected_inputs": [row.dump() for row in self.expected_inputs],
            **({"input_schema": dict(self.input_schema)} if self.input_schema else {}),
            "output_mode": self.output_mode,
            "expected_outputs": [row.dump() for row in self.expected_outputs],
            "returned_files": [row.dump() for row in self.returned_files],
            **({"contract_id": self.contract_id} if self.contract_id else {}),
        }

    def variable_outputs_required(self) -> bool:
        return bool(self.expected_outputs)

    def artifacts_required(self) -> bool:
        return bool(self.returned_files)

    def definition_issues(self, *, path_prefix: str = "workflow.callable") -> list[dict[str, Any]]:
        issues: list[WorkflowContractIssue] = list(self.settings_issues)
        if not self.enabled:
            return []

        if not self.trigger_channels:
            issues.append(
                WorkflowContractIssue(
                    code="workflow.interface.trigger_channels_missing",
                    message="Callable workflow must declare trigger_channels",
                    path=f"{path_prefix}.trigger_channels",
                )
            )

        def _validate_rows(
            rows: list[Any],
            *,
            collection: str,
        ) -> None:
            seen: set[str] = set()
            for idx, row in enumerate(rows):
                name = row.name
                row_path = f"{path_prefix}.{collection}[{idx}]"
                if not name:
                    issues.append(
                        WorkflowContractIssue(
                            code="workflow.interface.name_required",
                            message="Workflow interface field name is required",
                            path=f"{row_path}.name",
                        )
                    )
                    continue
                if any(ch.isspace() for ch in name):
                    issues.append(
                        WorkflowContractIssue(
                            code="workflow.interface.name_whitespace",
                            message=f"Workflow interface field '{name}' cannot contain whitespace",
                            path=f"{row_path}.name",
                        )
                    )
                    continue
                if name in RESERVED_CONTRACT_NAMES:
                    issues.append(
                        WorkflowContractIssue(
                            code="workflow.interface.name_reserved",
                            message=(
                                f"Workflow interface field '{name}' is reserved; use sys.{name} "
                                "for runtime metadata instead"
                            ),
                            path=f"{row_path}.name",
                        )
                    )
                    continue
                if name in seen:
                    issues.append(
                        WorkflowContractIssue(
                            code="workflow.interface.duplicate_name",
                            message=f"Duplicate workflow interface field '{name}'",
                            path=f"{row_path}.name",
                        )
                    )
                    continue
                seen.add(name)
                if isinstance(row, WorkflowContractField):
                    if row.type not in CANONICAL_FIELD_TYPES:
                        issues.append(
                            WorkflowContractIssue(
                                code="workflow.interface.invalid_type",
                                message=(
                                    f"Workflow interface field '{name}' has unsupported type "
                                    f"'{row.type}'"
                                ),
                                path=f"{row_path}.type",
                            )
                        )
                    if row.type == "select" and not row.options:
                        issues.append(
                            WorkflowContractIssue(
                                code="workflow.interface.select_options_missing",
                                message=f"Select field '{name}' must declare options",
                                path=f"{row_path}.options",
                            )
                        )
                    if row.is_secret and row.default is not None:
                        # Without this, the ordinary contract update is a way
                        # around the write-only path: the value would be stored
                        # in workflow settings, exported in YAML and echoed back
                        # to the browser like any other default.
                        issues.append(
                            WorkflowContractIssue(
                                code="workflow.interface.secret_default_plaintext",
                                message=(
                                    f"Secret field '{name}' cannot carry a default value; "
                                    "set it through the workflow's secret input settings"
                                ),
                                path=f"{row_path}.default",
                            )
                        )
                    if (
                        collection == "expected_inputs"
                        and row.has_default
                        and not row.is_secret
                        and not _configured_default_is_valid(row)
                    ):
                        issues.append(
                            WorkflowContractIssue(
                                code="workflow.interface.default_invalid",
                                message=(
                                    f"Configured default for workflow input '{name}' "
                                    f"does not match type '{row.type}'"
                                ),
                                path=f"{row_path}.default",
                            )
                        )
                    if collection == "expected_outputs":
                        if row.is_secret:
                            issues.append(
                                WorkflowContractIssue(
                                    code="workflow.interface.output_secret_forbidden",
                                    message=f"Workflow output '{name}' cannot have type 'secret'",
                                    path=f"{row_path}.type",
                                )
                            )
                        if row.default is not None or bool(row.default_secret_ref):
                            issues.append(
                                WorkflowContractIssue(
                                    code="workflow.interface.output_default_forbidden",
                                    message=f"Workflow output '{name}' cannot have a default",
                                    path=f"{row_path}.default",
                                )
                            )

        _validate_rows(self.expected_inputs, collection="expected_inputs")
        _validate_rows(self.expected_outputs, collection="expected_outputs")

        if self.input_mode == "json_schema":
            if not self.input_schema:
                issues.append(
                    WorkflowContractIssue(
                        code="workflow.interface.input_schema_missing",
                        message="input_mode json_schema requires input_schema",
                        path=f"{path_prefix}.input_schema",
                    )
                )
            elif self.input_schema.get("type") not in (None, "object"):
                issues.append(
                    WorkflowContractIssue(
                        code="workflow.interface.input_schema_not_object",
                        message="Workflow input_schema must describe a JSON object",
                        path=f"{path_prefix}.input_schema.type",
                    )
                )

        return [issue.dump() for issue in issues]


def interface_from_settings(settings: Any, *, steps: Any = None) -> WorkflowInterface:
    return WorkflowInterface.from_settings(settings, steps=steps)


def interface_from_definition(definition: Any) -> WorkflowInterface:
    """The interface of a workflow row, manifest or pinned definition.

    Reads ``settings`` and ``steps`` from a mapping or an object, so returned
    files are always derived from the same definition as the rest.
    """
    if isinstance(definition, dict):
        settings, steps = definition.get("settings"), definition.get("steps")
    else:
        settings = getattr(definition, "settings", None)
        steps = getattr(definition, "steps", None)
    if isinstance(steps, dict):
        steps = list(steps.values())
    return WorkflowInterface.from_settings(settings, steps=steps if steps is not None else [])
