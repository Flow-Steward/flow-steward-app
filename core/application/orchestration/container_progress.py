"""Runtime-owned container facts carried in the same checkpoint as the cursor.

Lifecycle hooks capture frames before removal. No callback or pending write is
public evidence; the authorized reader validates the committed envelope again.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from core.application.jobs.public_secret_redaction import redact_public_secrets
from core.application.jobs.workflow_run_models import PublicContainerProgressV1
from core.application.observability_vocabulary import job_status_meta
from core.application.orchestration.run_cursor import (
    ForEachFrame,
    Frame,
    FrozenDefinition,
    RunCursor,
    RunCursorError,
)
from core.application.security.credential_redaction import redact_text
from core.application.workflows.execution_graph import NodeRef

CONTAINER_PROGRESS_KEY = "workflow_container_progress"
CONTAINER_PROGRESS_LIMIT = 1000
logger = logging.getLogger(__name__)


def frame_identity(frame: Frame) -> dict[str, Any]:
    return {
        "kind": frame.kind,
        "id": frame.container_id,
        "execution_id": frame.execution_id,
        **({"item_index": frame.item_index} if isinstance(frame, ForEachFrame) else {}),
    }


def container_patch(
    cursor: RunCursor,
    frame: Frame,
    *,
    started: bool = False,
    outcome: str | None = None,
    lanes: Sequence[Mapping[str, Any]] | None = None,
    failure: Mapping[str, Any] | None = None,
    boundary: str = "body",
) -> dict[str, Any] | None:
    """Snapshot an explicit lifecycle boundary; never retain items or raw errors."""
    try:
        position = next(
            i for i, item in enumerate(cursor.frames) if item.execution_id == frame.execution_id
        )
        patch: dict[str, Any] = {
            "container": frame.node.dump(),
            "execution_id": frame.execution_id,
            "parent_frames": [frame_identity(item) for item in cursor.frames[:position]],
            "_started": started,
        }
        if outcome is not None:
            patch["outcome"] = outcome
        # Digest is assigned only after selection succeeds, before publication.
        if isinstance(frame, ForEachFrame) and frame.digest:
            patch["for_each"] = {
                **frame.counts(),
                "item_index": frame.item_index if frame.selected_count else None,
            }
        if lanes is not None:
            rows = [{"lane_id": row["lane_id"], "status": row["status"]} for row in lanes]
            patch["lanes"] = {
                "item_index": frame.item_index if isinstance(frame, ForEachFrame) else None,
                "rows": rows,
                "completed_count": sum(row["status"] == "completed" for row in rows),
                "total_count": len(rows),
            }
        if failure is not None:
            patch["failure"] = {
                "code": redact_public_secrets(
                    redact_text(str(failure.get("code") or "container_failed"))
                )[:500],
                "message": redact_public_secrets(redact_text(str(failure.get("message") or "")))[
                    :500
                ],
                "boundary": boundary,
                **{
                    key: failure[key]
                    for key in ("step_id", "step_execution_id", "attempt", "lane_id")
                    if failure.get(key) is not None
                },
            }
            if failure.get("item_index") is not None:
                patch["failure"]["item_index"] = failure["item_index"]
            elif isinstance(frame, ForEachFrame) and frame.digest and frame.selected_count:
                patch["failure"]["item_index"] = frame.item_index
        return deepcopy(patch)
    except (AttributeError, KeyError, StopIteration, TypeError, ValueError):
        logger.warning("Container lifecycle evidence is unavailable")
        return None


def append_container_progress(
    state: Mapping[str, Any],
    patches: Sequence[dict[str, Any] | None],
    committed_update: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Materialize ordered facts without changing execution on evidence failure."""
    if str(state.get("task_source") or "").lower() == "step_test_run":
        return None
    try:
        ref = state.get("workflow_definition_ref")
        if not isinstance(ref, Mapping) or not all(
            state.get(key) for key in ("job_id", "project_id", "workflow_id")
        ):
            return None
        identity = {key: str(state[key]) for key in ("job_id", "project_id", "workflow_id")}
        identity.update({key: ref[key] for key in ("snapshot_id", "revision", "digest")})
        previous = state.get(CONTAINER_PROGRESS_KEY)
        if previous is not None:
            progress = PublicContainerProgressV1.model_validate(previous).model_dump(mode="json")
            if any(progress[key] != value for key, value in identity.items()):
                return None
        else:
            progress = PublicContainerProgressV1.model_validate(
                {
                    **identity,
                    "covered_from_start": not bool(
                        state.get("run_cursor") or state.get("routed_transitions")
                    ),
                }
            ).model_dump(mode="json")
        cursor = RunCursor.load(committed_update.get("run_cursor", state.get("run_cursor")))
        all_patches = [*patches, *(container_patch(cursor, frame) for frame in cursor.frames)]
        for row in progress["passes"]:
            row["parent_frames"] = [
                {k: v for k, v in parent.items() if v is not None}
                for parent in row["parent_frames"]
            ]
        records = {row["execution_id"]: row for row in progress["passes"]}
        for raw in all_patches:
            if raw is None:
                progress["truncated"] = True
                continue
            patch = deepcopy(raw)
            started = patch.pop("_started", False)
            key = patch["execution_id"]
            old = records.get(key)
            if old is None and not started:
                # An already active legacy frame has no observed start boundary.
                continue
            if old is not None and any(
                old[field] != patch[field] for field in ("container", "parent_frames")
            ):
                # Normalize absent optional parent item indices for JSON equality.
                parents = [
                    {k: v for k, v in item.items() if v is not None}
                    for item in old["parent_frames"]
                ]
                if old["container"] != patch["container"] or parents != patch["parent_frames"]:
                    progress["truncated"] = True
                    continue
            merged = {**(old or {}), **patch}
            if (
                old is not None
                and old.get("for_each")
                and patch.get("for_each")
                and old["for_each"]["item_index"] != patch["for_each"]["item_index"]
            ):
                merged["lanes"] = patch.get("lanes")
            if old is not None and old.get("finished_sequence"):
                if patch.get("outcome", old["outcome"]) != old["outcome"]:
                    progress["truncated"] = True
                continue
            # Replay of the same boundary is a no-op, not a display counter.
            compared = {field: old.get(field) for field in patch} if old else None
            if compared == patch:
                continue
            sequence = progress["last_sequence"] + 1
            merged.update(
                started_sequence=old["started_sequence"] if old else sequence,
                updated_sequence=sequence,
                current=False,
            )
            merged.setdefault("outcome", None)
            if patch.get("outcome") is not None:
                merged["finished_sequence"] = sequence
            records[key] = merged
            progress["last_sequence"] = sequence
        # A budget recovery can accept the same failed pass's edge in a later
        # Command. Its exact source execution is retained by D061; no neighbor
        # or status inference is needed. Ancestor continuations remain unknown.
        trace = committed_update.get("workflow_run_trace", state.get("workflow_run_trace"))
        for fact in (trace or {}).get("transitions", []):
            record = records.get(fact.get("container_execution_id"))
            if record is None or record.get("outcome") != "error" or not record.get("failure"):
                continue
            if (
                record["failure"].get("handling") is not None
                or fact["source"] != record["container"]
                or fact["outcome"]["name"] != "error"
            ):
                continue
            record["failure"]["handling"] = {
                "kind": "route",
                "execution_id": record["execution_id"],
                "transition_id": fact["id"],
            }
            progress["last_sequence"] += 1
            record["updated_sequence"] = progress["last_sequence"]
        terminal = bool(
            job_status_meta(committed_update.get("status", state.get("status")))["terminal"]
        )
        progress["checkpoint_terminal"] = progress["checkpoint_terminal"] or terminal
        active = {frame.execution_id for frame in cursor.frames}
        for row in records.values():
            row["current"] = (
                row["execution_id"] in active
                and not progress["checkpoint_terminal"]
                and row.get("outcome") is None
            )
        retained = sorted(
            records.values(),
            key=lambda row: (row["execution_id"] in active, row["updated_sequence"]),
            reverse=True,
        )
        if len(retained) > CONTAINER_PROGRESS_LIMIT:
            progress["truncated"] = True
            retained = retained[:CONTAINER_PROGRESS_LIMIT]
        progress["passes"] = sorted(retained, key=lambda row: row["started_sequence"])
        result = PublicContainerProgressV1.model_validate(progress).model_dump(mode="json")
        for row in result["passes"]:
            row["parent_frames"] = [
                {k: v for k, v in parent.items() if v is not None}
                for parent in row["parent_frames"]
            ]
        return result
    except (AttributeError, KeyError, TypeError, ValueError, RunCursorError):
        logger.warning("Container checkpoint evidence is unavailable")
        return None


def committed_container_progress(
    values: Mapping[str, Any],
    definition: FrozenDefinition,
    *,
    identity: Mapping[str, Any],
    trace: Any,
) -> PublicContainerProgressV1 | None:
    """Validate exact archived ownership, then derive current from this checkpoint."""
    raw = values.get(CONTAINER_PROGRESS_KEY)
    if raw is None:
        return None
    incoming = PublicContainerProgressV1.model_validate(raw)
    if any(getattr(incoming, key) != value for key, value in identity.items()):
        raise ValueError("Foreign container checkpoint")
    if any(
        str(values.get(key) or "") != identity[key]
        for key in ("job_id", "project_id", "workflow_id")
    ):
        raise ValueError("Foreign container runtime identity")
    cursor = RunCursor.load(values.get("run_cursor"))
    active = {frame.execution_id: frame for frame in cursor.frames}
    terminal = bool(job_status_meta(values.get("status"))["terminal"])
    records = {row.execution_id: row for row in incoming.passes}
    transitions = {fact.id: fact for fact in trace.transitions} if trace is not None else {}
    projected = []
    for record in incoming.passes:
        node = NodeRef(record.container.kind, record.container.id)
        container = definition.container(node)
        parent_id = container.get("group_id") if node.kind == "for_each" else None
        expected_parents = [("group", str(parent_id))] if parent_id else []
        if [(p.kind, p.id) for p in record.parent_frames] != expected_parents:
            raise ValueError("Container ancestry differs from archive")
        for parent in record.parent_frames:
            if parent.execution_id == record.execution_id or (
                parent.kind == "group" and parent.item_index is not None
            ):
                raise ValueError("Invalid container ancestry identity")
            retained = records.get(parent.execution_id)
            if retained and (retained.container.kind, retained.container.id) != (
                parent.kind,
                parent.id,
            ):
                raise ValueError("Foreign retained parent pass")
        if record.lanes is not None:
            body = container.get("body") or {}
            declared = [row["lane_id"] for row in body.get("lanes", [])]
            if (
                body.get("kind") != "lanes"
                or [row.lane_id for row in record.lanes.rows] != declared
            ):
                raise ValueError("Container lane inventory differs from archive")
        frame = active.get(record.execution_id)
        if frame is not None:
            if frame.node != node or record.outcome is not None:
                raise ValueError("Active frame conflicts with container pass")
            captured = container_patch(cursor, frame)
            parents = [parent.model_dump(exclude_none=True) for parent in record.parent_frames]
            if captured is None or captured["parent_frames"] != parents:
                raise ValueError("Active container ancestry conflicts")
            if record.for_each is not None and record.for_each.model_dump() != captured.get(
                "for_each"
            ):
                raise ValueError("Active item differs from committed cursor")
            # Only the innermost active lane body owns parallel_lanes. Retained
            # finished passes and enclosing sequence frames must not borrow it.
            if frame is cursor.frames[-1] and (container.get("body") or {}).get("kind") == "lanes":
                rows = values.get("parallel_lanes")
                if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                    raise ValueError("Committed lane inventory is unavailable")
                lane_capture = container_patch(cursor, frame, lanes=rows)
                if (
                    record.lanes is None
                    or lane_capture is None
                    or record.lanes.model_dump() != lane_capture.get("lanes")
                ):
                    raise ValueError("Active lanes differ from committed checkpoint")
        failure = record.failure
        if failure is not None:
            if failure.step_id:
                step = definition.step(failure.step_id)
                owners = definition.step_containers(failure.step_id)
                if not any(owner["_node"] == node for owner in owners):
                    raise ValueError("Container failure source is foreign")
                if failure.lane_id and step.get("lane_id") != failure.lane_id:
                    raise ValueError("Container failure lane is foreign")
            handling = failure.handling
            if handling is not None and handling.kind == "route":
                fact = transitions.get(handling.transition_id)
                accepted = {
                    (node.kind, node.id, record.execution_id),
                    *((p.kind, p.id, p.execution_id) for p in record.parent_frames),
                }
                if handling.execution_id not in {scope[2] for scope in accepted}:
                    raise ValueError("Container handling belongs to another pass")
                if fact is None:
                    # The runtime-owned proof may outlive the bounded edge tail.
                    if trace is None or not trace.truncated:
                        raise ValueError("Container handling lacks an accepted route")
                elif (
                    fact.outcome.get("name") != "error"
                    or (fact.source.kind, fact.source.id, fact.container_execution_id)
                    not in accepted
                    or fact.container_execution_id != handling.execution_id
                ):
                    raise ValueError("Container handling lacks an accepted route")
            if (
                handling is not None
                and handling.kind == "exit_approval"
                and handling.execution_id != record.execution_id
            ):
                raise ValueError("Container approval belongs to another pass")
        projected.append(record.model_copy(update={"current": frame is not None and not terminal}))
    return incoming.model_copy(update={"checkpoint_terminal": terminal, "passes": projected})


def container_progress_advances(
    old: PublicContainerProgressV1, new: PublicContainerProgressV1
) -> bool:
    """Reject stale/conflicting evidence inside the repository's locked callback."""
    if any(
        getattr(old, key) != getattr(new, key)
        for key in (
            "job_id",
            "project_id",
            "workflow_id",
            "snapshot_id",
            "revision",
            "digest",
            "covered_from_start",
        )
    ):
        return False
    if (
        new.last_sequence < old.last_sequence
        or (old.truncated and not new.truncated)
        or (old.checkpoint_terminal and not new.checkpoint_terminal)
    ):
        return False
    if new.last_sequence == old.last_sequence:
        # Only terminal/current projection may change without a lifecycle patch.
        def facts(value: PublicContainerProgressV1) -> list[dict[str, Any]]:
            return [row.model_dump(exclude={"current"}) for row in value.passes]

        return facts(old) == facts(new) and (not old.checkpoint_terminal or new.checkpoint_terminal)
    newer = {row.execution_id: row for row in new.passes}
    for before in old.passes:
        after = newer.get(before.execution_id)
        if after is None:
            if not new.truncated:
                return False
            continue
        if (
            any(
                getattr(before, key) != getattr(after, key)
                for key in ("container", "parent_frames", "started_sequence")
            )
            or after.updated_sequence < before.updated_sequence
        ):
            return False
        if before.finished_sequence is not None:
            left = before.model_dump(exclude={"current", "updated_sequence"})
            right = after.model_dump(exclude={"current", "updated_sequence"})
            if before.failure and after.failure and before.failure.handling is None:
                left["failure"]["handling"] = right["failure"]["handling"]
            if left != right:
                return False
        if after.updated_sequence == before.updated_sequence and before.model_dump(
            exclude={"current"}
        ) != after.model_dump(exclude={"current"}):
            return False
        if before.for_each and after.for_each:
            if (
                any(
                    getattr(before.for_each, key) != getattr(after.for_each, key)
                    for key in ("source_count", "selected_count", "applied_limit", "truncated")
                )
                or after.for_each.processed_count < before.for_each.processed_count
            ):
                return False
        elif before.for_each is not None:
            return False
    return True
