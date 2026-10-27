"""`batch` splits an array into chunks the author sized, and nothing more.

The ledger recorded two things about `batch`: that the step only splits and does
not execute (which authoring validation already says, via
`workflow.step_batch_steps_unsupported`), and that a connector's own per-call item
cap is invisible until runtime.

Core deliberately does not know a provider's batch maximum: there is no clamp, no
discovery and no adaptive retry. The author sets `batch_size`, and a provider that
refuses the size it is handed reports that through the ordinary external-error
path. What Core owes the author is that the number they wrote is the number used —
so an unusable `batch_size` is refused rather than silently becoming 1, which is
what turned a 20-call run into a 1000-call one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.control_flow_step_helpers import _chunk_items
from tests._step_run_state import step_run_state

# ---------------------------------------------------------------------------
# Splitting semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", [50, 100, 250, 1000])
def test_chunking_preserves_every_item_and_its_order(batch_size):
    items = list(range(1000))

    chunks = _chunk_items(items, batch_size, True)

    assert [item for chunk in chunks for item in chunk] == items
    assert all(len(chunk) == batch_size for chunk in chunks[:-1])
    assert len(chunks) == -(-len(items) // batch_size)


def test_the_last_partial_chunk_is_kept():
    chunks = _chunk_items(list(range(1005)), 100, True)

    assert len(chunks) == 11
    assert len(chunks[-1]) == 5
    assert chunks[-1] == [1000, 1001, 1002, 1003, 1004]


def test_a_partial_chunk_can_be_dropped_when_the_author_asks():
    chunks = _chunk_items(list(range(1005)), 100, False)

    assert len(chunks) == 10
    assert all(len(chunk) == 100 for chunk in chunks)


def test_an_empty_input_produces_no_chunks():
    assert _chunk_items([], 50, True) == []
    assert _chunk_items([], 50, False) == []


def test_a_single_item_is_one_chunk():
    assert _chunk_items(["only"], 50, True) == [["only"]]


# ---------------------------------------------------------------------------
# The size the author wrote is the size that is used
# ---------------------------------------------------------------------------


def _batch_step(batch_size):
    return {
        "step_id": "chunk_items",
        "step_kind": "batch",
        "override": {
            "batch": {
                "source_path": "state.items",
                "result_target": "state.item_chunks",
                "batch_size": batch_size,
            }
        },
    }


def _run_batch_step(batch_size, items, *, state_overrides=None):
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        _batch_step(batch_size), items=items, workflow_data={}, agent_messages=[]
    )
    state.update(state_overrides or {})
    command = control_flow_step_node(state)
    update = command
    message = (update.get("agent_messages") or [])[-1]
    return update, message


def test_the_authored_batch_size_is_the_one_used():
    update, message = _run_batch_step(50, list(range(1000)))

    assert message["status"] == "pass"
    assert message["artifacts"]["batch_size"] == 50
    assert message["artifacts"]["batch_count"] == 20
    assert len(update["item_chunks"]) == 20


@pytest.mark.parametrize("batch_size", [0, -1, "", None, "fifty", 1.5, True])
def test_an_unusable_batch_size_is_refused_rather_than_silently_becoming_one(batch_size):
    """Silently chunking by 1 turned a 20-call run into 1000 calls."""
    _update, message = _run_batch_step(batch_size, list(range(1000)))

    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "batch_size_invalid"
    assert "positive" in message["summary"]


def test_an_empty_source_array_is_a_defined_result_not_a_failure():
    update, message = _run_batch_step(50, [])

    assert message["status"] == "pass"
    assert update["item_chunks"] == []
    assert message["artifacts"]["item_count"] == 0
    assert message["artifacts"]["batch_count"] == 0


def test_dataset_batch_emits_a_cursor_descriptor_without_rows():
    source = {
        "dataset_handle": "dst_catalog_20260909",
        "row_count": 134_992,
        "schema": {"columns": [{"name": "sku", "type": "String"}]},
        "preview_rows": [{"sku": "must-not-leak"}],
    }

    update, message = _run_batch_step(250, source)

    assert message["status"] == "pass"
    assert update["item_chunks"] == {
        "kind": "dataset_cursor_v1",
        "dataset_handle": "dst_catalog_20260909",
        "row_count": 134_992,
        "page_size": 250,
        "emit_partial_batch": True,
    }
    assert "must-not-leak" not in repr(update["item_chunks"])
    assert message["artifacts"]["batch_count"] == 540


@pytest.mark.parametrize("canonical", [False, True])
def test_dataset_batch_resolves_bare_dataset_handles_through_the_dataset_plane(canonical):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    plane = ArtifactDatasetPlane()
    handle, _meta = plane.create_dataset(
        owner_scope={"account_id": "acc-1", "project_id": "project-1"},
        run_id="job-1",
        step_id="write-dataset",
        rows=[{"sku": "A"}, {"sku": "B"}, {"sku": "C"}],
    )
    source = f"dataset:{handle.dataset_id}" if canonical else handle.dataset_id

    update, message = _run_batch_step(
        2,
        source,
        state_overrides={
            "_artifact_plane": plane,
            "account_id": "acc-1",
            "project_id": "project-1",
        },
    )

    assert message["status"] == "pass"
    assert update["item_chunks"] == {
        "kind": "dataset_cursor_v1",
        "dataset_handle": handle.dataset_id,
        "row_count": 3,
        "page_size": 2,
        "emit_partial_batch": True,
    }


def test_dataset_batch_accepts_a_connector_produced_dataset_descriptor():
    source = {
        "ok": True,
        "runtime": "extension_action",
        "dataset_handle": "dataset:dst_connector_rows",
        "produced_datasets": [
            {
                "dataset_id": "dst_connector_rows",
                "dataset_handle": "dataset:dst_connector_rows",
                "binding_key": "processed_rows",
                "row_count": 5,
            }
        ],
        "grant_outputs": {"processed_rows": "dataset:dst_connector_rows"},
    }

    update, message = _run_batch_step(2, source)

    assert message["status"] == "pass"
    assert update["item_chunks"]["dataset_handle"] == "dst_connector_rows"
    assert update["item_chunks"]["row_count"] == 5
    assert message["artifacts"]["batch_count"] == 3


def test_batch_rejects_an_oversized_control_plane_array():
    _update, message = _run_batch_step(250, list(range(1001)))

    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "control_plane_collection_too_large"


@pytest.mark.parametrize("invalid_value", [b"binary-row"])
def test_batch_rejects_non_json_control_plane_values(invalid_value):
    _update, message = _run_batch_step(1, [invalid_value])

    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "control_plane_collection_not_serializable"


def test_batch_rejects_a_cyclic_control_plane_collection():
    cyclic = []
    cyclic.append(cyclic)

    _update, message = _run_batch_step(1, cyclic)

    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "control_plane_collection_not_serializable"


def test_batch_size_check_stops_encoding_after_the_byte_limit():
    class NotJsonSerializable:
        pass

    source = ["x" * (256 * 1024 + 1), NotJsonSerializable()]

    _update, message = _run_batch_step(1, source)

    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "control_plane_collection_too_large"


def test_large_scalar_size_check_uses_a_fixed_json_encoding_window(monkeypatch):
    from core.application.orchestration import dataset_cursor_runtime

    encoded_string_lengths: list[int] = []
    real_dumps = dataset_cursor_runtime.json.dumps

    def tracked_dumps(value, *args, **kwargs):
        if isinstance(value, str):
            encoded_string_lengths.append(len(value))
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(dataset_cursor_runtime.json, "dumps", tracked_dumps)

    with pytest.raises(dataset_cursor_runtime.DatasetCursorError) as error:
        dataset_cursor_runtime.assert_bounded_control_plane_array(["x" * (1024 * 1024)])

    assert error.value.code == "control_plane_collection_too_large"
    assert encoded_string_lengths
    assert max(encoded_string_lengths) <= 4096


@pytest.mark.parametrize("row_count", [1.5, float("inf")])
def test_dataset_batch_rejects_non_integer_row_count(row_count):
    _update, message = _run_batch_step(
        2,
        {"dataset_handle": "dst_invalid_count", "row_count": row_count},
    )

    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "dataset_cursor_row_count_invalid"


def test_batch_result_target_cannot_overwrite_trusted_test_mode():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    state = step_run_state(
        {
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.items",
                    "result_target": "state.trusted_runtime_context.test_mode",
                    "batch_size": 1,
                }
            },
        },
        items=[1, 2],
        trusted_runtime_context={"test_mode": False},
        agent_messages=[],
    )

    command = control_flow_step_node(state)
    update = command
    merged = {**state, **update}

    assert update.get("trusted_runtime_context") is None
    assert runtime_identity_from_state(merged).test_mode is False
