"""One connection binding per connector step.

A connector step can carry its connection twice: the step's
``connection_binding_jsonb`` (what the runtime, readiness and change
authorization read first) and ``override.connector.connection_binding`` (what
the builder edits). A write that set only one of them left the other naming a
connection that may since have been deleted: the editor showed the new
connection while runs, readiness and saves still used the old one. Every write
of a step goes through :func:`sync_step_connection_binding`, so both copies
always name the same connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_COLUMN = "connection_binding_jsonb"
_CONNECTOR_KEY = "connection_binding"


def _binding(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def sync_step_connection_binding(step: dict[str, Any], written: Mapping[str, Any]) -> None:
    """Make both copies of the step's binding the one *written* set.

    *written* is the payload of this write (a patch, a new step or a kind change).
    The column wins when the write names it; otherwise the connector override's
    binding, when the write carries one. A write that names neither leaves the
    step as it is.
    """
    override = step.get("override")
    connector = override.get("connector") if isinstance(override, Mapping) else None
    if not isinstance(connector, dict):
        return
    if _COLUMN in written:
        chosen = _binding(written.get(_COLUMN))
    else:
        written_override = written.get("override")
        written_connector = (
            written_override.get("connector") if isinstance(written_override, Mapping) else None
        )
        if not isinstance(written_connector, Mapping) or _CONNECTOR_KEY not in written_connector:
            return
        chosen = _binding(written_connector.get(_CONNECTOR_KEY))
    if chosen is None:
        return
    step[_COLUMN] = dict(chosen)
    connector[_CONNECTOR_KEY] = dict(chosen)
