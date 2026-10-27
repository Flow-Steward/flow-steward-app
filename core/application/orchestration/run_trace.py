"""Bounded routed-edge facts carried with the cursor in a checkpoint update.

Nothing here writes a database or publishes a callback. A constructed node output
is only a candidate until the checkpoint stores its channel values.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from core.application.jobs.workflow_run_models import (
    PublicWorkflowTraceV1,
    PublicWorkflowTransitionV1,
)
from core.application.orchestration.run_cursor import (
    ForEachFrame,
    Frame,
    FrozenDefinition,
    RunCursor,
)
from core.application.workflows.execution_graph import Edge

TRACE_LIMIT = 1000
TRACE_KEY = "workflow_run_trace"


def edge_fact(
    state: Mapping[str, Any],
    definition: FrozenDefinition,
    edge: Edge | None,
    cursor: RunCursor,
    *,
    source_frame: Frame | None = None,
) -> dict[str, Any] | None:
    if edge is None or str(state.get("task_source") or "").lower() == "step_test_run":
        return None
    frames = [*cursor.frames]
    if source_frame is not None and not any(
        frame.execution_id == source_frame.execution_id for frame in frames
    ):
        frames.append(source_frame)
    fact: dict[str, Any] = {
        "source": edge.source.dump(),
        "outcome": edge.outcome.dump(),
        "target": edge.target.dump(),
        "frames": [
            {
                "kind": frame.kind,
                "id": frame.container_id,
                "execution_id": frame.execution_id,
                **({"item_index": frame.item_index} if isinstance(frame, ForEachFrame) else {}),
            }
            for frame in frames
        ],
    }
    if edge.source.kind == "step":
        if cursor.step_id != edge.source.id or not cursor.step_execution_id:
            return None
        step = definition.step(edge.source.id)
        fact.update(step_execution_id=cursor.step_execution_id, attempt=cursor.attempt)
        if step.get("lane_id"):
            fact["lane_id"] = str(step["lane_id"])
        if step.get("step_kind") == "approval":
            for message in reversed(list(state.get("agent_messages") or [])):
                if (
                    isinstance(message, Mapping)
                    and message.get("step_execution_id") == cursor.step_execution_id
                    and message.get("attempt") == cursor.attempt
                ):
                    artifacts = message.get("artifacts")
                    approval_id = (
                        artifacts.get("approval_id") if isinstance(artifacts, Mapping) else None
                    )
                    if approval_id:
                        fact["approval_id"] = str(approval_id)
                    break
    else:
        if source_frame is None or source_frame.node != edge.source:
            return None
        fact["container_execution_id"] = source_frame.execution_id
    return fact


def append_run_trace(
    state: Mapping[str, Any], facts: Sequence[dict[str, Any] | None]
) -> dict[str, Any] | None:
    if str(state.get("task_source") or "").lower() == "step_test_run":
        return None
    previous = state.get(TRACE_KEY)
    if isinstance(previous, Mapping):
        trace = PublicWorkflowTraceV1.model_validate(previous).model_dump(mode="json")
        ref = state.get("workflow_definition_ref")
        if (
            not isinstance(ref, Mapping)
            or trace["job_id"] != str(state.get("job_id") or "")
            or trace["workflow_id"] != str(state.get("workflow_id") or "")
            or any(trace[key] != ref.get(key) for key in ("snapshot_id", "revision", "digest"))
        ):
            return None
    else:
        ref = state.get("workflow_definition_ref")
        if not isinstance(ref, Mapping) or not state.get("job_id") or not state.get("workflow_id"):
            return None
        try:
            trace = PublicWorkflowTraceV1.model_validate(
                {
                    "job_id": str(state["job_id"]),
                    "workflow_id": str(state["workflow_id"]),
                    "snapshot_id": ref.get("snapshot_id"),
                    "revision": ref.get("revision"),
                    "digest": ref.get("digest"),
                    "covered_from_start": not bool(
                        state.get("run_cursor") or state.get("routed_transitions")
                    ),
                }
            ).model_dump(mode="json")
        except ValidationError:
            # A malformed checkpoint ref must not mask the original definition
            # failure while the hub constructs its terminal command.
            return None
    retained = list(trace["transitions"])
    known = {fact["id"] for fact in retained}
    for raw in facts:
        if raw is None:
            trace["truncated"] = True
            continue
        identity = json.dumps(
            [trace["job_id"], trace["snapshot_id"], trace["digest"], raw],
            sort_keys=True,
            separators=(",", ":"),
        )
        fact_id = hashlib.sha256(identity.encode()).hexdigest()
        if fact_id in known:
            continue
        sequence = trace["last_sequence"] + 1
        fact = PublicWorkflowTransitionV1.model_validate(
            {**raw, "id": fact_id, "sequence": sequence}
        ).model_dump(mode="json")
        retained.append(fact)
        known.add(fact_id)
        trace["last_sequence"] = sequence
    if len(retained) > TRACE_LIMIT:
        trace["truncated"] = True
        retained = retained[-TRACE_LIMIT:]
    trace["transitions"] = retained
    return PublicWorkflowTraceV1.model_validate(trace).model_dump(mode="json")
