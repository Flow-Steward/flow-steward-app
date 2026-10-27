"""A connector output mapping that resolves to nothing must say so.

Regression for the workflow E2E ledger finding F35(b). `output_mapping` matches
the top-level keys of the result envelope, so a connector returning
`{"data": {"id": 1150}}` yielded nothing at all for a mapping of `id` — silently.
The run continued and failed one step later with `product_id must be a
non-negative integer`, which named neither the mapping nor the step that produced
it.

The supported way to read a nested field stays the same: map the parent object
and then read `state.<target>.<field>`. What changes is that a mapping which
produces nothing is reported at the step that owns it, and the message names the
keys the result actually offered so the author can see that `data` is the one to
map.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.connector_mapping import _apply_output_mapping


def _step(output_mapping: dict) -> dict:
    return {
        "step_id": "create_product",
        "step_kind": "connector",
        "output_mapping_jsonb": dict(output_mapping),
    }


def _dispatch(result: dict) -> dict:
    return {"ok": True, "handled": True, "result": dict(result)}


# ---------------------------------------------------------------------------
# What still works
# ---------------------------------------------------------------------------


def test_mapping_a_top_level_key_still_works():
    dispatch = _dispatch({"id": 1150, "sku": "A1"})
    state: dict = {}

    _apply_output_mapping(
        step_payload=_step({"id": "state.product_id"}), dispatch_result=dispatch, state=state
    )

    assert dispatch["state_patch"]["product_id"] == 1150
    assert dispatch.get("unmapped_outputs") in (None, [])


def test_mapping_the_parent_object_is_the_supported_way_to_read_a_nested_field():
    dispatch = _dispatch({"data": {"id": 1150, "sku": "A1"}})
    state: dict = {}

    _apply_output_mapping(
        step_payload=_step({"data": "state.created"}), dispatch_result=dispatch, state=state
    )

    assert dispatch["state_patch"]["created"] == {"id": 1150, "sku": "A1"}
    assert dispatch.get("unmapped_outputs") in (None, [])


def test_output_mapping_cannot_write_host_owned_test_mode():
    dispatch = _dispatch({"enabled": True})

    _apply_output_mapping(
        step_payload=_step({"enabled": "state.test_mode"}),
        dispatch_result=dispatch,
        state={"test_mode": False},
    )

    assert dispatch["ok"] is False
    assert "state_patch" not in dispatch
    assert dispatch["errors"][0]["code"] == "output_mapping_reserved_target"


def test_a_step_with_no_output_mapping_is_untouched():
    dispatch = _dispatch({"data": {"id": 1150}})

    _apply_output_mapping(step_payload=_step({}), dispatch_result=dispatch, state={})

    assert "state_patch" not in dispatch
    assert dispatch.get("unmapped_outputs") in (None, [])


# ---------------------------------------------------------------------------
# The silent miss
# ---------------------------------------------------------------------------


def test_a_mapping_that_resolves_to_nothing_is_reported_on_this_step():
    """`id` against `{"data": {"id": ...}}` used to yield nothing, silently."""
    dispatch = _dispatch({"data": {"id": 1150, "sku": "A1"}})
    state: dict = {}

    _apply_output_mapping(
        step_payload=_step({"id": "state.product_id"}), dispatch_result=dispatch, state=state
    )

    assert dispatch["ok"] is False
    assert dispatch["unmapped_outputs"] == ["id"]
    detail = str(dispatch.get("detail") or "")
    assert "id" in detail
    assert "data" in detail, "the message must name what the result did offer"
    errors = dispatch.get("errors") or []
    assert errors
    assert errors[0].get("code") == "output_mapping_unresolved"


def test_unresolved_mapping_names_fields_inside_the_runtime_response_envelope():
    dispatch = {
        "ok": True,
        "handled": True,
        "invocation": {
            "response": {
                "result": {
                    "inventory_adjustment_group": {
                        "id": "gid://acme-store/InventoryAdjustmentGroup/1"
                    },
                    "external_effect_status": "confirmed",
                }
            }
        },
    }

    _apply_output_mapping(
        step_payload=_step({"no_op": "state.inventory_was_unchanged"}),
        dispatch_result=dispatch,
        state={},
    )

    detail = str(dispatch.get("detail") or "")
    assert "inventory_adjustment_group" in detail
    assert "external_effect_status" in detail
    assert "The result provides: (nothing)" not in detail


def test_every_unresolved_output_is_named():
    dispatch = _dispatch({"data": {"id": 1150}})
    state: dict = {}

    _apply_output_mapping(
        step_payload=_step({"id": "state.a", "sku": "state.b", "data": "state.c"}),
        dispatch_result=dispatch,
        state=state,
    )

    assert dispatch["ok"] is False
    assert sorted(dispatch["unmapped_outputs"]) == ["id", "sku"]
    # The mapping that did resolve is still applied.
    assert dispatch["state_patch"]["c"] == {"id": 1150}


def test_suppressed_safe_test_does_not_fake_or_require_provider_outputs():
    dispatch = {
        "ok": True,
        "handled": True,
        "invocation": {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "test_mode_status": "suppressed",
                    "external_effect_status": "suppressed",
                    "definitely_no_external_effect": True,
                },
                "external_effect_status": "suppressed",
                "definitely_no_external_effect": True,
            },
        },
    }

    _apply_output_mapping(
        step_payload=_step(
            {
                "name": "state.drive_name",
                "size": "state.drive_size",
                "file_id": "state.drive_file_id",
                "web_view_link": "state.drive_url",
            }
        ),
        dispatch_result=dispatch,
        state={},
    )

    assert dispatch["ok"] is True
    assert dispatch["suppressed_output_mappings"] == [
        "name",
        "size",
        "file_id",
        "web_view_link",
    ]
    assert set(dispatch["state_patch"]) == {"_test_mode_suppressed_outputs"}
    assert set(dispatch["state_patch"]["_test_mode_suppressed_outputs"]) == {
        "state.drive_name",
        "state.drive_size",
        "state.drive_file_id",
        "state.drive_url",
    }
    assert "mapped_outputs" not in dispatch
    assert "unmapped_outputs" not in dispatch
    assert "errors" not in dispatch


def test_untrusted_suppression_shape_does_not_bypass_output_validation():
    dispatch = {
        "ok": True,
        "handled": True,
        "invocation": {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"test_mode_status": "suppressed"},
                "external_effect_status": "suppressed",
                "definitely_no_external_effect": False,
            },
        },
    }

    _apply_output_mapping(
        step_payload=_step({"file_id": "state.drive_file_id"}),
        dispatch_result=dispatch,
        state={},
    )

    assert dispatch["ok"] is False
    assert dispatch["unmapped_outputs"] == ["file_id"]
    assert dispatch["errors"][0]["code"] == "output_mapping_unresolved"


@pytest.mark.parametrize("already_failed", [{"ok": False}, {"ok": False, "detail": "boom"}])
def test_an_already_failed_dispatch_is_not_relabelled(already_failed):
    """A connector failure keeps its own cause; the mapping does not overwrite it."""
    dispatch = {"handled": True, "result": {}, **already_failed}
    original_detail = dispatch.get("detail")

    _apply_output_mapping(
        step_payload=_step({"id": "state.product_id"}), dispatch_result=dispatch, state={}
    )

    assert dispatch["ok"] is False
    assert dispatch.get("detail") == original_detail
