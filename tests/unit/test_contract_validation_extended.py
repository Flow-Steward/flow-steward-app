"""
Unit tests for registry/contract_validation.py — contract validation logic.

Tests:
- Contract item validation (name, type, required, duplicates)
- Helper functions (_normalize_name, _normalize_type, _to_object)
- Callable workflow contract extraction
- Allowed contract types
- Issue generation

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_contract_validation_extended.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Tests: validate_contract_items
# ---------------------------------------------------------------------------


class TestValidateContractItems:
    """Contract item validation."""

    def test_none_items_returns_empty(self):
        from core.application.registry.contract_validation import validate_contract_items

        normalized, issues = validate_contract_items(
            None, path_prefix="test", default_required=False
        )
        assert normalized == []
        assert issues == []

    def test_non_list_items_returns_issue(self):
        from core.application.registry.contract_validation import validate_contract_items

        _normalized, issues = validate_contract_items(
            "not a list", path_prefix="test", default_required=False
        )
        assert len(issues) == 1
        assert issues[0]["code"] == "contract.items_not_list"

    def test_valid_item_normalizes(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": "output", "type": "text", "description": "Output text"}]
        normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert len(normalized) == 1
        assert normalized[0]["name"] == "output"
        assert normalized[0]["type"] == "paragraph"
        assert issues == []

    def test_empty_name_rejected(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": "", "type": "text"}]
        _normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert len(issues) == 1
        assert issues[0]["code"] == "contract.name_required"

    def test_name_with_whitespace_rejected(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": "output text", "type": "text"}]
        _normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert len(issues) == 1
        assert issues[0]["code"] == "contract.name_whitespace"

    def test_duplicate_name_rejected(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [
            {"name": "output", "type": "text"},
            {"name": "output", "type": "json"},
        ]
        _normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert any(i["code"] == "contract.duplicate_name" for i in issues)

    def test_invalid_type_rejected(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": "output", "type": "xml"}]
        _normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert len(issues) == 1
        assert issues[0]["code"] == "contract.invalid_type"

    @pytest.mark.parametrize("valid_type", ["text", "json", "list", "number", "boolean"])
    def test_all_valid_types_accepted(self, valid_type):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": f"field_{valid_type}", "type": valid_type}]
        normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert len(normalized) == 1
        assert issues == []

    def test_non_dict_item_rejected(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = ["not a dict"]
        _normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert len(issues) == 1
        assert issues[0]["code"] == "contract.item_not_object"

    def test_default_required_applied(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": "output", "type": "text"}]
        normalized, _issues = validate_contract_items(
            items, path_prefix="test", default_required=True
        )
        assert normalized[0]["required"] is True

    def test_explicit_required_overrides_default(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": "output", "type": "text", "required": False}]
        normalized, _issues = validate_contract_items(
            items, path_prefix="test", default_required=True
        )
        assert normalized[0]["required"] is False

    def test_contract_id_on_non_json_type_rejected(self):
        from core.application.registry.contract_validation import validate_contract_items

        items = [{"name": "output", "type": "text", "contract_id": "my_contract"}]
        _normalized, issues = validate_contract_items(
            items, path_prefix="test", default_required=False
        )
        assert any(i["code"] == "contract.contract_id_invalid_type" for i in issues)

    def test_contract_id_on_json_type_accepted(self):
        from core.application.registry.contract_validation import validate_contract_items

        mock_db = MagicMock()
        mock_db.get_data_contract.return_value = {"id": "my_contract", "fields": []}
        items = [{"name": "output", "type": "json", "contract_id": "my_contract"}]
        normalized, _issues = validate_contract_items(
            items, path_prefix="test", default_required=False, db=mock_db
        )
        assert len(normalized) == 1
        assert normalized[0].get("contract_id") == "my_contract"


# ---------------------------------------------------------------------------
# Tests: _normalize_name, _normalize_type
# ---------------------------------------------------------------------------


class TestNormalizeFunctions:
    """Name and type normalization."""

    def test_normalize_name_strips_whitespace(self):
        from core.application.registry.contract_validation import _normalize_name

        assert _normalize_name("  output  ") == "output"

    def test_normalize_name_none_returns_empty(self):
        from core.application.registry.contract_validation import _normalize_name

        assert _normalize_name(None) == ""

    def test_normalize_type_lowercase(self):
        from core.application.registry.contract_validation import _normalize_type

        assert _normalize_type("JSON") == "object"
        assert _normalize_type("Text") == "paragraph"

    def test_normalize_type_none_defaults_to_text(self):
        from core.application.registry.contract_validation import _normalize_type

        assert _normalize_type(None) == "string"

    def test_normalize_type_empty_defaults_to_text(self):
        from core.application.registry.contract_validation import _normalize_type

        assert _normalize_type("") == "string"


# ---------------------------------------------------------------------------
# Tests: _to_object
# ---------------------------------------------------------------------------


class TestToObject:
    """JSON string to dict conversion."""

    def test_dict_passthrough(self):
        from core.application.registry.contract_validation import _to_object

        assert _to_object({"key": "value"}) == {"key": "value"}

    def test_json_string_parsed(self):
        from core.application.registry.contract_validation import _to_object

        assert _to_object('{"key": "value"}') == {"key": "value"}

    def test_invalid_json_returns_empty(self):
        from core.application.registry.contract_validation import _to_object

        assert _to_object("not json") == {}

    def test_none_returns_empty(self):
        from core.application.registry.contract_validation import _to_object

        assert _to_object(None) == {}

    def test_list_json_returns_empty(self):
        from core.application.registry.contract_validation import _to_object

        assert _to_object("[1, 2, 3]") == {}

    def test_int_returns_empty(self):
        from core.application.registry.contract_validation import _to_object

        assert _to_object(42) == {}


# ---------------------------------------------------------------------------
# Tests: _extract_workflow_callable_contract
# ---------------------------------------------------------------------------


class TestExtractWorkflowCallableContract:
    """Callable workflow contract extraction."""

    def test_boolean_callable_is_not_supported(self):
        from core.application.registry.contract_validation import (
            _extract_workflow_callable_contract,
        )

        enabled, inputs, outputs = _extract_workflow_callable_contract(
            {"callable": True, "callable_expected_inputs": [{"name": "x"}]}
        )
        assert enabled is True
        assert inputs == []
        assert outputs == []

    def test_object_callable(self):
        from core.application.registry.contract_validation import (
            _extract_workflow_callable_contract,
        )

        enabled, inputs, outputs = _extract_workflow_callable_contract(
            {
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "y"}],
                    "expected_outputs": [{"name": "z"}],
                }
            }
        )
        assert enabled is True
        assert inputs == [{"name": "y", "type": "string", "required": False}]
        assert outputs == [{"name": "z", "type": "string", "required": False}]

    def test_callable_false_is_not_a_contract(self):
        from core.application.registry.contract_validation import (
            _extract_workflow_callable_contract,
        )

        enabled, inputs, outputs = _extract_workflow_callable_contract({"callable": False})
        assert enabled is False
        assert inputs == []
        assert outputs == []

    def test_no_callable_key(self):
        from core.application.registry.contract_validation import (
            _extract_workflow_callable_contract,
        )

        enabled, _inputs, _outputs = _extract_workflow_callable_contract({})
        assert enabled is False

    def test_string_settings_parsed(self):
        import json

        from core.application.registry.contract_validation import (
            _extract_workflow_callable_contract,
        )

        settings = json.dumps({"callable": True})
        enabled, inputs, outputs = _extract_workflow_callable_contract(settings)
        assert enabled is True
        assert inputs == []
        assert outputs == []


# ---------------------------------------------------------------------------
# Tests: _issue helper
# ---------------------------------------------------------------------------


class TestIssueHelper:
    """Issue dict generation."""

    def test_issue_has_required_fields(self):
        from core.application.registry.contract_validation import _issue

        result = _issue(code="test.error", message="Something went wrong", path="field.name")
        assert result["code"] == "test.error"
        assert result["message"] == "Something went wrong"
        assert result["path"] == "field.name"
        assert result["severity"] == "error"

    def test_issue_default_severity_is_error(self):
        from core.application.registry.contract_validation import _issue

        result = _issue(code="c", message="m")
        assert result["severity"] == "error"

    def test_issue_custom_severity(self):
        from core.application.registry.contract_validation import _issue

        result = _issue(code="c", message="m", severity="warning")
        assert result["severity"] == "warning"

    def test_issue_strips_whitespace(self):
        from core.application.registry.contract_validation import _issue

        result = _issue(code="  c  ", message="  m  ", path="  p  ")
        assert result["code"] == "c"
        assert result["message"] == "m"
        assert result["path"] == "p"


# ---------------------------------------------------------------------------
# Tests: ALLOWED_CONTRACT_TYPES constant
# ---------------------------------------------------------------------------


class TestAllowedContractTypes:
    """Verify the allowed types constant."""

    def test_expected_types_present(self):
        from core.application.registry.contract_validation import ALLOWED_CONTRACT_TYPES

        expected = {
            "string",
            "paragraph",
            "text",
            "object",
            "json",
            "array",
            "list",
            "number",
            "boolean",
            "select",
            "file",
        }
        assert expected == ALLOWED_CONTRACT_TYPES

    def test_xml_not_allowed(self):
        from core.application.registry.contract_validation import ALLOWED_CONTRACT_TYPES

        assert "xml" not in ALLOWED_CONTRACT_TYPES

    def test_string_allowed(self):
        from core.application.registry.contract_validation import ALLOWED_CONTRACT_TYPES

        assert "string" in ALLOWED_CONTRACT_TYPES


@pytest.mark.parametrize(
    ("draft_kind", "draft_target", "expected_code"),
    [
        ("template", "state.outputs.text", None),
        ("template", "state.workflow_data.outputs.text", None),
        ("template", "state.workflow_data.text", None),
        ("template", "state.customer.text", "workflow.callable.output_not_produced"),
        ("template", "state.workflow_data.customer.text", "workflow.callable.output_not_produced"),
        ("transform", "state.workflow_data.text", "workflow.callable.output_type_mismatch"),
        ("template", "state.outputs.other", "workflow.callable.output_not_produced"),
        ("transform", "state.outputs.text", "workflow.callable.output_type_mismatch"),
    ],
)
@pytest.mark.parametrize("projection_key", [None, "outputs", "_outputs"])
@pytest.mark.parametrize("draft_id", ["stored", "fresh"])
def test_callable_producer_validation_uses_current_draft_not_stored_steps(
    draft_kind, draft_target, expected_code, draft_id, projection_key
):
    from core.application.registry.contract_validation import _validate_callable_outputs_produced

    stored = {
        "workflow_id": "stored",
        "steps": [
            {
                "step_id": "produce",
                "step_kind": "template",
                "override": {"template": {"text": "stale", "result_target": "state.text"}},
            }
        ],
    }

    class Store:
        def get_workflow(self, workflow_id):
            return stored if workflow_id == "stored" else None

        def get_published_block_contract(self, block_key):
            if projection_key and block_key == "workflow.stored":
                return {projection_key: [{"field_key": "text", "value_type": "string"}]}
            return None

    draft = {
        "workflow_id": draft_id,
        "steps": [
            {
                "step_id": "produce",
                "step_kind": draft_kind,
                "override": {draft_kind: {"text": "current", "result_target": draft_target}},
            }
        ],
    }
    issues = _validate_callable_outputs_produced(
        db=Store(),
        workflow=draft,
        callable_outputs=[{"name": "text", "type": "string", "required": True}],
        path_prefix="workflow.callable.expected_outputs",
        code_prefix="workflow.callable",
    )
    assert [item["code"] for item in issues] == ([expected_code] if expected_code else [])


def test_callable_expected_outputs_metadata_does_not_invent_a_producer():
    from core.application.registry.contract_validation import _validate_callable_outputs_produced

    class Store:
        def get_published_block_contract(self, block_key):
            return {"expected_outputs": [{"name": "text", "type": "string"}]}

    issues = _validate_callable_outputs_produced(
        db=Store(),
        workflow={"workflow_id": "fresh", "steps": []},
        callable_outputs=[{"name": "text", "type": "string", "required": True}],
        path_prefix="workflow.callable.expected_outputs",
        code_prefix="workflow.callable",
    )
    assert [item["code"] for item in issues] == ["workflow.callable.output_not_produced"]


def test_public_output_aliases_preserve_full_paths_and_all_producer_types():
    from types import SimpleNamespace

    from core.application.registry.contract_validation_workflow import (
        _collect_workflow_output_types,
    )

    workflow = {
        "workflow_id": "draft",
        "steps": [
            {
                "step_id": "text",
                "step_kind": "template",
                "override": {"template": {"result_target": "state.outputs.value"}},
            },
            {
                "step_id": "object",
                "step_kind": "transform",
                "override": {"transform": {"result_target": "state.workflow_data.outputs.value"}},
            },
        ],
    }
    types = _collect_workflow_output_types(SimpleNamespace(), workflow)
    assert types["value"] == {"paragraph", "object"}
    assert types["outputs.value"] == {"paragraph"}
    assert types["workflow_data.outputs.value"] == {"object"}
