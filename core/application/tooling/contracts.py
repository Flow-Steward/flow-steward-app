"""Application contracts for isolated tooling execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.application.management.command_specs.contracts import OperationProgressEvent

ToolCallable = Callable[[], int | None]
ToolProgressSink = Callable[[OperationProgressEvent], None]


class ToolRunResult(BaseModel):
    """Structured result for long-running tooling commands."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    status: str
    exit_code: int
    command: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    stdout_full: str = Field(default="", exclude=True)
    stderr_full: str = Field(default="", exclude=True)
    artifacts: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ToolRunner(Protocol):
    """Execution port used by application tooling services."""

    def run_callable(
        self,
        *,
        operation: str,
        target: ToolCallable,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        artifacts: list[str] | None = None,
        progress_sink: ToolProgressSink | None = None,
    ) -> ToolRunResult: ...

    def run_process(
        self,
        *,
        operation: str,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        artifacts: list[str] | None = None,
        progress_sink: ToolProgressSink | None = None,
    ) -> ToolRunResult: ...
