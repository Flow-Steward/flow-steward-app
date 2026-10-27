"""The hook of an ordinary Invoke: its author declaration and result rules (spec §9).

A hook is ``step_kind=invoke_workflow`` with ``override.invoke_workflow.hook``
instead of a direct ``workflow_id``: the author declares the place and the
contract, a client binding (per project) names the child workflow. Nothing
here runs a child; these are pure rules shared by the validators, the binding
service and the Invoke runtime.

* The declaration is the one canonical shape. Unknown fields — including the
  never-supported ``outcomes``, ``allow_waits`` and ``max_duration_ms`` — are
  refused, not ignored.
* The hook output is a separate value (``state.hooks.<hook_id>.output``). Its
  inputs pass through by presence; only ``mutable_fields`` may be replaced,
  output-only fields come from the binding or their declared default.
* Typing, required, nullable, enum and patterns use the existing JSON Schema
  (Draft 2020-12) validator of the workflow Interface.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

HOOK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
HOOK_CONFIG_KEY = "hook"
DIRECT_TARGET_KEY = "workflow_id"
#: The one step of a hook's new storage workflow, its hidden end (R2-11): the builder
#: never draws it, a step that leads here ends the hook, and a storage workflow
#: that holds only this step runs nothing (the hook passes its inputs through).
HOOK_END_STEP_ID = "hook_end"
#: The settings key that marks a hook's storage workflow:
#: ``{author_workflow_id, hook_id, project_id}``, written by create-and-bind.
HOOK_STORAGE_SETTING = "hook_storage"

_DECLARATION_FIELDS = frozenset(
    {
        "hook_id",
        "contract_version",
        "display_name",
        "description",
        "input_schema",
        "input_mapping",
        "mutable_fields",
        "additional_outputs",
        "stage_contract",
    }
)
_REQUIRED_FIELDS = (
    "hook_id",
    "contract_version",
    "display_name",
    "input_schema",
    "input_mapping",
    "stage_contract",
)
_OUTPUT_FIELDS = frozenset({"schema", "optional", "default"})
_STAGE_FIELDS = frozenset({"summary", "effects_before", "effects_after"})


class HookContractError(ValueError):
    def __init__(self, code: str, message: str, path: str = "hook") -> None:
        super().__init__(message)
        self.code = code
        self.path = path

    def issue(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self), "path": self.path}


@dataclass(frozen=True)
class HookDeclaration:
    hook_id: str
    contract_version: int
    display_name: str
    description: str
    input_schema: dict[str, Any]
    input_mapping: dict[str, str]
    mutable_fields: tuple[str, ...]
    additional_outputs: dict[str, dict[str, Any]]
    stage_contract: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def input_names(self) -> frozenset[str]:
        return frozenset(_properties(self.input_schema))

    def dump(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "contract_version": self.contract_version,
            "display_name": self.display_name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema),
            "input_mapping": dict(self.input_mapping),
            "mutable_fields": list(self.mutable_fields),
            "additional_outputs": copy.deepcopy(self.additional_outputs),
            "stage_contract": copy.deepcopy(self.stage_contract),
        }


def _properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties")
    return dict(properties) if isinstance(properties, Mapping) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _check_schema(schema: Any, code: str, path: str) -> None:
    if not isinstance(schema, Mapping):
        raise HookContractError(code, "A JSON Schema object is required", path)
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise HookContractError(code, f"Invalid JSON Schema: {exc.message}", path) from exc


def _schema_errors(schema: Mapping[str, Any], value: Any) -> list[str]:
    validator = Draft202012Validator(dict(schema))
    return [error.message for error in sorted(validator.iter_errors(value), key=str)]


def _parse_input_schema(raw: Any) -> dict[str, Any]:
    path = "hook.input_schema"
    _check_schema(raw, "workflow.hook.input_schema_invalid", path)
    schema = dict(raw)
    if schema.get("type") != "object" or not isinstance(schema.get("properties", {}), Mapping):
        raise HookContractError(
            "workflow.hook.input_schema_invalid",
            "The hook inputs are an object schema with named properties",
            path,
        )
    return copy.deepcopy(schema)


def _parse_outputs(raw: Any, input_names: frozenset[str]) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise HookContractError(
            "workflow.hook.additional_outputs_invalid",
            "additional_outputs maps output names to their contracts",
            "hook.additional_outputs",
        )
    outputs: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        path = f"hook.additional_outputs.{name}"
        if not isinstance(name, str) or not HOOK_ID_PATTERN.match(name):
            raise HookContractError(
                "workflow.hook.output_name_invalid", "Output names are [a-z][a-z0-9_]*", path
            )
        if name in input_names:
            raise HookContractError(
                "workflow.hook.output_name_conflict",
                f"'{name}' is a published input; an output-only field has its own name",
                path,
            )
        if not isinstance(spec, Mapping) or set(spec) - _OUTPUT_FIELDS:
            raise HookContractError(
                "workflow.hook.field_unknown",
                "An output-only field has only schema, optional and default",
                path,
            )
        raw_schema = spec.get("schema")
        _check_schema(raw_schema, "workflow.hook.output_schema_invalid", f"{path}.schema")
        schema = dict(raw_schema) if isinstance(raw_schema, Mapping) else {}
        optional = spec.get("optional", False)
        if not isinstance(optional, bool):
            raise HookContractError(
                "workflow.hook.output_optional_invalid", "optional is true or false", path
            )
        if "default" in spec:
            problems = _schema_errors(schema, spec["default"])
            if problems:
                raise HookContractError(
                    "workflow.hook.output_default_invalid",
                    f"The default of '{name}' does not match its schema: {problems[0]}",
                    f"{path}.default",
                )
        elif not optional:
            raise HookContractError(
                "workflow.hook.output_default_required",
                f"'{name}' is required, so the empty hook needs a valid default for it",
                path,
            )
        outputs[name] = copy.deepcopy(dict(spec))
    return outputs


def _parse_stage(raw: Any) -> dict[str, Any]:
    path = "hook.stage_contract"
    if not isinstance(raw, Mapping) or set(raw) - _STAGE_FIELDS or not _text(raw.get("summary")):
        raise HookContractError(
            "workflow.hook.stage_contract_invalid",
            "stage_contract states where the hook runs (summary) and the effects "
            "before and after it",
            path,
        )
    stage: dict[str, Any] = {"summary": _text(raw.get("summary"))}
    for key in ("effects_before", "effects_after"):
        effects = raw.get(key, [])
        if not isinstance(effects, list) or not all(_text(item) for item in effects):
            raise HookContractError(
                "workflow.hook.stage_contract_invalid", f"{key} is a list of effect names", path
            )
        stage[key] = [_text(item) for item in effects]
    return stage


def _output_schema(
    input_schema: Mapping[str, Any], outputs: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    properties = {**_properties(input_schema)}
    properties.update({name: copy.deepcopy(spec["schema"]) for name, spec in outputs.items()})
    required = [str(name) for name in input_schema.get("required") or []]
    required.extend(name for name, spec in outputs.items() if not spec.get("optional", False))
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(set(required)),
        "additionalProperties": False,
    }


def parse_hook_declaration(raw: Any) -> HookDeclaration:
    """The validated declaration, or a precise :class:`HookContractError`."""
    if not isinstance(raw, Mapping):
        raise HookContractError("workflow.hook.invalid", "The hook declaration is an object")
    unknown = sorted(str(key) for key in set(raw) - _DECLARATION_FIELDS)
    if unknown:
        raise HookContractError(
            "workflow.hook.field_unknown",
            f"Unknown hook field(s) {', '.join(unknown)}",
            f"hook.{unknown[0]}",
        )
    missing = [key for key in _REQUIRED_FIELDS if key not in raw]
    if missing:
        raise HookContractError(
            "workflow.hook.field_missing",
            f"The hook declaration needs {', '.join(missing)}",
            f"hook.{missing[0]}",
        )
    hook_id = raw.get("hook_id")
    if not isinstance(hook_id, str) or not HOOK_ID_PATTERN.match(hook_id):
        raise HookContractError(
            "workflow.hook.id_invalid", "hook_id is a stable [a-z][a-z0-9_]* id", "hook.hook_id"
        )
    version = raw.get("contract_version")
    if type(version) is not int or version < 1:
        raise HookContractError(
            "workflow.hook.contract_version_invalid",
            "contract_version is a positive integer",
            "hook.contract_version",
        )
    display_name = _text(raw.get("display_name"))
    if not display_name:
        raise HookContractError(
            "workflow.hook.display_name_missing",
            "The hook needs a business name",
            "hook.display_name",
        )
    input_schema = _parse_input_schema(raw.get("input_schema"))
    input_names = frozenset(_properties(input_schema))
    mapping = raw.get("input_mapping")
    if not isinstance(mapping, Mapping) or not all(
        isinstance(value, str) and value.strip() for value in mapping.values()
    ):
        raise HookContractError(
            "workflow.hook.input_mapping_invalid",
            "input_mapping maps each published input to an author state selector",
            "hook.input_mapping",
        )
    for name in mapping:
        if name not in input_names:
            raise HookContractError(
                "workflow.hook.input_mapping_unknown",
                f"'{name}' is not a published hook input",
                f"hook.input_mapping.{name}",
            )
    for name in input_schema.get("required") or []:
        if name not in mapping:
            raise HookContractError(
                "workflow.hook.input_mapping_missing",
                f"The required input '{name}' has no source in the author state",
                f"hook.input_mapping.{name}",
            )
    mutable = raw.get("mutable_fields", [])
    if not isinstance(mutable, list) or not all(isinstance(item, str) for item in mutable):
        raise HookContractError(
            "workflow.hook.mutable_fields_invalid",
            "mutable_fields is a list of input names",
            "hook.mutable_fields",
        )
    for name in mutable:
        if name not in input_names:
            raise HookContractError(
                "workflow.hook.mutable_field_unknown",
                f"'{name}' is not a published hook input",
                "hook.mutable_fields",
            )
    outputs = _parse_outputs(raw.get("additional_outputs"), input_names)
    return HookDeclaration(
        hook_id=hook_id,
        contract_version=version,
        display_name=display_name,
        description=_text(raw.get("description")),
        input_schema=input_schema,
        input_mapping={str(key): str(value).strip() for key, value in mapping.items()},
        mutable_fields=tuple(dict.fromkeys(mutable)),
        additional_outputs=outputs,
        stage_contract=_parse_stage(raw.get("stage_contract")),
        output_schema=_output_schema(input_schema, outputs),
    )


def hook_semantic_digest(hook: HookDeclaration) -> str:
    """What a binding was made against: schemas, mappings, mutability, outputs, stage.

    Labels and descriptions are excluded; moving the hook to another stage
    changes the digest even when every type stays the same.
    """
    semantic = hook.dump()
    semantic.pop("display_name")
    semantic.pop("description")
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def hook_storage_marker(
    *, author_workflow_id: str, hook_id: str, project_id: str
) -> dict[str, str]:
    """The ``settings.hook_storage`` marker of a hook's storage workflow."""
    return {
        "author_workflow_id": str(author_workflow_id or "").strip(),
        "hook_id": str(hook_id or "").strip(),
        "project_id": str(project_id or "").strip(),
    }


def is_hook_storage(workflow: Mapping[str, Any] | None) -> bool:
    """Whether *workflow* keeps a hook's steps: it carries ``settings.hook_storage``.

    Such a workflow is a place inside another workflow, never a workflow of its
    own: lists and pickers a person browses hide it, and a hook made only of Set
    variable steps is complete (it changes the hook's results). A row that
    already says ``hook_storage`` (a list projection) counts as well.
    """
    if not isinstance(workflow, Mapping):
        return False
    if workflow.get(HOOK_STORAGE_SETTING) is True:
        return True
    settings = workflow.get("settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings or "{}")
        except ValueError:
            return False
    return isinstance(settings, Mapping) and isinstance(settings.get(HOOK_STORAGE_SETTING), Mapping)


def hook_run_label(workflow: Mapping[str, Any] | None) -> str | None:
    """How a run of a hook's steps reads in Jobs, or ``None`` for an ordinary workflow.

    A hook's storage workflow is never named to a person: its run reads as the
    hook (the storage workflow carries the hook's display name) followed by the
    workflow that owns the hook, e.g. ``Before the order is finished · Order
    fulfilment``. *workflow* is a job row joined with its workflow:
    ``selected_workflow_display_name``, ``hook_storage`` and
    ``hook_author_workflow_display_name``.
    """
    if not is_hook_storage(workflow):
        return None
    assert workflow is not None
    hook_name = str(workflow.get("selected_workflow_display_name") or "").strip()
    parent_name = str(workflow.get("hook_author_workflow_display_name") or "").strip()
    return " · ".join(part for part in (hook_name, parent_name) if part) or None


def hook_of(config: Mapping[str, Any] | None) -> Any:
    """The raw hook declaration of an ``invoke_workflow`` config, if it has one."""
    return dict(config or {}).get(HOOK_CONFIG_KEY)


def invoke_config_issues(config: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """An Invoke targets either one direct workflow or one hook, never both or none."""
    source = dict(config or {})
    has_direct = bool(_text(source.get(DIRECT_TARGET_KEY)))
    has_hook = HOOK_CONFIG_KEY in source
    if has_direct and has_hook:
        return [
            {
                "code": "workflow.hook.direct_target_conflict",
                "message": "An Invoke names a direct workflow_id or a hook, not both",
                "path": "override.invoke_workflow",
            }
        ]
    if not has_direct and not has_hook:
        return [
            {
                "code": "workflow.invoke.target_missing",
                "message": "An Invoke names the workflow it calls, or declares a hook",
                "path": "override.invoke_workflow.workflow_id",
            }
        ]
    if has_hook:
        mixed = sorted(set(source) & {"input_mapping", "output_mapping", "artifact_mapping"})
        if mixed:
            return [
                {
                    "code": "workflow.hook.mapping_conflict",
                    "message": (
                        "A hook declares its inputs in hook.input_mapping; the child "
                        "mappings belong to each project's binding, not to the Invoke"
                    ),
                    "path": f"override.invoke_workflow.{mixed[0]}",
                }
            ]
        try:
            parse_hook_declaration(source[HOOK_CONFIG_KEY])
        except HookContractError as exc:
            issue = exc.issue()
            issue["path"] = f"override.invoke_workflow.{exc.path}"
            return [issue]
    return []


def select_hook_targets(
    output_mapping: Mapping[str, str], child_outputs: Mapping[str, Any]
) -> dict[str, Any]:
    """The hook targets a child result fills: ``child output → hook target``.

    Unused declared child outputs are allowed. A mapped output the hook's steps
    did not set selects nothing: :func:`normalize_hook_output` then keeps the
    hook input of that name (an output-only field gets its declared default),
    exactly as an empty hook would. A value that was set, ``null`` included, is
    a value and is still checked against the hook contract.
    """
    return {
        str(target): copy.deepcopy(child_outputs[source])
        for source, target in output_mapping.items()
        if source in child_outputs
    }


def target_issue(hook: HookDeclaration, target: str) -> HookContractError | None:
    """Why *target* may not receive a child value, or None."""
    if target in hook.additional_outputs or target in hook.mutable_fields:
        return None
    if target in hook.input_names:
        return HookContractError(
            "workflow.hook.target_read_only",
            f"'{target}' is a read-only hook input",
            f"output_mapping.{target}",
        )
    return HookContractError(
        "workflow.hook.target_unknown",
        f"'{target}' is not a mutable input or an output-only field of the hook",
        f"output_mapping.{target}",
    )


def normalize_hook_output(
    hook: HookDeclaration,
    inputs: Mapping[str, Any],
    targets: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The whole hook output, validated before it is published.

    *targets* is ``None`` for the empty hook (no binding, no child). Inputs pass
    through by presence; falsy values and a nullable ``null`` are values.
    """
    output = {
        name: copy.deepcopy(value) for name, value in inputs.items() if name in hook.input_names
    }
    for target, value in (targets or {}).items():
        problem = target_issue(hook, str(target))
        if problem is not None:
            raise problem
        output[str(target)] = copy.deepcopy(value)
    for name, spec in hook.additional_outputs.items():
        if name in output:
            continue
        if "default" in spec:
            output[name] = copy.deepcopy(spec["default"])
    problems = _schema_errors(hook.output_schema, output)
    if problems:
        raise HookContractError(
            "workflow.hook.output_invalid",
            f"The hook output does not match its contract: {problems[0]}",
            "hooks.output",
        )
    return output


__all__ = [
    "DIRECT_TARGET_KEY",
    "HOOK_CONFIG_KEY",
    "HOOK_END_STEP_ID",
    "HOOK_ID_PATTERN",
    "HOOK_STORAGE_SETTING",
    "HookContractError",
    "HookDeclaration",
    "hook_of",
    "hook_run_label",
    "hook_semantic_digest",
    "hook_storage_marker",
    "invoke_config_issues",
    "is_hook_storage",
    "normalize_hook_output",
    "parse_hook_declaration",
    "select_hook_targets",
    "target_issue",
]
