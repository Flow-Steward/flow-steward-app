"""One line per step for long operations: a spinner while it runs, then ``✓ label  12s``.

The operation log keeps the full machine-oriented progress lines; the terminal shows
what an operator reads at a glance. Redirected output prints the same marks as plain
lines and, during long quiet work, a short "still working" note.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import click

from core.application.management.command_specs.contracts import (
    OperationProgressEvent,
    OperationStepStatus,
)

_FINISHED = frozenset(
    {
        OperationStepStatus.SUCCEEDED,
        OperationStepStatus.FAILED,
        OperationStepStatus.SKIPPED,
        OperationStepStatus.CANCELLED,
    }
)
_MARKS = {
    OperationStepStatus.SUCCEEDED: ("✓", "green"),
    OperationStepStatus.FAILED: ("✗", "bold red"),
    OperationStepStatus.SKIPPED: ("-", "dim"),
    OperationStepStatus.CANCELLED: ("✗", "yellow"),
}
# A redirected run is read later, in a log or a CI page: one note per half minute of
# quiet work says it is alive without burying the steps.
REDIRECTED_HEARTBEAT_SECONDS = 30.0


def format_duration(seconds: float) -> str:
    """``7s``, ``2m 10s``, ``1h 4m``."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def step_text(event: OperationProgressEvent) -> str:
    """The step's words: its label, an n/m counter, and a detail that adds something."""
    text = event.label.strip() or event.step_id
    if event.completed is not None and event.total is not None:
        text += f" ({_number(event.completed)}/{_number(event.total)})"
    detail = event.detail.strip()
    if detail and detail != event.label.strip():
        text += f" — {detail}"
    return text


def _number(value: float) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


class _Clock:
    """Remembers when each step started so a finished step can say how long it took."""

    def __init__(self) -> None:
        self.started: dict[str, float] = {}

    def elapsed(self, event: OperationProgressEvent) -> float:
        now = time.monotonic()
        start = self.started.setdefault(event.step_id, now)
        if event.elapsed_seconds is not None:
            return event.elapsed_seconds
        return now - start


def finished_line(event: OperationProgressEvent, elapsed: float) -> str:
    mark, _style = _MARKS.get(event.status, ("✓", "green"))
    suffix = ""
    if event.status is OperationStepStatus.SKIPPED:
        suffix = "  (nothing to do)"
    elif event.status is OperationStepStatus.CANCELLED:
        suffix = "  (cancelled)"
    elif elapsed >= 1:
        suffix = f"  {format_duration(elapsed)}"
    return f"{mark} {step_text(event)}{suffix}"


@contextmanager
def redirected_steps(
    *, technical_sink: Callable[[str], None] | None, log_line: Callable[[Any], str]
) -> Iterator[Callable[[OperationProgressEvent], None]]:
    """Plain lines: ``→ step`` when it starts, ``✓ step  12s`` when it ends."""
    clock = _Clock()
    lock = threading.Lock()
    running: dict[str, OperationProgressEvent] = {}
    stop = threading.Event()

    def sink(event: OperationProgressEvent) -> None:
        with lock:
            elapsed = clock.elapsed(event)
            if event.status in _FINISHED:
                running.pop(event.step_id, None)
                click.echo(finished_line(event, elapsed))
            elif event.step_id not in running and not event.heartbeat:
                running[event.step_id] = event
                click.echo(f"→ {step_text(event)}")
            else:
                running[event.step_id] = event
        if technical_sink is not None:
            technical_sink(log_line(event))

    def heartbeat() -> None:
        while not stop.wait(REDIRECTED_HEARTBEAT_SECONDS):
            with lock:
                for step_id, event in running.items():
                    started = clock.started.get(step_id, time.monotonic())
                    click.echo(
                        f"  still working: {step_text(event)} "
                        f"({format_duration(time.monotonic() - started)})"
                    )

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield sink
    finally:
        stop.set()
        thread.join(timeout=1)


@contextmanager
def live_steps(
    console: Any,
    *,
    technical_sink: Callable[[str], None] | None,
    log_line: Callable[[Any], str],
) -> Iterator[Callable[[OperationProgressEvent], None]]:
    """A spinner on the running step; finished steps stay on screen with their mark."""
    from rich.progress import Progress, ProgressColumn, SpinnerColumn, TaskID, TextColumn
    from rich.text import Text

    class MarkColumn(SpinnerColumn):
        def render(self, task: Any) -> Any:
            mark = task.fields.get("mark")
            if mark:
                return Text(mark, style=task.fields.get("mark_style", ""))
            return super().render(task)

    class ElapsedColumn(ProgressColumn):
        def render(self, task: Any) -> Text:
            final = task.fields.get("final")
            if final is not None:
                return Text(final, style="dim")
            started = task.fields.get("started") or time.monotonic()
            elapsed = time.monotonic() - started
            return Text(format_duration(elapsed) if elapsed >= 1 else "", style="dim")

    clock = _Clock()
    lock = threading.Lock()
    tasks: dict[str, TaskID] = {}
    progress = Progress(
        MarkColumn(spinner_name="dots", style="cyan"),
        TextColumn("{task.description}"),
        ElapsedColumn(),
        console=console,
        transient=False,
    )
    progress.start()

    def sink(event: OperationProgressEvent) -> None:
        with lock:
            elapsed = clock.elapsed(event)
            task_id = tasks.get(event.step_id)
            if task_id is None:
                task_id = progress.add_task(
                    step_text(event),
                    total=None,
                    started=clock.started[event.step_id],
                    mark="",
                    final=None,
                )
                tasks[event.step_id] = task_id
            else:
                progress.update(task_id, description=step_text(event))
            if event.status in _FINISHED:
                mark, style = _MARKS[event.status]
                final = ""
                if event.status is OperationStepStatus.SKIPPED:
                    final = "nothing to do"
                elif elapsed >= 1:
                    final = format_duration(elapsed)
                progress.update(task_id, mark=mark, mark_style=style, final=final)
                progress.stop_task(task_id)
        if technical_sink is not None:
            technical_sink(log_line(event))

    try:
        yield sink
    finally:
        progress.stop()
