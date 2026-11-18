"""Project user sections (ADR 0075): configuration validated against the contract."""

from __future__ import annotations

import pytest

from core.application.projects.user_sections import (
    ProjectUserSection,
    SectionContract,
    field_kind,
    parse_sections,
    resolve_schema_path,
    resolve_sections,
    section_briefs,
    validate_sections,
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "receipt": {"type": "object", "x-fs-type": "file"},
        "note": {"type": "string", "format": "textarea"},
        "currency": {"type": "string", "enum": ["EUR", "USD"]},
        "amount": {"type": "number"},
        "urgent": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "pair": {"type": "array", "items": {"type": "string"}, "minItems": 2},
        "scans": {"type": "array", "items": {"type": "object", "x-fs-type": "file"}},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "qty": {"type": "number"}},
            },
        },
        "free": {"type": "object", "additionalProperties": True},
        "nullable": {"type": ["string", "null"]},
        "loose": {"type": "array", "items": {}},
        "api_key": {},
    },
    "required": ["receipt"],
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"supplier": {"type": "string"}, "total": {"type": "number"}},
}


def _contract(**overrides) -> SectionContract:
    fields = {
        "workflow_id": "wf.receipts",
        "name": "Receipts",
        "revision": 3,
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "secret_inputs": ["api_key"],
    }
    fields.update(overrides)
    return SectionContract(**fields)


def _section(**overrides) -> ProjectUserSection:
    fields = {
        "id": "receipts",
        "title": "Receipts",
        "workflow_id": "wf.receipts",
        "fields": [{"path": "receipt"}, {"path": "note"}],
        "columns": [
            {"id": "status", "source": "job", "path": "status"},
            {"id": "supplier", "source": "output", "path": "supplier"},
        ],
        "submission": {"mode": "foreach", "target_input": "receipt"},
        "effects_acknowledged": True,
    }
    fields.update(overrides)
    return ProjectUserSection.model_validate(fields)


def _codes(section: ProjectUserSection, contract: SectionContract | None = None) -> set[str]:
    loaded = contract or _contract()
    return {issue.code for issue in validate_sections([section], load_contract=lambda _id: loaded)}


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("receipt", "file"),
        ("note", "text_long"),
        ("currency", "enum"),
        ("amount", "number"),
        ("urgent", "boolean"),
        ("tags", "string_list"),
        ("scans", "file_list"),
        ("lines", "object_list"),
        ("lines[].sku", "text"),
        ("free", ""),
        ("nullable", ""),
        ("loose", ""),
        ("api_key", ""),
    ],
)
def test_field_kind_follows_the_described_schema(path, kind) -> None:
    assert field_kind(resolve_schema_path(INPUT_SCHEMA, path)) == kind


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["string", "null"]},
        {"type": ["number", "string"]},
        {"type": {"oops": True}},
        {"anyOf": [{"type": "string"}, {"type": "number"}]},
        {"type": "array", "items": {"type": ["string", "null"]}},
        {"type": "array", "items": "string"},
        {"type": "object", "properties": "not-a-map"},
    ],
)
def test_unsupported_schema_shapes_are_unsupported_not_errors(schema) -> None:
    assert field_kind(schema) == ""


def test_a_type_union_is_a_clear_field_error() -> None:
    issues = validate_sections(
        [_section(fields=[{"path": "receipt"}, {"path": "nullable"}])],
        load_contract=lambda _id: _contract(),
    )

    union = [issue for issue in issues if issue.code == "field_unsupported"]
    assert union and "union" in union[0].message
    assert union[0].path == "user_sections[0].fields[1]"


def test_a_stored_section_on_a_union_still_reads() -> None:
    views = resolve_sections(
        [_section(fields=[{"path": "receipt"}, {"path": "nullable"}])],
        load_contract=lambda _id: _contract(),
        include_hidden=False,
    )

    assert len(views) == 1
    assert views[0].runtime.runnable is False
    assert "field_unsupported" in {issue.code for issue in views[0].runtime.issues}


def test_a_valid_receipts_section_has_no_issues() -> None:
    assert _codes(_section()) == set()


@pytest.mark.parametrize("visible", [True, False])
def test_a_list_of_files_in_one_input_is_a_validation_error_never_a_crash(visible) -> None:
    section = _section(
        fields=[{"path": "receipt"}, {"path": "scans", "visible": visible, "widget": "file"}]
    )

    assert "field_unsupported" in _codes(section)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ([{"path": "receipt"}, {"path": "missing"}], "field_unknown"),
        ([{"path": "receipt"}, {"path": "api_key"}], "field_secret"),
        ([{"path": "receipt", "visible": False}], "field_required_hidden"),
        ([{"path": "note"}], "field_required_missing"),
        ([{"path": "receipt"}, {"path": "amount", "widget": "checkbox"}], "field_widget"),
        ([{"path": "receipt"}, {"path": "free"}], "field_unsupported"),
        ([{"path": "receipt"}, {"path": "receipt"}], "field_duplicate"),
    ],
)
def test_field_configuration_errors(fields, code) -> None:
    assert code in _codes(_section(fields=fields))


@pytest.mark.parametrize(
    ("column", "code"),
    [
        ({"id": "x", "source": "output", "path": "supplier.name"}, "column_unknown"),
        ({"id": "x", "source": "output", "path": "unknown"}, "column_unknown"),
        ({"id": "x", "source": "input", "path": "api_key"}, "column_unknown"),
        ({"id": "x", "source": "job", "path": "meta"}, "column_unknown"),
        ({"id": "x", "source": "job", "path": "status", "format": "html"}, "column_format"),
    ],
)
def test_column_configuration_errors(column, code) -> None:
    assert code in _codes(_section(columns=[column]))


def test_nested_input_columns_follow_the_described_structure() -> None:
    column = {"id": "sku", "source": "input", "path": "lines[].sku", "format": "list"}

    assert _codes(_section(columns=[column])) == set()


@pytest.mark.parametrize(
    ("submission", "code"),
    [
        ({"mode": "foreach", "target_input": ""}, "foreach_target"),
        ({"mode": "foreach", "target_input": "lines[].sku"}, "foreach_target"),
        ({"mode": "foreach", "target_input": "amount"}, "foreach_target_kind"),
        ({"mode": "foreach", "target_input": "pair"}, "foreach_min_items"),
        ({"mode": "bulk"}, "bulk_file_collection_unsupported"),
    ],
)
def test_submission_configuration_errors(submission, code) -> None:
    assert code in _codes(_section(submission=submission))


def test_foreach_over_a_described_list_is_allowed() -> None:
    section = _section(
        fields=[{"path": "receipt"}, {"path": "tags"}],
        submission={"mode": "foreach", "target_input": "tags"},
    )

    assert _codes(section) == set()


def test_duplicate_section_ids_are_refused() -> None:
    issues = validate_sections([_section(), _section()], load_contract=lambda _id: _contract())

    assert "section_duplicate" in {issue.code for issue in issues}


def test_unknown_keys_and_bad_ids_are_refused() -> None:
    with pytest.raises(ValueError):
        parse_sections([{"id": "Receipts!", "title": "R", "workflow_id": "wf"}])
    with pytest.raises(ValueError):
        parse_sections([{"id": "r", "title": "R", "workflow_id": "wf", "type": "string"}])


@pytest.mark.parametrize(
    ("contract", "code"),
    [
        ({"linked": False}, "workflow_unlinked"),
        ({"enabled": False}, "workflow_disabled"),
        ({"ui_channel": False}, "workflow_ui_channel"),
        ({"found": False}, "workflow_missing"),
    ],
)
def test_an_unavailable_workflow_keeps_the_section_readable(contract, code) -> None:
    views = resolve_sections(
        [_section()], load_contract=lambda _id: _contract(**contract), include_hidden=False
    )

    assert len(views) == 1
    assert views[0].runtime.runnable is False
    assert code in {issue.code for issue in views[0].runtime.issues}


def test_a_section_runs_only_after_the_admin_acknowledged_real_effects() -> None:
    views = resolve_sections(
        [_section(effects_acknowledged=False), _section(id="other")],
        load_contract=lambda _id: _contract(),
        include_hidden=False,
    )

    assert [view.runtime.runnable for view in views] == [False, True]


def test_hidden_sections_are_left_out_for_the_operator_and_viewer() -> None:
    sections = [_section(), _section(id="draft", visible=False)]

    for_users = resolve_sections(
        sections, load_contract=lambda _id: _contract(), include_hidden=False
    )
    for_admin = resolve_sections(
        sections, load_contract=lambda _id: _contract(), include_hidden=True
    )

    assert [view.id for view in for_users] == ["receipts"]
    assert {view.id for view in for_admin} == {"receipts", "draft"}


def test_navigation_keeps_an_unlinked_section_and_respects_the_scope() -> None:
    config = {
        "user_sections": [
            _section().model_dump(),
            _section(id="invoices", title="Invoices", workflow_id="wf.invoices").model_dump(),
            _section(id="draft", visible=False).model_dump(),
            {"id": "broken!"},
        ]
    }

    everything = section_briefs(config)
    scoped = section_briefs(config, allowed_workflow_ids=["wf.invoices"])

    assert [brief.id for brief in everything] == ["invoices", "receipts"]
    assert [brief.id for brief in scoped] == ["invoices"]
