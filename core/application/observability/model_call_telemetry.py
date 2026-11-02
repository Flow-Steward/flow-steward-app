"""Model call telemetry for operations outside a workflow segment.

Inside a workflow the job's ``PlatformLoggerCallback`` already produces the
model and tool spans, so ``model_call_scope`` only names the call's purpose
there. Elsewhere (step replay, capability verification, a classifier run at
intake) it attaches one ``ModelCallTelemetryHandler`` to every LangChain call in
the block through langchain_core's configure hook, which also reaches models
returned by ``bind_tools`` that drop a bound config.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

from core.application.observability.llm_usage import usage_from_llm_result
from core.application.observability.workflow_telemetry import (
    RunTelemetry,
    active_workflow_telemetry,
    llm_purpose,
)
from core.infrastructure.observability import otel_instruments

_handler: ContextVar[ModelCallTelemetryHandler | None] = ContextVar(
    "flow_steward_model_call_telemetry", default=None
)


class ModelCallTelemetryHandler(BaseCallbackHandler):
    """Model and tool spans and measurements of one operation; records no usage rows."""

    def __init__(self, purpose: str) -> None:
        super().__init__()
        self._purpose = purpose
        self._telemetry = RunTelemetry()
        self._finished: set[str] = set()

    def on_chat_model_start(
        self, serialized, messages, *, run_id=None, parent_run_id=None, metadata=None, **_kwargs
    ) -> None:
        del serialized, messages
        metadata = metadata if isinstance(metadata, dict) else {}
        self._telemetry.model_started(
            run_id,
            parent_run_id,
            provider_type=metadata.get("flow_steward_provider_policy")
            or metadata.get("flow_steward_provider"),
            model=metadata.get("flow_steward_model"),
            purpose=self._purpose,
        )

    def on_llm_end(self, response=None, *, run_id=None, **_kwargs) -> None:
        key = str(run_id or "")
        if not key or key in self._finished:
            return
        self._finished.add(key)
        llm_output = getattr(response, "llm_output", None)
        llm_output = llm_output if isinstance(llm_output, dict) else {}
        self._telemetry.model_finished(
            run_id,
            usage=usage_from_llm_result(response),
            response_model=llm_output.get("model_name") or llm_output.get("model"),
        )

    def on_llm_error(self, error=None, *, run_id=None, **_kwargs) -> None:
        self._telemetry.model_finished(run_id, error=error if error is not None else True)

    def on_tool_start(
        self, serialized=None, input_str=None, *, run_id=None, parent_run_id=None, **kwargs
    ) -> None:
        del input_str
        name = kwargs.get("name") or (serialized or {}).get("name")
        self._telemetry.tool_started(run_id, parent_run_id, tool_name=name)

    def on_tool_end(self, output=None, *, run_id=None, **_kwargs) -> None:
        del output
        self._telemetry.tool_finished(run_id)

    def on_tool_error(self, error=None, *, run_id=None, **_kwargs) -> None:
        self._telemetry.tool_finished(run_id, error=error if error is not None else True)


register_configure_hook(_handler, True)


@contextmanager
def model_call_scope(purpose: str) -> Iterator[None]:
    """Attribute the model calls in the block to *purpose*, with telemetry exactly once."""
    with llm_purpose(purpose):
        if active_workflow_telemetry() is not None or not (
            otel_instruments.traces_enabled() or otel_instruments.metrics_enabled()
        ):
            yield
            return
        token = _handler.set(ModelCallTelemetryHandler(purpose))
        try:
            yield
        finally:
            _handler.reset(token)


__all__ = ["ModelCallTelemetryHandler", "model_call_scope"]
