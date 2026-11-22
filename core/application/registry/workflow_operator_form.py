"""The operator form of a workflow (ADR 0075): ``settings.operator`` next to ``settings.callable``.

An admin configures once, in Workflow Settings, how the workflow looks to an
operator: the form over its inputs, how its result is shown, and the columns and
filters of its jobs list. The configuration stores presentation only. Types,
requirements, allowed values and defaults stay in the workflow contract; the
operator settings refer to contract fields by name or path and never copy them.

This module resolves the stored settings into the view the operator UI renders.
The stored model and the contract as the form sees it live in
``workflow_operator_form_model``; the checks, reading and saving live in
``workflow_operator_form_checks``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .workflow_operator_form_checks import (
    _BASE,
    OperatorSettingsRejected,
    _issue,
    _value_node,
    acknowledgement_current,
    operator_settings_issues,
    parse_operator_settings,
    portable_settings,
    prepare_operator_settings_for_save,
)
from .workflow_operator_form_model import (
    COLUMN_FORMATS,
    CONDITION_OPERATORS_BY_KIND,
    FILTER_TYPES_BY_KIND,
    JOB_COLUMN_PATHS,
    JOB_FILTER_TYPES,
    JOB_STATUS_OPTIONS,
    RESULT_DISPLAYS,
    WIDGETS_BY_KIND,
    WIDGETS_BY_STRING_FORMAT,
    OperatorContract,
    OperatorFormField,
    OperatorIssue,
    OperatorListColumn,
    OperatorSettings,
    _properties,
    _required,
    _sub_field_container,
    compatible_filter_types,
    compatible_widgets,
    contract_fingerprint,
    field_kind,
    operator_contract,
)

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
    "JOB_STATUS_OPTIONS",
    "OperatorFieldView",
    "OperatorFilterView",
    "OperatorListColumn",
    "OperatorRuntimeView",
    "OperatorSettings",
    "OperatorSettingsRejected",
    "compatibility_tables",
    "compatible_filter_types",
    "compatible_widgets",
    "contract_fingerprint",
    "field_kind",
    "operator_contract",
    "operator_filter_views",
    "operator_form_fields",
    "operator_runtime_view",
    "operator_settings_issues",
    "parse_operator_settings",
    "portable_settings",
    "prepare_operator_settings_for_save",
]
