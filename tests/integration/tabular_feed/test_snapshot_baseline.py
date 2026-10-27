"""Snapshot publication binds an immutable candidate to its actual baseline."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import psycopg
import pytest

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.tabular_feed.context import TabularFeedExecutionContext
from core.application.tabular_feed.service import TabularFeedService
from core.application.tabular_feed.snapshot_models import SnapshotScope
from core.application.tabular_feed.snapshot_repository import SnapshotRepository
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.tabular_feed import engine_plane

pytestmark = pytest.mark.integration
_DEFAULT_REPOSITORY = object()


@pytest.fixture
def runtime(disposable_db):
    project_id = str(uuid.uuid4())
    with psycopg.connect(disposable_db.dsn) as conn:
        conn.execute(
            "INSERT INTO projects (id, account_id, name) VALUES (%s, %s, %s)",
            (project_id, DEFAULT_ACCOUNT_ID, project_id),
        )
        conn.commit()
        context = TabularFeedExecutionContext(
            account_id=DEFAULT_ACCOUNT_ID,
            project_id=project_id,
            workflow_id="feed",
            run_id="run",
            step_id="step",
        )
        yield SimpleNamespace(
            plane=ArtifactDatasetPlane(),
            repo=SnapshotRepository(conn),
            conn=conn,
            context=context,
            scope=SnapshotScope(DEFAULT_ACCOUNT_ID, project_id, "feed", "supplier"),
        )
        conn.rollback()
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


def _run(runtime, operation, payload, *, repository=_DEFAULT_REPOSITORY, context=None):
    repo = runtime.repo if repository is _DEFAULT_REPOSITORY else repository
    return TabularFeedService(plane=runtime.plane, snapshot_repository=repo).execute(
        operation=operation,
        payload={"input": payload},
        context=context or runtime.context,
    )


def _ok(runtime, operation, payload, **kwargs):
    value = _run(runtime, operation, payload, **kwargs)
    assert value["ok"], value
    return value["result"]


def _dataset(runtime, rows):
    return _ok(runtime, "create_dataset", {"rows": rows})["dataset_handle"]


def _read(runtime, artifact, **options):
    return _ok(runtime, "read", {"artifact_handle": artifact, **options})["dataset_handle"]


def _diff(runtime, current, baseline, ref=None, *, snapshot_mode=None):
    payload = {
        "dataset_handle": current,
        "baseline_dataset_handle": baseline,
        "profile_key": "supplier",
        "key_column": "sku",
        "compare_columns": ["price"],
        "enrichment_invalidate_on": ["price"],
    }
    if ref is not None:
        payload["expected_snapshot_ref"] = ref
    if snapshot_mode is not None:
        payload["snapshot_mode"] = snapshot_mode
    return _run(runtime, "diff", payload)


def _publish(runtime, rows, ref=None, baseline=None):
    if ref is None:
        ref = runtime.repo.read_current(runtime.scope).ref.as_dict()
    if baseline is None:
        baseline = (
            _read(runtime, ref["snapshot_artifact_id"])
            if ref["revision"]
            else _dataset(runtime, [])
        )
    result = _diff(runtime, _dataset(runtime, rows), baseline, ref)
    assert result["ok"], result
    candidate = result["result"]["snapshot_artifact_id"]
    receipt = _ok(
        runtime, "commit_snapshot", {"profile_key": "supplier", "snapshot_artifact_id": candidate}
    )
    return candidate, receipt["committed_snapshot_ref"]


def _rows(runtime, dataset):
    return _ok(runtime, "read_rows", {"dataset_handle": dataset})["rows"]


def _empty_keyed_dataset(runtime):
    return _ok(
        runtime,
        "transform",
        {
            "dataset_handle": _dataset(runtime, [{"sku": "placeholder", "price": "0"}]),
            "relational": [{"op": "filter", "when": {"column": "sku", "equals": "absent"}}],
        },
    )["dataset_handle"]


@pytest.mark.parametrize("advance_head", [False, True])
def test_empty_committed_baseline_retains_its_historical_positive_reference(
    runtime, monkeypatch, advance_head
):
    empty_current = _empty_keyed_dataset(runtime)
    initial = _diff(
        runtime, empty_current, _dataset(runtime, []), {"revision": 0, "snapshot_artifact_id": ""}
    )
    assert initial["ok"], initial
    artifact_a = initial["result"]["snapshot_artifact_id"]
    ref_a = _ok(
        runtime, "commit_snapshot", {"profile_key": "supplier", "snapshot_artifact_id": artifact_a}
    )["committed_snapshot_ref"]
    assert ref_a["revision"] == 1
    baseline_a = _read(runtime, artifact_a)
    assert _rows(runtime, baseline_a) == []
    latest = ref_a
    if advance_head:
        _artifact_b, latest = _publish(runtime, [{"sku": "B", "price": "20"}])
    with monkeypatch.context() as probe:
        probe.setattr(
            runtime.repo,
            "read_current",
            lambda *_args: pytest.fail("empty canonical baseline must not borrow current head"),
        )
        candidate = _diff(
            runtime,
            _dataset(runtime, [{"sku": "A", "price": "10"}]),
            baseline_a,
            snapshot_mode="publish",
        )
    assert candidate["ok"], candidate
    artifact = candidate["result"]["snapshot_artifact_id"]
    action = {
        "runtime_context": runtime.context.as_runtime_context(),
        "_trusted_core_context": True,
    }
    metadata = json.loads(
        runtime.plane._storage.get(engine_plane._candidate_meta_key(action, "supplier", artifact))
    )
    assert metadata["expected_snapshot_ref"] == ref_a
    committed = _run(
        runtime, "commit_snapshot", {"profile_key": "supplier", "snapshot_artifact_id": artifact}
    )
    if advance_head:
        assert committed["ok"] is False
        assert committed["errors"][0]["code"] == "snapshot_conflict"
        assert runtime.repo.read_head(runtime.scope).as_dict() == latest
    else:
        assert committed["ok"], committed
        assert committed["result"]["committed_snapshot_ref"] == {
            "revision": 2,
            "snapshot_artifact_id": artifact,
        }


def test_publication_mode_captures_empty_baseline_without_a_user_reference(runtime):
    current = _dataset(runtime, [{"sku": "A", "price": "10"}])
    empty = _dataset(runtime, [])
    candidate = _diff(runtime, current, empty, snapshot_mode="publish")
    assert candidate["ok"], candidate
    committed = _run(
        runtime,
        "commit_snapshot",
        {
            "profile_key": "supplier",
            "snapshot_artifact_id": candidate["result"]["snapshot_artifact_id"],
        },
    )
    assert committed["ok"], committed
    assert committed["result"]["committed_snapshot_ref"] == {
        "revision": 1,
        "snapshot_artifact_id": candidate["result"]["snapshot_artifact_id"],
    }


def test_publication_uses_historical_baseline_receipt_without_reading_current_head(
    runtime, monkeypatch
):
    artifact_a, ref_a = _publish(runtime, [{"sku": "A", "price": "10"}])
    baseline_a = _read(runtime, artifact_a)
    _artifact_b, ref_b = _publish(runtime, [{"sku": "A", "price": "20"}])
    current = _dataset(runtime, [{"sku": "A", "price": "11"}])
    with monkeypatch.context() as probe:
        probe.setattr(
            runtime.repo,
            "read_current",
            lambda *_args: pytest.fail("publication diff must not rebind from the current head"),
        )
        candidate = _diff(runtime, current, baseline_a, snapshot_mode="publish")
    assert candidate["ok"], candidate
    artifact = candidate["result"]["snapshot_artifact_id"]
    action = {
        "runtime_context": runtime.context.as_runtime_context(),
        "_trusted_core_context": True,
    }
    metadata = json.loads(
        runtime.plane._storage.get(engine_plane._candidate_meta_key(action, "supplier", artifact))
    )
    assert metadata["expected_snapshot_ref"] == ref_a
    stale = _run(
        runtime, "commit_snapshot", {"profile_key": "supplier", "snapshot_artifact_id": artifact}
    )
    assert stale["ok"] is False
    assert stale["errors"][0]["code"] == "snapshot_conflict"
    assert runtime.repo.read_head(runtime.scope).as_dict() == ref_b


@pytest.mark.parametrize("baseline_rows", [[], [{"sku": "A", "price": "9"}]])
def test_comparison_mode_keeps_arbitrary_baselines_noncommittable(runtime, baseline_rows):
    current = _dataset(runtime, [{"sku": "A", "price": "10"}])
    baseline = _dataset(runtime, baseline_rows)
    comparison = _run(
        runtime,
        "diff",
        {
            "dataset_handle": current,
            "baseline_dataset_handle": baseline,
            "profile_key": "supplier",
            "key_column": "sku",
            "snapshot_mode": "compare",
        },
        repository=None,
    )
    assert comparison["ok"], comparison
    commit = _run(
        runtime,
        "commit_snapshot",
        {
            "profile_key": "supplier",
            "snapshot_artifact_id": comparison["result"]["snapshot_artifact_id"],
        },
    )
    assert commit["ok"] is False
    assert commit["errors"][0]["code"] == "snapshot_baseline_required"
    assert runtime.repo.read_head(runtime.scope).revision == 0


@pytest.mark.parametrize("kind", ["mapped", "concat", "uncommitted", "uncommitted_empty"])
def test_publication_requires_a_committed_canonical_baseline(runtime, kind):
    artifact, ref = _publish(runtime, [{"sku": "A", "price": "10"}])
    baseline = _read(runtime, artifact)
    current = _dataset(runtime, [{"sku": "A", "price": "11"}])
    if kind == "mapped":
        baseline = _read(runtime, artifact, column_map={"sku": "sku"})
    elif kind == "concat":
        baseline = _ok(
            runtime,
            "concat",
            {"dataset_handles": [baseline, _dataset(runtime, [{"sku": "B", "price": "10"}])]},
        )["dataset_handle"]
    else:
        pending_current = _empty_keyed_dataset(runtime) if kind == "uncommitted_empty" else current
        pending = _diff(runtime, pending_current, baseline, ref)
        assert pending["ok"], pending
        baseline = _read(runtime, pending["result"]["snapshot_artifact_id"])
    assert _diff(runtime, current, baseline, snapshot_mode="compare")["ok"]
    blocked = _diff(runtime, current, baseline, snapshot_mode="publish")
    assert blocked["ok"] is False
    assert blocked["errors"][0]["code"] in {
        "snapshot_baseline_required",
        "snapshot_baseline_mismatch",
    }
    assert runtime.repo.read_head(runtime.scope).as_dict() == ref


@pytest.mark.parametrize("explicit", ["wrong_revision", "new_head"])
def test_explicit_publication_reference_must_match_historical_receipt(runtime, explicit):
    artifact, original = _publish(runtime, [{"sku": "A", "price": "10"}])
    baseline = _read(runtime, artifact)
    _new_artifact, latest = _publish(runtime, [{"sku": "A", "price": "20"}])
    supplied = (
        {**original, "revision": latest["revision"]} if explicit == "wrong_revision" else latest
    )
    result = _diff(
        runtime,
        _dataset(runtime, [{"sku": "A", "price": "11"}]),
        baseline,
        supplied,
        snapshot_mode="publish",
    )
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "snapshot_baseline_mismatch"
    assert runtime.repo.read_head(runtime.scope).as_dict() == latest


def test_publication_diff_requires_database_while_comparison_remains_available(runtime):
    current = _dataset(runtime, [{"sku": "A", "price": "10"}])
    baseline = _dataset(runtime, [])
    payload = {
        "dataset_handle": current,
        "baseline_dataset_handle": baseline,
        "profile_key": "supplier",
        "key_column": "sku",
    }
    unavailable = _run(runtime, "diff", {**payload, "snapshot_mode": "publish"}, repository=None)
    assert unavailable["ok"] is False
    assert unavailable["errors"][0]["code"] == "snapshot_storage_unavailable"
    assert _run(runtime, "diff", {**payload, "snapshot_mode": "compare"}, repository=None)["ok"]


def test_checksum_default_service_requires_database_authority(runtime):
    result = TabularFeedService(plane=runtime.plane).execute(
        operation="checksum",
        payload={"input": {"profile_key": "supplier", "raw_sha256": "raw"}},
        context=runtime.context,
    )
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "snapshot_storage_unavailable"


def test_checksum_returns_atomic_reference_and_preserves_checksum_fields(runtime):
    empty = _ok(runtime, "checksum", {"profile_key": "supplier", "raw_sha256": "raw"})
    assert empty["committed_snapshot_ref"] == {"revision": 0, "snapshot_artifact_id": ""}
    assert empty["unchanged"] is False
    artifact, ref = _publish(runtime, [{"sku": "A", "price": "10"}])
    current = _ok(runtime, "checksum", {"profile_key": "supplier", "raw_sha256": "raw"})
    assert current["committed_snapshot_ref"] == ref
    assert current["committed_snapshot_artifact_id"] == artifact
    assert "committed_raw_sha256" in current


def test_stale_baseline_cannot_borrow_new_head_reference_or_publish_old_candidate(runtime):
    artifact_a, ref_a = _publish(runtime, [{"sku": "A", "price": "10"}])
    baseline_a = _read(runtime, artifact_a)
    current = _dataset(runtime, [{"sku": "A", "price": "11"}])
    pending = _diff(runtime, current, baseline_a, ref_a)
    assert pending["ok"], pending
    artifact_b, ref_b = _publish(runtime, [{"sku": "A", "price": "12"}])
    forged = _diff(runtime, current, baseline_a, ref_b)
    assert forged["ok"] is False
    assert forged["errors"][0]["code"] == "snapshot_baseline_mismatch"
    stale = _run(
        runtime,
        "commit_snapshot",
        {
            "profile_key": "supplier",
            "snapshot_artifact_id": pending["result"]["snapshot_artifact_id"],
        },
    )
    assert stale["ok"] is False
    assert stale["errors"][0]["code"] == "snapshot_conflict"
    assert runtime.repo.read_head(runtime.scope).as_dict() == ref_b
    assert ref_b["snapshot_artifact_id"] == artifact_b


def test_pure_comparison_without_reference_is_available_but_not_committable(runtime):
    current = _dataset(runtime, [{"sku": "A", "price": "10"}])
    baseline = _dataset(runtime, [{"sku": "A", "price": "9"}])
    diff = _diff(runtime, current, baseline)
    assert diff["ok"], diff
    commit = _run(
        runtime,
        "commit_snapshot",
        {"profile_key": "supplier", "snapshot_artifact_id": diff["result"]["snapshot_artifact_id"]},
    )
    assert commit["ok"] is False
    assert commit["errors"][0]["code"] == "snapshot_baseline_required"
    assert runtime.repo.read_head(runtime.scope).revision == 0


@pytest.mark.parametrize("derived", ["mapped", "transform", "merge", "ordinary_parquet"])
def test_derived_or_arbitrary_parquet_baseline_cannot_publish(runtime, derived):
    artifact, ref = _publish(runtime, [{"sku": "A", "price": "10", "enriched_title": "Alpha"}])
    baseline = _read(runtime, artifact)
    if derived == "mapped":
        baseline = _read(runtime, artifact, column_map={"enriched_title": "old_title"})
    elif derived == "transform":
        baseline = _ok(
            runtime,
            "transform",
            {
                "dataset_handle": baseline,
                "actions": [{"action": "text.trim", "target": "enriched_title"}],
            },
        )["dataset_handle"]
    elif derived == "merge":
        baseline = _ok(
            runtime,
            "merge_rows",
            {
                "dataset_handle": baseline,
                "key_column": "sku",
                "rows": [{"sku": "A", "enriched_title": "Changed"}],
            },
        )["dataset_handle"]
    else:
        # An ordinary artifact may have identical Parquet bytes, filename and content type.
        meta = runtime.plane.get_artifact(artifact)
        handle, _ = runtime.plane.create_artifact(
            owner_scope=runtime.context.as_runtime_context()["owner_scope"],
            run_id="run",
            step_id="upload",
            filename=meta["filename"],
            content_type=meta["content_type"],
            body=runtime.plane._storage.get(meta["object_key"]),
        )
        baseline = _read(runtime, handle.artifact_id)
    current = _dataset(runtime, [{"sku": "A", "price": "11"}])
    assert _diff(runtime, current, baseline)["ok"] is True
    blocked = _diff(runtime, current, baseline, ref)
    assert blocked["ok"] is False
    assert blocked["errors"][0]["code"] == "snapshot_baseline_mismatch"
    assert runtime.plane.get_dataset(baseline).get("snapshot_baseline_eligible") is not True


def test_ref_zero_requires_actual_empty_rows_not_spoofed_metadata(runtime):
    current = _dataset(runtime, [{"sku": "A", "price": "10"}])
    nonempty = _dataset(runtime, [{"sku": "A", "price": "9"}])
    # Simulate stale metadata: eligibility checks must inspect actual bounded Parquet.
    runtime.plane._datasets[nonempty]["row_count"] = 0
    result = _diff(runtime, current, nonempty, {"revision": 0, "snapshot_artifact_id": ""})
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "snapshot_baseline_mismatch"
    empty = _dataset(runtime, [])
    assert _diff(runtime, current, empty, {"revision": 0, "snapshot_artifact_id": ""})["ok"]


def test_baseline_eligibility_is_server_owned_and_survives_plane_restart(runtime):
    artifact, _ref = _publish(runtime, [{"sku": "A", "price": "10"}])
    runtime.plane = ArtifactDatasetPlane(storage=runtime.plane._storage)
    baseline = _read(runtime, artifact)
    meta = runtime.plane.get_dataset(baseline)
    assert meta["source_artifact_id"] == artifact
    assert meta["snapshot_baseline_eligible"] is True
    runtime.plane = ArtifactDatasetPlane(storage=runtime.plane._storage)
    assert runtime.plane.get_dataset(baseline)["snapshot_baseline_eligible"] is True
    fake = _ok(
        runtime,
        "create_dataset",
        {
            "rows": [{"sku": "A"}],
            "source_artifact_id": artifact,
            "snapshot_baseline_eligible": True,
        },
    )["dataset_handle"]
    assert runtime.plane.get_dataset(fake).get("snapshot_baseline_eligible") is not True


def test_database_absence_fails_closed_but_pure_plane_operations_work(runtime):
    assert _ok(runtime, "create_dataset", {"rows": [{"sku": "A"}]}, repository=None)
    checksum = _run(
        runtime, "checksum", {"profile_key": "supplier", "raw_sha256": "raw"}, repository=None
    )
    assert checksum["ok"] is False
    assert checksum["errors"][0]["code"] == "snapshot_storage_unavailable"
    artifact, _ = _publish(runtime, [{"sku": "A", "price": "10"}])
    commit = _run(
        runtime,
        "commit_snapshot",
        {"profile_key": "supplier", "snapshot_artifact_id": artifact},
        repository=None,
    )
    assert commit["ok"] is False
    assert commit["errors"][0]["code"] == "snapshot_storage_unavailable"


def test_existing_development_pointer_requires_explicit_transition(runtime):
    action = {"runtime_context": runtime.context.as_runtime_context()}
    key = engine_plane._pointer_key(action, "supplier")
    original = json.dumps(
        {"snapshot_artifact_id": "old", "metadata": {"raw_sha256": "raw"}}
    ).encode()
    runtime.plane._storage.put(key, original, content_type="application/json")
    checksum = _run(runtime, "checksum", {"profile_key": "supplier", "raw_sha256": "raw"})
    assert checksum["ok"] is False
    assert checksum["errors"][0]["code"] == "snapshot_dev_data_transition_required"
    assert runtime.plane._storage.get(key) == original
    assert runtime.repo.read_head(runtime.scope).revision == 0


@pytest.mark.parametrize("deletion", ["artifact", "body"])
def test_receipt_replay_rechecks_artifact_access_and_preserves_latest_head(runtime, deletion):
    artifact_a, ref_a = _publish(runtime, [{"sku": "A", "price": "10"}])
    artifact_b, ref_b = _publish(runtime, [{"sku": "A", "price": "11"}])
    duplicate = _ok(
        runtime, "commit_snapshot", {"profile_key": "supplier", "snapshot_artifact_id": artifact_a}
    )
    assert duplicate["committed_snapshot_ref"] == ref_a
    assert runtime.repo.read_head(runtime.scope).as_dict() == ref_b
    denied = _run(
        runtime,
        "commit_snapshot",
        {"profile_key": "supplier", "snapshot_artifact_id": artifact_a},
        context=replace(runtime.context, project_id=str(uuid.uuid4())),
    )
    assert denied["ok"] is False
    assert denied["errors"][0]["code"] == "access_denied"
    if deletion == "artifact":
        runtime.plane.delete_artifact(artifact_a)
    else:
        runtime.plane._storage.delete(runtime.plane.get_artifact(artifact_a)["object_key"])
    missing = _run(
        runtime, "commit_snapshot", {"profile_key": "supplier", "snapshot_artifact_id": artifact_a}
    )
    assert missing["ok"] is False
    assert runtime.repo.read_head(runtime.scope).snapshot_artifact_id == artifact_b


@pytest.mark.parametrize(
    "current_value,expected",
    [(None, "Alpha"), ("", "Alpha"), ("  ", "Alpha"), ("Explicit", "Explicit")],
)
def test_candidate_parquet_carries_unchanged_enrichment_and_preserves_explicit_values(
    runtime, current_value, expected
):
    _artifact, ref = _publish(runtime, [{"sku": "A", "price": "10", "enriched_title": "Alpha"}])
    candidate, next_ref = _publish(
        runtime, [{"sku": "A", "price": "10", "enriched_title": current_value}]
    )
    rows = _rows(runtime, _read(runtime, candidate))
    assert rows == [{"sku": "A", "price": "10", "enriched_title": expected}]
    assert next_ref["revision"] == ref["revision"] + 1
    assert runtime.plane.get_artifact(candidate)["retention_class"] == "persistent"


def test_candidate_parquet_handles_missing_enrichment_invalidation_added_and_false_zero(runtime):
    _publish(
        runtime,
        [
            {"sku": "A", "price": "10", "enriched_title": "Alpha"},
            {"sku": "B", "price": "20", "enriched_title": "Beta"},
        ],
    )
    candidate, _ = _publish(
        runtime,
        [{"sku": "A", "price": "10"}, {"sku": "B", "price": "21"}, {"sku": "C", "price": "30"}],
    )
    rows = sorted(_rows(runtime, _read(runtime, candidate)), key=lambda row: row["sku"])
    assert rows == [
        {"sku": "A", "price": "10", "enriched_title": "Alpha"},
        {"sku": "B", "price": "21", "enriched_title": None},
        {"sku": "C", "price": "30", "enriched_title": None},
    ]
    _publish(runtime, [{"sku": "A", "price": "10", "enriched_rank": 5, "enriched_flag": True}])
    candidate, _ = _publish(
        runtime, [{"sku": "A", "price": "10", "enriched_rank": 0, "enriched_flag": False}]
    )
    row = _rows(runtime, _read(runtime, candidate))[0]
    assert row["enriched_rank"] == 0
    assert row["enriched_flag"] is False


@pytest.mark.parametrize(
    "ref_selector",
    [
        None,
        "state.checksum.committed_snapshot_ref",
        {"source": "state.checksum.committed_snapshot_ref"},
    ],
)
def test_workflow_exports_complete_final_candidate_and_commits_that_same_artifact(
    runtime, ref_selector
):
    from core.application.orchestration.tabular_feed_step_runtime import execute_tabular_feed_step

    _publish(
        runtime,
        [
            {"sku": "A", "price": "10", "enriched_title": "Alpha"},
            {"sku": "B", "price": "20", "enriched_title": "Beta"},
        ],
    )
    ref = runtime.repo.read_head(runtime.scope).as_dict()
    baseline = _read(runtime, ref["snapshot_artifact_id"])
    current = _dataset(
        runtime,
        [{"sku": "A", "price": "10"}, {"sku": "B", "price": "21"}],
    )
    state = {
        "_artifact_plane": runtime.plane,
        "_snapshot_repository": runtime.repo,
        "trusted_runtime_context": runtime.context.as_runtime_context(),
        "workflow_data": {
            "current": current,
            "baseline": baseline,
        },
    }

    def run_step(name, config):
        step = {
            "step_id": name,
            "step_kind": "tabular_feed",
            "override": {"tabular_feed": {**config, "result_target": f"state.{name}"}},
        }
        outcome = execute_tabular_feed_step(step=step, state=state)
        assert outcome["ok"], outcome
        state["workflow_data"].update(outcome["state_patch"]["workflow_data"])
        return state["workflow_data"][name]

    source = run_step(
        "source_write",
        {"operation": "write", "format": "csv", "input": {"dataset_handle": "state.current"}},
    )
    source_meta = runtime.plane.get_artifact(source["artifact_handle"])
    assert (
        source["sha256"]
        == hashlib.sha256(runtime.plane._storage.get(source_meta["object_key"])).hexdigest()
    )
    source_read = run_step(
        "source_read",
        {"operation": "read", "input": {"artifact_handle": "state.source_write.artifact_handle"}},
    )
    state["workflow_data"]["current"] = source_read["dataset_handle"]
    first_checksum = run_step(
        "checksum",
        {
            "operation": "checksum",
            "profile_key": "supplier",
            "raw_sha256": "state.source_write.sha256",
        },
    )
    assert first_checksum["unchanged"] is False
    assert first_checksum["committed_snapshot_ref"] == ref
    comparison = {
        "operation": "diff",
        "snapshot_mode": "publish",
        "raw_sha256": "state.source_write.sha256",
        "profile_key": "supplier",
        "key_column": "sku",
        "compare_columns": ["price"],
        "enrichment_invalidate_on": ["price"],
    }
    original_baseline_input = {
        "dataset_handle": "state.current",
        "baseline_dataset_handle": "state.baseline",
    }
    if ref_selector is not None:
        original_baseline_input["expected_snapshot_ref"] = ref_selector
    preliminary = run_step("preliminary_diff", {**comparison, "input": original_baseline_input})
    preliminary_candidate = preliminary["snapshot_artifact_id"]
    preliminary_key = runtime.plane.get_artifact(preliminary_candidate)["object_key"]
    preliminary_bytes = runtime.plane._storage.get(preliminary_key)
    preliminary_rows = sorted(
        _rows(runtime, _read(runtime, preliminary_candidate)), key=lambda row: row["sku"]
    )
    assert preliminary_rows == [
        {"sku": "A", "price": "10", "enriched_title": "Alpha"},
        {"sku": "B", "price": "21", "enriched_title": None},
    ]
    selected = run_step(
        "enrichment_page",
        {
            "operation": "read_rows",
            "input": {"dataset_handle": "state.preliminary_diff.diff_dataset_handle"},
            "filter": {"status_in": ["changed"], "missing": ["enriched_title"]},
            "columns": ["sku", "price", "enriched_title"],
            "limit": 1,
        },
    )
    assert selected["rows"] == [{"sku": "B", "price": "21", "enriched_title": None}]
    assert selected["returned"] == selected["total_matching"] == 1
    assert selected["next_offset"] is None
    # Supply a deterministic enrichment result for the selected row; no provider call.
    state["workflow_data"]["enrichment_updates"] = [
        {"key_column_value": row["sku"], "columns_to_set": {"enriched_title": "New Beta"}}
        for row in selected["rows"]
    ]
    merged = run_step(
        "enriched_current",
        {
            "operation": "merge_rows",
            "input": {"dataset_handle": "state.current"},
            "key_column": "sku",
            "rows": "state.enrichment_updates",
        },
    )
    assert merged["merged"] == 1
    assert merged["row_count"] == 2
    assert merged["dataset_handle"] != current
    assert _rows(runtime, current) == [
        {"sku": "A", "price": "10"},
        {"sku": "B", "price": "21"},
    ]
    assert runtime.repo.read_head(runtime.scope).as_dict() == ref
    assert state["workflow_data"]["baseline"] == baseline
    assert state["workflow_data"]["checksum"]["committed_snapshot_ref"] == ref
    run_step(
        "final_diff",
        {
            **comparison,
            "input": {
                **original_baseline_input,
                "dataset_handle": "state.enriched_current.dataset_handle",
            },
        },
    )
    run_step(
        "final_read",
        {
            "operation": "read",
            "input": {"artifact_handle": "state.final_diff.snapshot_artifact_id"},
        },
    )
    run_step(
        "exported",
        {
            "operation": "write",
            "format": "csv",
            "input": {"dataset_handle": "state.final_read.dataset_handle"},
        },
    )
    run_step(
        "committed",
        {
            "operation": "commit_snapshot",
            "input": {
                "profile_key": "supplier",
                "snapshot_artifact_id": "state.final_diff.snapshot_artifact_id",
            },
        },
    )
    repeated_checksum = run_step(
        "repeated_checksum",
        {
            "operation": "checksum",
            "profile_key": "supplier",
            "raw_sha256": "state.source_write.sha256",
        },
    )
    assert repeated_checksum["unchanged"] is True
    assert repeated_checksum["committed_raw_sha256"] == source["sha256"]
    outputs = state["workflow_data"]
    final_candidate = outputs["final_diff"]["snapshot_artifact_id"]
    assert repeated_checksum["committed_snapshot_artifact_id"] == final_candidate
    assert final_candidate != preliminary_candidate
    assert runtime.plane._storage.get(preliminary_key) == preliminary_bytes
    assert (
        sorted(_rows(runtime, _read(runtime, preliminary_candidate)), key=lambda row: row["sku"])
        == preliminary_rows
    )
    assert outputs["final_diff"]["summary"]["unchanged"] == 1
    assert outputs["committed"]["snapshot_artifact_id"] == final_candidate
    assert runtime.repo.read_head(runtime.scope).snapshot_artifact_id == final_candidate
    exported_rows = sorted(
        _rows(runtime, _read(runtime, outputs["exported"]["artifact_handle"])),
        key=lambda row: row["sku"],
    )
    candidate_rows = sorted(
        _rows(runtime, _read(runtime, final_candidate)), key=lambda row: row["sku"]
    )
    assert (
        exported_rows
        == candidate_rows
        == [
            {"sku": "A", "price": "10", "enriched_title": "Alpha"},
            {"sku": "B", "price": "21", "enriched_title": "New Beta"},
        ]
    )


def test_disconnected_database_is_an_explicit_error_not_object_pointer_fallback(
    runtime, disposable_db
):
    conn = psycopg.connect(disposable_db.dsn)
    conn.close()
    response = _run(
        runtime,
        "checksum",
        {"profile_key": "supplier", "raw_sha256": "raw"},
        repository=SnapshotRepository(conn),
    )
    assert response["ok"] is False
    assert response["errors"][0]["code"] == "snapshot_storage_unavailable"


def test_runtime_failure_preserves_injected_callers_uncommitted_transaction(runtime):
    from core.application.orchestration.tabular_feed_step_runtime import execute_tabular_feed_step

    runtime.conn.execute(
        "UPDATE projects SET description = %s WHERE id = %s",
        ("caller-owned pending write", runtime.context.project_id),
    )
    state = {
        "_artifact_plane": runtime.plane,
        "_snapshot_repository": runtime.repo,
        "trusted_runtime_context": runtime.context.as_runtime_context(),
    }
    outcome = execute_tabular_feed_step(
        step={
            "step_id": "invalid-commit",
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "commit_snapshot",
                    "input": {"profile_key": "supplier", "snapshot_artifact_id": "art_missing"},
                    "result_target": "state.committed",
                }
            },
        },
        state=state,
    )
    assert outcome["ok"] is False
    assert (
        runtime.conn.execute(
            "SELECT description FROM projects WHERE id = %s", (runtime.context.project_id,)
        ).fetchone()[0]
        == "caller-owned pending write"
    )
    runtime.conn.rollback()
    assert (
        runtime.conn.execute(
            "SELECT description FROM projects WHERE id = %s", (runtime.context.project_id,)
        ).fetchone()[0]
        == ""
    )


def test_production_snapshot_context_rolls_back_failed_receipt_insert(
    runtime, disposable_db, monkeypatch
):
    from core.application.orchestration.tabular_feed_step_runtime import execute_tabular_feed_step

    _artifact_a, ref_a = _publish(runtime, [{"sku": "A", "price": "10"}])
    baseline = _read(runtime, ref_a["snapshot_artifact_id"])
    pending = _diff(runtime, _dataset(runtime, [{"sku": "A", "price": "11"}]), baseline, ref_a)
    assert pending["ok"], pending
    candidate = pending["result"]["snapshot_artifact_id"]
    runtime.conn.commit()
    function = "reject_snapshot_" + uuid.uuid4().hex
    trigger = "reject_snapshot_" + uuid.uuid4().hex
    runtime.conn.execute(
        psycopg.sql.SQL(
            "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'receipt insertion unavailable'; END $$"
        ).format(psycopg.sql.Identifier(function))
    )
    runtime.conn.execute(
        psycopg.sql.SQL(
            "CREATE TRIGGER {} BEFORE INSERT ON fs_tabular_snapshot_commits "
            "FOR EACH ROW EXECUTE FUNCTION {}()"
        ).format(psycopg.sql.Identifier(trigger), psycopg.sql.Identifier(function))
    )
    runtime.conn.commit()
    from core.infrastructure.tabular_feed.storage import CONFIG

    monkeypatch.setitem(CONFIG, "fs_db_dsn", disposable_db.dsn)
    try:
        outcome = execute_tabular_feed_step(
            step={
                "step_id": "commit",
                "step_kind": "tabular_feed",
                "override": {
                    "tabular_feed": {
                        "operation": "commit_snapshot",
                        "input": {"profile_key": "supplier", "snapshot_artifact_id": candidate},
                        "result_target": "state.committed",
                    }
                },
            },
            state={
                "_artifact_plane": runtime.plane,
                "trusted_runtime_context": runtime.context.as_runtime_context(),
            },
        )
        assert outcome["ok"] is False
        assert outcome["errors"][0]["code"] == "snapshot_storage_unavailable"
        assert runtime.repo.read_head(runtime.scope).as_dict() == ref_a
        assert (
            runtime.conn.execute(
                "SELECT count(*) FROM fs_tabular_snapshot_commits WHERE candidate_artifact_id = %s",
                (candidate,),
            ).fetchone()[0]
            == 0
        )
    finally:
        runtime.conn.rollback()
        runtime.conn.execute(
            psycopg.sql.SQL("DROP TRIGGER {} ON fs_tabular_snapshot_commits").format(
                psycopg.sql.Identifier(trigger)
            )
        )
        runtime.conn.execute(
            psycopg.sql.SQL("DROP FUNCTION {}()").format(psycopg.sql.Identifier(function))
        )
        runtime.conn.commit()
