"""For each runtime: the items one pass walks and the item each body run reads.

A For each resolves its ``over`` selector once per execution and keeps the
selected items in its checkpointed frame, so retries and restarts continue the
same pass, while a rework re-entry (a new execution) selects again (ADR 0038).

* A list is walked as is, null or missing is an empty pass, and any other value
  — a scalar or an object — is a single item. A dictionary is not expanded.
* A dataset cursor is refused: one item is one element of a ready collection,
  never a page that the loop fetches.
* The pass takes the first ``min(count, N)`` items, N being the one operator
  limit. Truncation is reported, not an error.

Each item is published under the For each's ``item_name`` (and its position
under ``index_name``) through the declared ``workflow_data`` channel. The
selected items are kept once per pass in the ``for_each_items`` channel, not in
the run cursor that is rewritten on every step.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.expected_input_sources import resolve_state_path
from core.application.orchestration.run_cursor import ForEachFrame
from core.application.orchestration.step_templating import set_state_path
from core.infrastructure.config.workflow_collection_limits import (
    InvalidCollectionLimitError,
    resolve_collection_item_limit,
)

SOURCE_CURSOR_UNSUPPORTED = "for_each.source_cursor_unsupported"
LIMIT_INVALID = "for_each.limit_invalid"


@dataclass
class ForEachStep:
    """State updates and journal lines of selecting a source or publishing an item."""

    updates: dict[str, Any] = field(default_factory=dict)
    journal: list[str] = field(default_factory=list)
    failure: dict[str, Any] | None = None


def _names(item: Mapping[str, Any]) -> tuple[str, str, str | None]:
    over = str(item.get("over") or "").strip()
    item_name = str(item.get("item_name") or "").strip()
    index_name = str(item.get("index_name") or "").strip() or None
    return over, item_name, index_name


def _failure(code: str, message: str, state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "workflow_data": copy.deepcopy(state.get("workflow_data") or {}),
    }


def _selector_failure(item: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any] | None:
    from core.application.orchestration.workflow_state_selector_contract import (
        RUNTIME_ERROR_CODE,
        discover_for_each_state_selectors,
        validate_selector_references,
    )

    issues = [
        issue
        for issue in validate_selector_references(discover_for_each_state_selectors(item))
        if str(issue.get("path") or "").endswith(".over")
    ]
    if not issues:
        return None
    return {
        **_failure(RUNTIME_ERROR_CODE, str(issues[0].get("message") or ""), state),
        "selector": issues[0].get("selector"),
        "path": str(issues[0].get("path") or "").removeprefix("for_each."),
    }


def items_digest(items: list[Any]) -> str:
    """A stable digest of the selected items, for diagnostics and audit only."""
    encoded = json.dumps(items, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def select_items(
    item: Mapping[str, Any], frame: ForEachFrame, state: Mapping[str, Any]
) -> ForEachStep:
    """Fix this pass's items in the frame and publish the first one.

    An empty selection publishes nothing; the caller finishes the For each on
    its success edge (its exit contract still applies).
    """
    over, item_name, index_name = _names(item)
    label = str(item.get("for_each_id") or "unknown")
    failure = _selector_failure(item, state)
    if failure is not None:
        return ForEachStep(
            failure=failure,
            journal=[f"For each '{label}' failed: {failure['code']} at source '{over}'"],
        )
    try:
        limit = resolve_collection_item_limit()
    except InvalidCollectionLimitError as exc:
        return ForEachStep(
            failure=_failure(LIMIT_INVALID, str(exc), state),
            journal=[f"For each '{label}' failed: {exc}"],
        )

    from core.application.orchestration.dataset_cursor_runtime import is_dataset_cursor

    resolved = resolve_state_path(dict(state), over) if over else None
    if isinstance(resolved, Mapping) and is_dataset_cursor(resolved):
        message = (
            f"For each '{label}' walks a ready collection; '{over}' is a dataset cursor. "
            "Read the rows first (one item is one row, never a page)."
        )
        return ForEachStep(
            failure=_failure(SOURCE_CURSOR_UNSUPPORTED, message, state), journal=[message]
        )
    if isinstance(resolved, list):
        source = list(resolved)
    elif resolved is None:
        source = []
    else:
        source = [resolved]
    selected = copy.deepcopy(source[:limit])
    frame.selected_count = len(selected)
    frame.item_index = 0
    frame.processed_count = 0
    frame.broke_at = None
    frame.source_count = len(source)
    frame.applied_limit = limit
    frame.truncated = len(source) > limit
    frame.digest = items_digest(selected)
    journal = [
        f"For each '{label}' walks {len(selected)} of {len(source)} item(s) of '{over}'"
        + (f" (limit {limit})" if frame.truncated else "")
    ]
    # Only the passes on the current container stack are kept: one For each
    # at a time (a For each never holds another).
    items_channel = {frame.for_each_execution_id: selected}
    if not selected:
        # A previous pass must not leave its last item behind for later steps.
        updates: dict[str, Any] = {"for_each_items": items_channel}
        set_state_path(updates, dict(state), item_name, None)
        if index_name:
            set_state_path(updates, {**dict(state), **updates}, index_name, None)
        return ForEachStep(updates=updates, journal=journal)
    step = publish_item(item, frame, {**dict(state), "for_each_items": items_channel})
    step.updates["for_each_items"] = items_channel
    step.journal = journal + step.journal
    return step


def pass_items(frame: ForEachFrame, state: Mapping[str, Any]) -> list[Any] | None:
    """The items fixed for *frame*'s pass, or None when the checkpoint lost them."""
    channel = state.get("for_each_items")
    items = channel.get(frame.for_each_execution_id) if isinstance(channel, Mapping) else None
    return items if isinstance(items, list) else None


def publish_item(
    item: Mapping[str, Any], frame: ForEachFrame, state: Mapping[str, Any]
) -> ForEachStep:
    """Publish the item at ``frame.item_index`` (and its index) for the body."""
    _over, item_name, index_name = _names(item)
    index = frame.item_index
    items = pass_items(frame, state)
    if items is None or not 0 <= index < len(items) or len(items) != frame.selected_count:
        return ForEachStep(
            failure=_failure(
                "for_each.item_unavailable",
                f"For each '{frame.for_each_id}' has no fixed item at position {index}",
                state,
            )
        )
    updates: dict[str, Any] = {}
    set_state_path(updates, dict(state), item_name, copy.deepcopy(items[index]))
    if index_name:
        set_state_path(updates, {**dict(state), **updates}, index_name, index)
    return ForEachStep(
        updates=updates,
        journal=[f"For each '{frame.for_each_id}' item {index + 1}/{frame.selected_count}"],
    )


def compact_item_messages(messages: Any, body_step_ids: set[str]) -> list[dict[str, Any]] | None:
    """Keep only the newest message of each body step (actor, skill) at an item boundary.

    Every item re-runs the body, so its messages would grow the checkpoint with
    the item count. ``message.step.<id>`` and ``message.group.<id>`` read the
    latest result, which is kept; messages outside the body are untouched.
    """
    if not isinstance(messages, list) or not body_step_ids:
        return None
    retained: set[int] = set()
    latest_by_step: dict[tuple[str, str, str], int] = {}
    for index, message in enumerate(messages):
        step_id = str(message.get("step_id") or "").strip() if isinstance(message, dict) else ""
        if step_id not in body_step_ids:
            retained.add(index)
            continue
        key = (
            step_id,
            str(message.get("from_agent") or "").strip(),
            str(message.get("skill") or "").strip(),
        )
        latest_by_step[key] = index
    retained.update(latest_by_step.values())
    compacted = [message for index, message in enumerate(messages) if index in retained]
    return compacted if len(compacted) < len(messages) else None


__all__ = [
    "LIMIT_INVALID",
    "SOURCE_CURSOR_UNSUPPORTED",
    "ForEachStep",
    "compact_item_messages",
    "items_digest",
    "pass_items",
    "publish_item",
    "select_items",
]
