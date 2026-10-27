"""External effects through the step hub: no blind repeat of a mutation (T07, T08).

The hub gives every entry of a step a new execution id. The connector runtime
keys a declared external effect by its logical identity — run, group, step and
loop item — never by the execution id, so neither a rework entry of the same
step nor a crash between the provider call and the checkpoint can repeat the
mutation: the second claim finds the ledger row and replays its invocation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import core.application.orchestration.agents.common as agents_common
from core.application.orchestration.agent_messages import build_agent_message
from core.application.orchestration.graph.nodes import execute_step_node
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.workflows.workflow_definition_snapshot import definition_digest
from tests._workflow_definitions import edge, node

EXTENSION = {
    "runtime": {"contract_version": "extension_host_v2"},
    "extension_key": "example.host-fixture",
    "extension_install_id": "install-1",
    "action_definitions": [{"action_id": "publish_record", "handler": {"mode": "extension"}}],
    "compiled_manifest": {
        "external_effects": [
            {
                "operation_id": "publish_record",
                "effect_kind": "message_delivery",
                "channel": "api",
                "idempotency": "required",
            }
        ],
        "operation_manifest": [
            {"operation_id": "publish_record", "outputs": [{"name": "record"}]},
        ],
    },
}

DEFINITION = {
    "steps": [
        {
            "step_id": "publish",
            "step_kind": "connector",
            "connector_id": "example.host-fixture.publish_record",
            "connector_install_id": "install-1",
            "input_mapping_jsonb": {"record_id": "record-a"},
        },
        {
            "step_id": "review",
            "step_kind": "transform",
            "error_policy": {"on_error": "route_to_step"},
        },
        {"step_id": "done", "step_kind": "stop_success"},
    ],
    "groups": [],
    "execution_graph": {
        "schema_version": 1,
        "entry": node("publish"),
        "edges": [
            edge("publish", node("review")),
            edge("review", node("done")),
            # A rejected review sends the run back to the same action.
            edge("review", node("publish"), "error"),
        ],
    },
    "settings": {},
}


class _Ledger:
    """The external-effect ledger, keyed by the claim's logical identity."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.provider_calls: list[dict[str, Any]] = []

    def claim(self, _db: Any, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["idempotency_key_override"])
        row = self.rows.get(key)
        if row is None:
            row = {"effect_id": f"eff-{len(self.rows) + 1}", "status": "claimed"}
            self.rows[key] = row
            return {
                "effect": dict(row),
                "should_spawn": True,
                "suppressed": False,
                "deduped": False,
            }
        return {"effect": dict(row), "should_spawn": False, "suppressed": False, "deduped": True}

    def _row(self, effect_id: str) -> dict[str, Any]:
        return next(row for row in self.rows.values() if row["effect_id"] == effect_id)

    def mark_running(self, _db: Any, *, effect_id: str, **_kwargs: Any) -> dict[str, Any]:
        row = self._row(effect_id)
        row["status"] = "running"
        return dict(row)

    def confirm(self, _db: Any, *, invocation: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        effect_id = str(
            kwargs.get("effect_id") or invocation["response"].get("external_effect_id") or ""
        )
        row = self._row(effect_id)
        row["status"] = "succeeded"
        row["invocation"] = {"status": "succeeded", "response": dict(invocation["response"])}
        return {**row, "__confirmed_replay_invocation": dict(row["invocation"])}

    def replay(self, _db: Any, *, effect: dict[str, Any]) -> dict[str, Any]:
        return {"invocation": dict(self._row(str(effect["effect_id"]))["invocation"])}

    def provider(self, *_args: Any, **kwargs: Any) -> dict[str, Any]:
        self.provider_calls.append(dict(kwargs))
        return {"status": "succeeded", "response": {"ok": True, "record": "record-a"}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setenv("ENCRYPTION_KEY", "workflow-intent-test-root")
    yield
    clear_run_definition_cache()


@pytest.fixture
def ledger():
    ledger = _Ledger()
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    effects = "core.application.extensions.external_effects"
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(EXTENSION, MagicMock()),
        ),
        patch(f"{effects}.cleanup_stale_external_effects", return_value={}),
        patch(f"{effects}.claim_external_effect", side_effect=ledger.claim),
        patch(f"{effects}.mark_external_effect_running", side_effect=ledger.mark_running),
        patch(f"{effects}.confirm_external_effect_from_invocation", side_effect=ledger.confirm),
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_replay",
            side_effect=ledger.replay,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            side_effect=ledger.provider,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        yield ledger


def _run() -> dict:
    ref = {"snapshot_id": "snap", "revision": 1, "digest": definition_digest(DEFINITION)}
    prime_run_definition(ref, DEFINITION)
    return {
        "workflow_id": "wf-effects",
        "job_id": "job-effects",
        "account_id": "acct",
        "project_id": "project",
        "workspace_id": "workspace",
        "trusted_runtime_context": {
            "account_id": "acct",
            "project_id": "project",
            "workspace_id": "workspace",
            "workflow_id": "wf-effects",
            "job_id": "job-effects",
            "test_mode": False,
        },
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "agent_messages": [],
    }


def _apply(state: dict, update: dict | None) -> dict:
    """Merge a node or hub update the way the checkpoint would."""
    merged = dict(state)
    for key, value in (update or {}).items():
        if key in {"orchestrator_journal", "agent_messages"}:
            merged[key] = [*(merged.get(key) or []), *value]
        else:
            merged[key] = value
    return merged


def _hub(state: dict) -> tuple[dict, Any]:
    command = orchestrator_hub_node(state)
    return _apply(state, command.update), command


def _review(state: dict, status: str) -> dict:
    cursor = state["run_cursor"]
    message = build_agent_message(
        from_agent="transform_step",
        to_agent="orchestrator",
        skill="step",
        status=status,
        correlation_id=f"{cursor['step_execution_id']}:{cursor['attempt']}",
        artifacts={"error": "rejected by review"} if status == "fail" else {},
        summary=f"review {status}",
        published_at=datetime.now(UTC).isoformat(),
        step_id=cursor["step_id"],
        step_execution_id=cursor["step_execution_id"],
        attempt=cursor["attempt"],
    )
    return {**state, "agent_messages": [*state["agent_messages"], message]}


def _publish(state: dict) -> dict:
    """Run the connector step for the cursor's execution and check its result."""
    update = execute_step_node(state)
    messages = update.get("agent_messages") or []
    assert messages, update
    result = messages[-1]
    assert result["status"] == "pass", result
    assert result["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    return _apply(state, update)


def test_a_rework_entry_of_the_same_action_replays_the_effect_without_a_second_mutation(
    ledger,
) -> None:
    state, command = _hub(_run())
    assert (command.goto, state["current_step_id"]) == ("execute_step", "publish")
    first_execution = state["run_cursor"]["step_execution_id"]
    state = _publish(state)
    assert len(ledger.provider_calls) == 1

    state, command = _hub(state)
    assert state["current_step_id"] == "review"
    # The review rejects the published record: the error edge re-enters the action.
    state, command = _hub(_review(state, "fail"))
    assert (command.goto, state["current_step_id"]) == ("execute_step", "publish")
    assert state["run_cursor"]["step_execution_id"] != first_execution

    state = _publish(state)

    # The new execution claimed the same logical effect and replayed its result.
    assert len(ledger.provider_calls) == 1
    assert len(ledger.rows) == 1
    replayed = state["agent_messages"][-1]["artifacts"]["connector_invocation"]
    assert replayed["response"]["record"] == "record-a"

    state, command = _hub(state)
    assert state["current_step_id"] == "review"
    state, command = _hub(_review(state, "pass"))
    state, command = _hub(_apply(state, {"agent_messages": [_stop_result(state)]}))
    assert command.update["workflow_terminal"]["kind"] == "stop_success"


def _stop_result(state: dict) -> dict:
    cursor = state["run_cursor"]
    return build_agent_message(
        from_agent="control_flow_step",
        to_agent="orchestrator",
        skill="step",
        status="pass",
        correlation_id=f"{cursor['step_execution_id']}:{cursor['attempt']}",
        artifacts={},
        summary="stop",
        published_at=datetime.now(UTC).isoformat(),
        step_id=cursor["step_id"],
        step_execution_id=cursor["step_execution_id"],
        attempt=cursor["attempt"],
    )


def test_a_crash_after_the_effect_and_before_the_checkpoint_does_not_repeat_it(ledger) -> None:
    state, _command = _hub(_run())
    dispatched = dict(state)

    # The provider call succeeds, then the worker dies before the node's
    # result reaches the checkpoint.
    execute_step_node(dispatched)
    assert len(ledger.provider_calls) == 1

    # Recovery re-runs the node for the same dispatched execution.
    state = _publish(dispatched)

    assert len(ledger.provider_calls) == 1
    state, command = _hub(state)
    assert (command.goto, state["current_step_id"]) == ("execute_step", "review")


def test_a_crash_before_the_dispatch_checkpoint_redispatches_without_a_mutation(ledger) -> None:
    initial = _run()
    # The hub's dispatch never reached the checkpoint: recovery runs the hub again
    # and gets a fresh execution of the entry step. Nothing ran for the lost one.
    _lost, _ = _hub(initial)
    state, command = _hub(initial)
    assert command.goto == "execute_step"
    assert ledger.provider_calls == []

    state = _publish(state)
    assert len(ledger.provider_calls) == 1


def test_a_result_delivered_twice_advances_the_run_once(ledger) -> None:
    state, _command = _hub(_run())
    state = _publish(state)
    publish_result = state["agent_messages"][-1]

    state, command = _hub(state)
    assert state["current_step_id"] == "review"

    # The review answers, and the connector result is delivered once more after it.
    reviewed = _review(state, "pass")
    redelivered = {
        **reviewed,
        "agent_messages": [*reviewed["agent_messages"], dict(publish_result)],
    }
    state, command = _hub(redelivered)

    # The hub accepts only the review's own execution and takes one step forward.
    assert (command.goto, state["current_step_id"]) == ("control_flow_step", "done")
    state, command = _hub(_apply(state, {"agent_messages": [_stop_result(state)]}))
    assert command.update["workflow_terminal"]["kind"] == "stop_success"
    assert len(ledger.provider_calls) == 1
