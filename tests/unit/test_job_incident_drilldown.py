from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.management.errors import ServiceForbiddenError
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext


def _service(bl: MagicMock) -> JobQueryService:
    return JobQueryService(
        get_logger=lambda: bl,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda _status, _acked: "running",
        build_runtime_payload_for_api=lambda _job, _bl: {},
        build_runtime_payload=lambda _job, _bl: {
            "last_stderr_excerpt": "tool timeout while calling connector",
            "last_structured_error": {"error_message": "connector timeout"},
        },
    )


def test_incident_drilldown_builds_root_cause_chain():
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": 42,
        "project_id": 7,
        "status": "failed",
        "error": "",
    }
    bl.get_project.return_value = {"id": 7, "account_id": "acc_default", "name": "Demo"}
    bl.list_domain_events.return_value = [
        {
            "event_id": "evt-1",
            "account_id": "acc_default",
            "project_id": 7,
            "workflow_id": "wf_demo",
            "job_id": 42,
            "status": "failed",
        }
    ]
    bl.list_event_dispatches.return_value = [
        {
            "dispatch_id": "dsp-1",
            "event_id": "evt-1",
            "account_id": "acc_default",
            "project_id": 7,
            "workflow_id": "wf_demo",
            "status": "failed",
            "reason_code": "delivery_timeout",
            "reason_detail": "tool connector timed out",
        }
    ]
    bl.list_event_delivery_attempts.return_value = [
        {
            "dispatch_id": "dsp-1",
            "attempt_no": 2,
            "status": "timeout",
            "handler": "tool.dispatch",
            "error_text": "request timeout",
        }
    ]

    payload = _service(bl).get_incident_drilldown(
        42,
        scope=ScopeContext(account_id="acc_default", actor_id="alice", principal_type="user"),
    )

    assert payload.job_id == "42"
    assert payload.root_cause["category"] == "timeout"
    assert payload.root_cause["dispatch_id"] == "dsp-1"
    assert payload.root_cause["attempt_no"] == 2
    assert payload.related_events[0]["event_id"] == "evt-1"


def test_incident_drilldown_applies_limit_to_each_database_bound_relation():
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": 42,
        "project_id": 7,
        "status": "failed",
        "error": "",
    }
    bl.get_project.return_value = {"id": 7, "account_id": "acc_default", "name": "Demo"}
    bl.list_domain_events.return_value = [
        {
            "event_id": "evt-1",
            "account_id": "acc_default",
            "project_id": 7,
            "workflow_id": "wf_demo",
        }
    ]
    bl.list_event_dispatches.return_value = [
        {
            "dispatch_id": "dsp-1",
            "event_id": "evt-1",
            "account_id": "acc_default",
            "project_id": 7,
            "workflow_id": "wf_demo",
        }
    ]
    bl.list_event_delivery_attempts.return_value = [
        {"dispatch_id": "dsp-1", "attempt_no": 1, "status": "failed"}
    ]

    payload = _service(bl).get_incident_drilldown(
        42,
        limit=7,
        scope=ScopeContext(account_id="acc_default", actor_id="alice", principal_type="user"),
    )

    assert len(payload.related_events) == 1
    bl.list_domain_events.assert_called_once_with(account_id="acc_default", job_id="42", limit=7)
    bl.list_event_dispatches.assert_called_once_with(
        event_id="evt-1", account_id="acc_default", limit=7
    )
    bl.list_event_delivery_attempts.assert_called_once_with(dispatch_id="dsp-1", limit=7)


def test_incident_drilldown_denies_cross_account_scope():
    bl = MagicMock()
    bl.get_job.return_value = {"id": 42, "project_id": 7, "status": "failed"}
    bl.get_project.return_value = {"id": 7, "account_id": "acc_alpha", "name": "Demo"}

    _raises_callable_128_1 = _service(bl).get_incident_drilldown
    _raises_input_128_2 = ScopeContext(account_id="acc_beta", actor_id="eve", principal_type="user")
    with pytest.raises(ServiceForbiddenError):
        _raises_callable_128_1(42, scope=_raises_input_128_2)
