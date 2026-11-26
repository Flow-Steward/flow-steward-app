"""The stored model of the operator form (ADR 0075) and the contract as the form sees it.

Compatibility tables, the ``settings.operator`` model, and the input/output schema
of a workflow revision that the checks in ``workflow_operator_form_checks`` and the
runtime view in ``workflow_operator_form`` resolve against.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
#: Choices of the job status filter. ``awaiting_review`` is the operator queue:
#: a job waiting for approval itself or anywhere down its Invoke chain.
JOB_STATUS_OPTIONS: tuple[str, ...] = (
    "awaiting_review",
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
