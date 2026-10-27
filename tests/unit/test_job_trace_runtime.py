from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from core.application.jobs import job_trace_runtime


def _identity(value):
    return value


def test_resolve_job_project_name_uses_snapshot_then_meta_name() -> None:
    assert (
        job_trace_runtime.resolve_job_project_name(
            None,
            {
                "meta": {
                    "project_config_snapshot": {"project_name": "snapshot-name"},
                    "project_name": "meta-name",
                },
            },
            to_json_obj=_identity,
        )
        == "snapshot-name"
    )
    assert (
        job_trace_runtime.resolve_job_project_name(
            None,
            {"meta": {"project_name": "meta-name"}},
            to_json_obj=_identity,
        )
        == "meta-name"
    )
    assert (
        job_trace_runtime.resolve_job_project_name(
            None,
            {"meta": {}},
            to_json_obj=_identity,
        )
        == ""
    )


def test_collect_replay_step_trace_uses_step_heuristic_when_needed() -> None:
    now = datetime.now(UTC)
    bl = MagicMock()
    bl.get_events.return_value = [
        {
            "id": 11,
            "event_type": "routing_decision",
            "phase": "orchestrator",
            "detail": {"step_id": "design"},
        }
    ]
    bl.get_logs.return_value = [
        {
            "id": 21,
            "action_type": "prompt_request",
            "agent": "designer",
            "meta": {"correlation_id": "corr-2"},
            "payload": {"prompt_version": "v1"},
            "result": {},
        }
    ]
    bl.get_agent_messages.return_value = [
        {
            "id": 101,
            "timestamp": now,
            "correlation_id": "corr-1",
            "from_agent": "scout",
            "step_id": "analysis",
            "caused_by": [],
        },
        {
            "id": 102,
            "timestamp": now,
            "correlation_id": "corr-2",
            "from_agent": "designer",
            "step_id": "design",
            "caused_by": [{"correlation_id": "corr-1"}],
        },
    ]

    payload = job_trace_runtime.collect_replay_step_trace(
        bl,
        55,
        {
            "id": 102,
            "timestamp": now,
            "correlation_id": "corr-2",
            "from_agent": "designer",
            "step_id": "design",
            "caused_by": [{"correlation_id": "corr-1"}],
        },
        serialize_agent_message_row=_identity,
        serialize_event_row=_identity,
        serialize_log_row=lambda row, _job_id: row,
        to_json_obj=_identity,
    )

    assert payload["prompt_request"]["action_type"] == "prompt_request"
    assert payload["routing_decisions"][0]["detail"]["linkage_method"] == "heuristic_step"
    assert payload["related_messages"][0]["correlation_id"] == "corr-1"


def test_build_job_causal_chain_links_messages_by_correlation_id() -> None:
    now = datetime.now(UTC)
    bl = MagicMock()
    bl.get_agent_messages.return_value = [
        {
            "id": 101,
            "timestamp": now,
            "correlation_id": "corr-1",
            "from_agent": "scout",
            "skill": "analyze",
            "status": "pass",
            "caused_by": [],
        },
        {
            "id": 102,
            "timestamp": now,
            "correlation_id": "corr-2",
            "from_agent": "designer",
            "skill": "design",
            "status": "pass",
            "caused_by": [{"correlation_id": "corr-1"}],
        },
    ]
    bl.get_events.return_value = []

    payload = job_trace_runtime.build_job_causal_chain(
        bl,
        55,
        serialize_agent_message_row=_identity,
        serialize_event_row=_identity,
    )

    assert payload["job_id"] == "55"
    assert any(edge["via"] == "correlation_id" for edge in payload["edges"])
    assert "graph TD" in payload["mermaid"]
