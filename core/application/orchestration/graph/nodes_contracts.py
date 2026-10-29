# mypy: ignore-errors
"""Contract and state-mapping helpers for graph nodes."""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from core.application.orchestration.models import normalize_phase_status
from core.application.orchestration.state_path_guards import (
    RESERVED_STATE_ROOTS,
    is_reserved_state_target,
)
from core.application.orchestration.state_paths import (
    StatePathError,
    parse_state_path,
    resolve_state_path,
    resolve_state_path_with_workflow_data,
)
from core.application.orchestration.step_templating import set_state_path
from core.infrastructure.security.prompt_safety import is_sensitive_name

_TOKEN_LITERAL = "literal:"

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState


STATE_PREFIX = "state."
logger = logging.getLogger(__name__)


def _get_observability_logger():
    return None


def _build_message_causal_links(state: OrchestratorState) -> list[dict]:
    """Build best-effort causal references for AgentMessageV1.caused_by."""
    refs: list[dict] = []

    try:
        from core.application.orchestration.agent_messages import get_state_messages

        history = get_state_messages(state)
        if history:
            latest = history[-1]
            if isinstance(latest, dict):
                refs.append(
                    {
                        "kind": "message",
                        "correlation_id": str(latest.get("correlation_id") or ""),
                        "from_agent": str(latest.get("from_agent") or ""),
                        "step_id": str(latest.get("step_id") or ""),
                    }
                )
    except Exception:
        pass

    feedback = str(state.get("feedback") or "").strip()
    if feedback:
        refs.append({"kind": "feedback", "excerpt": feedback[:300]})

    job_id = str(state.get("job_id") or "").strip()
    if job_id:
        bl = _get_observability_logger()
        if bl is not None:
            try:
                latest_route = bl.get_latest_event(job_id, ["routing_decision", "decision"])
            except Exception:
                latest_route = None
            if isinstance(latest_route, dict):
                detail = (
                    latest_route.get("detail")
                    if isinstance(latest_route.get("detail"), dict)
                    else {}
                )
                refs.append(
                    {
                        "kind": "event",
                        "event_id": str(latest_route.get("id") or "").strip(),
                        "event_type": str(latest_route.get("event_type") or ""),
                        "step_id": str((detail or {}).get("step_id") or ""),
                        "reason": str((detail or {}).get("reason") or ""),
                        "cause_correlation_id": str(
                            (detail or {}).get("cause_correlation_id") or ""
                        ),
                    }
                )

    deduped: list[dict] = []
    seen: set[tuple] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        key = (
            str(ref.get("kind") or ""),
            str(ref.get("correlation_id") or ""),
            str(ref.get("event_id") or ""),
            str(ref.get("excerpt") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def _resolve_dotted(data: Any, path: str) -> Any:
    """Resolve dotted path from nested dicts."""
    current = data
    for part in str(path or "").split("."):
        if not part:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def _has_value(value: Any) -> bool:
    """True when value should count as a present contract field."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _required_fields_from_schema(
    skill: str, schema_kind: str, schema: Any
) -> tuple[list[str], list[str]]:
    """Extract required field list from JSON schema with validation errors."""
    if not isinstance(schema, dict) or not schema:
        return [], [f"Skill '{skill}' missing {schema_kind}"]

    errors: list[str] = []
    schema_type = str(schema.get("type") or "object").strip().lower()
    if schema_type != "object":
        errors.append(f"Skill '{skill}' {schema_kind} must have type='object'")

    properties = schema.get("properties", {})
    if properties is not None and not isinstance(properties, dict):
        errors.append(f"Skill '{skill}' {schema_kind}.properties must be an object")
        properties = {}

    required_raw = schema.get("required", [])
    if required_raw is None:
        required_raw = []
    if not isinstance(required_raw, list):
        return [], [*errors, f"Skill '{skill}' {schema_kind}.required must be a list"]

    required: list[str] = []
    for field in required_raw:
        name = str(field or "").strip()
        if not name:
            continue
        if properties and name not in properties:
            errors.append(
                f"Skill '{skill}' {schema_kind}.required field '{name}' is missing from properties"
            )
        required.append(name)
    return required, errors


def _load_skill_schema(skill: str, schema_kind: str) -> tuple[Any, str | None]:
    """Load skill JSON schema from core.application.registry."""

    def _field_json_schema(field_type: str, description: str) -> dict[str, Any]:
        normalized = str(field_type or "text").strip().lower()
        json_type = "string"
        if normalized in {"int", "integer"}:
            json_type = "integer"
        elif normalized in {"float", "number"}:
            json_type = "number"
        elif normalized in {"bool", "boolean"}:
            json_type = "boolean"
        elif normalized in {"json", "object", "map"}:
            json_type = "object"
        elif normalized in {"list", "array"}:
            json_type = "array"

        schema: dict[str, Any] = {"type": json_type}
        if description:
            schema["description"] = description
        return schema

    def _schema_from_contract_rows(rows: Any) -> dict[str, Any] | None:
        if not isinstance(rows, list):
            return None

        properties: dict[str, Any] = {}
        required: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            properties[name] = _field_json_schema(
                str(row.get("type") or "text"),
                str(row.get("description") or "").strip(),
            )
            if bool(row.get("required", True)):
                required.append(name)

        if not properties and not required:
            return None
        return {"type": "object", "properties": properties, "required": required}

    def _fallback_schema(db_store: Any) -> dict[str, Any] | None:
        try:
            if schema_kind == "input_schema":
                rows = db_store.get_skill_inputs(skill)
            else:
                rows = db_store.get_skill_outputs(skill)
        except Exception:
            return None
        return _schema_from_contract_rows(rows)

    if not skill:
        return None, "Missing skill id for current workflow step"
    try:
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        skill_manifest = registry.skills.get_skill(skill)
        manifest_schema = getattr(skill_manifest, schema_kind, None)
        if isinstance(manifest_schema, dict) and manifest_schema:
            return manifest_schema, None
        skill_row = registry.db_store.get_skill(skill)
    except Exception as exc:
        db_store = getattr(locals().get("registry", None), "db_store", None)
        if db_store is not None:
            with contextlib.suppress(Exception):
                clear_failed_tx = getattr(db_store, "_clear_failed_transaction", None)
                if callable(clear_failed_tx):
                    clear_failed_tx()
            fallback = _fallback_schema(db_store)
            if fallback is not None:
                logger.warning(
                    "Falling back to synthesized %s for skill '%s' after registry load failed: %s",
                    schema_kind,
                    skill,
                    exc,
                )
                return fallback, None
        return None, f"Could not load skill '{skill}' schema: {exc}"
    if not isinstance(skill_row, dict):
        return None, f"Skill '{skill}' not found in registry"
    schema = skill_row.get(schema_kind)
    if schema:
        return schema, None

    fallback = _fallback_schema(registry.db_store)
    if fallback is not None:
        logger.warning(
            "Falling back to synthesized %s for skill '%s' because registry row had no schema",
            schema_kind,
            skill,
        )
        return fallback, None
    return schema, None


def _resolve_required_input(field: str, state: OrchestratorState, messages: list[dict]) -> Any:
    """Resolve required skill input from state/message context."""
    # Explicit message selectors can be referenced directly from schema required fields.
    if field.startswith("message."):
        try:
            from core.application.orchestration.agent_messages import resolve_message_selector

            return resolve_message_selector(field, state)
        except Exception:
            pass

    try:
        value, direct_found = resolve_state_path(state, field)
    except StatePathError:
        value, direct_found = None, False
    if value is not None:
        return value

    materialized_inputs = state.get("materialized_inputs")
    if isinstance(materialized_inputs, dict):
        if "." in field:
            nested = _resolve_dotted(materialized_inputs, field)
            if nested is not None:
                return nested
        elif field in materialized_inputs:
            return materialized_inputs.get(field)

    # Dynamic values survive the graph in this mirror. Keep direct null handling
    # and materialized input precedence; a found null remains a missing required value.
    if not direct_found:
        try:
            value, found = resolve_state_path_with_workflow_data(dict(state), field)
        except StatePathError:
            value, found = None, False
        if found:
            return value

    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        artifacts = msg.get("artifacts")
        if isinstance(artifacts, dict):
            nested = _resolve_dotted(artifacts, field)
            if nested is not None:
                return nested
            if "." not in field and field in artifacts:
                return artifacts.get(field)
        if "." not in field and field in msg:
            return msg.get(field)
    return None


def _active_agent_step_skill_id(state: OrchestratorState) -> str:
    from core.application.orchestration.active_step import active_step

    current = active_step(state)
    if current is None:
        return ""
    step = current.step
    try:
        from core.application.orchestration.helpers import resolve_step_agent
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        agent_id = resolve_step_agent(step, registry, state=dict(state))
    except Exception:
        agent_id = ""
    return _agent_primary_skill_id(agent_id)


def _agent_primary_skill_id(agent_id: str) -> str:
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        return ""
    try:
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        db = registry.db_store
        agent = db.get_agent(normalized_agent_id)
        base_skill = str((agent or {}).get("base_skill") or "").strip()
        if base_skill:
            return base_skill
        for row in db.get_agent_skills(normalized_agent_id) or []:
            skill_id = str((row or {}).get("skill_id") or "").strip()
            if skill_id:
                return skill_id
    except Exception:
        return ""
    return ""


def _skill_requires_request_input(skill_id: str) -> bool:
    normalized_skill = str(skill_id or "").strip()
    if not normalized_skill:
        return False
    schema, schema_error = _load_skill_schema(normalized_skill, "input_schema")
    if schema_error or not isinstance(schema, dict):
        return False
    required_fields, schema_errors = _required_fields_from_schema(
        normalized_skill,
        "input_schema",
        schema,
    )
    return not schema_errors and "request" in set(required_fields or [])


def _synthesized_request_from_materialized_inputs(
    materialized_inputs: dict[str, Any],
    raw_mapping: dict[str, Any],
) -> str:
    ordered_values: list[tuple[str, Any]] = []
    for field in raw_mapping:
        key = str(field or "").strip()
        if not key or key == "request":
            continue
        if key not in materialized_inputs:
            continue
        value = materialized_inputs.get(key)
        if not _has_value(value):
            continue
        ordered_values.append((key, value))
    if not ordered_values:
        return ""
    if len(ordered_values) == 1:
        _name, only_value = ordered_values[0]
        if isinstance(only_value, str):
            return only_value.strip()
        return json.dumps(only_value, ensure_ascii=True, sort_keys=True)
    parts: list[str] = []
    for key, value in ordered_values:
        rendered = (
            value.strip()
            if isinstance(value, str)
            else json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        if not rendered:
            continue
        parts.append(f"{key}: {rendered}")
    return "\n".join(parts).strip()


def _mapping_values_differ(left: Any, right: Any) -> bool:
    """Compare resolved JSON without treating booleans as numbers."""
    if left is right:
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is not type(right) or left != right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() != right.keys() or any(
            _mapping_values_differ(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) != len(right) or any(
            _mapping_values_differ(value, right[index]) for index, value in enumerate(left)
        )
    # Keep the existing inequality semantics for other scalar/opaque values.
    return left != right


def _materialize_agent_step_input_mapping(state: OrchestratorState) -> None:
    """Resolve active agent step input mappings into ``materialized_inputs``.

    Agent skill validation happens before PromptAgent execution, so required
    inputs authored via step-level ``input_mapping`` must be available from the
    canonical runtime container first.
    """

    from core.application.orchestration.active_step import active_step

    current = active_step(state)
    if current is None:
        return
    step = current.step
    step_kind = str(step.get("step_kind") or "agent").strip()
    if step_kind not in {"", "agent"}:
        return

    raw_override = step.get("override")
    override: dict[str, Any] = dict(raw_override) if isinstance(raw_override, dict) else {}
    raw_agent_override = override.get("agent")
    agent_override: dict[str, Any] = (
        dict(raw_agent_override) if isinstance(raw_agent_override, dict) else {}
    )
    raw_step_mapping = step.get("input_mapping_jsonb")
    raw_agent_mapping_jsonb = agent_override.get("input_mapping_jsonb")
    raw_agent_mapping = agent_override.get("input_mapping")
    if isinstance(raw_step_mapping, dict):
        raw_mapping = raw_step_mapping
    elif isinstance(raw_agent_mapping_jsonb, dict):
        raw_mapping = raw_agent_mapping_jsonb
    elif isinstance(raw_agent_mapping, dict):
        raw_mapping = raw_agent_mapping
    else:
        raw_mapping = {}
    from core.application.orchestration.expected_input_sources import (
        resolve_expected_input_source as _resolve_expected_input_source,
    )

    raw_materialized_inputs = state.get("materialized_inputs")
    materialized_inputs: dict[str, Any] = (
        dict(raw_materialized_inputs) if isinstance(raw_materialized_inputs, dict) else {}
    )
    # Sensitivity belongs to the value, not to the name it is currently filed
    # under. Mapping `state.inputs.wp_password` to `login` used to launder a
    # credential into an innocuous-looking key, and every downstream guard that
    # looks at the target key alone then let it through.
    raw_sensitive = state.get("materialized_input_sensitive_keys")
    sensitive_keys: set[str] = (
        set(raw_sensitive) if isinstance(raw_sensitive, (list, set)) else set()
    )
    changed = False
    for field, selector in raw_mapping.items():
        key = str(field or "").strip()
        if not key:
            continue
        if isinstance(selector, str):
            source = selector.strip()
            if not source:
                continue
            if source.startswith(_TOKEN_LITERAL):
                raw_literal = source[len(_TOKEN_LITERAL) :].strip()
                if not raw_literal:
                    value = ""
                else:
                    with contextlib.suppress(Exception):
                        value = json.loads(raw_literal)
                        if key not in materialized_inputs or _mapping_values_differ(
                            materialized_inputs[key], value
                        ):
                            materialized_inputs[key] = value
                            changed = True
                        continue
                    value = raw_literal
            else:
                value, source_error = _resolve_expected_input_source(
                    dict(state),
                    source,
                    f"agent.input_mapping.{key}",
                )
                if source_error:
                    continue
        else:
            value = selector
        if _mapping_source_is_sensitive(selector) and key not in sensitive_keys:
            sensitive_keys.add(key)
            changed = True
        if key not in materialized_inputs or _mapping_values_differ(
            materialized_inputs[key], value
        ):
            materialized_inputs[key] = value
            changed = True
    # A declared input can be supplied naturally by an upstream state write,
    # without an explicit map. Materialize that same value for the actual prompt
    # boundary; validating it alone would leave the model without the input.
    from core.application.orchestration.agent_messages import get_state_messages
    from core.application.orchestration.helpers import resolve_step_skill

    skill_id = resolve_step_skill(step, state=dict(state))
    schema, schema_error = _load_skill_schema(skill_id, "input_schema") if skill_id else ({}, None)
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not schema_error and isinstance(properties, dict):
        messages = get_state_messages(state)
        for name in properties:
            if name in raw_mapping or name in materialized_inputs:
                continue
            # Only the demonstrated dynamic mirror is a new automatic source.
            # A declared runtime global is not made into a public step input.
            try:
                tokens = parse_state_path(name)
                public_tokens = (
                    tokens[1:] if tokens and tokens[0].value == "workflow_data" else tokens
                )
                if not public_tokens or str(public_tokens[0].value).strip() in RESERVED_STATE_ROOTS:
                    continue
                _, mirrored = resolve_state_path(state.get("workflow_data"), name)
            except StatePathError:
                continue
            if not mirrored:
                continue
            value = _resolve_required_input(name, state, messages)
            if _has_value(value):
                materialized_inputs[name] = value
                if _mapping_source_is_sensitive(name):
                    sensitive_keys.add(name)
                changed = True

    if "request" not in materialized_inputs:
        step_skill = _active_agent_step_skill_id(state)
        if step_skill and _skill_requires_request_input(step_skill):
            synthesized_request = _synthesized_request_from_materialized_inputs(
                materialized_inputs,
                raw_mapping,
            )
            if synthesized_request and materialized_inputs.get("request") != synthesized_request:
                materialized_inputs["request"] = synthesized_request
                changed = True
    if changed:
        state["materialized_inputs"] = materialized_inputs
        state["materialized_input_sensitive_keys"] = sorted(sensitive_keys)


def agent_step_input_preview(state: OrchestratorState, step: dict[str, Any]) -> dict[str, Any]:
    """The inputs an agent step's ``input_mapping`` resolved to, as the agent got them.

    Only the mapped fields are shown, with the values materialized for this run;
    a field whose source was a secret shows the mask, whatever it is named.
    """
    from core.application.orchestration.step_io_preview import step_io_payload
    from core.infrastructure.security.prompt_safety import REDACTED_CREDENTIAL_MARKER

    override = step.get("override") if isinstance(step.get("override"), dict) else {}
    agent_override = override.get("agent") if isinstance(override.get("agent"), dict) else {}
    mapping = next(
        (
            candidate
            for candidate in (
                step.get("input_mapping_jsonb"),
                agent_override.get("input_mapping_jsonb"),
                agent_override.get("input_mapping"),
            )
            if isinstance(candidate, dict) and candidate
        ),
        {},
    )
    if not mapping:
        return {}
    materialized = state.get("materialized_inputs")
    materialized = materialized if isinstance(materialized, dict) else {}
    sensitive = set(state.get("materialized_input_sensitive_keys") or [])
    inputs: dict[str, Any] = {}
    for field in mapping:
        key = str(field or "").strip()
        if not key or key not in materialized:
            continue
        inputs[key] = REDACTED_CREDENTIAL_MARKER if key in sensitive else materialized[key]
    return step_io_payload(inputs)


def _mapping_source_is_sensitive(selector: object) -> bool:
    """Whether a mapping reads from something the policy calls a credential.

    Any segment of the selector counts: `state.inputs.wp_password` is a secret
    however the step chooses to name it on arrival.
    """
    if not isinstance(selector, str):
        return False
    source = selector.strip()
    if source.startswith(_TOKEN_LITERAL):
        return False
    return any(is_sensitive_name(segment) for segment in re.split(r"[.\[\]]+", source) if segment)


def _set_agent_step_output_state_path(
    update: dict[str, Any],
    source_state: dict[str, Any],
    target: str,
    value: Any,
) -> None:
    set_state_path(update, source_state, target, value)


def _apply_agent_step_output_mapping(
    *,
    step: dict[str, Any],
    artifacts: dict[str, Any],
    state: OrchestratorState,
    state_updates: dict[str, Any],
) -> None:
    raw_override = step.get("override")
    override = dict(raw_override) if isinstance(raw_override, dict) else {}
    raw_agent_override = override.get("agent")
    agent_override = dict(raw_agent_override) if isinstance(raw_agent_override, dict) else {}
    raw_step_output_mapping = step.get("output_mapping_jsonb")
    raw_agent_output_mapping_jsonb = agent_override.get("output_mapping_jsonb")
    raw_agent_output_mapping = agent_override.get("output_mapping")
    if isinstance(raw_step_output_mapping, dict):
        output_mapping = raw_step_output_mapping
    elif isinstance(raw_agent_output_mapping_jsonb, dict):
        output_mapping = raw_agent_output_mapping_jsonb
    elif isinstance(raw_agent_output_mapping, dict):
        output_mapping = raw_agent_output_mapping
    else:
        output_mapping = {}
    apply_step_output_mapping(
        output_mapping=output_mapping,
        artifacts=artifacts,
        state=state,
        state_updates=state_updates,
    )


def apply_step_output_mapping(
    *,
    output_mapping: Mapping[str, Any],
    artifacts: dict[str, Any],
    state: Mapping[str, Any],
    state_updates: dict[str, Any],
) -> None:
    """Map a step's result outputs to state targets: ``{output_name: target}``."""
    if not output_mapping or not isinstance(artifacts, dict):
        return

    blocked_targets = sorted(
        {
            str(target).strip()
            for target in output_mapping.values()
            if is_reserved_state_target(target)
        }
    )
    if blocked_targets:
        artifacts["output_mapping_errors"] = [
            {
                "code": "output_mapping_reserved_target",
                "blocked_targets": blocked_targets,
            }
        ]
        return

    mapped_outputs: dict[str, Any] = {}
    for output_name, target in output_mapping.items():
        output_key = str(output_name or "").strip()
        target_text = str(target or "").strip()
        if not output_key or not target_text:
            continue
        value = _resolve_dotted(artifacts, output_key)
        if value is None and "." not in output_key:
            value = artifacts.get(output_key)
        if value is None:
            continue
        mapped_outputs[output_key] = value
        if target_text.startswith(STATE_PREFIX):
            _set_agent_step_output_state_path(state_updates, dict(state or {}), target_text, value)
        else:
            mapped_outputs[target_text] = value

    if not mapped_outputs:
        return

    # What the agent produced, under its own output names. Without this the
    # run view showed the state patch (with workflow bookkeeping) instead.
    from core.application.orchestration.step_io_preview import step_io_payload

    artifacts.setdefault("output_payload", step_io_payload(mapped_outputs))
    existing_patch = artifacts.get("_state_patch")
    if state_updates:
        if isinstance(existing_patch, dict):
            merged_patch = copy.deepcopy(existing_patch)
            merged_patch.update(state_updates)
            artifacts["_state_patch"] = merged_patch
        else:
            artifacts["_state_patch"] = copy.deepcopy(state_updates)
    artifacts["mapped_outputs"] = {
        **(
            dict(artifacts.get("mapped_outputs") or {})
            if isinstance(artifacts.get("mapped_outputs"), dict)
            else {}
        ),
        **mapped_outputs,
    }


def _validate_skill_input_contract(state: OrchestratorState, skill: str) -> list[str]:
    """Validate required skill inputs before running the agent."""
    schema, schema_error = _load_skill_schema(skill, "input_schema")
    if schema_error:
        return [schema_error]

    required, schema_errors = _required_fields_from_schema(skill, "input_schema", schema)
    if schema_errors:
        return schema_errors

    from core.application.orchestration.agent_messages import get_state_messages

    messages = get_state_messages(state)
    missing: list[str] = []
    for field in required:
        value = _resolve_required_input(field, state, messages)
        if not _has_value(value):
            missing.append(field)
    return [f"Missing required input '{field}'" for field in missing]


def _validate_skill_output_contract(skill: str, artifacts: Any) -> list[str]:
    """Validate required skill outputs before publishing message."""
    schema, schema_error = _load_skill_schema(skill, "output_schema")
    if schema_error:
        return [schema_error]

    required, schema_errors = _required_fields_from_schema(skill, "output_schema", schema)
    if schema_errors:
        return schema_errors

    payload = artifacts if isinstance(artifacts, dict) else {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    errors: list[str] = []
    for field in required:
        value = _resolve_dotted(payload, field)
        if value is None and "." not in field:
            value = payload.get(field)
        field_schema = properties.get(field, {}) if isinstance(properties, dict) else {}
        field_type = str(field_schema.get("type") or "").strip().lower()
        if value is None:
            errors.append(f"Missing required output '{field}'")
            continue
        if isinstance(value, str) and not value.strip():
            errors.append(f"Required output '{field}' is empty")
            continue
        # Required collection outputs may legitimately be empty (`{}` / `[]`).
        if field_type in {"json", "list", "object", "array"}:
            continue
        if isinstance(value, (list, tuple, set, dict)) and len(value) == 0:
            errors.append(f"Required output '{field}' is empty")
    return errors


def _normalize_message_status(status: str) -> str:
    return normalize_phase_status(status, default="blocked")
