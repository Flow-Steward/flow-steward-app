"""Declared field shapes of the workflow contract (ADR 0075, item 6).

``items``, ``properties``, ``min_items`` / ``max_items`` and ``format`` are
optional and checked only where declared; a released contract without them
validates exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.application.registry.workflow_contract import (
    WorkflowContractField,
    WorkflowInterface,
    interface_from_definition,
)
from core.application.registry.workflow_contract_service import (
    WorkflowInterfaceService,
    coerce_contract_value,
)
from core.application.registry.workflow_contract_shape import MAX_FIELD_DEPTH
from core.application.workflows.workflow_portability_import import _workflow_definition_issues

_REPO = Path(__file__).resolve().parents[2]
_SHAPE_CODES = {
    "workflow.interface.format_unknown",
    "workflow.interface.format_type_mismatch",
    "workflow.interface.item_bound_invalid",
    "workflow.interface.item_bound_not_array",
    "workflow.interface.item_bounds_inverted",
    "workflow.interface.items_not_array",
    "workflow.interface.items_invalid",
    "workflow.interface.properties_not_object",
    "workflow.interface.properties_invalid",
    "workflow.interface.nesting_too_deep",
    "workflow.interface.nested_secret_forbidden",
}


def _callable(inputs, outputs=None):
    return {
        "callable": {
            "enabled": True,
            "trigger_channels": ["ui", "api", "mcp", "parent_workflow"],
            "strict_inputs": True,
            "expected_inputs": list(inputs),
            "expected_outputs": list(outputs or []),
        }
    }


def _interface(*inputs, outputs=None):
    return WorkflowInterface.from_settings(_callable(inputs, outputs))


def _codes(interface):
    return [issue["code"] for issue in interface.definition_issues()]


def _validate(interface, payload, channel="api"):
    return WorkflowInterfaceService().validate_inputs(interface, payload, channel=channel)


# --- Compatibility -----------------------------------------------------------

_RELEASED_INPUTS = [
    {"name": "brief", "type": "text", "required": True},
    {"name": "tags", "type": "list"},
    {"name": "payload", "type": "json"},
    {"name": "rows", "type": "array", "default": '[{"a": 1}]'},
    {"name": "config", "type": "object", "default": '{"region": "eu"}'},
    {"name": "doc", "type": "file"},
    {"name": "level", "type": "select", "options": "high, low"},
    {"name": "when", "type": "string"},
]


def test_released_contract_without_shape_keys_has_no_issues_and_dumps_unchanged():
    interface = _interface(*_RELEASED_INPUTS)

    assert _codes(interface) == []
    dumped = interface.dump()["expected_inputs"]
    for row in dumped:
        assert not {"items", "properties", "min_items", "max_items", "format"} & set(row)
    assert all(not field.declares_shape for field in interface.expected_inputs)


@pytest.mark.parametrize("channel", ["ui", "api", "mcp"])
def test_released_contract_accepts_prior_data_including_json_strings(channel):
    interface = _interface(
        {"name": "tags", "type": "array", "required": True},
        {"name": "payload", "type": "object", "required": True},
        {"name": "when", "type": "string"},
        {"name": "doc", "type": "file"},
    )

    result = _validate(
        interface,
        {
            "tags": '["a", 2, {"b": true}]',
            "payload": '{"nested": {"x": [1]}}',
            "when": "not a date at all",
            "doc": {"artifact_id": "art-1"},
        },
        channel=channel,
    )

    assert result.ok, result.errors
    assert result.inputs["tags"] == ["a", 2, {"b": True}]
    assert result.inputs["payload"] == {"nested": {"x": [1]}}
    assert result.inputs["when"] == "not a date at all"


def test_released_defaults_as_json_strings_stay_valid_and_resolve():
    interface = _interface(
        {"name": "rows", "type": "array", "default": "[1, 2]"},
        {"name": "config", "type": "object", "default": '{"region": "eu"}'},
    )

    assert _codes(interface) == []
    result = _validate(interface, {})
    assert result.ok, result.errors
    assert result.inputs == {"rows": [1, 2], "config": {"region": "eu"}}


def test_released_json_schema_is_unchanged_without_shape_keys():
    interface = _interface(
        {"name": "tags", "type": "array"},
        {"name": "payload", "type": "object"},
        {"name": "notes", "type": "paragraph"},
    )

    schema = WorkflowInterfaceService().input_json_schema(interface)["properties"]

    assert schema["tags"] == {"type": "array", "items": {}}
    assert schema["payload"] == {"type": "object", "additionalProperties": True}
    assert schema["notes"] == {"type": "string", "format": "textarea"}


def test_import_of_released_definition_yields_no_new_issues():
    workflow = {
        "workflow_id": "released_wf",
        "display_name": "Released",
        "steps": [],
        "settings": _callable(
            _RELEASED_INPUTS,
            outputs=[{"name": "summary", "type": "object", "required": True}],
        ),
    }

    assert _workflow_definition_issues(workflow, workflow_path="workflows[0]") == []
    assert interface_from_definition(workflow).definition_issues() == []


def _callable_settings(node):
    if isinstance(node, dict):
        settings = node.get("settings")
        if isinstance(settings, dict) and isinstance(settings.get("callable"), dict):
            yield node
        for value in node.values():
            yield from _callable_settings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _callable_settings(value)


def test_repository_workflow_examples_raise_no_shape_issues():
    root = _REPO / "docs" / "agent-skills"
    definitions = []
    for path in sorted(root.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "expected_inputs" not in text:
            continue
        for document in yaml.safe_load_all(text):
            definitions.extend(_callable_settings(document))

    assert definitions
    for definition in definitions:
        codes = {
            issue["code"] for issue in interface_from_definition(definition).definition_issues()
        }
        assert not codes & _SHAPE_CODES


# --- Arrays ------------------------------------------------------------------


def _file_list(min_items=None, max_items=None):
    row = {"name": "photos", "type": "array", "required": True, "items": {"type": "file"}}
    if min_items is not None:
        row["min_items"] = min_items
    if max_items is not None:
        row["max_items"] = max_items
    return row


def test_file_array_passes_within_bounds_and_fails_outside():
    interface = _interface(_file_list(min_items=1, max_items=3))
    handle = {"artifact_id": "a"}

    assert _codes(interface) == []
    assert _validate(interface, {"photos": [handle, handle]}).ok
    too_few = _validate(interface, {"photos": []})
    too_many = _validate(interface, {"photos": [handle] * 4})
    empty_element = _validate(interface, {"photos": [handle, ""]})

    assert not too_few.ok and "at least 1" in too_few.errors[0]["message"]
    assert not too_many.ok and "at most 3" in too_many.errors[0]["message"]
    assert not empty_element.ok and "photos[1]" in empty_element.errors[0]["message"]


def test_select_array_checks_elements_and_bounds_and_accepts_json_string():
    interface = _interface(
        {
            "name": "kinds",
            "type": "array",
            "min_items": "1",
            "max_items": 2,
            "items": {"type": "select", "options": "contract, invoice, receipt"},
        }
    )

    assert _codes(interface) == []
    assert interface.expected_inputs[0].min_items == 1
    ok = _validate(interface, {"kinds": '["contract", "invoice"]'})
    assert ok.ok, ok.errors
    assert ok.inputs["kinds"] == ["contract", "invoice"]
    assert not _validate(interface, {"kinds": ["contract", "memo"]}).ok
    assert not _validate(interface, {"kinds": ["contract", "invoice", "receipt"]}).ok
    assert not _validate(interface, {"kinds": []}).ok


def test_object_array_checks_bounds_and_element_properties():
    interface = _interface(
        {
            "name": "lines",
            "type": "array",
            "max_items": 2,
            "items": {
                "type": "object",
                "properties": [
                    {"name": "item", "type": "string", "required": True},
                    {"name": "qty", "type": "number"},
                ],
            },
        }
    )

    assert _codes(interface) == []
    result = _validate(interface, {"lines": [{"item": "A", "qty": "2", "note": "kept"}]})
    assert result.ok, result.errors
    assert result.inputs["lines"] == [{"item": "A", "qty": 2, "note": "kept"}]
    missing = _validate(interface, {"lines": [{"qty": 1}]})
    assert not missing.ok and "lines[0].item" in missing.errors[0]["message"]
    wrong_type = _validate(interface, {"lines": [{"item": "A", "qty": "many"}]})
    assert not wrong_type.ok and "lines[0].qty" in wrong_type.errors[0]["message"]
    assert not _validate(interface, {"lines": [{"item": "A"}] * 3}).ok


# --- Objects -----------------------------------------------------------------


def test_nested_object_properties_validate_and_accept_a_json_string():
    interface = _interface(
        {
            "name": "company",
            "type": "object",
            "required": True,
            "properties": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "string", "format": "email"},
                {
                    "name": "address",
                    "type": "object",
                    "properties": [{"name": "city", "type": "string", "required": True}],
                },
            ],
        }
    )

    assert _codes(interface) == []
    ok = _validate(interface, {"company": '{"name": "Acme", "address": {"city": "Vilnius"}}'})
    assert ok.ok, ok.errors
    nested_missing = _validate(interface, {"company": {"name": "Acme", "address": {}}})
    assert not nested_missing.ok
    assert "company.address.city" in nested_missing.errors[0]["message"]
    bad_email = _validate(interface, {"company": {"name": "Acme", "email": "nope"}})
    assert not bad_email.ok and "company.email" in bad_email.errors[0]["message"]


def test_object_properties_shape_outputs_too():
    interface = _interface(
        outputs=[
            {
                "name": "verdict",
                "type": "object",
                "required": True,
                "properties": [{"name": "score", "type": "number", "required": True}],
            }
        ]
    )
    service = WorkflowInterfaceService()

    assert service.validate_outputs(interface, {"outputs": {"verdict": {"score": "7"}}}).ok
    assert not service.validate_outputs(interface, {"outputs": {"verdict": {}}}).ok


# --- Formats -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_format", "good", "bad"),
    [
        ("date", "2026-11-19", "19.11.2026"),
        ("date", "2024-02-29", "2026-02-30"),
        ("date-time", "2026-11-19T10:30", "2026-11-19"),
        ("date-time", "2026-11-19T10:30:00Z", "2026-11-19T25:00"),
        ("date-time", "2026-11-19 10:30:00+02:00", "tomorrow"),
        ("time", "10:30", "10.30"),
        ("time", "23:59:59", "24:00"),
        ("email", "ops@example.com", "ops@example"),
        ("email", "a.b@mail.example.lt", "two@@example.com"),
        ("uri", "https://example.com/path", "https://"),
        ("uri", "mailto:ops@example.com", "example.com"),
        # A malformed authority makes urlsplit raise; it is a field error, not a crash.
        ("uri", "http://[::1]:8080/x", "http://[::1"),
        ("uri", "https://example.com", "https://[bad/path"),
    ],
)
def test_declared_format_is_checked(field_format, good, bad):
    field = WorkflowContractField(name="value", type="string", format=field_format)

    assert coerce_contract_value(good, field)[0] is True
    ok, _, message = coerce_contract_value(bad, field)
    assert ok is False
    assert "value" in message


def test_a_malformed_uri_is_a_validation_error_not_an_exception():
    interface = _interface({"name": "site", "type": "string", "format": "uri"})

    result = _validate(interface, {"site": "http://[::1"})

    assert not result.ok


def test_format_is_checked_only_when_declared_and_empty_optional_passes():
    interface = _interface(
        {"name": "plain", "type": "string"},
        {"name": "due", "type": "string", "format": "date"},
    )

    assert _validate(interface, {"plain": "19.11.2026", "due": ""}).ok
    assert not _validate(interface, {"due": "19.11.2026"}).ok


def test_paragraph_format_overrides_textarea_hint_in_json_schema():
    interface = _interface({"name": "when", "type": "paragraph", "format": "Date-Time"})

    assert interface.expected_inputs[0].format == "date-time"
    schema = WorkflowInterfaceService().input_json_schema(interface)
    assert schema["properties"]["when"]["format"] == "date-time"


# --- JSON Schema -------------------------------------------------------------


def test_json_schema_carries_items_properties_bounds_and_format():
    interface = _interface(
        _file_list(min_items=1, max_items=4),
        {"name": "kinds", "type": "array", "items": {"type": "select", "options": ["a", "b"]}},
        {"name": "due", "type": "string", "format": "date"},
        {
            "name": "company",
            "type": "object",
            "properties": [
                {"name": "name", "type": "string", "required": True},
                {"name": "site", "type": "string", "format": "uri"},
            ],
        },
    )
    service = WorkflowInterfaceService()

    properties = service.input_json_schema(interface)["properties"]

    assert properties["photos"] == {
        "type": "array",
        "items": {"type": "object", "additionalProperties": True, "x-fs-type": "file"},
        "minItems": 1,
        "maxItems": 4,
    }
    assert properties["kinds"]["items"] == {"type": "string", "enum": ["a", "b"]}
    assert properties["due"] == {"type": "string", "format": "date"}
    assert properties["company"] == {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "name": {"type": "string"},
            "site": {"type": "string", "format": "uri"},
        },
        "required": ["name"],
    }
    public = service.public_contract_payload(interface)
    assert public["expected_inputs"][0]["items"] == {"type": "file"}
    assert public["expected_inputs"][0]["min_items"] == 1


def test_output_json_schema_carries_shape():
    interface = _interface(
        outputs=[
            {
                "name": "lines",
                "type": "array",
                "items": {"type": "object", "properties": [{"name": "qty", "type": "number"}]},
            }
        ]
    )

    schema = WorkflowInterfaceService().output_json_schema(interface)

    assert schema["properties"]["lines"]["items"]["properties"] == {"qty": {"type": "number"}}


# --- Definition issues -------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "code", "path_suffix"),
    [
        ({"type": "string", "items": {"type": "string"}}, "items_not_array", "items"),
        ({"type": "array", "items": "file"}, "items_invalid", "items"),
        ({"type": "array", "items": {"type": "secret"}}, "nested_secret_forbidden", "items.type"),
        ({"type": "array", "items": {"type": "array"}}, "invalid_type", "items.type"),
        ({"type": "array", "items": {"type": "select"}}, "select_options_missing", "items.options"),
        ({"type": "string", "min_items": 1}, "item_bound_not_array", "min_items"),
        ({"type": "array", "max_items": -1}, "item_bound_invalid", "max_items"),
        ({"type": "array", "min_items": "many"}, "item_bound_invalid", "min_items"),
        ({"type": "array", "min_items": 3, "max_items": 1}, "item_bounds_inverted", "min_items"),
        ({"type": "string", "format": "phone"}, "format_unknown", "format"),
        ({"type": "number", "format": "date"}, "format_type_mismatch", "format"),
        ({"type": "array", "properties": []}, "properties_not_object", "properties"),
        ({"type": "object", "properties": {"a": {}}}, "properties_invalid", "properties"),
        (
            {"type": "object", "properties": [{"name": "token", "type": "secret"}]},
            "nested_secret_forbidden",
            "properties[0].type",
        ),
        (
            {"type": "object", "properties": [{"name": "a"}, {"name": "a"}]},
            "duplicate_name",
            "properties[1].name",
        ),
        (
            {"type": "object", "properties": [{"name": "a b"}]},
            "name_whitespace",
            "properties[0].name",
        ),
        (
            {"type": "object", "properties": [{"name": "d", "format": "nope"}]},
            "format_unknown",
            "properties[0].format",
        ),
    ],
)
def test_invalid_shape_descriptions_are_issues(row, code, path_suffix):
    interface = _interface({"name": "field", **row})

    issues = interface.definition_issues()

    assert any(
        issue["code"] == f"workflow.interface.{code}"
        and issue["path"] == f"workflow.callable.expected_inputs[0].{path_suffix}"
        for issue in issues
    ), issues
    # A run cannot start with an invalid contract.
    assert not _validate(interface, {}).ok


def test_nesting_is_bounded():
    def nested(depth):
        if depth == 1:
            return {"name": "leaf", "type": "string"}
        return {"name": f"level{depth}", "type": "object", "properties": [nested(depth - 1)]}

    allowed = _interface({**nested(MAX_FIELD_DEPTH), "name": "root"})
    too_deep = _interface({**nested(MAX_FIELD_DEPTH + 1), "name": "root"})

    assert _codes(allowed) == []
    assert "workflow.interface.nesting_too_deep" in _codes(too_deep)


def test_configured_default_must_fit_the_declared_shape():
    fits = _interface({"name": "tags", "type": "array", "max_items": 2, "default": "[1]"})
    too_long = _interface({"name": "tags", "type": "array", "max_items": 2, "default": [1, 2, 3]})

    assert _codes(fits) == []
    assert "workflow.interface.default_invalid" in _codes(too_long)


def test_malformed_shape_is_kept_as_written():
    interface = _interface({"name": "field", "type": "object", "properties": {"a": {}}})

    assert interface.dump()["expected_inputs"][0]["properties"] == {"a": {}}
