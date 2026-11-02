"""Delegating guards in front of the standard OTLP exporters.

Neither guard transports, serializes, buffers or retries anything: each one only
decides whether, and with which already-public data, the standard exporter is
called.

* ``PidGuardedMetricExporter``: the SDK restarts an inherited periodic metric
  reader in every forked child and collects once more on its shutdown. Without
  the guard a child would export the parent's cumulative state.
* ``SanitizingSpanExporter``: finished spans are immutable, so the policy builds
  a reduced public ``ReadableSpan`` copy (or drops the span) before export.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from core.infrastructure.observability.otel_http import (
    FASTAPI_SCOPE,
    HTTP_SERVER_ATTRIBUTES,
    normalize_server_span,
)
from core.infrastructure.observability.otel_instruments import FLOW_STEWARD_SCOPE
from core.infrastructure.observability.otel_mcp import (
    MCP_CLIENT_ATTRIBUTES,
    MCP_SDK_SCOPE,
    normalize_client_span,
)


class OwnerProcess:
    """The process that created the telemetry providers."""

    def __init__(self, pid: int | None = None) -> None:
        self._pid = os.getpid() if pid is None else int(pid)

    @property
    def pid(self) -> int:
        return self._pid

    def is_current(self) -> bool:
        return os.getpid() == self._pid


class PidGuardedMetricExporter(MetricExporter):
    """Calls the delegate only from the owner process, including on shutdown."""

    def __init__(
        self,
        delegate: MetricExporter,
        *,
        preferred_temporality: dict[type, Any] | None = None,
        preferred_aggregation: dict[type, Any] | None = None,
        owner: OwnerProcess | None = None,
    ) -> None:
        # The reader asks the exporter it is given; the caller passes the same
        # preferences it gave the delegate.
        super().__init__(
            preferred_temporality=preferred_temporality,
            preferred_aggregation=preferred_aggregation,
        )
        self._delegate = delegate
        self._owner = owner or OwnerProcess()

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        if not self._owner.is_current():
            # A forked copy of the parent's state: reporting success keeps the
            # inherited reader quiet without anything leaving the child.
            return MetricExportResult.SUCCESS
        return self._delegate.export(metrics_data, timeout_millis=timeout_millis, **kwargs)

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        if not self._owner.is_current():
            return True
        return self._delegate.force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        if not self._owner.is_current():
            return
        self._delegate.shutdown(timeout_millis=timeout_millis, **kwargs)


@dataclass(frozen=True)
class ScopeRule:
    """What an instrumentation scope may export."""

    allowed_attributes: frozenset[str] = frozenset()
    allowed_attribute_prefixes: tuple[str, ...] = ()
    allowed_kinds: frozenset[Any] | None = None
    # Rebuilds the span name from the allowlisted attributes, reducing them in place.
    normalize: Callable[[ReadableSpan, dict[str, Any]], str] | None = None

    def allows_attribute(self, key: str) -> bool:
        return key in self.allowed_attributes or key.startswith(self.allowed_attribute_prefixes)


@dataclass(frozen=True)
class SpanExportPolicy:
    """Allowlist of scopes; spans from any other scope are dropped."""

    scopes: Mapping[str, ScopeRule] = field(default_factory=dict)

    def sanitize(self, span: ReadableSpan) -> ReadableSpan | None:
        scope = span.instrumentation_scope
        rule = self.scopes.get(scope.name if scope is not None else "")
        if rule is None:
            return None
        if rule.allowed_kinds is not None and span.kind not in rule.allowed_kinds:
            return None
        attributes = {
            key: value
            for key, value in (span.attributes or {}).items()
            if rule.allows_attribute(key)
        }
        name = span.name
        if rule.normalize is not None:
            name = rule.normalize(span, attributes)
        status = span.status
        # Status descriptions carry exception text; only the code is exported.
        safe_status = Status(status.status_code) if status is not None else Status(StatusCode.UNSET)
        return ReadableSpan(
            name=name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=attributes,
            # Events (exceptions included) carry free text; none is exported.
            events=(),
            links=tuple(Link(link.context) for link in span.links),
            kind=span.kind,
            status=safe_status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=scope,
        )


class SanitizingSpanExporter(SpanExporter):
    """Exports policy-reduced copies through the standard exporter."""

    def __init__(
        self,
        delegate: SpanExporter,
        policy: SpanExportPolicy,
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._delegate = delegate
        self._policy = policy
        self._on_error = on_error

    def _sanitized(self, spans: Iterable[ReadableSpan]) -> list[ReadableSpan]:
        reduced: list[ReadableSpan] = []
        for span in spans:
            try:
                copy = self._policy.sanitize(span)
            except Exception as exc:  # a malformed span must not stop the batch
                if self._on_error is not None:
                    self._on_error(exc)
                continue
            if copy is not None:
                reduced.append(copy)
        return reduced

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        reduced = self._sanitized(spans)
        if not reduced:
            return SpanExportResult.SUCCESS
        return self._delegate.export(reduced)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._delegate.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._delegate.shutdown()


# First-party scope: domain attributes are defined by the export contract and
# grow with it; nothing outside these names leaves the process.
DEFAULT_SPAN_EXPORT_POLICY = SpanExportPolicy(
    scopes={
        FLOW_STEWARD_SCOPE: ScopeRule(
            allowed_attribute_prefixes=("fs.",),
            allowed_attributes=frozenset(
                {
                    "error.type",
                    "gen_ai.operation.name",
                    "gen_ai.provider.name",
                    "gen_ai.request.model",
                    "gen_ai.response.model",
                    "gen_ai.tool.name",
                    "gen_ai.usage.cache_creation.input_tokens",
                    "gen_ai.usage.cache_read.input_tokens",
                    "gen_ai.usage.input_tokens",
                    "gen_ai.usage.output_tokens",
                    "mcp.method.name",
                }
            ),
        ),
        FASTAPI_SCOPE: ScopeRule(
            allowed_attributes=HTTP_SERVER_ATTRIBUTES,
            allowed_kinds=frozenset({SpanKind.SERVER}),
            normalize=normalize_server_span,
        ),
        # Only the SDK's client spans: fs-mcp replaces its server middleware.
        MCP_SDK_SCOPE: ScopeRule(
            allowed_attributes=MCP_CLIENT_ATTRIBUTES,
            allowed_kinds=frozenset({SpanKind.CLIENT}),
            normalize=normalize_client_span,
        ),
    }
)


__all__ = [
    "DEFAULT_SPAN_EXPORT_POLICY",
    "FLOW_STEWARD_SCOPE",
    "OwnerProcess",
    "PidGuardedMetricExporter",
    "SanitizingSpanExporter",
    "ScopeRule",
    "SpanExportPolicy",
]
