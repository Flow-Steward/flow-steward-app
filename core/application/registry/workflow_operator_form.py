"""The operator form of a workflow (ADR 0075): ``settings.operator`` next to ``settings.callable``.

An admin configures once, in Workflow Settings, how the workflow looks to an
operator: the form over its inputs, how its result is shown, and the columns and
filters of its jobs list. The configuration stores presentation only. Types,
requirements, allowed values and defaults stay in the workflow contract; the
operator settings refer to contract fields by name or path and never copy them.

Checks run on save (an error stops ``published: true``, not the save of an
unpublished configuration) and on read, where an error blocks the form with its
reason while the history stays readable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .workflow_contract import WorkflowInterface

OPERATOR_SETTING = "operator"

MAX_FORM_FIELDS = 200
MAX_COLUMNS = 30
MAX_FILTERS = 20
_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:\[\]-]{0,127}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}(?:\[\])?$")

# --- Compatibility tables (ADR 0075, «Форма: поля входов», «Список») ---------

#: Data kinds of a contract field, as the form sees them.
FieldKind = Literal[
    "string",
    "select",
    "select_list",
    "number",
    "boolean",
    "file",
    "file_list",
    "object",
    "object_list",
]
TEXT_WIDGETS: tuple[str, ...] = (
    "text",
    "textarea",
    "wysiwyg",
    "email",
    "url",
    "date",
    "date_time",
    "time",
)
#: The widgets a kind may use; the first is its default.
WIDGETS_BY_KIND: dict[str, tuple[str, ...]] = {
    "string": TEXT_WIDGETS,
    "select": ("select", "radio", "button_group"),
    "select_list": ("checkbox", "multiselect"),
    "number": ("number",),
    "boolean": ("true_false",),
    "file": ("file",),
    "file_list": ("files",),
    "object": ("group",),
    "object_list": ("repeater",),
}
#: A string with a declared format takes the matching widget, or plain text.
WIDGETS_BY_STRING_FORMAT: dict[str, tuple[str, ...]] = {
    "date": ("date", "text"),
    "date-time": ("date_time", "text"),
    "time": ("time", "text"),
    "email": ("email", "text"),
    "uri": ("url", "text"),
    # ``paragraph`` in the contract: a long text by default.
    "textarea": ("textarea", "text", "wysiwyg"),
}
DATE_FORMATS = frozenset({"date", "date-time"})
FilterType = Literal["search", "number_range", "date_range", "boolean", "choice", "choice_multi"]
#: Filter types a kind may use; files and groups have none.
FILTER_TYPES_BY_KIND: dict[str, tuple[str, ...]] = {
    "string": ("search",),
    "date": ("date_range",),
    "number": ("number_range",),
    "boolean": ("boolean",),
    "select": ("choice", "choice_multi"),
    "select_list": ("choice", "choice_multi"),
}
#: Properties of the job itself a column may show.
JOB_COLUMN_PATHS: frozenset[str] = frozenset(
    {"job_id", "status", "created_at", "started_at", "finished_at", "duration_ms", "submitted_by"}
)
#: Properties of the job a filter may use, with their filter types.
JOB_FILTER_TYPES: dict[str, tuple[str, ...]] = {"status": ("choice", "choice_multi")}
#: Choices of the job status filter.
JOB_STATUS_OPTIONS: tuple[str, ...] = (
    "queued",
    "running",
    "awaiting_approval",
    "awaiting_child",
    "awaiting_timer",
    "awaiting_event",
    "completed",
    "failed",
    "cancelled",
)
COLUMN_FORMATS: frozenset[str] = frozenset(
    {
        "auto",
        "text",
        "number",
        "currency",
        "date",
        "datetime",
        "time",
        "boolean",
        "status",
        "user",
        "file_links",
        "count",
        "list",
    }
)
RESULT_DISPLAYS: frozenset[str] = frozenset(
    {
        "auto",
        "text",
        "number",
        "date",
        "datetime",
        "time",
        "wysiwyg",
        "boolean",
        "file_links",
        "table",
        "list",
        "json",
    }
)
#: Condition operators per kind, as in ACF.
_EMPTINESS = ("==empty", "!=empty")
CONDITION_OPERATORS_BY_KIND: dict[str, tuple[str, ...]] = {
    "string": ("==", "!=", "==contains", "==pattern", *_EMPTINESS),
    "select": ("==", "!=", *_EMPTINESS),
    "select_list": ("==contains", "!=contains", *_EMPTINESS),
    "number": ("==", "!=", ">", "<", *_EMPTINESS),
    "boolean": ("==", "!="),
    "file": _EMPTINESS,
    "file_list": _EMPTINESS,
    "object": _EMPTINESS,
    "object_list": _EMPTINESS,
}
ConditionOperator = Literal[
    "==", "!=", ">", "<", "==contains", "!=contains", "==pattern", "==empty", "!=empty"
]

# --- Stored model -----------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperatorConditionRule(_Strict):
    field: str = Field(min_length=1, max_length=128)
    operator: ConditionOperator
    value: Any = None


class OperatorFormField(_Strict):
    #: Empty: the default widget of the field's kind.
    widget: str = Field(default="", max_length=32)
    label: str = Field(default="", max_length=200)
    instructions: str = Field(default="", max_length=2000)
    placeholder: str = Field(default="", max_length=200)
    prepend: str = Field(default="", max_length=40)
    append: str = Field(default="", max_length=40)
    #: Width in percent of the form row; ``None`` is full width.
    width: int | None = Field(default=None, ge=10, le=100)
    #: Labels of ``options`` from the contract; they never change the options.
    option_labels: dict[str, str] = Field(default_factory=dict)
    hidden: bool = False
    #: Groups joined by "or", rules inside a group joined by "and".
    conditional_logic: list[list[OperatorConditionRule]] = Field(
        default_factory=list, max_length=20
    )
    #: Repeater only.
    layout: Literal["", "table", "block", "row"] = ""
    button_label: str = Field(default="", max_length=80)
    #: Group and repeater: their sub-fields by name, in the same model.
    sub_fields: dict[str, OperatorFormField] = Field(default_factory=dict)


class OperatorForm(_Strict):
    order: list[str] = Field(default_factory=list, max_length=MAX_FORM_FIELDS)
    fields: dict[str, OperatorFormField] = Field(default_factory=dict)


class OperatorResultField(_Strict):
    label: str = Field(default="", max_length=200)
    display: str = Field(default="auto", max_length=32)
    hidden: bool = False
    decimals: int | None = Field(default=None, ge=0, le=10)


class OperatorResult(_Strict):
    order: list[str] = Field(default_factory=list, max_length=MAX_FORM_FIELDS)
    fields: dict[str, OperatorResultField] = Field(default_factory=dict)


class OperatorListColumn(_Strict):
    #: Stable id of the column in row values; defaults to ``<source>.<path>``.
    id: str = Field(default="", max_length=128)
    source: Literal["job", "input", "output"]
    path: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=200)
    format: str = Field(default="auto", max_length=32)

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = f"{self.source}.{self.path}"


class OperatorListFilter(_Strict):
    #: The id a jobs list request names the filter by; defaults to ``<source>.<path>``.
    id: str = Field(default="", max_length=128)
    source: Literal["job", "input", "output"]
    path: str = Field(min_length=1, max_length=512)
    type: FilterType
    label: str = Field(default="", max_length=200)

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = f"{self.source}.{self.path}"


class OperatorList(_Strict):
    columns: list[OperatorListColumn] = Field(default_factory=list, max_length=MAX_COLUMNS)
    filters: list[OperatorListFilter] = Field(default_factory=list, max_length=MAX_FILTERS)


class OperatorSubmission(_Strict):
    mode: Literal["foreach", "bulk"] = "bulk"
    #: For each: the top-level input whose values become one run each.
    target_input: str = Field(default="", max_length=128)


class EffectsAcknowledgement(_Strict):
    """The admin confirmed that a submission runs the workflow with real effects.

    ``by``, ``at``, ``revision`` and ``contract_sha`` are written by the server.
    A save sends ``{"confirm": true}`` to acknowledge now; sending back the stored
    object keeps it while the contract is unchanged.
    """

    by: str = Field(default="", max_length=255)
    at: str = Field(default="", max_length=64)
    revision: int = 0
    #: Fingerprint of the contract acknowledged; a different contract voids it.
    contract_sha: str = Field(default="", max_length=64)
    confirm: bool = False


class OperatorSettings(_Strict):
    published: bool = False
    menu_title: str = Field(default="", max_length=120)
    effects_acknowledged: EffectsAcknowledgement | None = None
    submission: OperatorSubmission = Field(default_factory=OperatorSubmission)
    form: OperatorForm = Field(default_factory=OperatorForm)
    result: OperatorResult = Field(default_factory=OperatorResult)
    list: OperatorList = Field(default_factory=OperatorList)

    @field_validator("menu_title")
    @classmethod
    def _stripped(cls, value: str) -> str:
        return str(value or "").strip()

    def stored_dump(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        ack = payload.get("effects_acknowledged")
        if isinstance(ack, dict):
            ack.pop("confirm", None)
        return payload


class OperatorIssue(BaseModel):
    code: str
    message: str
    path: str = ""
    severity: Literal["error", "warning"] = "error"


#: Issues that block a run but not saving a published configuration: they come from
#: the state of the workflow, not from what the admin configured.
RUN_ONLY_ISSUE_CODES = frozenset(
    {"operator.effects_not_acknowledged", "operator.workflow_disabled"}
)

# --- Contract as the form sees it -------------------------------------------


class OperatorContract(BaseModel):
    """The parts of a workflow the operator settings depend on."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    workflow_id: str = ""
    name: str = ""
    revision: int = 0
    enabled: bool = True
    ui_channel: bool = False
    input_mode: str = "fields"
    #: Every input, including fixed (defaulted) ones and secrets marked as such.
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    secret_inputs: list[str] = Field(default_factory=list)
    #: Inputs whose value the contract fixes for this revision; a caller cannot send them.
    fixed_inputs: list[str] = Field(default_factory=list)
    contract_sha: str = ""


def _field_schema(row: Any) -> dict[str, Any]:
    from .workflow_contract_service import _json_schema_for_field

    return _json_schema_for_field(row)


def operator_input_schema(interface: WorkflowInterface) -> dict[str, Any]:
    """Every input as JSON Schema, unlike the caller schema that leaves fixed ones out."""
    if interface.input_mode == "passthrough":
        return {"type": "object", "properties": {}}
    if interface.input_mode == "json_schema":
        schema = copy.deepcopy(interface.input_schema) if interface.input_schema else {}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema
    properties: dict[str, Any] = {}
    required: list[str] = []
    for row in interface.expected_inputs:
        if not row.name:
            continue
        node = {"x-fs-type": "secret"} if row.is_secret else _field_schema(row)
        if row.has_default:
            node["x-fs-default-configured"] = True
        properties[row.name] = node
        if row.required:
            required.append(row.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def contract_fingerprint(interface: WorkflowInterface) -> str:
    """What the admin acknowledged: names, types, requirements, shapes and defaults.

    Labels and descriptions do not change a run's effects; neither does replacing
    the stored copy of a secret default, so its storage key is left out.
    """
    ignored = {"label", "description", "contract_id", "sample_file", "default_secret_ref"}

    def rows(fields: Iterable[Any]) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in row.dump().items() if key not in ignored}
            for row in fields
        ]

    payload = {
        "input_mode": interface.input_mode,
        "input_schema": interface.input_schema if interface.input_mode == "json_schema" else {},
        "inputs": rows(interface.expected_inputs),
        "outputs": rows(interface.expected_outputs),
        "returned_files": sorted(row.name for row in interface.returned_files),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def operator_contract(
    interface: WorkflowInterface,
    *,
    workflow_id: str = "",
    name: str = "",
    revision: int = 0,
    enabled: bool = True,
) -> OperatorContract:
    from .workflow_contract_service import WorkflowInterfaceService

    fields_mode = interface.input_mode not in {"passthrough", "json_schema"}
    return OperatorContract(
        workflow_id=workflow_id,
        name=name or workflow_id,
        revision=revision,
        enabled=enabled,
        ui_channel=bool(interface.enabled and "ui" in interface.trigger_channels),
        input_mode=interface.input_mode,
        input_schema=operator_input_schema(interface),
        output_schema=WorkflowInterfaceService().output_json_schema(interface),
        secret_inputs=[row.name for row in interface.expected_inputs if row.is_secret],
        fixed_inputs=[
            row.name
            for row in interface.expected_inputs
            if fields_mode and row.has_default and not row.is_secret
        ],
        contract_sha=contract_fingerprint(interface),
    )


def field_kind(schema: Any) -> str:
    """The kind of a JSON Schema node, "secret", or "" when the form cannot render it."""
    if not isinstance(schema, dict) or not schema:
        return ""
    if schema.get("x-fs-type") == "file":
        return "file"
    if schema.get("writeOnly") or schema.get("x-fs-type") == "secret":
        return "secret"
    node_type = schema.get("type")
    if node_type == "integer":
        node_type = "number"
    if not isinstance(node_type, str):
        # A union such as ["string", "null"]: the form supports one type per field.
        return ""
    if node_type == "string":
        return "select" if schema.get("enum") else "string"
    if node_type in {"number", "boolean"}:
        return node_type
    if node_type == "object":
        properties = schema.get("properties")
        return "object" if isinstance(properties, dict) and properties else ""
    if node_type == "array":
        return {
            "select": "select_list",
            "file": "file_list",
            "object": "object_list",
        }.get(field_kind(schema.get("items")), "")
    return ""


def compatible_widgets(schema: Any) -> tuple[str, ...]:
    """The widgets this node may use, its default first; empty when none fits."""
    kind = field_kind(schema)
    if kind == "string":
        string_format = str(schema.get("format") or "")
        return WIDGETS_BY_STRING_FORMAT.get(string_format, TEXT_WIDGETS)
    return WIDGETS_BY_KIND.get(kind, ())


def filter_kind(schema: Any) -> str:
    kind = field_kind(schema)
    if kind == "string" and str(schema.get("format") or "") in DATE_FORMATS:
        return "date"
    return kind


def compatible_filter_types(schema: Any) -> tuple[str, ...]:
    return FILTER_TYPES_BY_KIND.get(filter_kind(schema), ())


def resolve_schema_path(schema: Any, path: str) -> dict[str, Any] | None:
    """The schema node at a dotted path; ``name[]`` steps into an array's items."""
    node: Any = schema
    for segment in str(path or "").split("."):
        if not _PATH_SEGMENT_RE.fullmatch(segment):
            return None
        name, into_items = (segment[:-2], True) if segment.endswith("[]") else (segment, False)
        properties = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(properties, dict) or name not in properties:
            return None
        node = properties[name]
        if into_items:
            node = node.get("items") if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _properties(schema: Any) -> dict[str, Any]:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return properties if isinstance(properties, dict) else {}


def _required(schema: Any) -> set[str]:
    required = schema.get("required") if isinstance(schema, dict) else None
    return {str(name) for name in required} if isinstance(required, list) else set()


def _sub_field_container(schema: dict[str, Any]) -> dict[str, Any]:
    """Where a group's or repeater's sub-fields are declared."""
    if field_kind(schema) == "object_list":
        items = schema.get("items")
        return items if isinstance(items, dict) else {}
    return schema


# --- Checks -------------------------------------------------------------------

_BASE = "settings.operator"


def _issue(code: str, message: str, path: str, severity: str = "error") -> OperatorIssue:
    return OperatorIssue(code=code, message=message, path=path, severity=severity)  # type: ignore[arg-type]


def _condition_value_ok(kind: str, operator: str, value: Any, schema: dict[str, Any]) -> bool:
    if operator in _EMPTINESS:
        return value in (None, "")
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "number" or operator in {">", "<"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "select":
        return isinstance(value, str) and value in (schema.get("enum") or [])
    if kind == "select_list":
        options = (schema.get("items") or {}).get("enum") or []
        return isinstance(value, str) and value in options
    if operator == "==pattern":
        if not isinstance(value, str):
            return False
        try:
            re.compile(value)
        except re.error:
            return False
        return True
    return isinstance(value, str)


def _condition_issues(
    field: OperatorFormField,
    *,
    name: str,
    siblings: dict[str, Any],
    secret_names: set[str],
    path: str,
) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    for group_index, group in enumerate(field.conditional_logic):
        for rule_index, rule in enumerate(group):
            rule_path = f"{path}.conditional_logic[{group_index}][{rule_index}]"
            target = siblings.get(rule.field)
            if rule.field == name or target is None or rule.field in secret_names:
                issues.append(
                    _issue(
                        "form.condition_field_unknown",
                        f"The condition of '{name}' refers to '{rule.field}', "
                        "which is not another field of this form",
                        f"{rule_path}.field",
                    )
                )
                continue
            kind = field_kind(target)
            if rule.operator not in CONDITION_OPERATORS_BY_KIND.get(kind, ()):
                issues.append(
                    _issue(
                        "form.condition_operator_incompatible",
                        f"Operator '{rule.operator}' does not fit the "
                        f"{kind or 'unsupported'} field '{rule.field}'",
                        f"{rule_path}.operator",
                    )
                )
                continue
            if not _condition_value_ok(kind, rule.operator, rule.value, target):
                issues.append(
                    _issue(
                        "form.condition_value_invalid",
                        f"The value compared with '{rule.field}' does not fit its type "
                        "or its options",
                        f"{rule_path}.value",
                    )
                )
    return issues


def _form_field_issues(
    name: str,
    field: OperatorFormField,
    node: dict[str, Any],
    *,
    required: bool,
    fixed: bool,
    siblings: dict[str, Any],
    secret_names: set[str],
    path: str,
    depth: int,
) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    kind = field_kind(node)
    widgets = compatible_widgets(node)
    if field.widget and field.widget not in widgets:
        issues.append(
            _issue(
                "form.widget_incompatible",
                f"Widget '{field.widget}' does not fit the {kind or 'unsupported'} field "
                f"'{name}'" + (f"; use one of: {', '.join(widgets)}" if widgets else ""),
                f"{path}.widget",
            )
        )
    if not widgets and not field.hidden:
        issues.append(
            _issue(
                "form.field_unsupported",
                f"The form cannot show '{name}': declare one type with its items or "
                "properties in the workflow contract",
                path,
                "error" if required and not fixed else "warning",
            )
        )
    if field.hidden and required and not fixed:
        issues.append(
            _issue(
                "form.hidden_required_without_default",
                f"'{name}' is required and hidden, but the contract gives it no default",
                f"{path}.hidden",
            )
        )
    if fixed and not field.hidden and depth == 1:
        issues.append(
            _issue(
                "form.field_fixed_by_contract",
                f"'{name}' has a fixed value in the contract; the form shows it read-only",
                path,
                "warning",
            )
        )
    if field.conditional_logic and required:
        issues.append(
            _issue(
                "form.condition_on_required",
                f"'{name}' has a display condition, so it cannot be required in the contract; "
                "the form checks it only while shown",
                f"{path}.conditional_logic",
            )
        )
    issues.extend(
        _condition_issues(field, name=name, siblings=siblings, secret_names=secret_names, path=path)
    )
    options = node.get("enum") if kind == "select" else (node.get("items") or {}).get("enum")
    for option in field.option_labels:
        if option not in (options or []):
            issues.append(
                _issue(
                    "form.option_label_unknown",
                    f"'{option}' is not an option of '{name}'",
                    f"{path}.option_labels.{option}",
                    "warning",
                )
            )
    if field.sub_fields:
        if kind not in {"object", "object_list"}:
            issues.append(
                _issue(
                    "form.sub_fields_unsupported",
                    f"'{name}' is not a group or repeater, so it has no sub-fields",
                    f"{path}.sub_fields",
                )
            )
        else:
            container = _sub_field_container(node)
            sub_properties = _properties(container)
            sub_required = _required(container)
            for sub_name, sub_field in field.sub_fields.items():
                sub_path = f"{path}.sub_fields.{sub_name}"
                sub_node = sub_properties.get(sub_name)
                if not isinstance(sub_node, dict):
                    issues.append(
                        _issue(
                            "form.field_unknown",
                            f"'{name}' has no sub-field '{sub_name}' in the contract",
                            sub_path,
                        )
                    )
                    continue
                issues.extend(
                    _form_field_issues(
                        sub_name,
                        sub_field,
                        sub_node,
                        required=sub_name in sub_required,
                        fixed=False,
                        siblings=sub_properties,
                        secret_names=set(),
                        path=sub_path,
                        depth=depth + 1,
                    )
                )
    return issues


def _form_issues(settings: OperatorSettings, contract: OperatorContract) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    properties = _properties(contract.input_schema)
    required = _required(contract.input_schema)
    secret_names = set(contract.secret_inputs) | {
        name for name, node in properties.items() if field_kind(node) == "secret"
    }
    fixed = set(contract.fixed_inputs)
    prefix = f"{_BASE}.form"
    for name, field in settings.form.fields.items():
        path = f"{prefix}.fields.{name}"
        if name in secret_names:
            if not field.hidden:
                issues.append(
                    _issue(
                        "form.field_secret",
                        f"'{name}' is a secret; its value is entered in the secret catalog, "
                        "not in the form",
                        path,
                    )
                )
            continue
        node = properties.get(name)
        if not isinstance(node, dict):
            issues.append(
                _issue(
                    "form.field_unknown",
                    f"The workflow contract has no input '{name}'",
                    path,
                )
            )
            continue
        issues.extend(
            _form_field_issues(
                name,
                field,
                node,
                required=name in required,
                fixed=name in fixed,
                siblings=properties,
                secret_names=secret_names,
                path=path,
                depth=1,
            )
        )
    seen: set[str] = set()
    for index, name in enumerate(settings.form.order):
        path = f"{prefix}.order[{index}]"
        if name in seen:
            issues.append(_issue("form.order_duplicate", f"'{name}' is listed twice", path))
        seen.add(name)
        if name in secret_names:
            issues.append(
                _issue("form.field_secret", f"'{name}' is a secret and cannot be ordered", path)
            )
        elif name not in properties:
            issues.append(
                _issue("form.field_unknown", f"The workflow contract has no input '{name}'", path)
            )
    for name in sorted(required - secret_names - fixed - set(settings.form.fields)):
        # A required input without an entry is shown with its default widget; one the
        # form cannot render blocks the form.
        if not compatible_widgets(properties.get(name)):
            issues.append(
                _issue(
                    "form.field_unsupported",
                    f"The form cannot show the required input '{name}': declare one type "
                    "with its items or properties in the workflow contract",
                    f"{prefix}.fields.{name}",
                )
            )
    return issues


def _result_issues(settings: OperatorSettings, contract: OperatorContract) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    outputs = _properties(contract.output_schema)
    prefix = f"{_BASE}.result"
    for name, field in settings.result.fields.items():
        path = f"{prefix}.fields.{name}"
        if name not in outputs:
            issues.append(
                _issue("result.field_unknown", f"The workflow returns no output '{name}'", path)
            )
        if field.display not in RESULT_DISPLAYS:
            issues.append(
                _issue(
                    "result.display_unknown",
                    f"Unknown display '{field.display}'",
                    f"{path}.display",
                )
            )
    for index, name in enumerate(settings.result.order):
        if name not in outputs:
            issues.append(
                _issue(
                    "result.field_unknown",
                    f"The workflow returns no output '{name}'",
                    f"{prefix}.order[{index}]",
                )
            )
    return issues


def _value_node(contract: OperatorContract, source: str, path: str) -> dict[str, Any] | None:
    if source == "input":
        if path.split(".")[0].removesuffix("[]") in contract.secret_inputs:
            return None
        node = resolve_schema_path(contract.input_schema, path)
        return None if field_kind(node) == "secret" else node
    return resolve_schema_path(contract.output_schema, path)


def _list_issues(settings: OperatorSettings, contract: OperatorContract) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    prefix = f"{_BASE}.list"
    seen: set[str] = set()
    for index, column in enumerate(settings.list.columns):
        path = f"{prefix}.columns[{index}]"
        if not _ID_RE.fullmatch(column.id) or column.id in seen:
            issues.append(
                _issue(
                    "list.column_id_invalid",
                    f"Column id '{column.id}' must be unique and simple",
                    f"{path}.id",
                )
            )
        seen.add(column.id)
        if column.format not in COLUMN_FORMATS:
            issues.append(
                _issue(
                    "list.column_format_unknown",
                    f"Unknown format '{column.format}'",
                    f"{path}.format",
                )
            )
        known = (
            column.path in JOB_COLUMN_PATHS
            if column.source == "job"
            else _value_node(contract, column.source, column.path) is not None
        )
        if not known:
            issues.append(
                _issue(
                    "list.column_unknown_path",
                    f"'{column.path}' is not a {column.source} value this workflow declares",
                    f"{path}.path",
                )
            )
    seen = set()
    for index, row in enumerate(settings.list.filters):
        path = f"{prefix}.filters[{index}]"
        if not _ID_RE.fullmatch(row.id) or row.id in seen:
            issues.append(
                _issue(
                    "list.filter_id_invalid",
                    f"Filter id '{row.id}' must be unique and simple",
                    f"{path}.id",
                )
            )
        seen.add(row.id)
        if row.source == "job":
            allowed = JOB_FILTER_TYPES.get(row.path)
            if allowed is None:
                issues.append(
                    _issue(
                        "list.filter_unknown_path",
                        f"Runs cannot be filtered by '{row.path}'; use the status or the "
                        "list's date filter",
                        f"{path}.path",
                    )
                )
                continue
        else:
            # Full values are kept per top-level input and output (meta.filter_values,
            # meta.workflow_outputs), so a filter names one of them.
            node = None if "." in row.path else _value_node(contract, row.source, row.path)
            if node is None:
                issues.append(
                    _issue(
                        "list.filter_unknown_path",
                        f"'{row.path}' is not a top-level {row.source} this workflow declares",
                        f"{path}.path",
                    )
                )
                continue
            if field_kind(node) in {"file", "file_list"}:
                issues.append(
                    _issue(
                        "list.filter_on_file",
                        f"'{row.path}' is a file; files cannot be filtered",
                        f"{path}.path",
                    )
                )
                continue
            allowed = compatible_filter_types(node)
        if row.type not in allowed:
            issues.append(
                _issue(
                    "list.filter_type_incompatible",
                    f"Filter type '{row.type}' does not fit '{row.path}'"
                    + (f"; use one of: {', '.join(allowed)}" if allowed else ""),
                    f"{path}.type",
                )
            )
    return issues


def _submission_issues(
    settings: OperatorSettings, contract: OperatorContract
) -> list[OperatorIssue]:
    submission = settings.submission
    if submission.mode == "bulk":
        return []
    path = f"{_BASE}.submission.target_input"
    target = submission.target_input
    properties = _properties(contract.input_schema)
    node = properties.get(target) if target else None
    if not isinstance(node, dict) or target in contract.secret_inputs:
        return [
            _issue(
                "submission.foreach_target_missing",
                "For each needs a top-level input whose values become one run each",
                path,
            )
        ]
    node_type = node.get("type")
    if field_kind(node) != "file" and node_type != "array":
        return [
            _issue(
                "submission.foreach_target_kind",
                f"For each cannot split '{target}': choose a list or a file input",
                path,
            )
        ]
    min_items = node.get("minItems")
    if isinstance(min_items, int) and min_items > 1:
        return [
            _issue(
                "submission.foreach_min_items",
                f"'{target}' needs at least {min_items} items, so one item per run is invalid",
                path,
            )
        ]
    return []


def acknowledgement_current(settings: OperatorSettings, contract: OperatorContract) -> bool:
    ack = settings.effects_acknowledged
    return bool(ack and ack.contract_sha and ack.contract_sha == contract.contract_sha)


def operator_settings_issues(
    settings: OperatorSettings, contract: OperatorContract
) -> list[OperatorIssue]:
    """Every problem of the configuration against the contract, each with a path."""
    issues: list[OperatorIssue] = []
    if settings.published and not contract.ui_channel:
        issues.append(
            _issue(
                "operator.ui_channel_missing",
                "Showing the workflow to operators needs the UI trigger channel",
                f"{_BASE}.published",
            )
        )
    if settings.published and not contract.enabled:
        issues.append(
            _issue(
                "operator.workflow_disabled",
                "The workflow is disabled",
                f"{_BASE}.published",
            )
        )
    if settings.published and not acknowledgement_current(settings, contract):
        issues.append(
            _issue(
                "operator.effects_not_acknowledged",
                "An admin has not confirmed the real effects of this workflow's current contract",
                f"{_BASE}.effects_acknowledged",
            )
        )
    issues.extend(_form_issues(settings, contract))
    issues.extend(_result_issues(settings, contract))
    issues.extend(_list_issues(settings, contract))
    issues.extend(_submission_issues(settings, contract))
    return issues


# --- Reading and saving -------------------------------------------------------


class OperatorSettingsRejected(ValueError):
    """A save that would publish a configuration with blocking issues."""

    def __init__(self, issues: list[OperatorIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues[:20]))


def _validation_issues(exc: ValidationError) -> list[OperatorIssue]:
    return [
        _issue(
            "operator.settings_invalid",
            str(error.get("msg") or "Invalid value"),
            ".".join([_BASE, *(str(part) for part in error.get("loc") or ())]),
        )
        for error in exc.errors()[:50]
    ]


def parse_operator_settings(raw: Any) -> tuple[OperatorSettings | None, list[OperatorIssue]]:
    """Stored or submitted settings; ``None`` with issues when they are malformed."""
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return None, [
            _issue("operator.settings_invalid", "settings.operator must be an object", _BASE)
        ]
    try:
        return OperatorSettings.model_validate(dict(raw)), []
    except ValidationError as exc:
        return None, _validation_issues(exc)


def stored_operator_settings(workflow: Mapping[str, Any] | None) -> Any:
    settings = workflow.get("settings") if isinstance(workflow, Mapping) else None
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            return None
    return settings.get(OPERATOR_SETTING) if isinstance(settings, Mapping) else None


def blocking_save_issues(
    settings: OperatorSettings, issues: Iterable[OperatorIssue]
) -> list[OperatorIssue]:
    if not settings.published:
        return []
    return [
        issue
        for issue in issues
        if issue.severity == "error" and issue.code not in RUN_ONLY_ISSUE_CODES
    ]


def prepare_operator_settings_for_save(
    *,
    existing: Mapping[str, Any],
    final_payload: dict[str, Any],
    actor_id: str,
    now: datetime | None = None,
) -> list[OperatorIssue]:
    """Normalize ``settings.operator`` of a workflow save in place; refuse a broken publication.

    - malformed settings are refused whether published or not;
    - ``effects_acknowledged``: ``{"confirm": true}`` acknowledges the saved contract;
      the stored acknowledgement is kept while the contract is unchanged and is
      dropped when it changes;
    - an error on a published configuration refuses the save; on an unpublished
      one it is returned with the warnings for the editor to show.
    """
    settings = final_payload.get("settings")
    if not isinstance(settings, dict) or OPERATOR_SETTING not in settings:
        return []
    raw = settings.get(OPERATOR_SETTING)
    if raw is None:
        settings.pop(OPERATOR_SETTING, None)
        return []
    parsed, parse_issues = parse_operator_settings(raw)
    if parsed is None:
        raise OperatorSettingsRejected(parse_issues)
    candidate = {**dict(existing), **final_payload, "settings": settings}
    if "steps" not in final_payload:
        candidate["steps"] = existing.get("steps")
    from .workflow_contract import interface_from_definition

    interface = interface_from_definition(candidate)
    revision = int(existing.get("revision") or 0) + 1
    contract = operator_contract(
        interface,
        workflow_id=str(existing.get("workflow_id") or final_payload.get("workflow_id") or ""),
        revision=revision,
        enabled=bool(final_payload.get("enabled", existing.get("enabled", False))),
    )
    previous, _ = parse_operator_settings(stored_operator_settings(existing))
    previous_ack = previous.effects_acknowledged if previous is not None else None
    ack = parsed.effects_acknowledged
    if ack is None:
        parsed.effects_acknowledged = None
    elif ack.confirm:
        moment = now or datetime.now(UTC)
        parsed.effects_acknowledged = EffectsAcknowledgement(
            by=str(actor_id or "").strip(),
            at=moment.isoformat(),
            revision=revision,
            contract_sha=contract.contract_sha,
        )
    elif previous_ack is not None and previous_ack.contract_sha == contract.contract_sha:
        parsed.effects_acknowledged = previous_ack.model_copy(update={"confirm": False})
    else:
        # A changed contract voids the acknowledgement; a caller cannot write one.
        parsed.effects_acknowledged = None
    issues = operator_settings_issues(parsed, contract)
    blocking = blocking_save_issues(parsed, issues)
    if blocking:
        raise OperatorSettingsRejected(blocking)
    settings[OPERATOR_SETTING] = parsed.stored_dump()
    return issues


# --- Runtime view -------------------------------------------------------------


class OperatorFieldView(BaseModel):
    """One field of the operator form, resolved against the contract."""

    name: str
    kind: str = ""
    widget: str = ""
    widgets: list[str] = Field(default_factory=list)
    label: str = ""
    instructions: str = ""
    placeholder: str = ""
    prepend: str = ""
    append: str = ""
    width: int | None = None
    required: bool = False
    #: The contract fixes the value; the form does not send it.
    fixed: bool = False
    hidden: bool = False
    options: list[str] = Field(default_factory=list)
    option_labels: dict[str, str] = Field(default_factory=dict)
    conditional_logic: list[list[dict[str, Any]]] = Field(default_factory=list)
    layout: str = ""
    button_label: str = ""
    min_items: int | None = None
    max_items: int | None = None
    format: str = ""
    sub_fields: list[OperatorFieldView] = Field(default_factory=list)


class OperatorFilterView(BaseModel):
    id: str
    source: str
    path: str
    type: str
    label: str = ""
    #: Choices of a ``choice`` / ``choice_multi`` filter, from the contract.
    options: list[str] = Field(default_factory=list)


class OperatorRuntimeView(BaseModel):
    """``settings.operator`` of one workflow as the operator UI renders it."""

    workflow_id: str = ""
    workflow_name: str = ""
    workflow_revision: int = 0
    title: str = ""
    published: bool = False
    runnable: bool = False
    effects_acknowledged: bool = False
    settings: dict[str, Any] | None = None
    form_fields: list[OperatorFieldView] = Field(default_factory=list)
    filters: list[OperatorFilterView] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    issues: list[OperatorIssue] = Field(default_factory=list)


def _options(node: dict[str, Any]) -> list[str]:
    kind = field_kind(node)
    raw = node.get("enum") if kind == "select" else (node.get("items") or {}).get("enum")
    return [str(item) for item in raw or []]


def _field_view(
    name: str,
    node: dict[str, Any],
    configured: OperatorFormField | None,
    *,
    required: bool,
    fixed: bool,
) -> OperatorFieldView:
    config = configured or OperatorFormField()
    widgets = list(compatible_widgets(node))
    widget = config.widget if config.widget in widgets else (widgets[0] if widgets else "")
    kind = field_kind(node)
    container = _sub_field_container(node) if kind in {"object", "object_list"} else {}
    sub_required = _required(container)
    sub_views = [
        _field_view(
            sub_name,
            sub_node,
            config.sub_fields.get(sub_name),
            required=sub_name in sub_required,
            fixed=False,
        )
        for sub_name, sub_node in _properties(container).items()
        if isinstance(sub_node, dict)
    ]
    string_format = str(node.get("format") or "") if kind == "string" else ""
    return OperatorFieldView(
        name=name,
        kind=kind,
        widget=widget,
        widgets=widgets,
        label=config.label or str(node.get("title") or "") or name,
        instructions=config.instructions or str(node.get("description") or ""),
        placeholder=config.placeholder,
        prepend=config.prepend,
        append=config.append,
        width=config.width,
        required=required,
        fixed=fixed,
        # A fixed value is the contract's; the form never sends it.
        hidden=config.hidden or fixed,
        options=_options(node),
        option_labels=dict(config.option_labels),
        conditional_logic=[
            [rule.model_dump(mode="json") for rule in group] for group in config.conditional_logic
        ],
        layout=config.layout or ("table" if kind == "object_list" else ""),
        button_label=config.button_label,
        min_items=node.get("minItems") if isinstance(node.get("minItems"), int) else None,
        max_items=node.get("maxItems") if isinstance(node.get("maxItems"), int) else None,
        format=string_format if string_format != "textarea" else "",
        sub_fields=sub_views,
    )


def operator_form_fields(
    settings: OperatorSettings | None, contract: OperatorContract
) -> list[OperatorFieldView]:
    """The form in order: configured order first, then the contract's order; no secrets."""
    config = settings or OperatorSettings()
    properties = _properties(contract.input_schema)
    required = _required(contract.input_schema)
    secret_names = set(contract.secret_inputs)
    fixed = set(contract.fixed_inputs)
    names = [name for name in config.form.order if name in properties]
    names.extend(name for name in properties if name not in names)
    views: list[OperatorFieldView] = []
    for name in names:
        node = properties.get(name)
        if name in secret_names or not isinstance(node, dict) or field_kind(node) == "secret":
            continue
        views.append(
            _field_view(
                name,
                node,
                config.form.fields.get(name),
                required=name in required,
                fixed=name in fixed,
            )
        )
    return views


def operator_filter_views(
    settings: OperatorSettings | None, contract: OperatorContract
) -> list[OperatorFilterView]:
    views: list[OperatorFilterView] = []
    for row in (settings or OperatorSettings()).list.filters:
        if row.source == "job":
            options = list(JOB_STATUS_OPTIONS) if row.path == "status" else []
        else:
            node = _value_node(contract, row.source, row.path) or {}
            options = _options(node)
        views.append(
            OperatorFilterView(
                id=row.id,
                source=row.source,
                path=row.path,
                type=row.type,
                label=row.label,
                options=options,
            )
        )
    return views


def operator_runtime_view(
    raw_settings: Any, contract: OperatorContract
) -> tuple[OperatorSettings | None, OperatorRuntimeView]:
    """Settings, issues and whether the form can submit now.

    A configuration that became incompatible stays readable: ``runnable`` is false
    with the reasons in ``issues``.
    """
    settings, parse_issues = parse_operator_settings(raw_settings)
    issues = list(parse_issues)
    if settings is not None:
        try:
            issues.extend(operator_settings_issues(settings, contract))
        except (TypeError, ValueError, AttributeError, KeyError):
            # A contract shape the checks do not understand must not hide the
            # history; the form stays blocked with a reason.
            issues.append(
                _issue(
                    "operator.contract_unreadable",
                    "The workflow contract has a shape the form does not support",
                    _BASE,
                )
            )
    published = bool(settings and settings.published)
    runnable = (
        published
        and contract.enabled
        and contract.ui_channel
        and not any(issue.severity == "error" for issue in issues)
    )
    stored = settings.stored_dump() if settings is not None else None
    if stored is not None and isinstance(stored.get("effects_acknowledged"), dict):
        stored["effects_acknowledged"].pop("contract_sha", None)
    return settings, OperatorRuntimeView(
        workflow_id=contract.workflow_id,
        workflow_name=contract.name,
        workflow_revision=contract.revision,
        title=(settings.menu_title if settings else "") or contract.name or contract.workflow_id,
        published=published,
        runnable=runnable,
        effects_acknowledged=bool(settings and acknowledgement_current(settings, contract)),
        settings=stored,
        form_fields=operator_form_fields(settings, contract),
        filters=operator_filter_views(settings, contract),
        input_schema=contract.input_schema,
        output_schema=contract.output_schema,
        issues=issues,
    )


def compatibility_tables() -> dict[str, Any]:
    """What the Workflow Settings editor offers per kind (ADR 0075 tables)."""
    return {
        "widgets_by_kind": {kind: list(widgets) for kind, widgets in WIDGETS_BY_KIND.items()},
        "widgets_by_string_format": {
            key: list(widgets) for key, widgets in WIDGETS_BY_STRING_FORMAT.items()
        },
        "filter_types_by_kind": {kind: list(types) for kind, types in FILTER_TYPES_BY_KIND.items()},
        "job_columns": sorted(JOB_COLUMN_PATHS),
        "job_filters": {key: list(types) for key, types in JOB_FILTER_TYPES.items()},
        "job_status_options": list(JOB_STATUS_OPTIONS),
        "column_formats": sorted(COLUMN_FORMATS),
        "result_displays": sorted(RESULT_DISPLAYS),
        "condition_operators_by_kind": {
            kind: list(ops) for kind, ops in CONDITION_OPERATORS_BY_KIND.items()
        },
    }


__all__ = [
    "COLUMN_FORMATS",
    "FILTER_TYPES_BY_KIND",
    "JOB_COLUMN_PATHS",
    "JOB_FILTER_TYPES",
    "JOB_STATUS_OPTIONS",
    "OPERATOR_SETTING",
    "RUN_ONLY_ISSUE_CODES",
    "WIDGETS_BY_KIND",
    "EffectsAcknowledgement",
    "OperatorContract",
    "OperatorFieldView",
    "OperatorFilterView",
    "OperatorFormField",
    "OperatorIssue",
    "OperatorListColumn",
    "OperatorListFilter",
    "OperatorRuntimeView",
    "OperatorSettings",
    "OperatorSettingsRejected",
    "acknowledgement_current",
    "blocking_save_issues",
    "compatibility_tables",
    "compatible_filter_types",
    "compatible_widgets",
    "contract_fingerprint",
    "field_kind",
    "filter_kind",
    "operator_contract",
    "operator_form_fields",
    "operator_input_schema",
    "operator_runtime_view",
    "operator_settings_issues",
    "parse_operator_settings",
    "prepare_operator_settings_for_save",
    "resolve_schema_path",
    "stored_operator_settings",
]
