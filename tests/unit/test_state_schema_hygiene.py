"""
State schema hygiene tests — guard against uncontrolled field growth and premature removal.

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_state_schema_hygiene.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.state import (
    _PROJECTION_ONLY_FIELDS,
    _STATE_MODEL_FIELDS,
    _STATE_TYPEDDICT_FIELDS,
    OrchestratorState,
    OrchestratorStateModel,
)

# Fields removed in pre-0.1.0 cleanup — must never reappear.
_REMOVED_DEPRECATED_FIELDS = frozenset(
    {
        "ui_test_verdict",
        "ui_test_flows_run",
        "ui_test_screenshots",
    }
)


class TestStateSchemaHygiene:
    """Ensure the state schema stays clean and intentional."""

    def test_removed_deprecated_fields_absent_from_typeddict(self):
        """Pre-0.1.0 deprecated fields must NOT reappear in OrchestratorState."""
        annotations = OrchestratorState.__annotations__
        for field in _REMOVED_DEPRECATED_FIELDS:
            assert field not in annotations, (
                f"Removed deprecated field '{field}' has reappeared in OrchestratorState. "
                f"These fields were intentionally deleted in pre-0.1.0 cleanup."
            )

    def test_removed_deprecated_fields_absent_from_pydantic_model(self):
        """Pre-0.1.0 deprecated fields must NOT reappear in OrchestratorStateModel."""
        model_fields = set(OrchestratorStateModel.model_fields.keys())
        for field in _REMOVED_DEPRECATED_FIELDS:
            assert field not in model_fields, (
                f"Removed deprecated field '{field}' has reappeared in OrchestratorStateModel. "
                f"These fields were intentionally deleted in pre-0.1.0 cleanup."
            )

    def test_typeddict_model_sync_guard(self):
        """OrchestratorStateModel must include every OrchestratorState field."""
        missing = _STATE_TYPEDDICT_FIELDS - _STATE_MODEL_FIELDS
        assert not missing, (
            f"OrchestratorStateModel is missing fields declared in OrchestratorState: {sorted(missing)}. "
            f"Add them to OrchestratorStateModel to keep the two definitions in sync."
        )

    def test_projection_only_fields_present(self):
        """Projection-only fields must remain declared."""
        annotations = OrchestratorState.__annotations__
        for field in _PROJECTION_ONLY_FIELDS:
            assert field in annotations, (
                f"Projection-only field '{field}' was removed from OrchestratorState."
            )

    def test_field_count_within_bounds(self):
        """Guard against uncontrolled field growth — require conscious acknowledgement."""
        field_count = len(OrchestratorState.__annotations__)
        # Typed workflow contracts added public input/output and terminal output evidence.
        # Allow small headroom before requiring explicit review.
        assert field_count <= 145, (
            f"OrchestratorState has {field_count} fields (limit: 145). "
            f"Review whether new fields are necessary or should be nested."
        )


def test_workflow_outputs_is_a_declared_state_channel():
    """D05 regression.

    The validation node emits both `workflow_outputs` (the full output-contract
    result) and `outputs`. `meta.workflow_outputs` is what the public run result
    is built from, but the key was never declared on the state schema, so
    LangGraph dropped it at the checkpoint boundary: completed runs persisted
    `meta.workflow_outputs = {}` and callers received no outputs at all, even
    though the steps had produced them.
    """
    assert "workflow_outputs" in _STATE_TYPEDDICT_FIELDS
    assert "workflow_outputs" in _STATE_MODEL_FIELDS
    assert "workflow_outputs" not in _PROJECTION_ONLY_FIELDS
    assert "workflow_outputs" in OrchestratorState.__annotations__
    assert "workflow_outputs" in OrchestratorStateModel.model_fields
