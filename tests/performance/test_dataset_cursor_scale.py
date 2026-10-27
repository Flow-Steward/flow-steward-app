"""Opt-in component scale/RSS acceptance for dataset cursor storage and paging.

Compiled orchestration and replay safety are covered separately by
``test_dataset_cursor_compiled_replay_postgres.py``. This probe intentionally
does not simulate a provider or claim end-to-end execution evidence.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

import pytest

PERFORMANCE_OPT_IN: Final = "FS_RUN_DATASET_CURSOR_SCALE"
MAX_PEAK_RSS_MIB: Final = 512.0
ROW_COUNTS: Final = (134_992, 1_500_000)
PROBE_PREFIX: Final = "DATASET_CURSOR_SCALE_EVIDENCE="

pytestmark = pytest.mark.skipif(
    os.environ.get(PERFORMANCE_OPT_IN) != "1",
    reason=f"dataset cursor scale gate requires {PERFORMANCE_OPT_IN}=1",
)


def _csv_chunks(row_count: int):
    buffer = bytearray(b"sku,qty,unit_price\n")
    for index in range(row_count):
        buffer.extend(f"{index:09d},{index % 50 + 1},{index % 1000}.25\n".encode("ascii"))
        if len(buffer) >= 1024 * 1024:
            yield bytes(buffer)
            buffer.clear()
    if buffer:
        yield bytes(buffer)


def _context():
    from core.application.tabular_feed.context import TabularFeedExecutionContext

    return TabularFeedExecutionContext(
        account_id="acct_scale",
        project_id="proj_scale",
        workflow_id="wf_scale",
        job_id="job_scale",
        run_id="job_scale",
        step_id="scale-step",
    )


def _execute(service: Any, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return service.execute(
        operation=operation,
        payload={"input": payload},
        context=_context(),
    )


def _peak_rss_mib() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def _run_probe(row_count: int) -> dict[str, Any]:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.dataset_cursor_runtime import (
        build_dataset_cursor,
        dataset_cursor_page_count,
        read_dataset_cursor_page,
    )
    from core.application.tabular_feed.service import TabularFeedService

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="fs-dataset-cursor-scale-") as root:
        plane = ArtifactDatasetPlane(storage=LocalFilesystemStorageAdapter(root_dir=root))
        artifact, _meta = plane.create_artifact_from_stream(
            owner_scope={
                "account_id": "acct_scale",
                "project_id": "proj_scale",
                "workflow_id": "wf_scale",
            },
            run_id="job_scale",
            step_id="stream-input",
            filename="supplier.csv",
            content_type="text/csv",
            chunks=_csv_chunks(row_count),
            max_size_bytes=1024 * 1024 * 1024,
            allow_in_memory=False,
        )
        rss_after_stream = _peak_rss_mib()
        service = TabularFeedService(plane=plane)
        read = _execute(
            service,
            "read",
            {
                "artifact_handle": artifact.artifact_id,
                "column_map": {"unit_price": "price"},
            },
        )
        assert read["ok"] is True, read
        rss_after_read = _peak_rss_mib()
        transformed = _execute(
            service,
            "transform",
            {
                "dataset_handle": read["result"]["dataset_handle"],
                "actions": [
                    {
                        "target": "qty",
                        "action": "number.multiply",
                        "params": {"factor": 2},
                    },
                    {
                        "target": "price",
                        "action": "number.add",
                        "params": {"operand": 1.25},
                    },
                ],
            },
        )
        assert transformed["ok"] is True, transformed
        assert transformed["result"]["row_count"] == row_count
        assert transformed["result"]["preview_rows"][0] == {
            "sku": "000000000",
            "qty": 2.0,
            "price": 1.5,
        }
        rss_after_transform = _peak_rss_mib()
        cursor = build_dataset_cursor(
            transformed["result"],
            page_size=1000,
            emit_partial_batch=True,
        )
        page_count = dataset_cursor_page_count(cursor)
        max_page_items = 0
        state_base = {
            "_artifact_plane": plane,
            "trusted_runtime_context": {
                "account_id": "acct_scale",
                "project_id": "proj_scale",
                "workflow_id": "wf_scale",
                "job_id": "job_scale",
            },
        }
        read_page_rows = 0
        for page_ordinal in range(page_count):
            page, _page_meta = read_dataset_cursor_page(
                cursor,
                page_ordinal=page_ordinal,
                state=state_base,
                step_id="cursor-page",
            )
            read_page_rows += len(page)
            max_page_items = max(max_page_items, len(page))

        return {
            "row_count": row_count,
            "read_row_count": read["result"]["row_count"],
            "transformed_row_count": transformed["result"]["row_count"],
            "page_count": page_count,
            "max_page_items": max_page_items,
            "read_page_rows": read_page_rows,
            "rss_after_stream_mib": rss_after_stream,
            "rss_after_read_mib": rss_after_read,
            "rss_after_transform_mib": rss_after_transform,
            "peak_rss_mib": _peak_rss_mib(),
            "wall_seconds": time.perf_counter() - started,
        }


@pytest.mark.parametrize("row_count", ROW_COUNTS)
def test_dataset_cursor_scale_components_stay_bounded(row_count: int) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.performance.test_dataset_cursor_scale",
            "--probe",
            str(row_count),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, PERFORMANCE_OPT_IN: "0"},
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    evidence_line = next(
        line for line in result.stdout.splitlines() if line.startswith(PROBE_PREFIX)
    )
    evidence = json.loads(evidence_line.removeprefix(PROBE_PREFIX))
    print(f"{PROBE_PREFIX}{json.dumps(evidence, sort_keys=True)}")

    assert evidence["read_row_count"] == row_count
    assert evidence["transformed_row_count"] == row_count
    assert evidence["page_count"] == -(-row_count // 1000)
    assert evidence["max_page_items"] <= 1000
    assert evidence["read_page_rows"] == row_count
    assert evidence["peak_rss_mib"] <= MAX_PEAK_RSS_MIB


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--probe":
    evidence = _run_probe(int(sys.argv[2]))
    print(f"{PROBE_PREFIX}{json.dumps(evidence, sort_keys=True)}")
