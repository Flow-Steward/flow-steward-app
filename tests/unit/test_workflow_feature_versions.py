"""Typed contract fields and review fields need a Core that has them (ADR 0075)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from core.application.extensions.extension_descriptor import DEFAULT_FS_RUNTIME_VERSION
from core.application.extensions.workflow_connection_slot_bundle import (
    connection_slot_descriptor_issues,
)
from core.application.workflows.workflow_feature_versions import (
    TYPED_CONTRACT_CORE_MIN,
    TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN,
    declares_at_least,
    workflow_uses_typed_contract,
)
from core.application.workflows.workflow_template_catalog import WorkflowTemplateCatalogError
from core.application.workflows.workflow_template_catalog_create import (
    _require_typed_contract_minimum,
)

RELEASED = {
    "workflow_id": "wf.released",
    "settings": {
        "callable": {
            "expected_inputs": [{"name": "doc", "type": "file", "required": True}],
            "expected_outputs": [{"name": "total", "type": "number"}],
        }
    },
    "steps": [{"step_id": "check", "step_kind": "approval", "override": {"approval": {}}}],
}
SHAPED = {
    **RELEASED,
    "settings": {
        "callable": {
            "expected_inputs": [{"name": "pages", "type": "array", "items": {"type": "file"}}],
        }
    },
}
REVIEWED = {
    **RELEASED,
    "steps": [
        {
            "step_id": "check",
            "step_kind": "approval",
            "override": {"approval": {"review": {"fields": [{"name": "total"}]}}},
        }
    ],
}


@pytest.mark.parametrize(
    ("workflow", "typed"),
    [(RELEASED, False), (SHAPED, True), (REVIEWED, True)],
)
def test_typed_contract_use_is_detected_from_the_definition(workflow, typed) -> None:
    assert workflow_uses_typed_contract(workflow) is typed


def test_the_host_advertises_the_typed_contract_version() -> None:
    assert declares_at_least(DEFAULT_FS_RUNTIME_VERSION, TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN)
    assert not declares_at_least("1.9.0", TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN)
    assert not declares_at_least("", TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN)


def _descriptor(workflow: dict[str, Any], platform_min: str) -> dict[str, Any]:
    return {
        "extension_id": "acme.reviews",
        "runtime": {"compatibility": {"platform_min": platform_min}},
        "artifacts": {"workflows": [{"workflow_id": "wf.local", "snapshot": workflow}]},
    }


def _codes(descriptor: dict[str, Any]) -> list[str]:
    return [issue["code"] for issue in connection_slot_descriptor_issues(descriptor)]


def test_an_extension_bundle_with_typed_workflows_declares_the_host_for_them() -> None:
    assert "workflow.typed_contract.host_minimum" in _codes(_descriptor(REVIEWED, "1.9.0"))
    assert "workflow.typed_contract.host_minimum" in _codes(_descriptor(SHAPED, ""))
    assert "workflow.typed_contract.host_minimum" not in _codes(_descriptor(REVIEWED, "1.10.0"))
    # A released workflow keeps importing with its released minimum.
    assert "workflow.typed_contract.host_minimum" not in _codes(_descriptor(RELEASED, "1.0.0"))


def _template(minimum: str) -> Any:
    return SimpleNamespace(
        item_id="invoice_review", version="1.0.0", compatible_min_version=minimum
    )


def _bundle(*workflows: dict[str, Any]) -> str:
    return yaml.safe_dump({"schema_version": 1, "workflows": list(workflows)})


def test_a_template_with_typed_fields_must_declare_the_core_that_has_them() -> None:
    with pytest.raises(WorkflowTemplateCatalogError) as refused:
        _require_typed_contract_minimum(_template("0.9.19"), _bundle(REVIEWED))
    assert refused.value.code == "workflow_template_min_version_too_low"
    with pytest.raises(WorkflowTemplateCatalogError):
        _require_typed_contract_minimum(_template(""), _bundle(RELEASED, SHAPED))

    _require_typed_contract_minimum(_template(TYPED_CONTRACT_CORE_MIN), _bundle(REVIEWED))
    # Templates of earlier releases still import without any minimum.
    _require_typed_contract_minimum(_template(""), _bundle(RELEASED))
