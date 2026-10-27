"""Workflow interface validation and runtime projection service."""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field

from core.application.orchestration.output_contract_receipt import (
    OutputProducerContext,
    verified_output_contract,
)
from core.application.registry.workflow_contract import (
    RUNTIME_PAYLOAD_KEYS,
    ArtifactOutputHandle,
    WorkflowContractField,
    WorkflowInterface,
    WorkflowReturnedFile,
    interface_from_definition,
    interface_from_settings,
    json_object,
)
from core.application.registry.workflow_lookup import get_workflow_row
from core.infrastructure.security.secret_reference import SecretReference

_TOKEN_WORKFLOW_INPUT_INVALID_TYPE = "workflow.input.invalid_type"


class WorkflowInterfaceLookupError(RuntimeError):
    """Raised when a workflow contract cannot be loaded from the registry."""


class WorkflowBoundaryValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    errors: list[dict[str, Any]] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    interface: dict[str, Any] = Field(default_factory=dict)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class WorkflowInterfaceService:
    """Application service for public workflow input/output contracts."""

    def interface_from_settings(self, settings: Any, *, steps: Any = None) -> WorkflowInterface:
        return interface_from_settings(settings, steps=steps)

    def interface_from_definition(self, definition: Any) -> WorkflowInterface:
        """Inputs, outputs and the files the definition's steps return."""
        return interface_from_definition(definition)

    def interface_for_workflow_id(self, workflow_id: str) -> WorkflowInterface:
        normalized = str(workflow_id or "").strip()
        if not normalized:
            raise WorkflowInterfaceLookupError("workflow_id is required")
        from core.application.registry.registry_manager import RegistryManager

        db = getattr(RegistryManager.instance(), "db_store", None)
        if db is None or not hasattr(db, "get_workflow"):
            raise WorkflowInterfaceLookupError("Workflow registry store is unavailable")
        workflow = get_workflow_row(db, normalized)
        if not isinstance(workflow, dict):
            raise WorkflowInterfaceLookupError(f"Workflow '{normalized}' was not found")
        return self.interface_from_definition(workflow)

    def input_json_schema(self, interface: WorkflowInterface) -> dict[str, Any]:
        if interface.input_mode == "passthrough":
            return {"type": "object", "additionalProperties": True}
        if interface.input_mode == "json_schema":
            schema = copy.deepcopy(interface.input_schema) if interface.input_schema else {}
            if not schema:
                schema = {"type": "object", "additionalProperties": False}
            if interface.contract_id:
                schema.setdefault("x-contract-id", interface.contract_id)
            return schema

        properties: dict[str, Any] = {}
        required: list[str] = []
        for row in interface.expected_inputs:
            properties[row.name] = _json_schema_for_field(row)
            if row.required:
                required.append(row.name)
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": not interface.strict_inputs,
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def output_json_schema(self, interface: WorkflowInterface) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for row in interface.expected_outputs:
            properties[row.name] = _json_schema_for_field(row)
            if row.required:
                required.append(row.name)
        for returned_file in interface.returned_files:
            properties[returned_file.name] = _json_schema_for_returned_file(returned_file)
            if returned_file.required:
                required.append(returned_file.name)
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": True,
            "properties": properties,
            "x-output-mode": interface.output_mode,
        }
        if required:
            schema["required"] = required
        return schema

    def public_contract_payload(self, interface: WorkflowInterface) -> dict[str, Any]:
        payload = interface.dump()
        # The editor's view of inputs: an ordinary default is shown so it can be
        # changed, a secret is reduced to configured/not-configured. Reusing the
        # storage dump here is how a credential reaches a browser.
        payload["expected_inputs"] = [row.public_dump() for row in interface.expected_inputs]
        payload["expected_outputs"] = [
            {
                key: value
                for key, value in row.dump().items()
                if key not in {"default", "default_configured", "default_secret_ref"}
            }
            for row in interface.expected_outputs
        ]
        payload["input_schema"] = self.input_json_schema(interface)
        payload["output_schema"] = self.output_json_schema(interface)
        return payload

    def validate_inputs_for_workflow_id(
        self,
        workflow_id: str,
        payload: Any,
        *,
        channel: str = "",
    ) -> WorkflowBoundaryValidationResult:
        interface = self.interface_for_workflow_id(workflow_id)
        return self.validate_inputs(interface, payload, channel=channel)

    def validate_inputs(
        self,
        interface: WorkflowInterface,
        payload: Any,
        *,
        channel: str = "",
    ) -> WorkflowBoundaryValidationResult:
        return self._validate_inputs(
            interface,
            payload,
            channel=channel,
            accept_trusted_secret_references=False,
        )

    def validate_delegated_inputs(
        self,
        interface: WorkflowInterface,
        payload: Any,
        *,
        channel: str = "parent_workflow",
    ) -> WorkflowBoundaryValidationResult:
        """Validate inputs already materialized by a running parent workflow."""
        return self._validate_inputs(
            interface,
            payload,
            channel=channel,
            accept_trusted_secret_references=True,
        )

    def _validate_inputs(
        self,
        interface: WorkflowInterface,
        payload: Any,
        *,
        channel: str,
        accept_trusted_secret_references: bool,
    ) -> WorkflowBoundaryValidationResult:
        raw_inputs = dict(payload or {}) if isinstance(payload, dict) else {}
        if not accept_trusted_secret_references and _contains_secret_reference(raw_inputs):
            return WorkflowBoundaryValidationResult(
                ok=False,
                errors=[
                    _error(
                        _TOKEN_WORKFLOW_INPUT_INVALID_TYPE,
                        "Workflow inputs must not contain an internal secret reference",
                        path="inputs",
                        channel=channel,
                    )
                ],
                inputs={},
                interface=interface.dump(),
            )
        if not interface.enabled:
            return WorkflowBoundaryValidationResult(
                ok=True,
                inputs=raw_inputs,
                interface=interface.dump(),
            )

        errors: list[dict[str, Any]] = []
        errors.extend(_interface_definition_errors(interface, channel=channel))
        normalized_channel = _normalize_trigger_channel(channel)
        if (
            normalized_channel
            and interface.trigger_channels
            and normalized_channel not in set(interface.trigger_channels)
        ):
            errors.append(
                _error(
                    "workflow.input.channel_forbidden",
                    f"Workflow cannot be triggered through channel '{normalized_channel}'",
                    path="trigger_channels",
                    channel=normalized_channel,
                )
            )
        if errors:
            return WorkflowBoundaryValidationResult(
                ok=False,
                errors=errors,
                inputs={},
                interface=interface.dump(),
            )

        if interface.input_mode == "passthrough":
            if not isinstance(payload, dict):
                errors.append(
                    _error("workflow.input.not_object", "Workflow inputs must be an object")
                )
            return WorkflowBoundaryValidationResult(
                ok=not errors,
                errors=errors,
                inputs=raw_inputs if not errors else {},
                interface=interface.dump(),
            )

        if interface.input_mode == "json_schema":
            if not isinstance(payload, dict):
                errors.append(
                    _error("workflow.input.not_object", "Workflow inputs must be an object")
                )
            else:
                errors.extend(
                    _validate_json_schema_payload(
                        interface.input_schema,
                        raw_inputs,
                        channel=normalized_channel or channel,
                    )
                )
            return WorkflowBoundaryValidationResult(
                ok=not errors,
                errors=errors,
                inputs=raw_inputs if not errors else {},
                interface=interface.dump(),
            )

        fields = {row.name: row for row in interface.expected_inputs if row.name}
        normalized: dict[str, Any] = {}
        for name, row in fields.items():
            if row.has_default:
                # A configured default is what this workflow revision runs with.
                # Accepting a caller's value alongside it would make the
                # configured value advisory, and ignoring the caller's value
                # silently would be worse: the run would use one value while the
                # caller believed it had supplied another. Presence of the key
                # is the test — comparing values would make the rule depend on
                # what the caller happened to guess.
                if name in raw_inputs:
                    errors.append(
                        _error(
                            "workflow.input.override_forbidden",
                            f"Workflow input '{name}' has a configured default and "
                            f"cannot be supplied by the caller",
                            path=f"inputs.{name}",
                            channel=channel,
                        )
                    )
                    continue
                ok, value, message = _resolved_default(row)
                if not ok:
                    # The configuration is wrong, not the call. Saying so here
                    # keeps the blame off a run that supplied nothing.
                    errors.append(
                        _error(
                            "workflow.interface.default_invalid",
                            message or f"Configured default for '{name}' is not valid",
                            path=f"inputs.{name}",
                            channel=channel,
                        )
                    )
                    continue
                normalized[name] = value
                continue
            if name not in raw_inputs or _is_missing_contract_value(raw_inputs.get(name), row):
                if row.required:
                    errors.append(
                        _error(
                            "workflow.input.required",
                            f"Workflow input '{name}' is required",
                            path=f"inputs.{name}",
                            channel=channel,
                        )
                    )
                continue
            coercer = (
                _coerce_delegated_contract_value
                if accept_trusted_secret_references
                else coerce_contract_value
            )
            ok, value, message = coercer(raw_inputs.get(name), row)
            if not ok:
                errors.append(
                    _error(
                        _TOKEN_WORKFLOW_INPUT_INVALID_TYPE,
                        message or f"Workflow input '{name}' has invalid type",
                        path=f"inputs.{name}",
                        channel=channel,
                    )
                )
                continue
            normalized[name] = value

        if interface.strict_inputs:
            for key in sorted(raw_inputs):
                if key in fields or key in RUNTIME_PAYLOAD_KEYS:
                    continue
                errors.append(
                    _error(
                        "workflow.input.unknown",
                        f"Workflow input '{key}' is not declared by this workflow interface",
                        path=f"inputs.{key}",
                        channel=channel,
                    )
                )

        runtime_inputs = {
            key: value for key, value in raw_inputs.items() if key in RUNTIME_PAYLOAD_KEYS
        }
        return WorkflowBoundaryValidationResult(
            ok=not errors,
            errors=errors,
            inputs={**runtime_inputs, **normalized}
            if interface.strict_inputs
            else {**raw_inputs, **normalized},
            interface=interface.dump(),
        )

    def validate_outputs_for_workflow_id(
        self,
        workflow_id: str,
        state: dict[str, Any],
    ) -> WorkflowBoundaryValidationResult:
        context = (
            OutputProducerContext.for_state(state) if state.get("workflow_definition_ref") else None
        )
        interface = (
            self.interface_from_definition(context.definition)
            if context
            else self.interface_for_workflow_id(workflow_id)
        )
        return self.validate_outputs(interface, state, producer_context=context)

    def validate_outputs(
        self,
        interface: WorkflowInterface,
        state: dict[str, Any],
        *,
        producer_context: OutputProducerContext | None = None,
    ) -> WorkflowBoundaryValidationResult:
        if not interface.enabled:
            return WorkflowBoundaryValidationResult(ok=True, interface=interface.dump())

        outputs, artifacts = collect_runtime_outputs(
            state, interface=interface, producer_context=producer_context
        )
        errors: list[dict[str, Any]] = []

        if interface.variable_outputs_required():
            for row in interface.expected_outputs:
                if not row.required:
                    continue
                if row.name not in outputs or _is_missing_contract_value(
                    outputs.get(row.name), row
                ):
                    errors.append(
                        _error(
                            "workflow.output.required",
                            f"Workflow output '{row.name}' is required",
                            path=f"outputs.{row.name}",
                        )
                    )
                    continue
                ok, value, message = coerce_contract_value(outputs.get(row.name), row)
                if not ok:
                    errors.append(
                        _error(
                            "workflow.output.invalid_type",
                            message or f"Workflow output '{row.name}' has invalid type",
                            path=f"outputs.{row.name}",
                        )
                    )
                    continue
                outputs[row.name] = value

        if interface.artifacts_required():
            for row in interface.returned_files:
                if not row.required:
                    continue
                if row.name not in artifacts or _is_empty_value(artifacts.get(row.name)):
                    errors.append(
                        _error(
                            "workflow.artifact.required",
                            f"Workflow artifact '{row.name}' is required",
                            path=f"artifacts.{row.name}",
                        )
                    )

        return WorkflowBoundaryValidationResult(
            ok=not errors,
            errors=errors,
            outputs=outputs,
            artifacts=artifacts,
            interface=interface.dump(),
        )


def _error(
    code: str,
    message: str,
    *,
    path: str = "",
    channel: str = "",
) -> dict[str, Any]:
    payload = {
        "code": str(code or "").strip(),
        "message": str(message or "").strip(),
        "path": str(path or "").strip(),
        "severity": "error",
    }
    if channel:
        payload["channel"] = str(channel or "").strip()
    return payload


def _normalize_trigger_channel(channel: str) -> str:
    normalized = str(channel or "").strip().lower()
    aliases = {
        "manual_api": "api",
        "task_ingress": "api",
        "manual": "ui",
        "manual_ui": "ui",
        "web": "ui",
        "chat": "ui",
        "scheduled": "schedule",
        "mcp_workflow": "mcp",
    }
    return aliases.get(normalized, normalized)


def _interface_definition_errors(
    interface: WorkflowInterface,
    *,
    channel: str = "",
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for issue in interface.definition_issues():
        if str(issue.get("severity") or "error").strip().lower() != "error":
            continue
        errors.append(
            _error(
                str(issue.get("code") or "workflow.interface.invalid"),
                str(issue.get("message") or "Workflow interface contract is invalid"),
                path=str(issue.get("path") or ""),
                channel=channel,
            )
        )
    return errors


def _validate_json_schema_payload(
    schema: dict[str, Any],
    payload: dict[str, Any],
    *,
    channel: str = "",
) -> list[dict[str, Any]]:
    if not isinstance(schema, dict) or not schema:
        return [
            _error(
                "workflow.input.schema_missing",
                "Workflow input schema is missing",
                path="input_schema",
                channel=channel,
            )
        ]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [
            _error(
                "workflow.input.schema_invalid",
                f"Workflow input schema is invalid: {exc.message}",
                path=_json_schema_error_path(exc, prefix="input_schema"),
                channel=channel,
            )
        ]

    validator = Draft202012Validator(schema)
    errors: list[dict[str, Any]] = []
    for validation_error in sorted(validator.iter_errors(payload), key=str):
        errors.append(
            _error(
                _json_schema_error_code(validation_error.validator),
                f"Workflow input schema validation failed: {validation_error.message}",
                path=_json_schema_error_path(validation_error),
                channel=channel,
            )
        )
    return errors


def _json_schema_error_code(validator: Any) -> str:
    keyword = str(validator or "").strip()
    if keyword == "required":
        return "workflow.input.required"
    if keyword == "additionalProperties":
        return "workflow.input.unknown"
    if keyword == "type":
        return _TOKEN_WORKFLOW_INPUT_INVALID_TYPE
    return "workflow.input.schema_validation_failed"


def _json_schema_error_path(error: Any, *, prefix: str = "inputs") -> str:
    parts = [str(part) for part in (getattr(error, "path", []) or [])]
    if not parts:
        parts = [str(part) for part in (getattr(error, "schema_path", []) or [])]
    return ".".join([prefix, *parts]) if parts else prefix


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _contains_secret_reference(value: Any) -> bool:
    from core.infrastructure.security.secret_reference import is_secret_reference

    if is_secret_reference(value):
        return True
    if isinstance(value, dict):
        return any(_contains_secret_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_reference(item) for item in value)
    return False


def _is_missing_contract_value(value: Any, field: WorkflowContractField) -> bool:
    if value is None:
        return True
    if field.type in {"object", "array"}:
        return False
    return _is_empty_value(value)


def _resolved_default(field: WorkflowContractField) -> tuple[bool, Any, str]:
    """Resolve a configured default through the same rules as a caller value.

    A secret resolves to its reference, not its plaintext: the value stays
    opaque through the run and is read only at an authorized tool or connector
    boundary, so nothing between here and there can print or persist it.
    """
    if field.is_secret:
        # The wire form, not the object: run state crosses a LangGraph
        # checkpoint, and a reference that arrives on the other side as a plain
        # string would be indistinguishable from a value.
        reference = SecretReference(name=field.name, ref_key=field.default_secret_ref)
        return True, reference.to_wire(), ""
    return coerce_contract_value(field.default, field)


def coerce_contract_value(value: Any, field: WorkflowContractField) -> tuple[bool, Any, str]:
    field_type = field.type
    name = field.name
    if field_type in {"string", "paragraph", "select"}:
        if isinstance(value, (dict, list, tuple, set)):
            return False, None, f"Workflow field '{name}' must be text"
        text = str(value if value is not None else "").strip()
        if field_type == "select" and field.options and text not in field.options:
            return (
                False,
                None,
                f"Workflow field '{name}' must be one of: {', '.join(field.options)}",
            )
        return True, text, ""
    if field_type == "secret":
        # A caller may supply a secret for a field with no configured default.
        # It is held for the run the way a configured one is — as a reference,
        # never as plaintext — so nothing between here and the tool boundary has
        # the value to print, persist or checkpoint.
        if isinstance(value, (dict, list, tuple, set)):
            return False, None, f"Workflow field '{name}' must be a secret value"
        text = str(value if value is not None else "")
        if not text.strip():
            return False, None, f"Workflow field '{name}' must be a secret value"
        from core.infrastructure.security.secret_codec import encrypt_secret
        from core.infrastructure.security.secret_reference import SecretReference

        return True, SecretReference(name=name, ciphertext=encrypt_secret(text)).to_wire(), ""
    if field_type == "number":
        if isinstance(value, bool):
            return False, None, f"Workflow field '{name}' must be a number"
        if isinstance(value, (int, float)):
            return (
                (True, value, "")
                if math.isfinite(float(value))
                else (False, None, f"Workflow field '{name}' must be a finite number")
            )
        text = str(value or "").strip()
        try:
            parsed = float(text)
        except ValueError:
            return False, None, f"Workflow field '{name}' must be a number"
        if not math.isfinite(parsed):
            return False, None, f"Workflow field '{name}' must be a finite number"
        return True, int(parsed) if parsed.is_integer() else parsed, ""
    if field_type == "boolean":
        if isinstance(value, bool):
            return True, value, ""
        if isinstance(value, (int, float)):
            return True, bool(value), ""
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True, True, ""
        if text in {"0", "false", "no", "off"}:
            return True, False, ""
        return False, None, f"Workflow field '{name}' must be boolean"
    if field_type == "object":
        obj = json_object(value)
        if obj:
            return True, obj, ""
        if value == {}:
            return True, {}, ""
        return False, None, f"Workflow field '{name}' must be an object"
    if field_type == "array":
        if isinstance(value, list):
            return True, value, ""
        if isinstance(value, (tuple, set)):
            return True, list(value), ""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return False, None, f"Workflow field '{name}' must be an array"
            if isinstance(parsed, list):
                return True, parsed, ""
        return False, None, f"Workflow field '{name}' must be an array"
    if field_type == "file":
        return (not _is_empty_value(value)), value, f"Workflow field '{name}' must be a file"
    return False, None, f"Workflow field '{name}' has unsupported type '{field_type}'"


def _coerce_delegated_contract_value(
    value: Any,
    field: WorkflowContractField,
) -> tuple[bool, Any, str]:
    if field.type == "secret":
        from core.infrastructure.security.secret_reference import (
            SecretReference,
            as_secret_reference,
        )

        existing = as_secret_reference(value)
        if existing is not None:
            if bool(existing.ref_key) == bool(existing.ciphertext):
                return (
                    False,
                    None,
                    f"Workflow field '{field.name}' has an invalid secret reference",
                )
            return (
                True,
                SecretReference(
                    name=field.name,
                    ref_key=existing.ref_key,
                    ciphertext=existing.ciphertext,
                ).to_wire(),
                "",
            )
    return coerce_contract_value(value, field)


def collect_runtime_outputs(
    state: dict[str, Any],
    *,
    interface: WorkflowInterface,
    producer_context: OutputProducerContext | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state_obj = dict(state or {})
    expected_output_names = {row.name for row in interface.expected_outputs if row.name}
    expected_artifact_names = {row.name for row in interface.returned_files if row.name}
    if interface.output_mode == "none":
        return {}, {}

    raw_outputs = _as_object(state_obj.get("outputs"))
    outputs = {key: value for key, value in raw_outputs.items() if key in expected_output_names}
    workflow_data = _as_object(state_obj.get("workflow_data"))
    workflow_data_outputs = _as_object(workflow_data.get("outputs"))
    for key, value in workflow_data_outputs.items():
        if key in expected_output_names:
            outputs.setdefault(key, value)
    for key in expected_output_names:
        if key in outputs:
            continue
        if key in workflow_data:
            outputs[key] = workflow_data.get(key)

    # A returned file is captured under its stable key when its step writes it
    # (see ``workflow_returned_files``); nothing else can stand in for it, so a
    # message or a variable that happens to share the name is never returned.
    raw_artifacts = _as_object(state_obj.get("artifacts"))
    artifacts = {
        key: copy.deepcopy(value)
        for key, value in raw_artifacts.items()
        if key in expected_artifact_names
    }

    for message in reversed(_state_messages(state_obj)):
        message_artifacts = _as_object(message.get("artifacts"))
        if not message_artifacts:
            continue
        state_patch = _as_object(message_artifacts.get("_state_patch"))
        patch_outputs = _as_object(state_patch.get("outputs"))
        for key, value in patch_outputs.items():
            if key in expected_output_names:
                outputs.setdefault(key, value)
        contract = verified_output_contract(message, producer_context)
        direct_names = {row["name"] for row in contract.rows} if contract is not None else set()
        for key in expected_output_names & direct_names:
            if key in message_artifacts:
                outputs.setdefault(key, message_artifacts.get(key))
        terminal = _as_object(message_artifacts.get("workflow_terminal"))
        terminal_outputs = _as_object(terminal.get("outputs"))
        for key, value in terminal_outputs.items():
            if key in expected_output_names:
                outputs.setdefault(key, value)

    return outputs, artifacts


def _state_messages(state: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from core.application.orchestration.agent_messages import get_state_messages

        messages = get_state_messages(state)
    except Exception:
        messages = state.get("agent_messages") or []
    return [dict(item) for item in messages if isinstance(item, dict)]


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return json_object(value)
    return {}


def _json_schema_for_field(field: WorkflowContractField) -> dict[str, Any]:
    field_type = field.type
    schema: dict[str, Any]
    if field_type in {"string", "paragraph", "select"}:
        schema = {"type": "string"}
        if field_type == "paragraph":
            schema["format"] = "textarea"
        if field_type == "select" and field.options:
            schema["enum"] = list(field.options)
    elif field_type == "number":
        schema = {"type": "number"}
    elif field_type == "boolean":
        schema = {"type": "boolean"}
    elif field_type == "object":
        schema = {"type": "object", "additionalProperties": True}
    elif field_type == "array":
        schema = {"type": "array", "items": {}}
    elif field_type == "file":
        schema = {"type": "object", "additionalProperties": True, "x-fs-type": "file"}
    else:
        schema = {}
    if field.description:
        schema["description"] = field.description
    if field.label:
        schema["title"] = field.label
    if field.contract_id:
        schema["x-contract-id"] = field.contract_id
    return schema


def _json_schema_for_returned_file(field: WorkflowReturnedFile) -> dict[str, Any]:
    """A file handle, or a list of them when the step's result is a collection."""
    handle = ArtifactOutputHandle.model_json_schema()
    return {
        "title": field.name,
        "x-fs-type": "file",
        "x-fs-source": field.source.model_dump(mode="json"),
        "anyOf": [
            {"type": "string"},
            handle,
            {"type": "array", "items": {"anyOf": [{"type": "string"}, handle]}},
        ],
    }
