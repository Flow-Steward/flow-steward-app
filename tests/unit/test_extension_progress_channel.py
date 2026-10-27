"""An extension's progress reaches the step that started it while it runs."""

from __future__ import annotations

import os
import sys
import threading

import pytest

from core.application.orchestration.step_activity import step_progress_reporter
from core.infrastructure.extension_runtime.extension_runtime_invocation import (
    _run_subprocess_capped,
)
from core.infrastructure.extension_runtime.progress_channel import (
    PROGRESS_FD_ENV,
    drain_progress,
    parse_progress_line,
)
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import report_progress


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            b'{"message": "Exporting", "done": 3, "total": 10}',
            {"message": "Exporting", "done": 3, "total": 10},
        ),
        (b'{"message": "Starting"}', {"message": "Starting"}),
        (b'{"done": 4}', {"message": "", "done": 4}),
        (b'{"message": "x", "done": -1, "total": true}', {"message": "x"}),
        (b"not json", None),
        (b"[1, 2]", None),
        (b'{"message": ""}', None),
        (b'{"message": "' + b"a" * 5000 + b'"}', None),
    ],
)
def test_a_progress_line_is_one_bounded_update_or_nothing(line, expected) -> None:
    assert parse_progress_line(line) == expected


def test_progress_lines_are_read_as_they_arrive_and_garbage_is_skipped() -> None:
    read_fd, write_fd = os.pipe()
    received: list[dict] = []
    reader = threading.Thread(target=drain_progress, args=(read_fd, received.append))
    reader.start()
    os.write(write_fd, b'{"message": "one", "done": 1}\nnoise\n{"mess')
    os.write(write_fd, b'age": "two", "done": 2}\n')
    os.close(write_fd)
    reader.join(timeout=5)

    assert received == [{"message": "one", "done": 1}, {"message": "two", "done": 2}]


def test_a_running_extension_reports_progress_on_its_own_pipe() -> None:
    script = (
        "import json, os\n"
        f"fd = int(os.environ['{PROGRESS_FD_ENV}'])\n"
        "for done in (1, 2):\n"
        "    os.write(fd, (json.dumps({'message': 'Exporting', 'done': done, 'total': 2}) + '\\n').encode())\n"
        "print(json.dumps({'status': 'succeeded'}))\n"
    )
    received: list[dict] = []

    returncode, stdout, stderr, exceeded = _run_subprocess_capped(
        [sys.executable, "-c", script],
        cwd=None,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=30,
        progress_sink=received.append,
    )

    assert returncode == 0, stderr
    assert exceeded is False
    assert '"succeeded"' in stdout
    assert received == [
        {"message": "Exporting", "done": 1, "total": 2},
        {"message": "Exporting", "done": 2, "total": 2},
    ]


def test_without_a_listener_the_extension_gets_no_progress_channel() -> None:
    returncode, stdout, _stderr, _exceeded = _run_subprocess_capped(
        [sys.executable, "-c", f"import os; print(os.environ.get('{PROGRESS_FD_ENV}', 'absent'))"],
        cwd=None,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=30,
    )

    assert returncode == 0
    assert stdout.strip() == "absent"


def test_the_sdk_writes_progress_to_the_channel_and_never_raises(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))

    report_progress("Exporting products", done=5, total=9)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stream:
        assert parse_progress_line(stream.read().strip()) == {
            "message": "Exporting products",
            "done": 5,
            "total": 9,
        }

    # The pipe is closed now: reporting again must neither raise nor keep trying.
    report_progress("again", done=6, total=9)
    assert PROGRESS_FD_ENV not in os.environ

    monkeypatch.delenv(PROGRESS_FD_ENV, raising=False)
    report_progress("outside a run")


class _Logger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.beats: list[str] = []

    def log_event(self, **event) -> None:
        self.events.append(event)

    def update_job_runtime(self, job_id, *, heartbeat_now, last_response_excerpt) -> None:
        self.beats.append(last_response_excerpt)


def test_progress_becomes_rate_limited_step_events_but_the_last_update_always_lands() -> None:
    logger = _Logger()
    ticks = iter([0.0, 0.5, 1.0, 3.0, 3.1])
    report = step_progress_reporter(
        logger,
        job_id=7,
        step_id="export-products",
        label="export-products",
        clock=lambda: next(ticks),
    )
    assert report is not None

    for done in (1, 2, 3, 4):
        report({"message": "Exporting", "done": done, "total": 5})
    report({"message": "Exporting", "done": 5, "total": 5})

    assert [event["title"] for event in logger.events] == [
        "Exporting (1 of 5)",
        "Exporting (4 of 5)",
        "Exporting (5 of 5)",
    ]
    event = logger.events[-1]
    assert event["event_type"] == "step_progress"
    # The event's legacy ``phase`` column names the step; there is no phase.
    assert event["phase"] == "export-products"
    assert event["detail"] == {
        "step_id": "export-products",
        "message": "Exporting",
        "done": 5,
        "total": 5,
        "source": "extension",
    }
    assert logger.beats[-1] == "export-products: Exporting (5 of 5)"


def test_no_job_means_no_progress_reporter() -> None:
    assert step_progress_reporter(_Logger(), job_id="", step_id="", label="") is None
