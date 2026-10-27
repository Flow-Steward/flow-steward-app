"""A step's file shows up on the running step once, and only when it is complete."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from core.application.extensions.runtime_grants import (
    PreparedExtensionGrants,
    finalize_prepared_extension_grants,
)
from core.application.orchestration.step_outputs import early_output_handler
from core.application.reports.ui_projection import _run_duration_ms
from core.infrastructure.extension_runtime.progress_channel import (
    PROGRESS_FD_ENV,
    parse_progress_line,
)
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.progress import (
    announce_output_ready,
)


class _Plane:
    def __init__(self, metas: dict[str, dict | None]) -> None:
        self.metas = metas
        self.finalized: list[str] = []

    def finalize_artifact_upload(self, object_id, *, max_size_bytes=None):
        self.finalized.append(object_id)
        return self.metas.get(object_id)

    def get_artifact(self, object_id):
        return self.metas.get(object_id)


class _Logger:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_event(self, **event) -> None:
        self.events.append(event)


def _meta(object_id: str) -> dict:
    return {
        "artifact_id": object_id,
        "filename": "export.jsonl",
        "content_type": "application/jsonl",
        "size_bytes": 2048,
        "sha256": "a" * 64,
    }


def _prepared(*ids: str) -> PreparedExtensionGrants:
    return PreparedExtensionGrants(
        artifact_output_ids=list(ids),
        produced_artifacts=[{"artifact_id": object_id} for object_id in ids],
    )


def _handler(plane, prepared, finalized, logger):
    return early_output_handler(
        grant_plane=plane,
        prepared_grants=prepared,
        finalized=finalized,
        event_logger=logger,
        job_id="job-1",
        step_id="export",
    )


def test_a_complete_output_is_finalized_once_and_announced_on_its_step() -> None:
    plane = _Plane({"art-1": _meta("art-1")})
    logger = _Logger()
    finalized: dict = {}
    handle = _handler(plane, _prepared("art-1"), finalized, logger)

    handle("artifact", "art-1")
    handle("artifact", "artifact:art-1")

    assert list(finalized) == ["art-1"]
    assert len(logger.events) == 1
    event = logger.events[0]
    assert event["event_type"] == "artifact_ready"
    # The event's legacy ``phase`` column names the step; there is no phase.
    assert event["phase"] == "export"
    assert event["title"] == "File ready: export.jsonl"
    assert event["detail"] == {
        "step_id": "export",
        "kind": "artifact",
        "artifact_id": "art-1",
        "filename": "export.jsonl",
        "content_type": "application/jsonl",
        "size_bytes": 2048,
        "source": "extension",
        "download_url": "/api/jobs/job-1/artifacts/art-1",
    }


def test_an_output_the_step_did_not_prepare_is_never_finalized() -> None:
    plane = _Plane({"other": _meta("other")})
    logger = _Logger()
    finalized: dict = {}
    handle = _handler(plane, _prepared("art-1"), finalized, logger)

    handle("artifact", "other")
    handle("dataset", "art-1")

    assert plane.finalized == []
    assert finalized == {}
    assert logger.events == []


def test_an_incomplete_upload_is_left_to_the_end_of_the_step() -> None:
    plane = _Plane({"art-1": {**_meta("art-1"), "sha256": ""}})
    logger = _Logger()
    finalized: dict = {}
    handle = _handler(plane, _prepared("art-1"), finalized, logger)

    handle("artifact", "art-1")

    assert finalized == {}
    assert logger.events == []


def test_without_a_grant_plane_there_is_no_handler() -> None:
    assert _handler(None, _prepared("art-1"), {}, _Logger()) is None


def test_the_end_of_the_step_reuses_what_was_finalized_early() -> None:
    plane = _Plane({"art-1": _meta("art-1"), "art-2": _meta("art-2")})
    early = {"art-1": _meta("art-1")}

    result = finalize_prepared_extension_grants(
        plane, _prepared("art-1", "art-2"), already_finalized=early
    )

    assert plane.finalized == ["art-2"]
    assert set(result.artifacts) == {"art-1", "art-2"}
    assert [meta["artifact_id"] for meta in result.produced_artifacts] == ["art-1", "art-2"]
    assert result.errors == []


def test_an_output_ready_line_is_parsed_strictly() -> None:
    assert parse_progress_line(b'{"output_ready": {"kind": "artifact", "id": " a-1 "}}') == {
        "output_ready": {"kind": "artifact", "id": "a-1"}
    }
    assert parse_progress_line(b'{"output_ready": {"kind": "file", "id": "a-1"}}') is None
    assert parse_progress_line(b'{"output_ready": {"kind": "dataset", "id": ""}}') is None


def test_the_sdk_announces_a_finished_output_on_the_channel(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))

    announce_output_ready("dataset", "ds-9")
    announce_output_ready("file", "ignored")
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stream:
        lines = stream.read().splitlines()

    assert [parse_progress_line(line) for line in lines] == [
        {"output_ready": {"kind": "dataset", "id": "ds-9"}}
    ]


def test_a_running_job_shows_how_long_it_has_run() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    started = (now - timedelta(minutes=3, seconds=5)).isoformat()

    assert _run_duration_ms({"started_at": started}, "in_progress", now) == 185_000
    assert (
        _run_duration_ms({"started_at": started, "duration_ms": 0}, "in_progress", now) == 185_000
    )
    assert _run_duration_ms({"started_at": started, "duration_ms": 9000}, "completed", now) == 9000
    assert _run_duration_ms({"started_at": started}, "failed", now) is None
    assert _run_duration_ms({"duration_ms": 0}, "in_progress", now) == 0
    for paused in ("awaiting_approval", "awaiting_timer", "awaiting_child"):
        assert _run_duration_ms({"started_at": started, "duration_ms": 0}, paused, now) == 185_000


def test_finished_outputs_and_progress_go_to_their_own_handlers() -> None:
    from core.application.orchestration.connector_events import _step_update_sink

    progress: list[dict] = []
    outputs: list[tuple[str, str]] = []
    sink = _step_update_sink(
        progress=progress.append, outputs=lambda kind, oid: outputs.append((kind, oid))
    )
    assert sink is not None

    sink({"message": "Exporting", "done": 1})
    sink({"output_ready": {"kind": "artifact", "id": "art-1"}})

    assert progress == [{"message": "Exporting", "done": 1}]
    assert outputs == [("artifact", "art-1")]
    assert _step_update_sink(progress=None, outputs=None) is None
