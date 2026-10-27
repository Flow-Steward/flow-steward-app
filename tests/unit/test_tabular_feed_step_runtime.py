from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_diff_config_requires_explicit_baseline_dataset():
    from core.application.tabular_feed.validation import validate_tabular_feed_config

    errors = validate_tabular_feed_config(
        {
            "operation": "diff",
            "input": {"dataset_handle": "state.current.dataset_handle"},
            "profile_key": "stock-import",
            "key_column": "sku",
            "result_target": "state.diff",
        }
    )
    assert any(error["path"].endswith("input.baseline_dataset_handle") for error in errors)


def test_malformed_selector_fails_before_tabular_plane_or_service_dispatch():
    from core.application.orchestration.tabular_feed_step_runtime import (
        execute_tabular_feed_step,
    )

    with (
        patch(
            "core.application.orchestration.tabular_feed_step_runtime.plane_from_state_or_env",
            side_effect=AssertionError("storage plane must not be resolved"),
        ) as plane,
        patch(
            "core.application.orchestration.tabular_feed_step_runtime.TabularFeedService",
            side_effect=AssertionError("tabular service must not be constructed"),
        ) as service,
    ):
        result = execute_tabular_feed_step(
            step={
                "step_kind": "tabular_feed",
                "override": {
                    "tabular_feed": {
                        "operation": "read_rows",
                        "input": {
                            "dataset_handle": "state.rows[abc].dataset_handle",
                        },
                        "result_target": "state.dataset",
                    }
                },
            },
            state={"rows": [{"sku": "A1"}]},
        )

    assert result["ok"] is False
    assert result["detail"] == "invalid_input_selector"
    assert result["errors"] == [
        {
            "reason": "invalid_input_selector",
            "selector": "state.rows[abc].dataset_handle",
            "path": "override.tabular_feed.input.dataset_handle",
        }
    ]
    assert result["state_patch"] == {}
    plane.assert_not_called()
    service.assert_not_called()


def test_tabular_feed_catalog_matches_core_dual_backend_projection():
    from core.application.orchestration.transform_step_runtime import (
        TRANSFORM_ACTION_CATALOG_VERSION,
        list_transform_action_contracts,
    )
    from core.infrastructure.tabular_feed.constants import (
        CATALOG_VERSION,
        TABULAR_ACTION_CATALOG,
    )

    engine_catalog = TABULAR_ACTION_CATALOG
    core_catalog = {row["action_id"]: row for row in list_transform_action_contracts()}

    assert CATALOG_VERSION == 1
    assert TRANSFORM_ACTION_CATALOG_VERSION == 1
    assert set(engine_catalog) == {
        action_id for action_id, row in core_catalog.items() if row["backends"]["polars"]
    }
    for action_id, metadata in engine_catalog.items():
        contract = core_catalog[action_id]
        properties = contract["params_schema"].get("properties") or {}
        assert set(properties) == set(metadata["params"])
        assert set(contract["params_schema"].get("required") or []) == set(
            metadata.get("required", set())
        )


def test_tabular_feed_runtime_executes_core_read_without_connector_dispatch():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.tabular_feed_step_runtime import (
        execute_tabular_feed_step,
    )

    plane = ArtifactDatasetPlane()
    artifact, _ = plane.create_artifact(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="run_read",
        step_id="fetch",
        filename="feed.csv",
        content_type="text/csv",
        body=b"sku,qty\n00123,2\n",
    )
    with patch(
        "core.application.orchestration.connector_step_runtime.execute_connector_step",
        side_effect=AssertionError("tabular_feed must not dispatch connector runtime"),
    ):
        result = execute_tabular_feed_step(
            step={
                "step_kind": "tabular_feed",
                "override": {
                    "tabular_feed": {
                        "operation": "read",
                        "input": {
                            "artifact_handle": "{{state.feed_artifact}}",
                        },
                        "key_column": "sku",
                        "result_target": "state.feed_dataset",
                    }
                },
            },
            state={
                "_trusted_runtime_context": {
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                },
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "_artifact_plane": plane,
                "feed_artifact": f"artifact:{artifact.artifact_id}",
            },
        )

    assert result["ok"] is True
    assert result["action_id"] == "read"
    assert result["state_patch"]["feed_dataset"]["dataset_handle"].startswith("dst_")
    assert result["state_patch"]["feed_dataset"]["preview_rows"][0]["sku"] == "00123"


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        # A line cut short: Polars raises ComputeError while the lazy scan is collected.
        (b'{"sku": "a", "qty": 1}\n{"sku": "b", "qty": \n', "Syntax at character"),
        # A value that does not fit the type the column was given.
        (
            b'{"sku": "a", "qty": 1}\n{"sku": "b", "qty": {"x": 1}}\n',
            "unexpected value while building Series",
        ),
    ],
)
def test_an_unreadable_ndjson_input_is_a_step_error_not_a_raw_polars_error(body, detail):
    """A parse failure of the input names the file format and keeps Polars' first line."""
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.step_failures import safe_step_error_rows
    from core.application.orchestration.tabular_feed_step_runtime import (
        execute_tabular_feed_step,
    )

    plane = ArtifactDatasetPlane()
    artifact, _ = plane.create_artifact(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="run_read",
        step_id="export",
        filename="shopify-products.jsonl",
        content_type="application/x-ndjson",
        body=body,
    )

    result = execute_tabular_feed_step(
        step={
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "read",
                    "input": {"artifact_handle": "{{state.feed_artifact}}"},
                    "key_column": "sku",
                    "result_target": "state.feed_dataset",
                }
            },
        },
        state={
            "_trusted_runtime_context": {
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
            },
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "_artifact_plane": plane,
            "feed_artifact": f"artifact:{artifact.artifact_id}",
        },
    )

    assert result["ok"] is False
    assert result["state_patch"] == {}
    [error] = result["errors"]
    assert error["code"] == "tabular_feed.read_failed"
    assert error["message"].startswith(f"Could not read the NDJSON file: {detail}")
    assert "\n" not in error["message"]
    assert "context stack" not in error["message"]
    # The message is short and single-line, so the job record keeps it.
    assert safe_step_error_rows(result["errors"]) == [
        {"code": "tabular_feed.read_failed", "message": error["message"]}
    ]


def test_tabular_feed_transform_executes_catalog_actions_without_connector_dispatch():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.tabular_feed_step_runtime import (
        execute_tabular_feed_step,
    )

    plane = ArtifactDatasetPlane()
    dataset, _ = plane.create_dataset(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="run_transform",
        step_id="read",
        schema={"columns": [{"name": "price", "type": "text"}]},
        rows=[{"price": "10.00"}],
    )
    with patch(
        "core.application.orchestration.connector_step_runtime.execute_connector_step",
        side_effect=AssertionError("tabular_feed must not dispatch connector runtime"),
    ):
        result = execute_tabular_feed_step(
            step={
                "override": {
                    "tabular_feed": {
                        "operation": "transform",
                        "input": {"dataset_handle": "state.dataset"},
                        "actions": [
                            {
                                "action": "money.multiply",
                                "target": "price",
                                "params": {"factor": "0.9"},
                            }
                        ],
                        "result_target": "state.out",
                    }
                }
            },
            state={
                "_trusted_runtime_context": {
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                },
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "_artifact_plane": plane,
                "dataset": f"dataset:{dataset.dataset_id}",
            },
        )

    assert result["ok"] is True
    assert result["action_id"] == "transform"
    assert result["result"]["dataset_handle"].startswith("dst_")


def test_tabular_feed_validation_rejects_missing_result_target():
    from core.application.orchestration.tabular_feed_step_runtime import (
        validate_tabular_feed_config,
    )

    errors = validate_tabular_feed_config({"operation": "read", "input": {}})

    assert any(
        error["code"] == "workflow.step_tabular_feed_result_target_missing" for error in errors
    )


def test_tabular_feed_validation_rejects_unknown_fields_and_missing_transform_contract():
    from core.application.orchestration.tabular_feed_step_runtime import (
        validate_tabular_feed_config,
    )

    errors = validate_tabular_feed_config(
        {"operation": "transform", "result_target": "state.out", "bogus": 1}
    )

    codes = {error["code"] for error in errors}
    assert "workflow.step_tabular_feed_field_unknown" in codes
    assert "workflow.step_tabular_feed_field_required" in codes
    assert "workflow.step_tabular_feed_actions_required" in codes


def test_tabular_feed_validation_rejects_concat_single_handle():
    from core.application.orchestration.tabular_feed_step_runtime import (
        validate_tabular_feed_config,
    )

    errors = validate_tabular_feed_config(
        {
            "operation": "concat",
            "result_target": "state.out",
            "input": {"dataset_handles": ["dataset:first"]},
        }
    )

    assert any(
        error["code"] == "workflow.step_tabular_feed_dataset_handles_invalid"
        and error["path"].endswith(".input.dataset_handles")
        for error in errors
    )


@pytest.mark.parametrize("format_fields", [{}, {"format": ""}, {"format": None}, {"format": "xml"}])
def test_tabular_feed_validation_rejects_write_without_supported_format(format_fields):
    from core.application.orchestration.tabular_feed_step_runtime import (
        validate_tabular_feed_config,
    )

    errors = validate_tabular_feed_config(
        {
            "operation": "write",
            "result_target": "state.out",
            "input": {"dataset_handle": "dataset:first"},
            **format_fields,
        }
    )

    assert any(error["code"] == "workflow.step_tabular_feed_format_invalid" for error in errors)


def test_tabular_feed_validation_rejects_unknown_or_malformed_catalog_actions():
    from core.application.orchestration.tabular_feed_step_runtime import (
        validate_tabular_feed_config,
    )

    errors = validate_tabular_feed_config(
        {
            "operation": "transform",
            "input": {"dataset_handle": "dataset:prices"},
            "actions": ["text.trim", {"action": "regex.replace", "target": "sku"}],
            "result_target": "state.result",
        }
    )

    assert {error["code"] for error in errors} >= {
        "workflow.step_tabular_feed_action_invalid",
        "workflow.step_tabular_feed_action_unknown",
    }


def test_tabular_feed_validation_rejects_malformed_relational_payloads_before_runtime():
    from core.application.orchestration.tabular_feed_step_runtime import (
        validate_tabular_feed_config,
    )

    base = {
        "operation": "transform",
        "input": {"dataset_handle": "dataset:prices"},
        "result_target": "state.result",
    }

    join_errors = validate_tabular_feed_config(
        {**base, "relational": [{"op": "join", "left": "sku", "right": "sku"}]}
    )
    sort_errors = validate_tabular_feed_config({**base, "relational": [{"op": "sort", "by": []}]})
    regex_errors = validate_tabular_feed_config(
        {**base, "relational": [{"op": "filter", "when": {"column": "x", "regex": "a"}}]}
    )
    action_when_errors = validate_tabular_feed_config(
        {
            **base,
            "actions": [
                {
                    "action": "text.trim",
                    "target": "sku",
                    "when": {"column": "supplier", "regex": "protected"},
                }
            ],
        }
    )

    assert any(
        error["code"] == "workflow.step_tabular_feed_relational_op_invalid" for error in join_errors
    )
    assert any(
        error["code"] == "workflow.step_tabular_feed_relational_sort_required"
        for error in sort_errors
    )
    assert any(
        error["code"] == "workflow.step_tabular_feed_condition_field_unknown"
        and error["path"].endswith(".regex")
        for error in regex_errors
    )
    assert any(
        error["code"] == "workflow.step_tabular_feed_condition_field_unknown"
        and error["path"].endswith(".regex")
        for error in action_when_errors
    )


@pytest.mark.parametrize("field", ["dataset_handle", "baseline_dataset_handle"])
def test_diff_missing_state_reference_identifies_input_before_storage(field) -> None:
    from core.application.orchestration.tabular_feed_step_runtime import execute_tabular_feed_step

    inputs = {"dataset_handle": "current-handle", "baseline_dataset_handle": "baseline-handle"}
    inputs[field] = "state.missing_dataset.dataset_handle"
    service = MagicMock()
    with patch(
        "core.application.orchestration.tabular_feed_step_runtime.TabularFeedService",
        return_value=service,
    ):
        result = execute_tabular_feed_step(
            step={
                "step_id": "compare",
                "step_kind": "tabular_feed",
                "override": {
                    "tabular_feed": {
                        "operation": "diff",
                        "input": inputs,
                        "result_target": "state.delta",
                        "profile_key": "inventory",
                        "key_column": "sku",
                    }
                },
            },
            state={"workflow_data": {}},
        )
    assert result["ok"] is False
    assert result["errors"] == [
        {
            "reason": "state_reference_missing",
            "selector": "state.missing_dataset.dataset_handle",
            "path": f"input.{field}",
        }
    ]
    service.execute.assert_not_called()


def test_tabular_feed_missing_plain_state_reference_never_reaches_storage() -> None:
    from core.application.orchestration.tabular_feed_step_runtime import (
        execute_tabular_feed_step,
    )

    service = MagicMock()
    with patch(
        "core.application.orchestration.tabular_feed_step_runtime.TabularFeedService",
        return_value=service,
    ):
        result = execute_tabular_feed_step(
            step={
                "step_id": "read-rows",
                "step_kind": "tabular_feed",
                "override": {
                    "tabular_feed": {
                        "operation": "read_rows",
                        "input": {"dataset_handle": "state.missing_dataset.dataset_handle"},
                        "result_target": "state.page",
                    }
                },
            },
            state={"workflow_data": {}},
        )

    assert result["ok"] is False
    assert result["errors"] == [
        {
            "reason": "state_reference_missing",
            "selector": "state.missing_dataset.dataset_handle",
            "path": "input.dataset_handle",
        }
    ]
    service.execute.assert_not_called()


@pytest.mark.parametrize(
    "reference",
    ["state.checksum.committed_snapshot_ref", {"source": "state.checksum.committed_snapshot_ref"}],
)
def test_diff_accepts_and_resolves_whole_snapshot_reference(reference):
    from core.application.orchestration.tabular_feed_step_runtime import (
        _action_payload,
        validate_tabular_feed_config,
    )

    ref = {"revision": 7, "snapshot_artifact_id": "art_baseline"}
    config = {
        "operation": "diff",
        "input": {
            "dataset_handle": "dst_current",
            "baseline_dataset_handle": "dst_baseline",
            "expected_snapshot_ref": reference,
        },
        "profile_key": "supplier",
        "key_column": "sku",
        "result_target": "state.diff",
    }
    assert validate_tabular_feed_config(config) == []
    payload = _action_payload(config, {"checksum": {"committed_snapshot_ref": ref}})
    assert payload["input"]["expected_snapshot_ref"] == ref


@pytest.mark.parametrize("operation", ["inspect", "read", "read_rows", "diff"])
def test_pure_tabular_operations_do_not_open_snapshot_database(operation):
    from core.infrastructure.tabular_feed.storage import snapshot_repository_from_state_or_env

    with (
        patch(
            "core.infrastructure.tabular_feed.storage.psycopg_runtime.connect",
            side_effect=AssertionError("pure plane operation must not connect"),
        ),
        snapshot_repository_from_state_or_env({}, operation=operation) as repo,
    ):
        assert repo is None


def test_serialized_state_cannot_supply_snapshot_repository_authority():
    from core.infrastructure.tabular_feed.storage import snapshot_repository_from_state_or_env

    state = {"_snapshot_repository": {"read_current": "forged authority"}}
    with (
        patch.dict("core.infrastructure.tabular_feed.storage.CONFIG", {"fs_db_dsn": ""}),
        snapshot_repository_from_state_or_env(state, operation="checksum") as repo,
    ):
        assert repo is None
    assert state == {"_snapshot_repository": {"read_current": "forged authority"}}


@pytest.mark.parametrize(
    "ref",
    [
        {"revision": True, "snapshot_artifact_id": "art_A"},
        {"revision": "state.revision", "snapshot_artifact_id": "art_A"},
        {"revision": 1},
        {"revision": 0, "snapshot_artifact_id": "art_A"},
    ],
)
def test_snapshot_reference_validation_rejects_partial_or_nested_bare_selectors(ref):
    from core.application.tabular_feed.validation import validate_tabular_feed_config

    errors = validate_tabular_feed_config(
        {
            "operation": "diff",
            "input": {
                "dataset_handle": "dst_current",
                "baseline_dataset_handle": "dst_baseline",
                "expected_snapshot_ref": ref,
            },
            "profile_key": "supplier",
            "key_column": "sku",
            "result_target": "state.diff",
        }
    )
    assert any(
        error["code"] == "workflow.step_tabular_feed_snapshot_ref_invalid" for error in errors
    )


@pytest.mark.parametrize("mode", ["compare", "publish"])
def test_snapshot_purpose_is_validated_and_carried_without_a_manual_reference(mode):
    from core.application.orchestration.tabular_feed_step_runtime import _action_payload
    from core.application.tabular_feed.validation import validate_tabular_feed_config

    config = {
        "operation": "diff",
        "snapshot_mode": mode,
        "input": {"dataset_handle": "state.current", "baseline_dataset_handle": "state.baseline"},
        "profile_key": "supplier",
        "key_column": "sku",
        "result_target": "state.comparison",
    }
    assert validate_tabular_feed_config(config) == []
    assert (
        _action_payload(config, {"current": "ds_current", "baseline": "ds_baseline"})[
            "snapshot_mode"
        ]
        == mode
    )


@pytest.mark.parametrize("mode", ["unknown", True, {"source": "state.mode"}])
def test_snapshot_purpose_rejects_unknown_or_untyped_choices(mode):
    from core.application.tabular_feed.validation import validate_tabular_feed_config

    config = {
        "operation": "diff",
        "snapshot_mode": mode,
        "input": {"dataset_handle": "ds_current", "baseline_dataset_handle": "ds_baseline"},
        "profile_key": "supplier",
        "key_column": "sku",
        "result_target": "state.comparison",
    }
    assert validate_tabular_feed_config(config)


def test_explicit_comparison_cannot_retain_a_hidden_publication_reference():
    from core.application.tabular_feed.validation import validate_tabular_feed_config

    config = {
        "operation": "diff",
        "snapshot_mode": "compare",
        "input": {
            "dataset_handle": "ds_current",
            "baseline_dataset_handle": "ds_baseline",
            "expected_snapshot_ref": {"revision": 0, "snapshot_artifact_id": ""},
        },
        "profile_key": "supplier",
        "key_column": "sku",
        "result_target": "state.comparison",
    }
    assert validate_tabular_feed_config(config)


def test_comparison_purpose_never_opens_snapshot_storage():
    from core.infrastructure.tabular_feed.storage import snapshot_repository_from_state_or_env

    with (
        patch(
            "core.infrastructure.tabular_feed.storage.psycopg_runtime.connect",
            side_effect=AssertionError("comparison must remain DB-independent"),
        ),
        snapshot_repository_from_state_or_env(
            {}, operation="diff", snapshot_mode="compare"
        ) as repository,
    ):
        assert repository is None


def test_only_publication_diff_receives_the_host_snapshot_repository():
    from core.application.tabular_feed.snapshot_repository import SnapshotRepository
    from core.infrastructure.tabular_feed.storage import snapshot_repository_from_state_or_env

    repository = SnapshotRepository(object())
    state = {"_snapshot_repository": repository}
    with snapshot_repository_from_state_or_env(
        state, operation="diff", snapshot_mode="publish"
    ) as selected:
        assert selected is repository
    with snapshot_repository_from_state_or_env(
        state, operation="diff", snapshot_mode="compare"
    ) as selected:
        assert selected is None


@pytest.mark.parametrize("handle_kind", ["plain", "mirror", "raw", "prefixed", "jinja", "mixed"])
def test_concat_resolves_earlier_dataset_outputs_in_authored_order(handle_kind: str) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.tabular_feed_step_runtime import execute_tabular_feed_step

    state = {
        "_trusted_runtime_context": {"account_id": "acc_alpha", "project_id": "project_alpha"},
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "_artifact_plane": ArtifactDatasetPlane(),
    }
    first_rows = [{"sku": "A1"}, {"sku": "A2"}, {"sku": "A3"}]
    second_rows = [{"sku": "B1"}, {"sku": "B2"}, {"sku": "B3"}]
    for name, rows in [("first", first_rows), ("second", second_rows)]:
        created = execute_tabular_feed_step(
            step={
                "step_id": name,
                "step_kind": "tabular_feed",
                "override": {
                    "tabular_feed": {
                        "operation": "create_dataset",
                        "rows": rows,
                        "result_target": f"state.{name}",
                    }
                },
            },
            state=state,
        )
        assert created["ok"] is True, created
        state.update(created["state_patch"])

    handles = ["state.first.dataset_handle", "state.second.dataset_handle"]
    if handle_kind == "raw":
        handles = [state["first"]["dataset_handle"], state["second"]["dataset_handle"]]
    elif handle_kind == "prefixed":
        handles = [f"dataset:{state[name]['dataset_handle']}" for name in ("first", "second")]
    elif handle_kind == "jinja":
        handles = ["{{ state.first.dataset_handle }}", "{{ state.second.dataset_handle }}"]
    elif handle_kind == "mixed":
        handles[1] = "{{ state.second.dataset_handle }}"
    elif handle_kind == "mirror":
        del state["first"]
        del state["second"]

    result = execute_tabular_feed_step(
        step={
            "step_id": "concat",
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "concat",
                    "input": {"dataset_handles": handles},
                    "result_target": "state.combined",
                }
            },
        },
        state=state,
    )

    assert result["ok"] is True, result
    assert result["action_id"] == "concat"
    assert result["result"]["row_count"] == 6
    assert result["result"]["preview_rows"] == first_rows + second_rows
    assert result["result"]["dataset_handle"].startswith("dst_")
    assert result["state_patch"]["combined"] == result["result"]
    assert result["state_patch"]["workflow_data"]["combined"] == result["result"]


@pytest.mark.parametrize("missing_index", [0, 1])
def test_concat_missing_member_fails_with_index_before_storage(missing_index: int) -> None:
    from core.application.orchestration.tabular_feed_step_runtime import execute_tabular_feed_step

    handles = ["dataset:first", "dataset:second"]
    handles[missing_index] = "state.missing.dataset_handle"
    with (
        patch(
            "core.application.orchestration.tabular_feed_step_runtime.snapshot_repository_from_state_or_env",
            side_effect=AssertionError("snapshot storage must not be resolved"),
        ) as snapshots,
        patch(
            "core.application.orchestration.tabular_feed_step_runtime.plane_from_state_or_env",
            side_effect=AssertionError("storage plane must not be resolved"),
        ) as plane,
        patch(
            "core.application.orchestration.tabular_feed_step_runtime.TabularFeedService",
            side_effect=AssertionError("tabular service must not be constructed"),
        ) as service,
    ):
        result = execute_tabular_feed_step(
            step={
                "step_kind": "tabular_feed",
                "override": {
                    "tabular_feed": {
                        "operation": "concat",
                        "input": {"dataset_handles": handles},
                        "result_target": "state.combined",
                    }
                },
            },
            state={"workflow_data": {}},
        )

    assert result == {
        "ok": False,
        "runtime": "tabular_feed",
        "detail": "tabular_feed state reference is missing",
        "errors": [
            {
                "reason": "state_reference_missing",
                "selector": "state.missing.dataset_handle",
                "path": f"input.dataset_handles[{missing_index}]",
            }
        ],
        "result": {},
        "state_patch": {},
    }
    snapshots.assert_not_called()
    plane.assert_not_called()
    service.assert_not_called()


def test_concat_members_render_once_and_keep_duplicates_without_chasing_selectors() -> None:
    from core.application.orchestration.tabular_feed_step_runtime import _action_payload

    payload = _action_payload(
        {
            "operation": "concat",
            "input": {
                "dataset_handles": [
                    "state.second",
                    "state.first",
                    "state.second",
                    "{{ state.template }}",
                ]
            },
        },
        {
            "first": "state.other",
            "second": "dst_second",
            "other": "dst_wrong",
            "template": "{{ state.other }}",
        },
    )

    expected = ["dst_second", "state.other", "dst_second", "{{ state.other }}"]
    assert payload["input"]["dataset_handles"] == expected
    assert payload["dataset_handles"] == expected


def test_tabular_literal_row_and_filter_values_remain_data() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.tabular_feed_step_runtime import (
        _action_payload,
        execute_tabular_feed_step,
    )

    state = {
        "_trusted_runtime_context": {"account_id": "acc_alpha", "project_id": "project_alpha"},
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "_artifact_plane": ArtifactDatasetPlane(),
        "foo": "wrong replacement",
    }
    rows = [{"sku": "state.foo"}]
    result = execute_tabular_feed_step(
        step={
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "create_dataset",
                    "rows": rows,
                    "result_target": "state.created",
                }
            },
        },
        state=state,
    )
    assert result["ok"] is True, result
    assert result["result"]["preview_rows"] == [{"sku": "state.foo"}]

    payload = _action_payload(
        {
            "operation": "transform",
            "input": {"dataset_handle": "dst_input"},
            "actions": [
                {
                    "action": "text.trim",
                    "target": "sku",
                    "when": {"column": "sku", "equals": "state.foo"},
                }
            ],
            "relational": [{"op": "filter", "when": {"column": "sku", "in": ["state.foo"]}}],
        },
        state,
    )
    assert payload["actions"][0]["when"] == {"column": "sku", "equals": "state.foo"}
    assert payload["relational"][0]["when"] == {"column": "sku", "in": ["state.foo"]}
