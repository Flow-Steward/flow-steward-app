"""`jobs list` must survive an acknowledgement, in text and in JSON.

RC7-007 / RA-006: the CLI wired serialize_attention_job_row as a pass-through
(`lambda row: dict(row)`), so a raw database row reached
AttentionJobView.acknowledged_at, which is declared `str | None`. After an
acknowledgement that field carries a datetime and the listing broke - while the
API, which has always used the canonical serializer, was fine. The defect was
the second projection, not the model.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.application.management import query_service_runtime
from core.application.management.query_models_admin import AttentionJobView

ACKNOWLEDGED_AT = datetime(2026, 8, 7, 12, 30, 45, tzinfo=UTC)


def _row(**overrides) -> dict:
    row = {
        "id": "job-1",
        "project_id": "7",
        "task_summary": "Fix the flow",
        "status": "failed",
        "queued_at": ACKNOWLEDGED_AT,
        "started_at": ACKNOWLEDGED_AT,
        "finished_at": ACKNOWLEDGED_AT,
        "heartbeat_at": ACKNOWLEDGED_AT,
        "acknowledged_at": ACKNOWLEDGED_AT,
        "acknowledged_by": "operator-1",
    }
    row.update(overrides)
    return row


def _serializer():
    return query_service_runtime.build_attention_job_row_serializer(
        truncate_text_fn=lambda value, max_len=4000: query_service_runtime.truncate_text(
            value, max_len=max_len
        ),
        build_display_summary_fn=lambda value: str(value or "").strip(),
    )


def test_an_acknowledged_job_serializes_to_iso_8601() -> None:
    payload = _serializer()(_row())

    assert payload["acknowledged_at"] == ACKNOWLEDGED_AT.isoformat()
    assert AttentionJobView.model_validate(payload).acknowledged_at == ACKNOWLEDGED_AT.isoformat()


def test_an_unacknowledged_job_serializes_to_null() -> None:
    payload = _serializer()(_row(acknowledged_at=None, acknowledged_by=None))

    assert payload["acknowledged_at"] is None
    assert AttentionJobView.model_validate(payload).acknowledged_at is None


def test_the_cli_uses_the_same_serializer_as_the_api() -> None:
    """The pass-through was the defect; a second projection is how it returned."""
    from core.application.management import management_runtime

    source = (
        management_runtime.__file__
        and __import__("pathlib").Path(management_runtime.__file__).read_text()
    )

    assert "serialize_attention_job_row=lambda row: dict(row)" not in source, (
        "the CLI is projecting attention rows itself again"
    )
    assert "build_attention_job_row_serializer" in source


def test_the_json_payload_is_serialisable_after_acknowledgement() -> None:
    """The CLI renders with model_dump(mode="json"), so test that, not a guess.

    Dumping in python mode leaves the sibling datetime fields as datetimes and
    json.dumps refuses them - which would be testing the test, not the product.
    """
    import json

    view = AttentionJobView.model_validate(_serializer()(_row()))

    encoded = json.dumps(view.model_dump(mode="json"), ensure_ascii=False, default=str)

    assert ACKNOWLEDGED_AT.isoformat() in encoded
    assert json.loads(encoded)["acknowledged_at"] == ACKNOWLEDGED_AT.isoformat()


def test_the_pass_through_projection_actually_breaks_the_listing() -> None:
    """The behavioural half: a raw row is rejected, it does not degrade quietly.

    AttentionJobView.acknowledged_at is `str | None`, so handing it the database
    datetime raises ValidationError - the listing fails outright rather than
    printing something odd. That is what the CLI did before it used the
    canonical serializer, and why `jobs list` broke only after an
    acknowledgement.
    """
    import pytest
    from pydantic import ValidationError

    raw_row = _row()

    with pytest.raises(ValidationError, match="acknowledged_at"):
        AttentionJobView.model_validate(raw_row)

    # The same row through the canonical serializer validates cleanly.
    assert AttentionJobView.model_validate(_serializer()(raw_row)).acknowledged_at
