from __future__ import annotations

import hashlib
import io
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
from core.application.jobs.job_artifact_access import artifact_metadata_is_downloadable
from core.application.tabular_feed.context import TabularFeedExecutionContext
from core.application.tabular_feed.service import TabularFeedService
from core.application.tabular_feed.snapshot_repository import SnapshotRepository
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.tabular_feed import engine, engine_plane, engine_snapshots

pytestmark = [pytest.mark.integration]

OWNER_SCOPE = {
    "account_id": "acct_tabular",
    "project_id": "proj_tabular",
    "workflow_id": "wf_tabular",
}


def _context(**overrides: str) -> TabularFeedExecutionContext:
    values = {
        "account_id": OWNER_SCOPE["account_id"],
        "project_id": OWNER_SCOPE["project_id"],
        "workflow_id": OWNER_SCOPE["workflow_id"],
        "run_id": "run_tabular",
        "step_id": "step_tabular",
    }
    values.update(overrides)
    return TabularFeedExecutionContext(**values)


def _execute(
    plane: ArtifactDatasetPlane,
    operation: str,
    payload: dict[str, Any],
    *,
    context: TabularFeedExecutionContext | None = None,
) -> dict[str, Any]:
    return TabularFeedService(
        plane=plane, snapshot_repository=getattr(plane, "_test_snapshot_repository", None)
    ).execute(
        operation=operation,
        payload={"input": dict(payload)},
        context=context or _context(),
    )


@pytest.fixture
def snapshot_plane(disposable_db, monkeypatch):
    project_id = str(uuid.uuid4())
    with psycopg.connect(disposable_db.dsn) as conn:
        conn.execute(
            "INSERT INTO projects (id, account_id, name) VALUES (%s, %s, %s)",
            (project_id, DEFAULT_ACCOUNT_ID, project_id),
        )
        conn.commit()
        monkeypatch.setitem(OWNER_SCOPE, "account_id", DEFAULT_ACCOUNT_ID)
        monkeypatch.setitem(OWNER_SCOPE, "project_id", project_id)
        plane = ArtifactDatasetPlane()
        plane._test_snapshot_repository = SnapshotRepository(conn)
        yield plane
        conn.rollback()
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


def _artifact(
    plane: ArtifactDatasetPlane,
    body: bytes,
    *,
    filename: str = "feed.csv",
    content_type: str = "text/csv",
    owner_scope: dict[str, Any] | None = None,
) -> str:
    handle, _meta = plane.create_artifact(
        owner_scope=dict(owner_scope or OWNER_SCOPE),
        run_id="test",
        step_id="upload",
        filename=filename,
        content_type=content_type,
        retention_class="run_window",
        body=body,
    )
    return handle.artifact_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("dataset_handle", None),
        ("dataset_handle", " "),
        ("baseline_dataset_handle", None),
        ("baseline_dataset_handle", " "),
        ("dataset_handle", "dst_missing"),
        ("baseline_dataset_handle", "dst_missing"),
    ],
)
def test_diff_requires_two_explicit_resolvable_dataset_handles(field, value) -> None:
    plane = ArtifactDatasetPlane()
    dataset = _execute(plane, "create_dataset", {"rows": [{"sku": "A1", "price": "10"}]})
    handle = dataset["result"]["dataset_handle"]
    payload = {
        "dataset_handle": handle,
        "baseline_dataset_handle": handle,
        "profile_key": "explicit-diff",
        "key_column": "sku",
    }
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value
    result = _execute(plane, "diff", payload)
    assert result["ok"] is False
    assert result["errors"][0]["path"] == f"input.{field}"
    assert field in result["errors"][0]["message"]
    assert "snapshot_artifact_id" not in result["result"]


@pytest.mark.parametrize("field", ["dataset_handle", "baseline_dataset_handle"])
def test_diff_rejects_cross_project_dataset_with_field_path(field) -> None:
    plane = ArtifactDatasetPlane()
    own = _execute(plane, "create_dataset", {"rows": [{"sku": "A1"}]})
    foreign = _execute(
        plane,
        "create_dataset",
        {"rows": [{"sku": "A1"}]},
        context=_context(project_id="proj_other"),
    )
    payload = {
        "dataset_handle": own["result"]["dataset_handle"],
        "baseline_dataset_handle": own["result"]["dataset_handle"],
        "profile_key": "scoped-diff",
        "key_column": "sku",
    }
    payload[field] = foreign["result"]["dataset_handle"]
    result = _execute(plane, "diff", payload)
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "access_denied"
    assert result["errors"][0]["path"] == f"input.{field}"


def test_diff_uses_explicit_baseline_without_loading_a_profile_snapshot(
    monkeypatch,
) -> None:
    plane = ArtifactDatasetPlane()
    current = _execute(plane, "create_dataset", {"rows": [{"sku": "A1", "price": "10"}]})
    baseline = _execute(plane, "create_dataset", {"rows": [{"sku": "A1", "price": "9"}]})

    def reject_implicit_pointer(*_args):
        raise AssertionError("diff must not load a profile pointer")

    monkeypatch.setattr(engine, "_current_snapshot", reject_implicit_pointer)
    result = _execute(
        plane,
        "diff",
        {
            "dataset_handle": current["result"]["dataset_handle"],
            "baseline_dataset_handle": baseline["result"]["dataset_handle"],
            "profile_key": "explicit-diff",
            "key_column": "sku",
            "compare_columns": ["price"],
        },
    )
    assert result["ok"] is True
    assert result["result"]["summary"] == {
        "added": 0,
        "changed": 1,
        "removed": 0,
        "unchanged": 0,
        "total": 1,
    }


def test_core_rejects_cross_account_and_cross_project_handle_reads() -> None:
    plane = ArtifactDatasetPlane()
    artifact_id = _artifact(plane, b"sku,price\nA1,10\n")

    cross_account = _execute(
        plane,
        "inspect",
        {"artifact_handle": artifact_id},
        context=_context(account_id="acct_other"),
    )
    cross_project = _execute(
        plane,
        "inspect",
        {"artifact_handle": artifact_id},
        context=_context(project_id="proj_other"),
    )

    assert cross_account["ok"] is False
    assert cross_account["errors"][0]["code"] == "access_denied"
    assert cross_account["errors"][0]["message"] == "artifact_access_denied"
    assert cross_project["ok"] is False
    assert cross_project["errors"][0]["code"] == "access_denied"


def test_core_ignores_payload_scope_overrides() -> None:
    plane = ArtifactDatasetPlane()
    artifact_id = _artifact(
        plane,
        b"sku,price\nA1,10\n",
        owner_scope={"account_id": "acct_other", "project_id": "proj_other"},
    )

    spoofed = TabularFeedService(plane=plane).execute(
        operation="inspect",
        payload={
            "input": {"artifact_handle": artifact_id},
            "runtime_context": {"account_id": "acct_other", "project_id": "proj_other"},
            "scope": {"account_id": "acct_other", "project_id": "proj_other"},
        },
        context=_context(account_id="acct_tabular", project_id="proj_tabular"),
    )

    assert spoofed["ok"] is False
    assert spoofed["errors"][0]["code"] == "access_denied"


def test_snapshot_pointers_are_scoped_and_checksum_alias_preserves_result_fields(
    snapshot_plane,
) -> None:
    plane = snapshot_plane
    feed = b"sku,price\nA1,10\n"
    raw_sha = hashlib.sha256(feed).hexdigest()
    artifact_id = _artifact(plane, feed)
    read = _execute(plane, "read", {"artifact_handle": artifact_id, "key_column": "sku"})
    empty_baseline = _execute(plane, "create_dataset", {"rows": []})
    diff = _execute(
        plane,
        "diff",
        {
            "dataset_handle": read["result"]["dataset_handle"],
            "baseline_dataset_handle": empty_baseline["result"]["dataset_handle"],
            "expected_snapshot_ref": {"revision": 0, "snapshot_artifact_id": ""},
            "profile_key": "shared-profile",
            "key_column": "sku",
            "raw_sha256": raw_sha,
        },
    )
    committed = _execute(
        plane,
        "commit_snapshot",
        {
            "profile_key": "shared-profile",
            "snapshot_artifact_id": diff["result"]["snapshot_artifact_id"],
        },
    )

    assert committed["ok"] is True

    unchanged = _execute(
        plane,
        "checksum",
        {"profile_key": "shared-profile", "raw_sha256": raw_sha},
    )
    assert unchanged["result"]["unchanged"] is True
    assert (
        unchanged["result"]["committed_snapshot_artifact_id"]
        == diff["result"]["snapshot_artifact_id"]
    )

    other_scope = _context(project_id=str(uuid.uuid4()))
    other_check = _execute(
        plane,
        "checksum",
        {"profile_key": "shared-profile", "raw_sha256": raw_sha},
        context=other_scope,
    )
    assert other_check["ok"] is True
    assert other_check["result"]["unchanged"] is False
    assert "committed_snapshot_artifact_id" in other_check["result"]


def test_snapshot_scope_namespace_is_injective_for_segment_boundaries() -> None:
    left = {
        "runtime_context": {
            "account_id": "a_b",
            "project_id": "c",
            "workflow_id": "wf",
        }
    }
    right = {
        "runtime_context": {
            "account_id": "a",
            "project_id": "b_c",
            "workflow_id": "wf",
        }
    }

    assert engine_plane._pointer_key(left, "shared") != engine_plane._pointer_key(right, "shared")


def test_csv_transform_diff_commit_read_rows_and_write_pipeline(snapshot_plane) -> None:
    plane = snapshot_plane
    feed = (
        b"sku,availability,qty,price,category,brand\n"
        b"A1,in stock,10,100,chairs,ACME Corp\n"
        b"B2,out of stock,3,50,tables,Other\n"
    )
    raw_sha = hashlib.sha256(feed).hexdigest()
    artifact_id = _artifact(plane, feed)

    inspected = _execute(plane, "inspect", {"artifact_handle": artifact_id})
    assert inspected["result"]["columns"][0]["name"] == "sku"
    assert inspected["result"]["row_count_estimate"] == 2

    read = _execute(plane, "read", {"artifact_handle": artifact_id, "key_column": "sku"})
    rules = [
        {"target": "stock_qty", "when": {"column": "availability", "equals": "in stock"}, "set": 1},
        {
            "target": "stock_qty",
            "when": {"column": "availability", "equals": "out of stock"},
            "set": 0,
        },
        {"target": "price", "multiply": 1.21},
        {"target": "price", "round": {"decimals": 2, "mode": "half_up"}},
        {"target": "stock_qty", "floor_multiply": 0.1},
        {
            "map_values": {
                "column": "brand",
                "mapping": {"ACME Corp": "ACME"},
                "keep_unmapped": True,
            }
        },
    ]
    transformed = _execute(
        plane,
        "transform",
        {"dataset_handle": read["result"]["dataset_handle"], "rules": rules},
    )

    assert transformed["ok"] is True
    assert transformed["result"]["applied_rules_report"]["0"] == 1

    empty_baseline = _execute(
        plane, "read", {"artifact_handle": _artifact(plane, b"sku\n"), "key_column": "sku"}
    )
    assert empty_baseline["ok"] is True
    diff = _execute(
        plane,
        "diff",
        {
            "dataset_handle": transformed["result"]["dataset_handle"],
            "baseline_dataset_handle": empty_baseline["result"]["dataset_handle"],
            "expected_snapshot_ref": {"revision": 0, "snapshot_artifact_id": ""},
            "profile_key": "client-feed",
            "key_column": "sku",
            "raw_sha256": raw_sha,
        },
    )
    assert diff["result"]["summary"] == {
        "added": 2,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
        "total": 2,
    }

    page = _execute(
        plane,
        "read_rows",
        {
            "dataset_handle": diff["result"]["diff_dataset_handle"],
            "filter": {"status_in": ["added"]},
            "columns": ["sku", "__status", "price", "brand"],
            "limit": 1,
        },
    )
    assert page["result"]["returned"] == 1
    assert page["result"]["next_offset"] == 1

    committed = _execute(
        plane,
        "commit_snapshot",
        {
            "profile_key": "client-feed",
            "snapshot_artifact_id": diff["result"]["snapshot_artifact_id"],
        },
    )
    assert committed["result"]["committed"] is True
    baseline = _execute(
        plane,
        "read",
        {"artifact_handle": committed["result"]["snapshot_artifact_id"], "key_column": "sku"},
    )
    assert baseline["ok"] is True
    second_diff = _execute(
        plane,
        "diff",
        {
            "dataset_handle": transformed["result"]["dataset_handle"],
            "baseline_dataset_handle": baseline["result"]["dataset_handle"],
            "expected_snapshot_ref": committed["result"]["committed_snapshot_ref"],
            "profile_key": "client-feed",
            "key_column": "sku",
            "raw_sha256": raw_sha,
        },
    )
    assert second_diff["result"]["summary"]["unchanged"] == 2
    assert second_diff["result"]["summary"]["total"] == 0

    exported = _execute(
        plane,
        "write",
        {
            "dataset_handle": transformed["result"]["dataset_handle"],
            "format": "csv",
            "options": {"columns": ["sku", "stock_qty", "price", "brand"]},
        },
    )
    assert exported["result"]["artifact_handle"].startswith("art_")
    assert exported["result"]["row_count"] == 2


def test_diff_streams_parquet_datasets_and_snapshots_without_python_row_materialization(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_plane,
) -> None:
    plane = snapshot_plane
    first_artifact = _artifact(plane, b"sku,price,enriched_title\nA1,10,Alpha\nB2,20,Beta\n")
    first_dataset = _execute(
        plane,
        "read",
        {"artifact_handle": first_artifact, "key_column": "sku"},
    )

    def reject_row_materialization(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("diff must not materialize complete datasets as Python rows")

    monkeypatch.setattr(engine, "_read_dataset_rows", reject_row_materialization)
    monkeypatch.setattr(engine_snapshots, "_snapshot_rows", reject_row_materialization)

    empty_baseline = _execute(
        plane, "read", {"artifact_handle": _artifact(plane, b"sku\n"), "key_column": "sku"}
    )
    assert empty_baseline["ok"] is True
    first_diff = _execute(
        plane,
        "diff",
        {
            "dataset_handle": first_dataset["result"]["dataset_handle"],
            "baseline_dataset_handle": empty_baseline["result"]["dataset_handle"],
            "expected_snapshot_ref": {"revision": 0, "snapshot_artifact_id": ""},
            "profile_key": "streaming-diff",
            "key_column": "sku",
            "compare_columns": ["price"],
        },
    )
    assert first_diff["result"]["summary"] == {
        "added": 2,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
        "total": 2,
    }
    snapshot_id = first_diff["result"]["snapshot_artifact_id"]
    snapshot_meta = plane.get_artifact(snapshot_id)
    assert snapshot_meta is not None
    assert snapshot_meta["filename"].endswith(".parquet")
    assert snapshot_meta["content_type"] == engine.PARQUET_CONTENT_TYPE

    committed = _execute(
        plane,
        "commit_snapshot",
        {
            "profile_key": "streaming-diff",
            "snapshot_artifact_id": snapshot_id,
        },
    )
    assert committed["result"]["committed"] is True

    baseline = _execute(plane, "read", {"artifact_handle": snapshot_id, "key_column": "sku"})
    assert baseline["ok"] is True
    second_artifact = _artifact(
        plane,
        b"sku,price,enriched_title\nA1,11,\nC3,30,Gamma\n",
    )
    second_dataset = _execute(
        plane,
        "read",
        {"artifact_handle": second_artifact, "key_column": "sku"},
    )
    second_diff = _execute(
        plane,
        "diff",
        {
            "dataset_handle": second_dataset["result"]["dataset_handle"],
            "baseline_dataset_handle": baseline["result"]["dataset_handle"],
            "expected_snapshot_ref": committed["result"]["committed_snapshot_ref"],
            "profile_key": "streaming-diff",
            "key_column": "sku",
            "compare_columns": ["price"],
            "enrichment_invalidate_on": ["price"],
        },
    )
    assert second_diff["result"]["summary"] == {
        "added": 1,
        "changed": 1,
        "removed": 1,
        "unchanged": 0,
        "total": 3,
    }
    changed_page = _execute(
        plane,
        "read_rows",
        {
            "dataset_handle": second_diff["result"]["diff_dataset_handle"],
            "filter": {"status_in": ["changed"]},
            "columns": ["sku", "price", "__status"],
            "limit": 10,
        },
    )
    assert changed_page["result"] == {
        "rows": [{"sku": "A1", "price": "11", "__status": "changed"}],
        "returned": 1,
        "total_matching": 1,
        "next_offset": None,
    }


@pytest.mark.parametrize(
    ("fmt", "filename", "expected_prefix"),
    [
        ("csv", "marketplace.csv", b"sku,price\n"),
        ("tsv", "marketplace.tsv", b"sku\tprice\n"),
        ("ndjson", "marketplace.jsonl", b'{"sku":"A1","price":"10"}'),
        ("parquet", "marketplace.parquet", b"PAR1"),
    ],
)
def test_write_streams_large_output_formats_without_dataframe_materialization(
    monkeypatch: pytest.MonkeyPatch,
    fmt: str,
    filename: str,
    expected_prefix: bytes,
) -> None:
    plane = ArtifactDatasetPlane()
    created = _execute(
        plane,
        "create_dataset",
        {"rows": [{"sku": "A1", "price": "10"}, {"sku": "B2", "price": "20"}]},
    )

    def reject_dataframe_materialization(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("streamable writes must not collect the complete dataset")

    monkeypatch.setattr(engine, "_dataset_dataframe", reject_dataframe_materialization)

    exported = _execute(
        plane,
        "write",
        {
            "dataset_handle": created["result"]["dataset_handle"],
            "format": fmt,
            "options": {"filename": filename, "columns": ["sku", "price"]},
        },
    )

    assert exported["ok"] is True
    assert exported["result"]["row_count"] == 2
    artifact_id = exported["result"]["artifact_handle"]
    metadata = plane.get_artifact(artifact_id)
    assert metadata is not None
    assert metadata["persistence_status"] == "available"
    assert artifact_metadata_is_downloadable(metadata) is True
    body = plane.read_artifact_bytes(artifact_id, max_size_bytes=1024)
    assert body is not None
    assert body.startswith(expected_prefix)


def test_create_validate_merge_concat_relational_and_column_map() -> None:
    plane = ArtifactDatasetPlane()
    created = _execute(
        plane,
        "create_dataset",
        {
            "rows": [
                {"sku": "A1", "price": "10", "enriched_description": ""},
                {"sku": "B2", "price": "bad", "enriched_description": ""},
            ]
        },
    )
    validation = _execute(
        plane,
        "validate",
        {
            "dataset_handle": created["result"]["dataset_handle"],
            "checks": {
                "required_columns": ["sku", "price"],
                "types": {"price": "number"},
                "unique": ["sku"],
                "non_empty": ["sku"],
            },
        },
    )
    assert validation["result"]["valid"] is False
    assert validation["result"]["report"]["types"]["fail_count"] == 1

    merged = _execute(
        plane,
        "merge_rows",
        {
            "dataset_handle": created["result"]["dataset_handle"],
            "key_column": "sku",
            "rows": [
                {"key_column_value": "A1", "columns_to_set": {"enriched_description": "First"}},
                {"key_column_value": "NOPE", "columns_to_set": {"enriched_description": "Missing"}},
            ],
        },
    )
    assert merged["result"]["merged"] == 1
    assert merged["result"]["unmatched_keys"]["count"] == 1

    first = _execute(plane, "create_dataset", {"rows": [{"sku": "A1", "price": "10"}]})
    second = _execute(plane, "create_dataset", {"rows": [{"sku": "B2", "cost": "20"}]})
    concatenated = _execute(
        plane,
        "concat",
        {
            "dataset_handles": [
                first["result"]["dataset_handle"],
                second["result"]["dataset_handle"],
            ],
            "column_map": {"cost": "price"},
        },
    )
    assert concatenated["ok"] is True
    assert concatenated["result"]["row_count"] == 2

    sorted_rows = _execute(
        plane,
        "transform",
        {
            "dataset_handle": concatenated["result"]["dataset_handle"],
            "relational": [{"op": "sort", "by": ["price"], "descending": True}],
        },
    )
    assert [row["sku"] for row in sorted_rows["result"]["preview_rows"]] == ["B2", "A1"]


def test_parquet_read_write_formats_and_schema_metadata() -> None:
    import polars as pl

    plane = ArtifactDatasetPlane()
    parquet_body = io.BytesIO()
    pl.DataFrame(
        [
            {"sku": "00123", "price": "19.99"},
            {"sku": "00002", "price": "5.00"},
        ]
    ).write_parquet(parquet_body)
    artifact_id = _artifact(
        plane,
        parquet_body.getvalue(),
        filename="feed.parquet",
        content_type="application/vnd.apache.parquet",
    )

    read = _execute(plane, "read", {"artifact_handle": artifact_id})
    assert read["result"]["preview_rows"][0]["sku"] == "00123"

    exported_parquet = _execute(
        plane,
        "write",
        {
            "dataset_handle": read["result"]["dataset_handle"],
            "format": "parquet",
            "options": {"filename": "out.parquet"},
        },
    )
    exported_ndjson = _execute(
        plane,
        "write",
        {
            "dataset_handle": read["result"]["dataset_handle"],
            "format": "ndjson",
            "options": {"filename": "out.ndjson"},
        },
    )
    assert exported_parquet["result"]["artifact_handle"].startswith("art_")
    assert exported_ndjson["result"]["row_count"] == 2

    requested_schema = {"columns": [{"name": "sku", "type": "text"}]}
    created = _execute(
        plane,
        "create_dataset",
        {"rows": [{"sku": "A1"}], "schema": requested_schema},
    )
    persisted = plane.get_dataset(created["result"]["dataset_handle"])
    assert persisted is not None
    assert persisted["schema"] == requested_schema
    assert persisted["encoding"] == "parquet"
    assert persisted["object_key"].endswith("/rows.parquet")


def test_money_actions_preserve_decimal_text_and_leading_zero_identifiers() -> None:
    plane = ArtifactDatasetPlane()
    created = _execute(
        plane,
        "create_dataset",
        {"rows": [{"sku": "00123", "supplier": "standard", "price": "19.9900"}]},
    )

    coerced = _execute(
        plane,
        "transform",
        {
            "dataset_handle": created["result"]["dataset_handle"],
            "actions": [{"action": "money.coerce", "target": "price", "params": {}}],
        },
    )
    assert coerced["result"]["preview_rows"][0]["price"] == "19.990000"

    transformed = _execute(
        plane,
        "transform",
        {
            "dataset_handle": created["result"]["dataset_handle"],
            "actions": [
                {"target": "price", "action": "money.multiply", "params": {"factor": "0.90"}},
                {
                    "target": "price",
                    "action": "money.round",
                    "params": {"decimals": 2, "mode": "half_up"},
                },
            ],
        },
    )
    assert transformed["result"]["preview_rows"][0]["sku"] == "00123"
    assert transformed["result"]["preview_rows"][0]["price"] == "17.99"


@pytest.mark.parametrize(
    ("extra_actions", "condition", "expected_skus"),
    [
        ([], {"column": "description", "is_empty": True}, ["empty", "missing"]),
        (
            [{"action": "text.length", "target": "description"}],
            {"column": "description", "lt": 10},
            ["empty", "short"],
        ),
    ],
)
def test_html_text_cleanup_filters_product_dataset(extra_actions, condition, expected_skus) -> None:
    plane = ArtifactDatasetPlane()
    created = _execute(
        plane,
        "create_dataset",
        {
            "rows": [
                {"sku": "empty", "description": "<p>&nbsp;</p><script>ignored()</script>"},
                {"sku": "missing", "description": None},
                {"sku": "short", "description": "<p><b>Two</b> words</p>"},
                {"sku": "long", "description": "<p>A complete product description.</p>"},
            ],
        },
    )
    assert created["ok"] is True, created

    transformed = _execute(
        plane,
        "transform",
        {
            "dataset_handle": created["result"]["dataset_handle"],
            "actions": [{"action": "text.html_to_text", "target": "description"}, *extra_actions],
            "relational": [{"op": "filter", "when": condition}],
        },
    )

    assert transformed["ok"] is True, transformed
    assert [row["sku"] for row in transformed["result"]["preview_rows"]] == expected_skus


def test_transform_uses_polars_expressions(monkeypatch) -> None:
    import polars as pl

    captured: dict[str, Any] = {}

    def fail_row_callback(_row: dict[str, Any], _rule: dict[str, Any]) -> bool:
        raise AssertionError("transform must not call the per-row rule callback")

    def capture_dataset(_action: dict[str, Any], *, df: Any) -> tuple[str, dict[str, Any]]:
        frame = df.collect(engine="streaming")
        captured["rows"] = frame.to_dicts()
        return "dataset:vectorized", {
            "schema": {"columns": []},
            "row_count": frame.height,
            "preview_rows": frame.head(10).to_dicts(),
        }

    @contextmanager
    def lazy_source(_action: dict[str, Any], _handle: str):
        frame = pl.DataFrame(
            [
                {"sku": "A1", "price": "19.99", "supplier": "standard"},
                {"sku": "B2", "price": "5.00", "supplier": "clearance"},
            ]
        )
        yield frame.lazy(), {"row_count": frame.height}

    monkeypatch.setattr(engine, "_dataset_lazyframe", lazy_source)
    monkeypatch.setattr(engine, "_apply_rule", fail_row_callback)
    monkeypatch.setattr(engine, "_create_dataset_from_df", capture_dataset)

    response = engine.transform(
        {
            "input": {
                "dataset_handle": "dataset:input",
                "actions": [
                    {
                        "target": "price",
                        "action": "money.multiply",
                        "params": {"factor": "0.90"},
                        "when": {"column": "supplier", "equals": "standard"},
                    },
                    {
                        "target": "price",
                        "action": "money.round",
                        "params": {"decimals": 2},
                    },
                ],
            }
        }
    )

    assert response["ok"] is True
    assert response["result"]["applied_rules_report"] == {"0": 1, "1": 2}
    assert captured["rows"][0]["price"] == "17.99"
    assert captured["rows"][1]["price"] == "5.00"


def test_missing_columns_limits_and_payload_budget_return_public_errors(monkeypatch) -> None:
    plane = ArtifactDatasetPlane()
    created = _execute(
        plane,
        "create_dataset",
        {"rows": [{"sku": "A1", "qty": "2"}, {"sku": "B2", "qty": "3"}]},
    )
    missing_column = _execute(
        plane,
        "transform",
        {
            "dataset_handle": created["result"]["dataset_handle"],
            "relational": [{"op": "filter", "when": {"column": "company", "equals": "C1"}}],
        },
    )
    assert missing_column["ok"] is False
    assert missing_column["errors"][0]["code"] == "invalid_rules"
    assert "references missing column: company" in missing_column["errors"][0]["message"]

    large_artifact = _artifact(plane, b"sku,qty\nA1,2\nB2,3\n")
    large_dataset = _execute(
        plane,
        "read",
        {"artifact_handle": large_artifact},
    )
    monkeypatch.setenv("FS_TABULAR_FEED_READ_ROWS_SCAN_MAX_ROWS", "1")
    bounded_page = _execute(
        plane,
        "read_rows",
        {"dataset_handle": large_dataset["result"]["dataset_handle"], "limit": 1},
    )
    assert bounded_page["ok"] is True
    assert bounded_page["result"]["rows"] == [{"sku": "A1", "qty": "2"}]
    assert bounded_page["result"]["total_matching"] == 2
    assert bounded_page["result"]["next_offset"] == 1

    too_many = _execute(
        plane,
        "read_rows",
        {
            "dataset_handle": large_dataset["result"]["dataset_handle"],
            "limit": 1,
            "filter": {"column_equals": {"sku": "A1"}},
        },
    )
    assert too_many["ok"] is False
    assert too_many["errors"][0]["code"] == "too_many_rows"
    monkeypatch.delenv("FS_TABULAR_FEED_READ_ROWS_SCAN_MAX_ROWS", raising=False)

    headers = [f"c{i}" for i in range(60)]
    row = ",".join(str(i) for i in range(60))
    artifact_id = _artifact(plane, (",".join(headers) + "\n" + "\n".join([row] * 1000)).encode())
    read = _execute(plane, "read", {"artifact_handle": artifact_id, "key_column": "c0"})
    budget = _execute(
        plane,
        "read_rows",
        {
            "dataset_handle": read["result"]["dataset_handle"],
            "limit": 1000,
            "columns": headers,
        },
    )
    assert budget["ok"] is False
    assert budget["errors"][0]["code"] == "payload_budget_exceeded"


def test_feed_size_and_xlsx_row_limits_are_enforced(monkeypatch) -> None:
    plane = ArtifactDatasetPlane()
    artifact_id = _artifact(plane, b"sku,price\nA1,10\nB2,20\n")
    monkeypatch.setenv("FS_FEED_MAX_BYTES", "16")
    too_large = _execute(
        plane,
        "read",
        {"artifact_handle": artifact_id, "options": {"max_feed_bytes": 1024 * 1024}},
    )
    assert too_large["ok"] is False
    assert too_large["errors"][0]["code"] == "feed_too_large"

    class TooLargeFrame:
        height = 1_048_577

    monkeypatch.setattr(
        engine, "_dataset_dataframe", lambda _action, _handle: (TooLargeFrame(), {})
    )
    xlsx = engine.write(
        {
            "input": {
                "dataset_handle": "dst_too_large",
                "format": "xlsx",
                "options": {"filename": "too-large.xlsx"},
            }
        }
    )
    assert xlsx["ok"] is False
    assert xlsx["errors"][0]["code"] == "xlsx_row_limit_exceeded"


@pytest.mark.parametrize(
    ("fmt", "filename", "scan_name"),
    [
        ("csv", "feed.csv", "scan_csv"),
        ("parquet", "feed.parquet", "scan_parquet"),
        ("ndjson", "feed.ndjson", "scan_ndjson"),
    ],
)
def test_artifact_lazy_readers_apply_row_limit_before_collect(
    monkeypatch,
    tmp_path,
    fmt: str,
    filename: str,
    scan_name: str,
) -> None:
    path = tmp_path / filename
    path.write_bytes(b"sku\nA1\nB2\n")

    class FakeFrame:
        height = 2

    class FakeLazyFrame:
        limited_to: int | None = None

        def limit(self, value: int):
            self.limited_to = value
            return self

        def collect(self, *, engine: str):
            assert engine == "streaming"
            assert self.limited_to == 2
            return FakeFrame()

    fake_lazy = FakeLazyFrame()

    class FakePolars:
        def scan_csv(self, *_args, **_kwargs):
            assert scan_name == "scan_csv"
            return fake_lazy

        def scan_parquet(self, *_args, **_kwargs):
            assert scan_name == "scan_parquet"
            return fake_lazy

        def scan_ndjson(self, *_args, **_kwargs):
            assert scan_name == "scan_ndjson"
            return fake_lazy

    monkeypatch.setattr(engine_plane, "_pl", lambda: FakePolars())

    with pytest.raises(OverflowError, match="too_many_rows"):
        engine_plane._dataframe_from_artifact_path(
            str(path),
            {"filename": filename},
            {"format": fmt, "max_rows": 1},
        )


def test_xlsx_reader_spools_batches_to_parquet_before_collect(
    monkeypatch,
    tmp_path,
) -> None:
    from core.infrastructure.tabular_feed import readers

    path = tmp_path / "feed.xlsx"
    path.write_bytes(b"not-a-real-xlsx")
    load_kwargs: dict[str, Any] = {}

    class FakeSheet:
        def iter_rows(self, *, values_only: bool):
            assert values_only is True
            return iter(
                [
                    ("sku", "qty"),
                    ("A1", 1),
                    ("B2", 2),
                    ("C3", 3),
                ]
            )

    class FakeWorkbook:
        def __init__(self) -> None:
            self.worksheets = [FakeSheet()]
            self.sheetnames = ["Sheet1"]

        def close(self) -> None:
            load_kwargs["closed"] = True

    def _load_workbook(workbook_path: str, **kwargs: Any) -> FakeWorkbook:
        assert workbook_path == str(path)
        load_kwargs.update(kwargs)
        return FakeWorkbook()

    monkeypatch.setitem(
        sys.modules,
        "openpyxl",
        SimpleNamespace(load_workbook=_load_workbook),
    )

    written_batches: list[list[dict[str, Any]]] = []
    scanned_paths: list[str] = []

    class FakeBatchFrame:
        def __init__(self, rows: list[dict[str, Any]], **_kwargs: Any) -> None:
            self.rows = [dict(row) for row in rows]

        def write_parquet(self, parquet_path: str) -> None:
            written_batches.append(self.rows)
            Path(parquet_path).write_bytes(b"parquet")

    class FakeLazyFrame:
        def __init__(self, height: int) -> None:
            self.height = height
            self.limited_to: int | None = None

        def limit(self, value: int):
            self.limited_to = value
            return self

        def collect(self, *, engine: str):
            assert engine == "streaming"
            assert self.limited_to == 11
            return SimpleNamespace(height=self.height, columns=["sku", "qty"])

    class FakePolars:
        def DataFrame(self, rows: Any, **kwargs: Any) -> FakeBatchFrame:
            assert kwargs.get("infer_schema_length") is None
            return FakeBatchFrame(list(rows), **kwargs)

        def scan_parquet(self, parquet_path: str) -> FakeLazyFrame:
            scanned_paths.append(parquet_path)
            return FakeLazyFrame(0)

        def concat(self, lazy_frames: list[FakeLazyFrame]) -> FakeLazyFrame:
            assert len(lazy_frames) == 2
            return FakeLazyFrame(3)

    df, info = readers._dataframe_from_artifact_path(
        str(path),
        {"filename": "feed.xlsx"},
        {"format": "xlsx", "max_rows": 10, "xlsx_batch_rows": 2},
        polars_factory=lambda: FakePolars(),
    )

    assert load_kwargs["read_only"] is True
    assert load_kwargs["data_only"] is True
    assert load_kwargs["keep_links"] is False
    assert load_kwargs["closed"] is True
    assert [[row["sku"] for row in batch] for batch in written_batches] == [
        ["A1", "B2"],
        ["C3"],
    ]
    assert len(scanned_paths) == 2
    assert all(not Path(item).exists() for item in scanned_paths)
    assert df.height == 3
    assert info["format"] == "xlsx"


def test_parquet_dataset_uses_streaming_storage_boundary(tmp_path) -> None:
    class NoBodyReadStorage(LocalFilesystemStorageAdapter):
        def __init__(self, *, root_dir: str) -> None:
            super().__init__(root_dir=root_dir)
            self.put_stream_keys: list[str] = []

        def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]:
            if key.endswith("/rows.parquet"):
                raise AssertionError("dataset parquet must be uploaded with put_stream")
            return super().put(key, body, content_type=content_type)

        def put_stream(
            self,
            key: str,
            chunks: Any,
            *,
            content_type: str,
            max_size_bytes: int,
        ) -> dict[str, Any]:
            self.put_stream_keys.append(key)
            return super().put_stream(
                key,
                chunks,
                content_type=content_type,
                max_size_bytes=max_size_bytes,
            )

        def get(self, key: str) -> bytes | None:
            if key.endswith("/meta.json"):
                return super().get(key)
            raise AssertionError(f"dataset parquet must be streamed, not loaded via get(): {key}")

    storage = NoBodyReadStorage(root_dir=str(tmp_path))
    plane = ArtifactDatasetPlane(storage=storage)
    created = _execute(plane, "create_dataset", {"rows": [{"sku": "A1", "price": "19.99"}]})
    page = _execute(
        plane,
        "read_rows",
        {"dataset_handle": created["result"]["dataset_handle"], "columns": ["sku"]},
    )

    assert storage.put_stream_keys == [
        plane.get_dataset(created["result"]["dataset_handle"])["object_key"]
    ]
    assert page["result"]["rows"] == [{"sku": "A1"}]
