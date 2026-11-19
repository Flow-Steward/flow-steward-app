"""The operator form of a workflow: settings.operator and its checks (ADR 0075)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_operator_form import (
    OperatorSettings,
    OperatorSettingsRejected,
    compatible_filter_types,
    compatible_widgets,
    contract_fingerprint,
    field_kind,
    operator_contract,
    operator_runtime_view,
    operator_settings_issues,
    parse_operator_settings,
    prepare_operator_settings_for_save,
)

INPUTS: list[dict[str, Any]] = [
    {"name": "doc_type", "type": "select", "options": ["contract", "invoice"], "required": True},
    {"name": "files", "type": "array", "items": {"type": "file"}, "min_items": 1},
    {
        "name": "companies",
        "type": "array",
        "items": {
            "type": "object",
            "properties": [
                {"name": "name", "type": "string", "required": True},
                {"name": "website", "type": "string", "format": "uri"},
                {"name": "email", "type": "string", "format": "email"},
            ],
        },
    },
    {"name": "urgent", "type": "boolean"},
    {"name": "urgent_reason", "type": "paragraph"},
    {"name": "ocr_model", "type": "string", "required": True, "default": "fast"},
    {"name": "min_confidence", "type": "number", "required": True},
    {"name": "signed_on", "type": "string", "format": "date"},
    {"name": "tags", "type": "array", "items": {"type": "select", "options": ["a", "b"]}},
    {"name": "amount", "type": "number"},
    {"name": "mail_password", "type": "secret"},
    {"name": "raw", "type": "array"},
    {"name": "pairs", "type": "array", "min_items": 2, "items": {"type": "string"}},
    {"name": "attachment", "type": "file"},
    {
        "name": "customer",
        "type": "object",
        "properties": [{"name": "vat", "type": "string"}],
    },
]
OUTPUTS: list[dict[str, Any]] = [
    {"name": "total", "type": "number"},
    {"name": "verdict", "type": "select", "options": ["ok", "check"]},
    {"name": "summary", "type": "string"},
]


def _callable(
    inputs: list[dict[str, Any]] | None = None, channels: tuple[str, ...] = ("ui", "api")
) -> dict[str, Any]:
    return {
        "enabled": True,
        "trigger_channels": list(channels),
        "expected_inputs": copy.deepcopy(INPUTS if inputs is None else inputs),
        "expected_outputs": copy.deepcopy(OUTPUTS),
    }


def _contract(
    inputs: list[dict[str, Any]] | None = None,
    *,
    channels: tuple[str, ...] = ("ui", "api"),
    enabled: bool = True,
):
    interface = WorkflowInterface.from_settings({"callable": _callable(inputs, channels)})
    return operator_contract(
        interface, workflow_id="wf.contracts", name="Contracts", revision=4, enabled=enabled
    )


def _settings(**overrides: Any) -> OperatorSettings:
    return OperatorSettings.model_validate(overrides)


def _codes(settings: OperatorSettings, contract=None) -> set[str]:
    return {issue.code for issue in operator_settings_issues(settings, contract or _contract())}


def _issue_paths(settings: OperatorSettings, code: str) -> list[str]:
    return [
        issue.path
        for issue in operator_settings_issues(settings, _contract())
        if issue.code == code
    ]


# --- Model ---------------------------------------------------------------------


def test_the_plan_example_parses_and_dumps_without_the_confirm_flag() -> None:
    raw = {
        "published": True,
        "menu_title": " Contracts ",
        "effects_acknowledged": {"by": "u1", "at": "2026-11-19T10:00:00+00:00", "revision": 12},
        "submission": {"mode": "bulk"},
        "form": {
            "order": ["doc_type", "files", "companies", "urgent", "urgent_reason"],
            "fields": {
                "doc_type": {
                    "widget": "select",
                    "label": "Document type",
                    "option_labels": {"contract": "Contract", "invoice": "Invoice"},
                },
                "companies": {
                    "widget": "repeater",
                    "layout": "table",
                    "button_label": "Add company",
                    "sub_fields": {
                        "name": {"widget": "text"},
                        "website": {"widget": "url"},
                        "email": {"widget": "email"},
                    },
                },
                "urgent_reason": {
                    "widget": "textarea",
                    "conditional_logic": [[{"field": "urgent", "operator": "==", "value": True}]],
                },
                "ocr_model": {"hidden": True},
            },
        },
        "result": {"order": ["total"], "fields": {"total": {"display": "number", "decimals": 2}}},
        "list": {
            "columns": [
                {"source": "job", "path": "created_at", "label": "Date", "format": "datetime"},
                {"source": "input", "path": "files", "format": "file_links"},
                {"source": "output", "path": "total", "format": "number"},
            ],
            "filters": [
                {"source": "input", "path": "doc_type", "type": "choice_multi"},
                {"source": "output", "path": "total", "type": "number_range"},
                {"source": "input", "path": "urgent", "type": "boolean"},
            ],
        },
    }

    settings, issues = parse_operator_settings(raw)

    assert issues == []
    assert settings is not None
    assert settings.menu_title == "Contracts"
    assert [column.id for column in settings.list.columns] == [
        "job.created_at",
        "input.files",
        "output.total",
    ]
    assert settings.list.filters[0].id == "input.doc_type"
    assert "confirm" not in settings.stored_dump()["effects_acknowledged"]
    contract = _contract()
    # Only the acknowledgement of an older contract is missing from this example.
    assert {issue.code for issue in operator_settings_issues(settings, contract)} == {
        "operator.effects_not_acknowledged"
    }


@pytest.mark.parametrize(
    "raw",
    [
        "published",
        {"published": True, "unknown": 1},
        {"submission": {"mode": "batch"}},
        {"form": {"fields": {"a": {"widget": "text", "colour": "red"}}}},
        {"list": {"filters": [{"source": "input", "path": "a", "type": "regex"}]}},
    ],
)
def test_malformed_settings_are_reported_not_guessed(raw) -> None:
    settings, issues = parse_operator_settings(raw)

    assert settings is None
    assert issues
    assert {issue.code for issue in issues} == {"operator.settings_invalid"}


# --- Compatibility tables --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind", "widgets", "filters"),
    [
        ("doc_type", "select", ("select", "radio", "button_group"), ("choice", "choice_multi")),
        ("tags", "select_list", ("checkbox", "multiselect"), ("choice", "choice_multi")),
        ("amount", "number", ("number",), ("number_range",)),
        ("urgent", "boolean", ("true_false",), ("boolean",)),
        ("attachment", "file", ("file",), ()),
        ("files", "file_list", ("files",), ()),
        ("customer", "object", ("group",), ()),
        ("companies", "object_list", ("repeater",), ()),
        ("signed_on", "string", ("date", "text"), ("date_range",)),
        ("urgent_reason", "string", ("textarea", "text", "wysiwyg"), ("search",)),
        (
            "ocr_model",
            "string",
            ("text", "textarea", "wysiwyg", "email", "url", "date", "date_time", "time"),
            ("search",),
        ),
        ("mail_password", "secret", (), ()),
        ("raw", "", (), ()),
    ],
)
def test_widgets_and_filter_types_follow_the_data_type(name, kind, widgets, filters) -> None:
    node = _contract().input_schema["properties"][name]

    assert field_kind(node) == kind
    assert compatible_widgets(node) == widgets
    assert compatible_filter_types(node) == filters


def test_a_string_format_selects_its_widget_in_sub_fields_too() -> None:
    items = _contract().input_schema["properties"]["companies"]["items"]["properties"]

    assert compatible_widgets(items["website"]) == ("url", "text")
    assert compatible_widgets(items["email"]) == ("email", "text")


# --- Form issues -------------------------------------------------------------------


def test_an_incompatible_widget_is_an_error_with_its_path() -> None:
    settings = _settings(form={"fields": {"amount": {"widget": "textarea"}}})

    assert _issue_paths(settings, "form.widget_incompatible") == [
        "settings.operator.form.fields.amount.widget"
    ]


def test_an_incompatible_sub_field_widget_is_reported_under_its_parent() -> None:
    settings = _settings(
        form={"fields": {"companies": {"sub_fields": {"email": {"widget": "number"}}}}}
    )

    assert _issue_paths(settings, "form.widget_incompatible") == [
        "settings.operator.form.fields.companies.sub_fields.email.widget"
    ]


def test_a_hidden_required_field_needs_a_contract_default() -> None:
    settings = _settings(
        form={"fields": {"min_confidence": {"hidden": True}, "ocr_model": {"hidden": True}}}
    )

    assert _issue_paths(settings, "form.hidden_required_without_default") == [
        "settings.operator.form.fields.min_confidence.hidden"
    ]


def test_a_secret_is_never_a_form_field() -> None:
    shown = _settings(form={"fields": {"mail_password": {"label": "Mail password"}}})
    ordered = _settings(form={"order": ["mail_password"]})

    assert "form.field_secret" in _codes(shown)
    assert "form.field_secret" in _codes(ordered)
    assert "form.field_secret" not in _codes(
        _settings(form={"fields": {"mail_password": {"hidden": True}}})
    )


def test_unknown_fields_and_order_entries_are_errors() -> None:
    settings = _settings(form={"order": ["ghost"], "fields": {"phantom": {"widget": "text"}}})

    assert sorted(_issue_paths(settings, "form.field_unknown")) == [
        "settings.operator.form.fields.phantom",
        "settings.operator.form.order[0]",
    ]


def test_an_unrenderable_required_input_blocks_and_an_optional_one_warns() -> None:
    required = _contract([{"name": "raw", "type": "array", "required": True}])
    optional = _settings(form={"fields": {"raw": {"label": "Raw"}}})

    issues = operator_settings_issues(_settings(), required)
    assert [(issue.code, issue.severity) for issue in issues] == [
        ("form.field_unsupported", "error")
    ]
    warnings = [issue for issue in operator_settings_issues(optional, _contract()) if issue.code]
    assert [(issue.code, issue.severity) for issue in warnings] == [
        ("form.field_unsupported", "warning")
    ]


def test_option_labels_never_add_options() -> None:
    settings = _settings(form={"fields": {"doc_type": {"option_labels": {"receipt": "Receipt"}}}})

    issues = operator_settings_issues(settings, _contract())
    assert [(issue.code, issue.severity) for issue in issues] == [
        ("form.option_label_unknown", "warning")
    ]


def test_sub_fields_need_a_group_or_repeater_with_that_property() -> None:
    not_group = _settings(form={"fields": {"amount": {"sub_fields": {"x": {}}}}})
    unknown = _settings(form={"fields": {"customer": {"sub_fields": {"iban": {}}}}})

    assert "form.sub_fields_unsupported" in _codes(not_group)
    assert _issue_paths(unknown, "form.field_unknown") == [
        "settings.operator.form.fields.customer.sub_fields.iban"
    ]


# --- Conditional logic --------------------------------------------------------------


def _condition(field: str, operator: str, value: Any = None, *, on: str = "urgent_reason"):
    return _settings(
        form={
            "fields": {
                on: {
                    "conditional_logic": [[{"field": field, "operator": operator, "value": value}]]
                }
            }
        }
    )


@pytest.mark.parametrize(
    ("field", "operator", "value"),
    [
        ("urgent", "==", True),
        ("urgent", "!=", False),
        ("doc_type", "==", "invoice"),
        ("doc_type", "!=empty", None),
        ("tags", "==contains", "a"),
        ("amount", ">", 100),
        ("signed_on", "==pattern", r"^2026-"),
        ("files", "==empty", None),
    ],
)
def test_conditions_that_fit_their_field_are_accepted(field, operator, value) -> None:
    assert _codes(_condition(field, operator, value)) == set()


@pytest.mark.parametrize(
    ("field", "operator", "value", "code"),
    [
        ("ghost", "==", "x", "form.condition_field_unknown"),
        ("mail_password", "!=empty", None, "form.condition_field_unknown"),
        ("urgent_reason", "!=empty", None, "form.condition_field_unknown"),
        ("urgent", ">", 1, "form.condition_operator_incompatible"),
        ("files", "==", "a.pdf", "form.condition_operator_incompatible"),
        ("tags", "==", "a", "form.condition_operator_incompatible"),
        ("urgent", "==", "yes", "form.condition_value_invalid"),
        ("doc_type", "==", "receipt", "form.condition_value_invalid"),
        ("amount", "<", "ten", "form.condition_value_invalid"),
        ("signed_on", "==pattern", "(", "form.condition_value_invalid"),
        ("doc_type", "==empty", "contract", "form.condition_value_invalid"),
    ],
)
def test_conditions_on_missing_fields_or_with_unfit_operators_are_errors(
    field, operator, value, code
) -> None:
    issues = operator_settings_issues(_condition(field, operator, value), _contract())

    assert [issue.code for issue in issues] == [code]
    assert issues[0].path.startswith(
        "settings.operator.form.fields.urgent_reason.conditional_logic[0][0]"
    )


def test_a_field_with_a_condition_cannot_be_required_in_the_contract() -> None:
    settings = _condition("urgent", "==", True, on="doc_type")

    assert _issue_paths(settings, "form.condition_on_required") == [
        "settings.operator.form.fields.doc_type.conditional_logic"
    ]


def test_sub_field_conditions_refer_to_siblings() -> None:
    settings = _settings(
        form={
            "fields": {
                "companies": {
                    "sub_fields": {
                        "email": {
                            "conditional_logic": [
                                [{"field": "website", "operator": "==empty"}],
                                [{"field": "urgent", "operator": "==", "value": True}],
                            ]
                        }
                    }
                }
            }
        }
    )

    assert _issue_paths(settings, "form.condition_field_unknown") == [
        "settings.operator.form.fields.companies.sub_fields.email.conditional_logic[1][0].field"
    ]


# --- Result, list and submission --------------------------------------------------------


def test_result_fields_must_be_declared_outputs() -> None:
    settings = _settings(
        result={"order": ["total", "ghost"], "fields": {"total": {"display": "sparkline"}}}
    )

    assert _codes(settings) == {"result.field_unknown", "result.display_unknown"}


def test_columns_and_filters_on_unknown_paths_are_errors() -> None:
    settings = _settings(
        list={
            "columns": [
                {"source": "job", "path": "worker_id"},
                {"source": "input", "path": "companies[].name"},
                {"source": "input", "path": "mail_password"},
                {"source": "output", "path": "ghost"},
                {"id": "x", "source": "output", "path": "total", "format": "sparkline"},
            ],
            "filters": [
                {"source": "input", "path": "ghost", "type": "search"},
                {"source": "input", "path": "companies.name", "type": "search"},
                {"source": "job", "path": "worker_id", "type": "search"},
            ],
        }
    )

    issues = operator_settings_issues(settings, _contract())
    assert [(issue.code, issue.path) for issue in issues] == [
        ("list.column_unknown_path", "settings.operator.list.columns[0].path"),
        ("list.column_unknown_path", "settings.operator.list.columns[2].path"),
        ("list.column_unknown_path", "settings.operator.list.columns[3].path"),
        ("list.column_format_unknown", "settings.operator.list.columns[4].format"),
        ("list.filter_unknown_path", "settings.operator.list.filters[0].path"),
        ("list.filter_unknown_path", "settings.operator.list.filters[1].path"),
        ("list.filter_unknown_path", "settings.operator.list.filters[2].path"),
    ]


def test_files_cannot_be_filtered_and_types_must_fit() -> None:
    settings = _settings(
        list={
            "filters": [
                {"source": "input", "path": "files", "type": "search"},
                {"source": "input", "path": "amount", "type": "search"},
                {"source": "input", "path": "signed_on", "type": "date_range"},
                {"source": "output", "path": "verdict", "type": "choice"},
                {"source": "job", "path": "status", "type": "choice_multi"},
                {"id": "status", "source": "job", "path": "status", "type": "boolean"},
            ]
        }
    )

    issues = operator_settings_issues(settings, _contract())
    assert [(issue.code, issue.path) for issue in issues] == [
        ("list.filter_on_file", "settings.operator.list.filters[0].path"),
        ("list.filter_type_incompatible", "settings.operator.list.filters[1].type"),
        ("list.filter_type_incompatible", "settings.operator.list.filters[5].type"),
    ]


def test_duplicate_column_and_filter_ids_are_errors() -> None:
    settings = _settings(
        list={
            "columns": [
                {"source": "output", "path": "total"},
                {"source": "output", "path": "total"},
            ],
            "filters": [
                {"id": "t", "source": "output", "path": "total", "type": "number_range"},
                {"id": "t", "source": "input", "path": "amount", "type": "number_range"},
            ],
        }
    )

    assert _codes(settings) == {"list.column_id_invalid", "list.filter_id_invalid"}


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("files", None),
        ("attachment", None),
        ("tags", None),
        ("", "submission.foreach_target_missing"),
        ("ghost", "submission.foreach_target_missing"),
        ("mail_password", "submission.foreach_target_missing"),
        ("amount", "submission.foreach_target_kind"),
        ("customer", "submission.foreach_target_kind"),
        ("pairs", "submission.foreach_min_items"),
    ],
)
def test_for_each_splits_a_list_or_a_file(target, code) -> None:
    settings = _settings(submission={"mode": "foreach", "target_input": target})

    assert _codes(settings) == ({code} if code else set())


# --- Publication -------------------------------------------------------------------------


def test_publishing_needs_the_ui_channel() -> None:
    settings = _settings(published=True)
    contract = _contract(channels=("api",))

    assert "operator.ui_channel_missing" in _codes(settings, contract)
    assert "operator.ui_channel_missing" not in _codes(_settings(published=False), contract)


def test_runtime_view_orders_the_form_and_leaves_secrets_out() -> None:
    contract = _contract()
    raw = {
        "published": True,
        "form": {
            "order": ["urgent", "doc_type"],
            "fields": {
                "doc_type": {"widget": "radio", "label": "Type"},
                "amount": {"widget": "textarea"},
            },
        },
        "list": {"filters": [{"source": "job", "path": "status", "type": "choice"}]},
    }

    _settings_model, view = operator_runtime_view(raw, contract)

    names = [field.name for field in view.form_fields]
    assert names[:2] == ["urgent", "doc_type"]
    assert "mail_password" not in names
    by_name = {field.name: field for field in view.form_fields}
    assert by_name["doc_type"].widget == "radio"
    assert by_name["doc_type"].options == ["contract", "invoice"]
    # An incompatible configured widget falls back to the default, with an issue.
    assert by_name["amount"].widget == "number"
    # A value the contract fixes is not part of what the form sends.
    assert by_name["ocr_model"].fixed is True
    assert by_name["ocr_model"].hidden is True
    assert [sub.name for sub in by_name["companies"].sub_fields] == ["name", "website", "email"]
    assert by_name["companies"].sub_fields[1].widget == "url"
    assert by_name["files"].min_items == 1
    assert view.filters[0].options[0] == "queued"
    assert view.runnable is False
    assert {issue.code for issue in view.issues} == {
        "operator.effects_not_acknowledged",
        "form.widget_incompatible",
    }
    assert view.title == "Contracts"


def test_an_unreadable_stored_configuration_blocks_the_form_but_stays_readable() -> None:
    _settings_model, view = operator_runtime_view({"published": "maybe?"}, _contract())

    assert view.published is False
    assert view.runnable is False
    assert view.issues[0].code == "operator.settings_invalid"
    assert view.form_fields


# --- Save: acknowledgement and refusal -------------------------------------------------------

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _workflow(operator: dict[str, Any] | None, *, inputs=None, channels=("ui", "api")) -> dict:
    settings: dict[str, Any] = {"callable": _callable(inputs, channels)}
    if operator is not None:
        settings["operator"] = operator
    return {"workflow_id": "wf.contracts", "revision": 7, "enabled": True, "settings": settings}


def _save(existing: dict, operator: dict | None, *, inputs=None, channels=("ui", "api")):
    final = copy.deepcopy(_workflow(operator, inputs=inputs, channels=channels))
    issues = prepare_operator_settings_for_save(
        existing=existing, final_payload=final, actor_id="admin-1", now=NOW
    )
    return final["settings"].get("operator"), issues


def _sha(inputs=None) -> str:
    return contract_fingerprint(WorkflowInterface.from_settings({"callable": _callable(inputs)}))


def test_confirm_acknowledges_the_saved_contract_on_the_server() -> None:
    stored, issues = _save(
        _workflow(None),
        {"published": True, "effects_acknowledged": {"confirm": True, "by": "forged"}},
    )

    assert stored["effects_acknowledged"] == {
        "by": "admin-1",
        "at": NOW.isoformat(),
        "revision": 8,
        "contract_sha": _sha(),
    }
    assert issues == []


def test_the_acknowledgement_survives_a_save_with_the_same_contract() -> None:
    ack = {
        "by": "admin-1",
        "at": "2026-11-01T00:00:00+00:00",
        "revision": 3,
        "contract_sha": _sha(),
    }
    existing = _workflow({"published": True, "effects_acknowledged": ack})

    stored, _issues = _save(
        existing, {"published": True, "menu_title": "Docs", "effects_acknowledged": dict(ack)}
    )

    assert stored["effects_acknowledged"] == ack
    assert stored["menu_title"] == "Docs"


def test_a_changed_contract_resets_the_acknowledgement() -> None:
    ack = {
        "by": "admin-1",
        "at": "2026-11-01T00:00:00+00:00",
        "revision": 3,
        "contract_sha": _sha(),
    }
    existing = _workflow({"published": True, "effects_acknowledged": ack})
    changed = [*INPUTS, {"name": "vat_rate", "type": "number"}]

    stored, issues = _save(
        existing, {"published": True, "effects_acknowledged": dict(ack)}, inputs=changed
    )

    assert stored["effects_acknowledged"] is None
    # Saving stays possible; the form is blocked until an admin confirms again.
    assert [issue.code for issue in issues] == ["operator.effects_not_acknowledged"]


def test_a_label_change_keeps_the_acknowledgement() -> None:
    ack = {"by": "a", "at": "t", "revision": 3, "contract_sha": _sha()}
    relabelled = copy.deepcopy(INPUTS)
    relabelled[0]["label"] = "Kind of document"
    existing = _workflow({"published": True, "effects_acknowledged": ack})

    stored, _issues = _save(
        existing, {"published": True, "effects_acknowledged": dict(ack)}, inputs=relabelled
    )

    assert stored["effects_acknowledged"] == ack


def test_a_caller_cannot_write_an_acknowledgement_it_did_not_confirm() -> None:
    forged = {"by": "admin-1", "at": "t", "revision": 7, "contract_sha": _sha()}

    stored, _issues = _save(_workflow(None), {"published": False, "effects_acknowledged": forged})

    assert stored["effects_acknowledged"] is None


def test_errors_refuse_a_publication_but_not_an_unpublished_save() -> None:
    broken = {"form": {"fields": {"amount": {"widget": "textarea"}}}}

    stored, issues = _save(_workflow(None), {**broken, "published": False})
    assert stored["form"]["fields"]["amount"]["widget"] == "textarea"
    assert [issue.code for issue in issues] == ["form.widget_incompatible"]

    with pytest.raises(OperatorSettingsRejected) as refused:
        _save(_workflow(None), {**broken, "published": True})
    assert [issue.code for issue in refused.value.issues] == ["form.widget_incompatible"]


def test_publishing_without_ui_is_refused() -> None:
    with pytest.raises(OperatorSettingsRejected) as refused:
        _save(_workflow(None), {"published": True}, channels=("api",))

    assert [issue.code for issue in refused.value.issues] == ["operator.ui_channel_missing"]


def test_malformed_settings_are_refused_even_unpublished() -> None:
    with pytest.raises(OperatorSettingsRejected) as refused:
        _save(_workflow(None), {"published": False, "colour": "red"})

    assert refused.value.issues[0].code == "operator.settings_invalid"


def test_a_save_without_operator_settings_is_untouched() -> None:
    final = {"settings": {"callable": _callable()}}

    assert (
        prepare_operator_settings_for_save(
            existing=_workflow(None), final_payload=final, actor_id="a"
        )
        == []
    )
    assert "operator" not in final["settings"]


# --- The workflow save route ---------------------------------------------------------------


def _route_client(monkeypatch, db):
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.application.management.scope import DEFAULT_ACCOUNT_ID, normalize_scope
    from core.entrypoints.admin import workflows as workflow_routes
    from core.entrypoints.admin import workflows_routes_write as write_routes
    from core.entrypoints.admin import workflows_scope

    registry = MagicMock()
    monkeypatch.setattr(write_routes, "_get_db", lambda *_a, **_k: db)
    monkeypatch.setattr(write_routes, "_get_registry", lambda *_a, **_k: registry)
    monkeypatch.setattr(workflows_scope, "_get_registry", lambda *_a, **_k: registry)
    monkeypatch.setattr(
        write_routes,
        "_get_workflow_validation",
        lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
    )
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin-7", principal_type="admin"
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stored_workflow():
    from unittest.mock import MagicMock

    db = MagicMock()
    db.get_workflow.return_value = {
        "workflow_id": "orders",
        "revision": 3,
        "enabled": True,
        "settings": {"callable": _callable()},
    }
    db.save_workflow.return_value = 4
    return db


def _put(client, operator: dict[str, Any]):
    return client.put(
        "/api/workflows/orders",
        json={
            "workflow_id": "orders",
            "display_name": "Orders",
            "enabled": True,
            "settings": {"callable": _callable(), "operator": operator},
            "expected_revision": 3,
        },
    )


def test_the_save_route_refuses_a_broken_publication(monkeypatch, stored_workflow) -> None:
    response = _put(
        _route_client(monkeypatch, stored_workflow),
        {"published": True, "form": {"fields": {"amount": {"widget": "textarea"}}}},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "workflow_operator_settings_invalid"
    assert detail["issues"][0]["path"] == "settings.operator.form.fields.amount.widget"
    stored_workflow.save_workflow.assert_not_called()


def test_the_save_route_stamps_the_acknowledgement_and_returns_issues(
    monkeypatch, stored_workflow
) -> None:
    response = _put(
        _route_client(monkeypatch, stored_workflow),
        {
            "published": True,
            "effects_acknowledged": {"confirm": True},
            "result": {"fields": {"ghost": {"display": "text"}}},
            "list": {"columns": [{"source": "output", "path": "total"}]},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["issues"][0]["code"] == "result.field_unknown"

    response = _put(
        _route_client(monkeypatch, stored_workflow),
        {
            "published": True,
            "effects_acknowledged": {"confirm": True},
            "list": {"columns": [{"source": "output", "path": "total"}]},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["operator_issues"] == []
    saved = stored_workflow.save_workflow.call_args.args[0]["settings"]["operator"]
    assert saved["effects_acknowledged"]["by"] == "admin-7"
    assert saved["effects_acknowledged"]["revision"] == 4
    assert saved["list"]["columns"][0]["id"] == "output.total"
