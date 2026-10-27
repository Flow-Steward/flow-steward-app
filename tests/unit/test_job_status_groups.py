"""The job status groups: one table per language, pinned to the same content."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from core.application.observability_vocabulary import (
    _JOB_STATUS_META,
    JOB_STATUS_GROUPS,
    job_status_group,
    job_status_group_statuses,
)
from core.application.reports.job_detail_contract_projection import _build_job_bucket

_TS_TABLE = Path(__file__).resolve().parents[2] / "assets/frontend/src/pages/jobs/status-groups.ts"
_TS_ROW = re.compile(
    r'\{\s*key:\s*"(?P<key>[a-z_]+)",\s*label:\s*"(?P<label>[^"]+)",\s*'
    r"statuses:\s*\[(?P<statuses>[^\]]*)\]\s*\}"
)


def _typescript_groups() -> list[tuple[str, str, tuple[str, ...]]]:
    source = _TS_TABLE.read_text(encoding="utf-8")
    table = source[source.index("export const JOB_STATUS_GROUPS") :]
    table = table[: table.index("] as const;")]
    return [
        (
            match["key"],
            match["label"],
            tuple(re.findall(r'"([a-z_]+)"', match["statuses"])),
        )
        for match in _TS_ROW.finditer(table)
    ]


def test_python_and_typescript_tables_hold_the_same_groups() -> None:
    assert _typescript_groups() == list(JOB_STATUS_GROUPS)


def test_the_eight_groups_in_display_order() -> None:
    assert [(key, label) for key, label, _statuses in JOB_STATUS_GROUPS] == [
        ("in_progress", "In progress"),
        ("waiting", "Waiting"),
        ("awaiting_approval", "Waiting for approval"),
        ("stuck", "Stuck"),
        ("interrupted", "Interrupted"),
        ("failed", "Failed"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]


def test_every_stored_status_is_in_at_most_one_group() -> None:
    statuses = [status for _key, _label, group in JOB_STATUS_GROUPS for status in group]
    assert len(statuses) == len(set(statuses))


def test_a_group_expands_to_its_stored_statuses() -> None:
    assert job_status_group_statuses("failed") == ("failed", "max_iterations", "triage_needed")
    assert job_status_group_statuses("stuck") == ("stuck", "orphaned")
    assert job_status_group_statuses("acknowledged") == ()


def test_unknown_and_legacy_codes_fall_into_no_group() -> None:
    assert job_status_group("max_iterations") == "failed"
    assert job_status_group("running") == "in_progress"  # the vocabulary alias
    assert job_status_group("acknowledged") == ""
    assert job_status_group("something_new") == ""
    # Only the queued alias of pending is outside the operator table.
    ungrouped = {status for status in _JOB_STATUS_META if not job_status_group(status)}
    assert ungrouped == {"queued"}


def test_job_details_carry_the_acknowledgement() -> None:
    bucket = _build_job_bucket(
        job={
            "id": "42",
            "status": "failed",
            "acknowledged_at": datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
            "acknowledged_by": "operator",
        },
        status_payload={},
        runtime={},
        lifecycle={},
    )
    assert bucket["acknowledged_at"] == "2026-10-17T09:00:00+00:00"
    assert bucket["acknowledged_by"] == "operator"
