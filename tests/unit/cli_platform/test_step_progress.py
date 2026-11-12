"""Long operations read as steps: a mark, the step, how long it took."""

from __future__ import annotations

import pytest

from core.application.management.command_specs.contracts import (
    OperationProgressEvent,
    OperationStepStatus,
)
from core.entrypoints.cli.platform.renderers import PlainRenderer
from core.entrypoints.cli.platform.step_progress import (
    finished_line,
    format_duration,
    step_text,
)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.4, "0s"), (7, "7s"), (130, "2m 10s"), (3840, "1h 04m")],
)
def test_durations_read_like_a_person_says_them(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


def _event(status: OperationStepStatus, **extra: object) -> OperationProgressEvent:
    return OperationProgressEvent(
        step_id="prepare", status=status, label="Prepare core images", **extra
    )


def test_a_detail_that_repeats_the_label_is_not_printed_twice() -> None:
    assert step_text(_event(OperationStepStatus.RUNNING, detail="Prepare core images")) == (
        "Prepare core images"
    )
    assert step_text(_event(OperationStepStatus.RUNNING, completed=3, total=7)) == (
        "Prepare core images (3/7)"
    )


@pytest.mark.parametrize(
    ("status", "elapsed", "line"),
    [
        (OperationStepStatus.SUCCEEDED, 130, "✓ Prepare core images  2m 10s"),
        (OperationStepStatus.SUCCEEDED, 0.2, "✓ Prepare core images"),
        (OperationStepStatus.FAILED, 12, "✗ Prepare core images  12s"),
        (OperationStepStatus.SKIPPED, 0, "- Prepare core images  (nothing to do)"),
    ],
)
def test_a_finished_step_has_a_mark_and_its_duration(
    status: OperationStepStatus, elapsed: float, line: str
) -> None:
    assert finished_line(_event(status), elapsed) == line


def test_plain_output_prints_each_step_once_when_it_starts_and_when_it_ends(capsys) -> None:
    logs: list[str] = []
    with PlainRenderer().operation_progress(technical_sink=logs.append) as sink:
        sink(_event(OperationStepStatus.RUNNING))
        sink(_event(OperationStepStatus.RUNNING, completed=1, total=7))
        sink(_event(OperationStepStatus.SUCCEEDED, elapsed_seconds=42))

    assert capsys.readouterr().out.splitlines() == [
        "→ Prepare core images",
        "✓ Prepare core images  42s",
    ]
    assert len(logs) == 3


def test_still_working_waits_until_the_step_itself_has_been_quiet() -> None:
    """Live update 2026-11-12: the global tick printed "still working ... (4s)"."""
    from core.entrypoints.cli.platform.step_progress import still_working_lines

    running = {"prepare": _event(OperationStepStatus.RUNNING)}

    assert still_working_lines(running, {"prepare": 100.0}, now=104.0) == []
    assert still_working_lines(running, {"prepare": 100.0}, now=131.0) == [
        "  still working: Prepare core images (31s)"
    ]
