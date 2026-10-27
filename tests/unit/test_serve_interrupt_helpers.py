from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.approval_payload import state_based_approval_payload
from core.infrastructure.runtime import serve_interrupt_helpers


@pytest.mark.parametrize("authored", [True, False])
def test_ws_approval_payload_preserves_decision_identity_with_graph_fallback(authored):
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.management.approval_decision_service import ApprovalDecisionService
    from core.application.management.command_models_api_inputs import ApprovalDecisionInput
    from core.application.management.errors import ServiceConflictError
    from core.application.management.query_services_approval import ApprovalQueryService
    from core.application.orchestration.approval_payload import approval_id_for_execution
    from tests.unit.test_approval_gate import _state_gate_command

    semantic_id = (
        approval_id_for_execution("11111111-1111-4111-8111-111111111111") if authored else None
    )
    original = {"stage": "Review publication"}
    if semantic_id is not None:
        original["approval_id"] = semantic_id
    before = dict(original)
    state = SimpleNamespace(
        values={"external_effect_resolution_ref": {"private": "never-public"}},
        tasks=[
            SimpleNamespace(interrupts=[SimpleNamespace(id="graph-occurrence", value=original)])
        ],
    )

    payload = serve_interrupt_helpers.ws_get_approval_payload(
        {"id": "job-1", "thread_id": "thread-1"},
        get_thread_state_snapshot=lambda _thread_id: state,
        state_based_approval_payload=state_based_approval_payload,
        get_logger_only=lambda: None,
        logger=MagicMock(),
    )

    assert original == before
    authority_state = SimpleNamespace(
        values=dict(state.values),
        tasks=[
            SimpleNamespace(interrupts=[SimpleNamespace(id="graph-occurrence", value=dict(before))])
        ],
    )
    row = {
        "id": "42",
        "thread_id": "thread-1",
        "project_id": "7",
        "status": "awaiting_approval",
        "meta": {},
    }
    logger = SimpleNamespace(
        get_job=lambda _: row, get_project=lambda _: {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
    )
    current_gate = ApprovalQueryService(
        get_logger=lambda: logger,
        get_thread_state_snapshot=lambda _: authority_state,
        approval_fallback_payload=lambda _: {"message": "fallback"},
    ).get_current_gate("42")
    command = _state_gate_command(authority_state)
    actual_gate = command._current_interrupt_payload("thread-1", attempts=1)
    assert ApprovalDecisionService.current_approval_id(actual_gate) == current_gate.approval_id
    assert payload["approval_id"] == current_gate.approval_id
    assert (
        command._validate_current_gate(
            row=row,
            payload=actual_gate,
            decision=ApprovalDecisionInput(approved=True, approval_id=payload["approval_id"]),
            token_principal=False,
        )
        == current_gate.approval_id
    )
    stale_ids = [approval_id_for_execution("22222222-2222-4222-8222-222222222222")]
    if authored:
        stale_ids.append("graph-occurrence")
    for stale_id in stale_ids:
        stale_decision = ApprovalDecisionInput(approved=True, approval_id=stale_id)
        with pytest.raises(ServiceConflictError, match="stale"):
            command._validate_current_gate(
                row=row,
                payload=actual_gate,
                decision=stale_decision,
                token_principal=False,
            )

    assert payload["interrupt_id"] == "graph-occurrence"
    assert original == before
    assert "external_effect_resolution_ref" not in payload
    assert "never-public" not in str(payload)


def _import_serve():
    try:
        from tests import api_runtime_test_surface as api_runtime
    except Exception as exc:
        pytest.skip(f"api_runtime not importable: {exc}")
    return api_runtime


def test_graph_interrupt_trace_detection():
    api_runtime = _import_serve()

    text = "langgraph.errors.GraphInterrupt: (Interrupt(value={'stage': 'Review'}, id='x'),)"
    assert api_runtime._is_graph_interrupt_trace(text) is True
    assert api_runtime._is_graph_interrupt_trace("RuntimeError: boom") is False


def test_extract_interrupt_payload_from_text_parses_value_dict():
    api_runtime = _import_serve()

    text = "Traceback...\nlanggraph.errors.GraphInterrupt: (Interrupt(value={'stage': 'Review', 'content': 'abc'}, id='x'),)"
    payload = api_runtime._extract_interrupt_payload_from_text(text)
    assert payload == {"stage": "Review", "content": "abc"}


def test_extract_interrupt_payload_from_text_handles_braces_inside_content_string():
    api_runtime = _import_serve()

    text = (
        "Traceback...\n"
        "langgraph.errors.GraphInterrupt: "
        "(Interrupt(value={'stage': 'Review', 'message': 'Please review requirements', "
        "'content': '{\\n"
        '  "schema_version": "phase_result_v2",\\n'
        '  "artifacts": {"REQUIREMENTS_DOC": "Body with {user_id} placeholder"}\\n'
        "}'}, id='x'),)"
    )
    payload = api_runtime._extract_interrupt_payload_from_text(text)
    assert isinstance(payload, dict)
    assert payload.get("stage") == "Review"
    assert payload.get("message") == "Please review requirements"
    assert '"REQUIREMENTS_DOC"' in str(payload.get("content"))


def test_build_runtime_payload_suppresses_graph_interrupt_errors():
    api_runtime = _import_serve()

    graph_interrupt_error = (
        "langgraph.errors.GraphInterrupt: (Interrupt(value={'stage': 'Architecture'}, id='x'),)"
    )
    job = {
        "id": 555,
        "status": "in_progress",
        "thread_id": "",
        "last_error": graph_interrupt_error,
        "selected_workflow": "",
        "workflow_id": "",
        "meta": {},
        "current_phase": "",
        "current_agent": "",
        "last_response_excerpt": "",
        "heartbeat_at": None,
    }
    bl = MagicMock()

    def _latest_log_entry(_job_id, *args, **kwargs):
        if kwargs.get("require_error"):
            return {"error": graph_interrupt_error}
        return None

    bl.get_latest_log_entry.side_effect = _latest_log_entry
    bl.get_latest_event.return_value = None
    bl.get_latest_agent_message.return_value = None

    with patch.object(api_runtime, "_get_thread_state_values", return_value={}):
        payload = api_runtime._build_runtime_payload(job, bl)

    assert payload["last_stderr_excerpt"] == ""


def test_approval_fallback_payload_uses_last_response_excerpt():
    api_runtime = _import_serve()

    payload = api_runtime._approval_fallback_payload(
        {
            "last_response_excerpt": "Generated requirements doc content",
        }
    )
    assert payload["stage"] == "Review"
    assert payload["message"] == "Awaiting your approval."
    assert payload["content"] == "Generated requirements doc content"


def test_approval_fallback_payload_sets_non_empty_placeholder():
    api_runtime = _import_serve()

    payload = api_runtime._approval_fallback_payload({})
    assert payload["stage"] == "Review"
    assert payload["message"] == "Awaiting your approval."
    assert "loading" in payload["content"].lower()


def test_approval_fallback_payload_uses_latest_agent_message_when_excerpt_is_task_summary():
    api_runtime = _import_serve()
    from core.infrastructure.runtime import http_app_events_resume

    bl = MagicMock()
    bl.get_agent_messages.return_value = [
        {
            "phase_id": "review",
            "summary": "Reviewed the implementation and confirmed there are no remaining blockers.",
            "artifacts": {
                "review_report": "## Review\nNo remaining blockers. Tests are green and release workflow is hardened.",
            },
        }
    ]

    with patch.object(http_app_events_resume, "_get_job_store", return_value=bl):
        payload = api_runtime._approval_fallback_payload(
            {
                "id": 42,
                "task_summary": "Stand up a fresh release-hardening workflow",
                "last_response_excerpt": "Stand up a fresh release-hardening workflow",
                "current_phase": "review",
            }
        )

    assert payload["stage"] == "Review"
    assert "No remaining blockers" in payload["content"]


def test_build_approval_entry_normalizes_comment_and_actor_name():
    entry = serve_interrupt_helpers.build_approval_entry(
        {"stage": "Review", "message": "Ship it?", "content": "Long body"},
        True,
        "continue",
        "  Looks good  ",
        override_content="  override body  ",
        actor_username="  alice  ",
        timestamp="2026-03-30T12:00:00+00:00",
    )

    assert "selected_stories" not in entry
    assert entry["stage"] == "Review"
    assert entry["question"] == "Ship it?"
    assert entry["comment"] == "Looks good"
    assert entry["override_content_preview"] == "override body"
    assert entry["actor"] == "alice"
    assert entry["timestamp"] == "2026-03-30T12:00:00+00:00"


def _state_based_effect_pause():
    from tests.unit.test_approval_gate import make_state

    return make_state(
        step_id="draft",
        task_source="workflow_test_run",
        status="awaiting_approval",
        needs_user_approval=True,
        pending={
            "request_id": "effect-review",
            "kind": "external_effect_unknown",
            "detail": {
                "external_effect": {
                    "effect_id": "11111111-1111-1111-1111-111111111111",
                    "claim_version": 1,
                }
            },
        },
    )


def test_ws_state_based_effect_pause_preserves_authoritative_resolution_metadata():
    snapshot = SimpleNamespace(tasks=[], values=_state_based_effect_pause())
    payload = serve_interrupt_helpers.ws_get_approval_payload(
        {"id": "job-1", "thread_id": "thread-1"},
        get_thread_state_snapshot=lambda _thread: snapshot,
        state_based_approval_payload=state_based_approval_payload,
        get_logger_only=lambda: None,
        logger=MagicMock(),
    )
    assert isinstance(payload, dict), (
        "State-based effect review must not become a generic WS fallback"
    )
    assert payload["approval_id"] == "recovery:effect-review"
    assert payload["recovery_kind"] == "external_effect_unknown"
    assert payload["gate_kind"] == "recovery"
    assert payload["request_id"] == "effect-review"
    assert payload["allowed_actions"] == ["approve", "reject"]
    assert "success path" in payload["message"]
    assert "external_effect" not in payload
    assert "external_effect_resolution_ref" not in payload
    for retired_field in (
        "approve_label",
        "reject_label",
        "comment_required_on_approve",
        "comment_required_on_reject",
        "override_content_supported",
    ):
        assert retired_field not in payload


def test_live_interrupt_stays_authoritative_over_state_based_effect_pause():
    project_state = MagicMock(side_effect=state_based_approval_payload)
    snapshot = SimpleNamespace(
        values=_state_based_effect_pause(),
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(
                        id="live-gate",
                        value={
                            "message": "Actual live approval",
                            "allowed_actions": ["approve", "reject"],
                        },
                    )
                ]
            )
        ],
    )
    payload = serve_interrupt_helpers.ws_get_approval_payload(
        {"id": "job-1", "thread_id": "thread-1"},
        get_thread_state_snapshot=lambda _thread: snapshot,
        state_based_approval_payload=project_state,
        get_logger_only=lambda: None,
        logger=MagicMock(),
    )
    assert payload == {
        "message": "Actual live approval",
        "allowed_actions": ["approve", "reject"],
        "approval_id": "live-gate",
        "interrupt_id": "live-gate",
    }

    project_state.assert_not_called()


@pytest.mark.parametrize("failure", ["no_state_gate", "snapshot_error", "projection_error"])
def test_ws_approval_projection_keeps_original_action_log_fallback(failure):
    snapshot = SimpleNamespace(tasks=[], values=_state_based_effect_pause())
    if failure == "no_state_gate":
        snapshot.values = {}
    read_snapshot = MagicMock(return_value=snapshot)
    project_state = MagicMock(side_effect=state_based_approval_payload)
    if failure == "snapshot_error":
        read_snapshot.side_effect = RuntimeError("snapshot unavailable")
    if failure == "projection_error":
        project_state.side_effect = RuntimeError("projection unavailable")
    store = MagicMock()
    store.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        "GraphInterrupt: Interrupt(value={'stage': 'Review', 'content': 'Saved log detail'})",
    )
    payload = serve_interrupt_helpers.ws_get_approval_payload(
        {"id": "job-1", "thread_id": "thread-1"},
        get_thread_state_snapshot=read_snapshot,
        state_based_approval_payload=project_state,
        get_logger_only=lambda: store,
        logger=MagicMock(),
    )
    assert payload == {"stage": "Review", "content": "Saved log detail"}
    read_snapshot.assert_called_once_with("thread-1")
    if failure == "snapshot_error":
        project_state.assert_not_called()
    else:
        project_state.assert_called_once_with(snapshot.values)


def test_ws_approval_reader_without_thread_does_not_read_or_project_state():
    read_snapshot, project_state, get_store = (MagicMock() for _ in range(3))
    assert (
        serve_interrupt_helpers.ws_get_approval_payload(
            {"id": "job-1"},
            get_thread_state_snapshot=read_snapshot,
            state_based_approval_payload=project_state,
            get_logger_only=get_store,
            logger=MagicMock(),
        )
        is None
    )
    read_snapshot.assert_not_called()
    project_state.assert_not_called()
    get_store.assert_not_called()
