"""The dataset query row limit is one operator-owned deployment maximum.

The connector extension's own half of this contract — that it honours the limit the
host approved instead of re-clamping it — lives in that extension's test suite,
where its module is importable.

`dataset_query` materialises its whole result set into workflow state, so how many
rows a run may pull is a deployment decision, not a per-workflow one. Before this
there were two independent caps: Core defaulted a step to 100 rows and passed it
through, while the data-connector extension clamped `max_runtime_rows` to its own
hard-coded 1000 — so raising the workflow's `row_limit` past 1000 silently did
nothing.

`FS_DATASET_QUERY_MAX_RUNTIME_ROWS` (default 1000) is now the single maximum: the
workflow's `row_limit` is the user's request, it is rejected when it exceeds the
deployment maximum, and the approved value is what the extension receives.
Preview limits are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.registry.dataset_query_limits import (
    DATASET_QUERY_MAX_RUNTIME_ROWS_ENV,
    InvalidDatasetQueryLimitError,
    resolve_dataset_query_row_limit,
)
from core.infrastructure.config.workflow_collection_limits import (
    DEFAULT_COLLECTION_ITEM_LIMIT,
    InvalidCollectionLimitError,
    resolve_collection_item_limit,
)
from tests._workflow_definitions import linear_workflow


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(DATASET_QUERY_MAX_RUNTIME_ROWS_ENV, raising=False)


def test_the_default_deployment_maximum_is_one_thousand():
    assert DEFAULT_COLLECTION_ITEM_LIMIT == 1000
    assert resolve_collection_item_limit() == 1000


@pytest.mark.parametrize("value", ["5000", "10000"])
def test_the_deployment_maximum_is_operator_owned(monkeypatch, value):
    monkeypatch.setenv(DATASET_QUERY_MAX_RUNTIME_ROWS_ENV, value)
    assert resolve_collection_item_limit() == int(value)
    assert resolve_dataset_query_row_limit(int(value)) == int(value)


@pytest.mark.parametrize("bad_value", ["0", "-5", "abc", "1.5", "", "true"])
def test_an_invalid_deployment_maximum_is_diagnosed_explicitly(monkeypatch, bad_value):
    """A typo must not silently fall back to the default — for queries and For each alike."""
    monkeypatch.setenv(DATASET_QUERY_MAX_RUNTIME_ROWS_ENV, bad_value)
    with pytest.raises(InvalidCollectionLimitError) as excinfo:
        resolve_collection_item_limit()
    assert DATASET_QUERY_MAX_RUNTIME_ROWS_ENV in str(excinfo.value)
    with pytest.raises(InvalidDatasetQueryLimitError):
        resolve_dataset_query_row_limit(10)


def test_raising_the_maximum_does_not_widen_a_smaller_request(monkeypatch):
    monkeypatch.setenv(DATASET_QUERY_MAX_RUNTIME_ROWS_ENV, "10000")
    assert resolve_dataset_query_row_limit(50) == 50


# ---------------------------------------------------------------------------
# The workflow's row_limit against the deployment maximum
# ---------------------------------------------------------------------------


def test_row_limit_within_the_default_maximum_is_approved():
    assert resolve_dataset_query_row_limit(500) == 500
    assert resolve_dataset_query_row_limit(1000) == 1000


def test_row_limit_above_the_default_maximum_is_rejected():
    with pytest.raises(InvalidDatasetQueryLimitError) as excinfo:
        resolve_dataset_query_row_limit(1001)
    message = str(excinfo.value)
    assert "1001" in message, "the message must name the requested value"
    assert "1000" in message, "the message must name the allowed value"


def test_a_raised_deployment_maximum_admits_a_larger_row_limit(monkeypatch):
    monkeypatch.setenv(DATASET_QUERY_MAX_RUNTIME_ROWS_ENV, "10000")
    assert resolve_dataset_query_row_limit(10000) == 10000
    with pytest.raises(InvalidDatasetQueryLimitError) as excinfo:
        resolve_dataset_query_row_limit(10001)
    assert "10001" in str(excinfo.value)
    assert "10000" in str(excinfo.value)


def test_an_unset_row_limit_keeps_the_existing_step_default():
    """Not setting `row_limit` must not change what existing workflows fetch."""
    assert resolve_dataset_query_row_limit(None) == 100
    assert resolve_dataset_query_row_limit("") == 100


@pytest.mark.parametrize("bad_value", ["-1", "abc", "1.5", -1, 0, "0"])
def test_an_invalid_row_limit_is_rejected(bad_value):
    with pytest.raises(InvalidDatasetQueryLimitError):
        resolve_dataset_query_row_limit(bad_value)


@pytest.mark.parametrize("zero", [0, "0"])
def test_zero_is_an_error_not_a_silent_default(zero):
    """`row_limit: 0` asks for no rows; answering with 100 is a wrong result."""
    with pytest.raises(InvalidDatasetQueryLimitError) as excinfo:
        resolve_dataset_query_row_limit(zero)

    message = str(excinfo.value)
    assert "row_limit" in message
    assert "positive" in message


# ---------------------------------------------------------------------------
# The step surfaces the rejection instead of silently truncating
# ---------------------------------------------------------------------------


def _dataset_query_step(row_limit):
    return {
        "step_id": "run_query",
        "step_kind": "dataset_query",
        "override": {
            "dataset_query": {"query_template_id": "qt-1", "params": {}, "row_limit": row_limit}
        },
    }


def test_the_runtime_rejects_an_out_of_range_row_limit_without_running_the_query():
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    result = execute_dataset_query_step(step=_dataset_query_step(5000), state={})

    assert result["ok"] is False
    errors = result.get("errors") or []
    assert errors
    assert errors[0].get("code") == "row_limit_exceeds_maximum"
    message = str(errors[0].get("message") or "")
    assert "5000" in message
    assert "1000" in message


def test_the_runtime_rejects_an_unusable_deployment_maximum(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    monkeypatch.setenv(DATASET_QUERY_MAX_RUNTIME_ROWS_ENV, "not-a-number")
    result = execute_dataset_query_step(step=_dataset_query_step(10), state={})

    assert result["ok"] is False
    errors = result.get("errors") or []
    assert errors
    assert errors[0].get("code") == "row_limit_exceeds_maximum"
    assert DATASET_QUERY_MAX_RUNTIME_ROWS_ENV in str(errors[0].get("message") or "")


def test_readiness_rejects_an_out_of_range_row_limit_at_authoring_time():
    from core.application.registry.contract_validation import validate_workflow_readiness

    workflow = linear_workflow(
        {
            "step_id": "seed",
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": "state.ready", "value": True}},
        },
        _dataset_query_step(5000),
        workflow_id="wf-row-limit",
    )

    class _DB:
        def get_workflow(self, workflow_id: str):
            return workflow if workflow_id == "wf-row-limit" else None

        def get_dataset_query_template(self, query_template_id: str):
            return {
                "query_template_id": "qt-1",
                "dataset_id": "",
                "params_schema_jsonb": {"type": "object", "properties": {}},
            }

    validation = validate_workflow_readiness(_DB(), "wf-row-limit")
    codes = {
        str(item.get("code") or "")
        for item in (validation.get("errors") or [])
        if isinstance(item, dict)
    }
    assert "workflow.step_dataset_query_row_limit_invalid" in codes
    issue = next(
        item
        for item in validation["errors"]
        if item.get("code") == "workflow.step_dataset_query_row_limit_invalid"
    )
    assert issue["path"] == "workflow.steps.run_query.override.dataset_query.row_limit"
    assert "Step 'run_query'" in issue["message"]


@pytest.mark.parametrize("zero", [0, "0"])
def test_the_runtime_rejects_a_zero_row_limit_rather_than_defaulting(zero):
    """`or` at the call site would turn a deliberate 0 back into the 100 default."""
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    result = execute_dataset_query_step(step=_dataset_query_step(zero), state={})

    assert result["ok"] is False
    errors = result.get("errors") or []
    assert errors
    assert errors[0].get("code") == "row_limit_exceeds_maximum"
    assert "positive" in str(errors[0].get("message") or "")
