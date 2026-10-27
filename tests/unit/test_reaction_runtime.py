from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from core.application.notifications import dispatch as notification_dispatch
from core.application.workflows import reaction_runtime
from core.application.workflows.reaction_scopes import split_scoped_reaction_key

CURRENT_WORKFLOW_ID = "weekly_report"


def _to_json_obj(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


def _install_langgraph_command_stub(monkeypatch) -> None:
    langgraph_mod = sys.modules.get("langgraph")
    if langgraph_mod is None:
        langgraph_mod = types.ModuleType("langgraph")
        monkeypatch.setitem(sys.modules, "langgraph", langgraph_mod)
    types_mod = types.ModuleType("langgraph.types")

    class Command:
        def __init__(self, *, resume):
            self.resume = resume

    types_mod.Command = Command
    monkeypatch.setitem(sys.modules, "langgraph.types", types_mod)
    langgraph_mod.types = types_mod


def test_interrupted_retry_reads_project_config_through_explicit_port() -> None:
    job_store = MagicMock()
    job_store.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "thread_id": "thread-1",
        "status": "failed",
        "meta": {"retryable_interrupted_step": {"agent_id": "dev"}},
    }
    project_config_port = MagicMock(side_effect=RuntimeError("config store offline"))

    result = reaction_runtime.retry_interrupted_step_from_reaction(
        "job-1",
        requested_by="operator",
        get_logger_only=lambda: job_store,
        err_database_not_configured="db unavailable",
        err_job_not_found_lower="job not found",
        to_json_obj=_to_json_obj,
        sanitize_retryable_interrupted_step_snapshot=lambda value: value,
        retryable_interrupted_step_status="retryable_interrupted_step",
        resolve_job_project_name=MagicMock(),
        get_project_run_config=project_config_port,
        build_rerun_workflow_selection=MagicMock(),
        load_replay_snapshot=MagicMock(),
        enqueue_task_in_queue=MagicMock(),
        append_runtime_journal=MagicMock(),
    )

    assert result["status"] == "failed"
    assert "config store offline" in result["reason"]
    project_config_port.assert_called_once_with("project-1")
    job_store.get_project_run_config.assert_not_called()


def test_interrupted_retry_from_reaction_preserves_configured_and_inline_secret_authority() -> None:
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection
    from core.infrastructure.runtime.test_mode import parse_test_mode
    from core.infrastructure.security.secret_reference import (
        SecretReference,
        inline_secret_ref_fingerprint,
    )

    fingerprints = sorted(
        [
            secret_ref_fingerprint("workflow/wf-1/configured/v1"),
            inline_secret_ref_fingerprint(
                SecretReference(name="inline", ciphertext="encrypted-inline")
            ),
        ]
    )
    replay_snapshot = {
        "workflow_id": "wf-1",
        "inputs": {"message": "original"},
        "files": [],
        "test_mode": False,
        "input_contract": "direct_inputs",
        "task_envelope": {"inputs": {"message": "original"}, "files": []},
        "_workflow_secret_ref_fingerprints": fingerprints,
    }
    job_store = MagicMock()
    job_store.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "thread_id": "thread-1",
        "task_id": "TASK-1",
        "task_summary": "Retry by reaction",
        "task_source": "automation",
        "status": "failed",
        "meta": {
            "replay_snapshot": {"artifact_id": "artifact-a"},
            "retryable_interrupted_step": {
                "agent_id": "composer",
                "skill": "write_section",
                "phase_id": "implementation",
            },
        },
    }
    enqueue = MagicMock()

    result = reaction_runtime.retry_interrupted_step_from_reaction(
        "job-1",
        requested_by="automation",
        get_logger_only=lambda: job_store,
        err_database_not_configured="db unavailable",
        err_job_not_found_lower="job not found",
        to_json_obj=_to_json_obj,
        sanitize_retryable_interrupted_step_snapshot=lambda value: value,
        retryable_interrupted_step_status="retryable_interrupted_step",
        resolve_job_project_name=lambda _store, _job: "Project One",
        get_project_run_config=lambda _project_id: {
            "project_name": "Project One",
            "project_root": "/target/project-one",
        },
        build_rerun_workflow_selection=lambda job, *, replay_snapshot: (
            build_rerun_workflow_selection(
                job,
                replay_snapshot=replay_snapshot,
                to_json_obj=_to_json_obj,
                parse_test_mode=parse_test_mode,
            )
        ),
        load_replay_snapshot=MagicMock(return_value=replay_snapshot),
        enqueue_task_in_queue=enqueue,
        append_runtime_journal=MagicMock(),
    )

    assert result["status"] == "pending"
    assert (
        enqueue.call_args.kwargs["extra_state"]["_workflow_secret_ref_fingerprints"] == fingerprints
    )


def test_reaction_conditions_match_supports_dotted_payload_and_partial_payload_dict() -> None:
    event = {
        "event_type": "external.comment.received",
        "source": "sample_tasks",
        "payload": json.dumps({"status": "approved", "nested": {"flag": True}}),
    }

    assert (
        reaction_runtime.reaction_conditions_match(
            event,
            {
                "event_type": "external.comment.received",
                "payload.status": "approved",
                "payload": {"nested": {"flag": True}},
            },
            to_json_obj=_to_json_obj,
        )
        is True
    )


def _scope_matches(rule: dict, event: dict) -> bool:
    return reaction_runtime.reaction_scope_matches_event(
        rule,
        event,
        split_scoped_reaction_key_fn=split_scoped_reaction_key,
        to_json_obj=_to_json_obj,
    )


def test_reaction_scope_matches_workflow_group_and_step_events() -> None:
    workflow_rule = {"reaction_key": "notify"}
    group_rule = {"reaction_key": "scope::group::batch::notify"}
    step_rule = {"reaction_key": "scope::step::review::notify"}
    step_event_in_group = {
        "event_type": "job.step_failed",
        "payload": json.dumps({"step_id": "review", "group_id": "batch"}),
    }
    root_step_event = {
        "event_type": "job.step_failed",
        "step_id": "publish",
        "payload": {"step_id": "publish", "group_id": None},
    }
    group_event = {
        "event_type": "job.group_completed",
        "group_id": "batch",
        "payload": {"group_id": "batch", "group_execution_id": "ge-1"},
    }

    assert _scope_matches(workflow_rule, step_event_in_group) is True
    assert _scope_matches(workflow_rule, root_step_event) is True
    assert _scope_matches(group_rule, step_event_in_group) is True
    assert _scope_matches(group_rule, group_event) is True
    assert _scope_matches(group_rule, root_step_event) is False
    assert _scope_matches(step_rule, step_event_in_group) is True
    assert _scope_matches(step_rule, root_step_event) is False
    assert _scope_matches(step_rule, group_event) is False


def test_reaction_scope_never_matches_a_retired_phase_scope() -> None:
    phase_rule = {"reaction_key": "scope::phase::phase-2::notify"}
    event = {"payload": {"phase_id": "phase-2", "current_phase_id": "phase-2"}}

    assert _scope_matches(phase_rule, event) is False


def test_notify_action_delivers_under_a_rule_scoped_event_id() -> None:
    delivered: list[dict] = []

    def _dispatch(event: dict, _action: dict) -> dict:
        delivered.append(event)
        return {"status": "completed", "executed": True}

    event = {"event_type": "job.approval_waiting", "event_id": "wf-event:job-1:x:w-1"}
    for key in ("first", "second"):
        reaction_runtime.apply_reaction_action(
            event,
            {"reaction_key": key, "action": {"type": "notify"}},
            to_json_obj=_to_json_obj,
            reaction_action_comment=lambda *_: "",
            reaction_action_result_executed=reaction_runtime.reaction_action_result_executed,
            resume_job_from_reaction=MagicMock(),
            cancel_job_from_reaction=MagicMock(),
            retry_interrupted_step_from_reaction=MagicMock(),
            dispatch_notification_from_reaction=_dispatch,
        )

    assert [row["event_id"] for row in delivered] == [
        "wf-event:job-1:x:w-1:rule:first",
        "wf-event:job-1:x:w-1:rule:second",
    ]
    assert event["event_id"] == "wf-event:job-1:x:w-1"


def test_reaction_is_debounced_ignores_same_event_id_and_other_types() -> None:
    now = datetime.now(UTC)
    bl = MagicMock()
    bl.list_job_reaction_events.return_value = [
        {
            "id": 11,
            "event_type": "job.failed",
            "created_at": (now - timedelta(seconds=5)).isoformat(),
        },
        {
            "id": 12,
            "event_type": "job.completed",
            "created_at": (now - timedelta(seconds=5)).isoformat(),
        },
        {
            "id": 10,
            "event_type": "job.completed",
            "created_at": (now - timedelta(seconds=5)).isoformat(),
        },
    ]

    assert (
        reaction_runtime.reaction_is_debounced(
            bl,
            {
                "job_id": 42,
                "id": 10,
                "event_type": "job.completed",
                "created_at": now.isoformat(),
            },
            {"debounce_seconds": 60},
        )
        is True
    )


def test_resolve_job_workflow_id_prefers_selected_then_requested() -> None:
    assert (
        reaction_runtime.resolve_job_workflow_id(
            {"meta": {"selected_workflow_id": "wf-1"}},
            to_json_obj=_to_json_obj,
        )
        == "wf-1"
    )
    assert (
        reaction_runtime.resolve_job_workflow_id(
            {"meta": {"requested_workflow_id": "wf-2"}},
            to_json_obj=_to_json_obj,
        )
        == "wf-2"
    )
    assert (
        reaction_runtime.resolve_job_workflow_id(
            {"meta": "not-json"},
            to_json_obj=lambda _value: {},
        )
        == ""
    )


def test_reaction_action_helpers_extract_comment_and_execution_state() -> None:
    event = {"payload": {"comment": " approved ", "text": "fallback"}}

    assert (
        reaction_runtime.reaction_action_comment(
            event,
            {},
            to_json_obj=_to_json_obj,
        )
        == "approved"
    )
    assert reaction_runtime.reaction_action_result_executed({"status": "resumed"}) is True
    assert reaction_runtime.reaction_action_result_executed({"status": "ignored"}) is False


def test_dispatch_notification_from_reaction_preserves_uuid_ids(monkeypatch) -> None:
    project_id = "00000000-0000-0000-0000-000000000123"
    job_id = "00000000-0000-0000-0000-000000000456"
    bl = MagicMock()
    bl.get_job.return_value = {"id": job_id, "project_id": project_id}
    captured: dict = {}

    def _dispatch(bl_arg, **kwargs):
        captured["bl"] = bl_arg
        captured.update(kwargs)
        return [{"delivery_id": "del-1"}]

    monkeypatch.setattr(notification_dispatch, "dispatch_project_notification_event", _dispatch)

    result = reaction_runtime.dispatch_notification_from_reaction(
        {
            "project_id": project_id,
            "job_id": job_id,
            "event_type": "job.failed",
            "payload": {"message": "needs attention"},
        },
        {"event_type": "custom.notify", "severity": "warning"},
        get_logger_only=lambda: bl,
        err_database_not_configured="db unavailable",
        to_json_obj=_to_json_obj,
        resolve_job_workflow_id=lambda _job: "wf-main",
        logger=MagicMock(),
    )

    assert result["status"] == "completed"
    assert captured["project_id"] == project_id
    assert captured["job_id"] == job_id
    bl.get_job.assert_called_once_with(job_id)


def test_dispatch_notification_from_reaction_supports_extension_channel_target(monkeypatch) -> None:
    bl = MagicMock()
    bl.get_job.return_value = {"id": "job-1", "project_id": "proj-1"}
    captured: dict = {}

    def _dispatch(bl_arg, **kwargs):
        captured["bl"] = bl_arg
        captured.update(kwargs)
        return [{"delivery_id": "del-2"}]

    monkeypatch.setattr(notification_dispatch, "dispatch_project_notification_event", _dispatch)

    result = reaction_runtime.dispatch_notification_from_reaction(
        {
            "project_id": "proj-1",
            "job_id": "job-1",
            "event_type": "job.approval_waiting",
            "payload": {"message": "review needed"},
        },
        {
            "target": "extension_channel",
            "provider_key": "demo_provider",
            "channel_id": "primary_review_queue",
            "channel_capability": "human_decision",
            "severity": "warning",
        },
        get_logger_only=lambda: bl,
        err_database_not_configured="db unavailable",
        to_json_obj=_to_json_obj,
        resolve_job_workflow_id=lambda _job: "wf-main",
        logger=MagicMock(),
    )

    assert result["status"] == "completed"
    assert captured["route_ids"] is None
    assert captured["ignore_filters"] is True
    assert captured["direct_routes"][0]["provider_key"] == "demo_provider"
    assert captured["direct_routes"][0]["route_config"]["channel_id"] == "primary_review_queue"
    assert captured["direct_routes"][0]["route_config"]["required_capability"] == "human_decision"


def test_dispatch_notification_from_reaction_maps_human_decision_channel_to_approval_event(
    monkeypatch,
) -> None:
    bl = MagicMock()
    bl.get_job.return_value = {"id": "job-1", "project_id": "proj-1"}
    captured: dict = {}

    def _dispatch(bl_arg, **kwargs):
        captured["bl"] = bl_arg
        captured.update(kwargs)
        return [{"delivery_id": "del-3"}]

    monkeypatch.setattr(notification_dispatch, "dispatch_project_notification_event", _dispatch)

    result = reaction_runtime.dispatch_notification_from_reaction(
        {
            "project_id": "proj-1",
            "job_id": "job-1",
            "event_type": "workflow.automation.notify",
            "payload": {"message": "review needed"},
        },
        {
            "target": "extension_channel",
            "provider_key": "fixture_notifier",
            "channel_id": "live-group",
            "channel_capability": "human_decision",
            "severity": "warning",
        },
        get_logger_only=lambda: bl,
        err_database_not_configured="db unavailable",
        to_json_obj=_to_json_obj,
        resolve_job_workflow_id=lambda _job: "wf-main",
        logger=MagicMock(),
    )

    assert result["status"] == "completed"
    assert captured["event_type"] == "approval.requested"
    assert captured["direct_routes"][0]["event_type"] == "approval.requested"


def test_default_external_comment_rule_uses_payload_approval_hint() -> None:
    captured_rules: list[dict] = []

    def _apply_action(event: dict, rule: dict) -> dict:
        captured_rules.append(rule)
        return {"status": "resumed"}

    reject_event = {
        "event_type": "external.comment.received",
        "job_id": 99,
        "payload": {"approved": False, "comment": "needs fixes"},
    }
    approve_event = {
        "event_type": "external.comment.received",
        "job_id": 99,
        "payload": {"approved": True, "comment": "looks good"},
    }

    reaction_runtime.evaluate_workflow_reactions(
        MagicMock(),
        reject_event,
        to_json_obj=_to_json_obj,
        reaction_scope_matches_event=lambda rule, event: True,
        reaction_conditions_match=lambda event, conditions: True,
        reaction_is_debounced=lambda bl, event, rule: False,
        apply_reaction_action=_apply_action,
        reaction_action_result_executed=lambda result: True,
        logger=MagicMock(),
    )
    reaction_runtime.evaluate_workflow_reactions(
        MagicMock(),
        approve_event,
        to_json_obj=_to_json_obj,
        reaction_scope_matches_event=lambda rule, event: True,
        reaction_conditions_match=lambda event, conditions: True,
        reaction_is_debounced=lambda bl, event, rule: False,
        apply_reaction_action=_apply_action,
        reaction_action_result_executed=lambda result: True,
        logger=MagicMock(),
    )

    assert captured_rules[0]["action"]["type"] == "reject_job"
    assert captured_rules[1]["action"]["type"] == "approve_job"


def test_default_interaction_rule_uses_payload_approval_hint() -> None:
    captured_rules: list[dict] = []

    def _apply_action(event: dict, rule: dict) -> dict:
        captured_rules.append(rule)
        return {"status": "resumed"}

    approve_event = {
        "event_type": "notification.interaction_received",
        "job_id": "99",
        "payload": {"approved": True, "action": "approve"},
    }
    reject_event = {
        "event_type": "notification.interaction_received",
        "job_id": "99",
        "payload": {"approved": False, "action": "reject"},
    }

    reaction_runtime.evaluate_workflow_reactions(
        MagicMock(),
        reject_event,
        to_json_obj=_to_json_obj,
        reaction_scope_matches_event=lambda rule, event: True,
        reaction_conditions_match=lambda event, conditions: True,
        reaction_is_debounced=lambda bl, event, rule: False,
        apply_reaction_action=_apply_action,
        reaction_action_result_executed=lambda result: True,
        logger=MagicMock(),
    )
    reaction_runtime.evaluate_workflow_reactions(
        MagicMock(),
        approve_event,
        to_json_obj=_to_json_obj,
        reaction_scope_matches_event=lambda rule, event: True,
        reaction_conditions_match=lambda event, conditions: True,
        reaction_is_debounced=lambda bl, event, rule: False,
        apply_reaction_action=_apply_action,
        reaction_action_result_executed=lambda result: True,
        logger=MagicMock(),
    )

    assert captured_rules[0]["action"]["type"] == "reject_job"
    assert captured_rules[1]["action"]["type"] == "approve_job"


def test_default_interaction_rule_prioritizes_abort_action_over_rejected_hint() -> None:
    captured_rules: list[dict] = []

    def _apply_action(event: dict, rule: dict) -> dict:
        captured_rules.append(rule)
        return {"status": "cancelled", "executed": True}

    reaction_runtime.evaluate_workflow_reactions(
        MagicMock(),
        {
            "event_type": "notification.interaction_received",
            "job_id": "99",
            "payload": {"approved": False, "action": "abort"},
        },
        to_json_obj=_to_json_obj,
        reaction_scope_matches_event=lambda rule, event: True,
        reaction_conditions_match=lambda event, conditions: True,
        reaction_is_debounced=lambda bl, event, rule: False,
        apply_reaction_action=_apply_action,
        reaction_action_result_executed=lambda result: True,
        logger=MagicMock(),
    )

    assert captured_rules[0]["action"] == {
        "type": "reject_job",
        "resume_action": "abort",
    }
