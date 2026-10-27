"""Capture a step's returned files into the run's result as the step completes.

A file result ticked "Return from workflow" is copied into ``state.artifacts``
under its stable key from the state update of the step that produced it. The
copy is taken from that step's own update, never from the shared variable
later, so another step writing the same variable — a parallel lane, the other
branch of an If, a later loop pass — cannot replace the file this step
returned.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import Any

from core.application.registry.workflow_returned_files import (
    ReturnedFile,
    captured_returned_files,
    derive_returned_files,
    returned_files_for_step,
)

logger = logging.getLogger(__name__)

_CACHE_ATTRIBUTE = "_fs_returned_files"


def _definition_returned_files(definition: Any) -> list[ReturnedFile]:
    cached = getattr(definition, _CACHE_ATTRIBUTE, None)
    if cached is not None:
        return cached
    from core.application.registry.workflow_contract import json_object

    settings = getattr(definition, "settings", None) or {}
    callable_obj = json_object(json_object(settings).get("callable"))
    steps = list((getattr(definition, "steps", None) or {}).values())
    outputs = callable_obj.get("expected_outputs")
    returned, _issues = derive_returned_files(
        steps,
        legacy_declarations=[
            row
            for row in (
                callable_obj.get("expected_artifacts") or callable_obj.get("artifacts") or []
            )
            if isinstance(row, Mapping)
        ],
        taken_names=[
            str(row.get("name") or "").strip()
            for row in (outputs if isinstance(outputs, list) else [])
            if isinstance(row, Mapping)
        ],
    )
    with contextlib.suppress(AttributeError):
        setattr(definition, _CACHE_ATTRIBUTE, returned)
    return returned


def with_returned_files(state: Mapping[str, Any], update: Any) -> Any:
    """*update* plus the run's returned files, when the active step produced one."""
    if not isinstance(update, dict) or not update:
        return update
    try:
        from core.application.orchestration.active_step import active_step
        from core.application.orchestration.run_definition import load_run_definition

        definition = load_run_definition(state)
        current = active_step(state, definition=definition)
    except Exception:
        # A run without a pinned definition (an isolated step test) returns nothing.
        return update
    if current is None:
        return update
    returned = returned_files_for_step(_definition_returned_files(definition), current.step_id)
    if not returned:
        return update
    artifacts = captured_returned_files(state=state, update=update, returned=returned)
    if artifacts is None:
        return update
    logger.debug(
        "returned files captured step_id=%s keys=%s",
        current.step_id,
        sorted(row.key for row in returned),
    )
    return {**update, "artifacts": artifacts}


__all__ = ["with_returned_files"]
