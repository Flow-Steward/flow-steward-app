"""Safe connector identity and structured MCP operation logging."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _McpObservabilityState:
    connector_id: str = ""
    job_id: str = ""
    protocol_era: str = ""
    protocol_version: str = ""


_ACTIVE_MCP_OBSERVABILITY: ContextVar[_McpObservabilityState | None] = ContextVar(
    "active_mcp_observability",
    default=None,
)


def _mcp_log_extra(
    state: _McpObservabilityState,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "connector_id": state.connector_id,
        "job_id": state.job_id,
        "protocol_era": state.protocol_era,
        "protocol_version": state.protocol_version,
        **fields,
    }


@contextmanager
def mcp_observability_context(
    *,
    connector_id: str,
    job_id: str = "",
) -> Iterator[_McpObservabilityState]:
    """Bind safe connector identity to short-lived protocol operations."""

    state = _McpObservabilityState(
        connector_id=str(connector_id or "").strip(),
        job_id=str(job_id or "").strip(),
    )
    reset_token = _ACTIVE_MCP_OBSERVABILITY.set(state)
    try:
        yield state
    finally:
        _ACTIVE_MCP_OBSERVABILITY.reset(reset_token)


def log_mcp_pagination_completed(
    state: _McpObservabilityState,
    *,
    page_count: int,
) -> None:
    """Log bounded discovery completion without remote request or result data."""

    logger.info(
        "MCP connector discovery pagination completed",
        extra=_mcp_log_extra(state, page_count=int(page_count)),
    )
