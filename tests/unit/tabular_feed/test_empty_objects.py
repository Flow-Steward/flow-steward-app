"""Valid JSON with empty objects reads, stores and exports as it came in (#35)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.tabular_feed.context import TabularFeedExecutionContext
from core.application.tabular_feed.service import TabularFeedService
from core.infrastructure.extension_runtime import dataset_empty_objects as empty_objects

OWNER_SCOPE = {"account_id": "acct_empty", "project_id": "proj_empty", "workflow_id": "wf_empty"}
ISSUE_INPUT = (
    b'{"sku":"001","variant":{"id":"v1","options":{}}}\n'
    b'{"sku":"002","variant":{"id":"v2","options":{}}}\n'
)


def _execute(plane: ArtifactDatasetPlane, operation: str, payload: dict[str, Any]) -> dict:
    context = TabularFeedExecutionContext(
        account_id=OWNER_SCOPE["account_id"],
        project_id=OWNER_SCOPE["project_id"],
        workflow_id=OWNER_SCOPE["workflow_id"],
        run_id="run_empty",
        step_id="step_empty",
    )
    return TabularFeedService(plane=plane).execute(
        operation=operation, payload={"input": dict(payload)}, context=context
    )


def _artifact(plane: ArtifactDatasetPlane, body: bytes, filename: str, content_type: str) -> str:
    handle, _meta = plane.create_artifact(
        owner_scope=dict(OWNER_SCOPE),
        run_id="run_empty",
        step_id="upload",
        filename=filename,
        content_type=content_type,
        retention_class="run_window",
        body=body,
    )
    return handle.artifact_id


def _ndjson(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


CONTENT_TYPES = {
    "feed.jsonl": "application/x-ndjson",
    "feed.json": "application/json",
    "feed.parquet": "application/vnd.apache.parquet",
}


def _read(plane: ArtifactDatasetPlane, body: bytes, filename: str = "feed.jsonl") -> dict:
    content_type = CONTENT_TYPES[filename]
    result = _execute(
        plane, "read", {"artifact_handle": _artifact(plane, body, filename, content_type)}
    )
    assert result["ok"] is True, result
    return result["result"]


def _export(plane: ArtifactDatasetPlane, dataset_handle: str, fmt: str) -> bytes:
    written = _execute(
        plane,
        "write",
        {"dataset_handle": dataset_handle, "format": fmt, "options": {"filename": f"out.{fmt}"}},
    )
    assert written["ok"] is True, written
    body = plane.read_artifact_bytes(written["result"]["artifact_handle"], max_size_bytes=1 << 20)
    assert body is not None
    return body


def _rows(plane: ArtifactDatasetPlane, dataset_handle: str) -> list[dict[str, Any]]:
    result = _execute(plane, "read_rows", {"dataset_handle": dataset_handle, "limit": 100})
    assert result["ok"] is True, result
    return result["result"]["rows"]


def test_issue_input_with_empty_nested_objects_reads_into_a_dataset() -> None:
    plane = ArtifactDatasetPlane()

    result = _read(plane, ISSUE_INPUT)

    assert result["row_count"] == 2
    assert result["preview_rows"] == [
        {"sku": "001", "variant": {"id": "v1", "options": {}}},
        {"sku": "002", "variant": {"id": "v2", "options": {}}},
    ]
    # The reserved storage field never reaches the shown schema.
    assert result["schema"]["columns"] == [
        {"name": "sku", "type": "String"},
        {"name": "variant", "type": "Struct({'id': String, 'options': Struct({})})"},
    ]


ROUNDTRIP_CASES = {
    "empty objects": (
        [{"sku": "1", "options": {}}, {"sku": "2", "options": {}}],
        [{"sku": "1", "options": {}}, {"sku": "2", "options": {}}],
    ),
    "null alongside empty objects": (
        [{"sku": "1", "options": {}}, {"sku": "2", "options": None}, {"sku": "3"}],
        [
            {"sku": "1", "options": {}},
            {"sku": "2", "options": None},
            {"sku": "3", "options": None},
        ],
    ),
    "nested objects and lists": (
        [
            {
                "sku": "1",
                "variant": {"id": "a", "options": {}},
                "tags": [{}, None],
                "deep": [[{}], []],
            },
            {"sku": "2", "variant": {"id": "b", "options": None}, "tags": [], "deep": None},
            {"sku": "3", "variant": None, "tags": None, "deep": [None]},
        ],
        [
            {
                "sku": "1",
                "variant": {"id": "a", "options": {}},
                "tags": [{}, None],
                "deep": [[{}], []],
            },
            {"sku": "2", "variant": {"id": "b", "options": None}, "tags": [], "deep": None},
            {"sku": "3", "variant": None, "tags": None, "deep": [None]},
        ],
    ),
    # One struct type per column: {} next to {"a": 1} is stored with "a" null, never as null.
    "mixed empty and non-empty objects": (
        [{"sku": "1", "o": {}}, {"sku": "2", "o": {"a": 1}}, {"sku": "3", "o": None}],
        [{"sku": "1", "o": {"a": None}}, {"sku": "2", "o": {"a": 1}}, {"sku": "3", "o": None}],
    ),
}


@pytest.mark.parametrize(("source", "expected"), ROUNDTRIP_CASES.values(), ids=ROUNDTRIP_CASES)
def test_ndjson_round_trip_keeps_empty_objects_apart_from_null(source, expected) -> None:
    plane = ArtifactDatasetPlane()
    read = _read(plane, _ndjson(source))
    handle = read["dataset_handle"]

    exported_lines = _export(plane, handle, "ndjson").decode().splitlines()
    exported_json = json.loads(_export(plane, handle, "json"))

    assert read["preview_rows"] == expected
    assert _rows(plane, handle) == expected
    assert [json.loads(line) for line in exported_lines] == expected
    assert exported_json == expected
    assert empty_objects.EMPTY_OBJECT_FIELD not in json.dumps(read["schema"])


def test_json_document_input_keeps_empty_objects_apart_from_null() -> None:
    plane = ArtifactDatasetPlane()
    source, expected = ROUNDTRIP_CASES["nested objects and lists"]

    read = _read(plane, json.dumps(source).encode(), filename="feed.json")

    assert _rows(plane, read["dataset_handle"]) == expected


def test_parquet_export_with_empty_objects_reads_back_as_empty_objects() -> None:
    plane = ArtifactDatasetPlane()
    source, expected = ROUNDTRIP_CASES["nested objects and lists"]
    first = _read(plane, _ndjson(source))

    parquet = _export(plane, first["dataset_handle"], "parquet")
    second = _read(plane, parquet, filename="feed.parquet")

    assert _rows(plane, second["dataset_handle"]) == expected
    assert [
        json.loads(line)
        for line in _export(plane, second["dataset_handle"], "ndjson").decode().splitlines()
    ] == expected


def test_created_and_appended_rows_keep_empty_objects_apart_from_null() -> None:
    plane = ArtifactDatasetPlane()
    rows = [
        {"sku": "1", "options": {}, "variant": {"id": "a", "options": {}}},
        {"sku": "2", "options": None, "variant": {"id": "b", "options": None}},
    ]
    created = _execute(plane, "create_dataset", {"rows": rows})
    assert created["ok"] is True, created

    appended = _execute(
        plane,
        "create_dataset",
        {
            "rows": [{"sku": "3", "options": {}, "variant": None}],
            "append_to": created["result"]["dataset_handle"],
        },
    )

    assert _rows(plane, appended["result"]["dataset_handle"]) == [
        *rows,
        {"sku": "3", "options": {}, "variant": None},
    ]


def test_extension_ndjson_representation_returns_empty_objects() -> None:
    plane = ArtifactDatasetPlane()
    source, expected = ROUNDTRIP_CASES["nested objects and lists"]
    read = _read(plane, _ndjson(source))

    representation = plane.materialize_dataset_representation(
        read["dataset_handle"], content_type="application/x-ndjson", max_size_bytes=1 << 20
    )

    body = plane._storage.get(representation["object_key"])
    assert [json.loads(line) for line in bytes(body).decode().splitlines()] == expected


def test_a_type_parquet_cannot_store_is_a_materialization_error_not_a_read_error(
    monkeypatch,
) -> None:
    import polars as pl

    plane = ArtifactDatasetPlane()

    def frame_with_object_column(rows):
        return pl.DataFrame({"sku": ["1"], "blob": pl.Series([object()], dtype=pl.Object)})

    monkeypatch.setattr(empty_objects, "frame_from_rows", frame_with_object_column)

    result = _execute(plane, "create_dataset", {"rows": [{"sku": "1"}]})

    assert result["ok"] is False
    [error] = result["errors"]
    assert error["code"] == "tabular_feed.materialization_failed"
    assert error["message"] == (
        "Could not store the table as a dataset: unsupported column type in 'blob'"
    )


def test_json_writer_drops_the_reserved_field_across_write_boundaries() -> None:
    import polars as pl

    member_dtype = pl.Struct({empty_objects.EMPTY_OBJECT_FIELD: pl.Null})
    frame = pl.DataFrame(
        {
            "only": pl.Series([{empty_objects.EMPTY_OBJECT_FIELD: None}, None], dtype=member_dtype),
            "text": [f'"{empty_objects.EMPTY_OBJECT_FIELD}":null', "x"],
        }
    )
    polars_json = io.BytesIO()
    frame.write_ndjson(polars_json)
    rendered = io.BytesIO()

    writer = empty_objects.EmptyObjectJsonWriter(rendered)
    for byte in polars_json.getvalue():  # every member is split across writes
        writer.write(bytes([byte]))
    writer.finish()

    assert [json.loads(line) for line in rendered.getvalue().decode().splitlines()] == [
        # A string that spells the member is data, so it is kept.
        {"only": {}, "text": f'"{empty_objects.EMPTY_OBJECT_FIELD}":null'},
        {"only": None, "text": "x"},
    ]
