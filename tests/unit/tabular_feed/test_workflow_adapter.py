from __future__ import annotations

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.orchestration.tabular_feed_step_runtime import execute_tabular_feed_step


def test_tabular_feed_step_honors_legacy_output_mapping_for_scalar_handle():
    plane = ArtifactDatasetPlane()
    artifact, _ = plane.create_artifact(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="run_read",
        step_id="fetch",
        filename="feed.csv",
        content_type="text/csv",
        body=b"sku,qty\n00123,2\n",
    )

    result = execute_tabular_feed_step(
        step={
            "step_kind": "tabular_feed",
            "output_mapping_jsonb": {"dataset_handle": "state.feed_dataset"},
            "override": {
                "tabular_feed": {
                    "operation": "read",
                    "input": {"artifact_handle": "state.feed_artifact"},
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
    assert result["state_patch"]["feed_dataset"].startswith("dst_")


def test_tabular_feed_step_uses_trusted_scope_before_mutable_state():
    plane = ArtifactDatasetPlane()
    artifact, _ = plane.create_artifact(
        owner_scope={"account_id": "victim_account", "project_id": "victim_project"},
        run_id="run_read",
        step_id="fetch",
        filename="secret.csv",
        content_type="text/csv",
        body=b"secret\nTOPSECRET\n",
    )

    result = execute_tabular_feed_step(
        step={
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "inspect",
                    "input": {"artifact_handle": f"artifact:{artifact.artifact_id}"},
                    "result_target": "state.inspect_result",
                }
            },
        },
        state={
            "_artifact_plane": plane,
            "_trusted_runtime_context": {
                "account_id": "caller_account",
                "project_id": "caller_project",
            },
            "account_id": "victim_account",
            "project_id": "victim_project",
        },
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "access_denied"
