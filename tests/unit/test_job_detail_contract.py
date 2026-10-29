from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests._step_run_state import step_run_state

import core.application.reports.job_detail_contract as job_detail_contract_module
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceConflictError, ServiceError
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext
from core.application.reports.job_detail_contract import (
    build_job_detail_contract,
    build_job_execution_steps_page,
)
from core.application.reports.job_detail_sections import (
    build_execution_steps,
    build_job_detail_sections,
)
from core.application.reports.ui_projection_helpers import _build_message_steps
from core.infrastructure.security.secret_detection import contains_detected_secret


class _Dump:
    def __init__(self, payload):
        self._payload = payload

    def dump(self):
        return self._payload


class _JobQueryService:
    def get_workflow_run(
        self,
        job_id,
        *,
        scope=None,
        approval_gate=None,
        job_errors=None,
    ):
        _ = (scope, approval_gate, job_errors)
        return _Dump(
            {
                "schema_version": "1",
                "job_id": str(job_id),
                "project_id": "proj-42",
                "workflow_id": "workflow-42",
                "status": "awaiting_approval",
                "terminal": False,
                "active_steps": [],
                "steps": [],
                "approval": None,
                "result": None,
                "error": None,
                "queued_at": "2026-05-18T06:59:00Z",
                "started_at": "2026-05-18T07:00:00Z",
                "updated_at": "2026-05-18T07:01:00Z",
                "finished_at": None,
            }
        )

    async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
        _ = scope
        return _Dump(
            {
                "id": str(job_id),
                "status": "awaiting_approval",
                "project_id": "proj-42",
                "thread_id": "thread-42",
                "task_summary": "Review the generated plan",
                "current_step_id": "approval_review",
                "current_agent": "planner",
                "failure_summary": {
                    "present": True,
                    "summary": "The run paused for human approval.",
                    "next_action": "Open the review workspace and decide whether to continue.",
                },
            }
        )

    def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
        _ = (job_id, initial_limit, scope)
        return _Dump(
            {
                "job": {
                    "id": "42",
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "status": "awaiting_approval",
                    "task_summary": "Review the generated plan",
                },
                "runtime": {
                    "current_step_id": "approval_review",
                    "active_agent": "planner",
                    "wait_reason": "Waiting for a human decision before resuming execution.",
                },
                "messages": [
                    {
                        "id": "msg-1",
                        "step_id": "approval_review",
                        "status": "waiting",
                        "summary": "The workflow paused and requested approval.",
                        "timestamp": "2026-05-18T07:00:00Z",
                    }
                ],
                "events": [],
                "decision_overrides": [
                    {
                        "id": "override-1",
                        "domain": "approval_policy",
                        "reason": "Operator forced manual review.",
                        "created_at": "2026-05-18T07:01:00Z",
                    }
                ],
                "job_errors": [
                    {
                        "id": "err-1",
                        "category": "approval_wait",
                        "error_message": "Execution paused at the approval gate.",
                        "created_at": "2026-05-18T07:00:30Z",
                    }
                ],
                "project_name": "Demo project",
                "job_failure_reason": "Awaiting approval.",
            }
        )

    def get_cost_breakdown(self, job_id, *, scope=None):
        _ = (job_id, scope)
        return _Dump(
            {
                "breakdown": [
                    {
                        "agent": "planner",
                        "model_used": "gpt-5",
                        "tokens_prompt": 120,
                        "tokens_completion": 80,
                        "calls": 1,
                        "cost_usd": 0.0123,
                    }
                ]
            }
        )

    def get_causal_chain(self, job_id, *, scope=None):
        _ = (job_id, scope)
        return _Dump(
            {
                "nodes": [
                    {
                        "title": "Approval requested",
                        "summary": "The workflow stopped and asked for human review.",
                    }
                ],
                "edges": [],
            }
        )

    def get_incident_drilldown(self, job_id, *, scope=None):
        _ = (job_id, scope)
        return _Dump(
            {
                "job_id": str(job_id),
                "root_cause": {
                    "category": "approval_wait",
                    "message": "A human approval gate paused the run.",
                    "source": "approval_system",
                    "reason_code": "awaiting_approval",
                    "attempt_no": 1,
                },
            }
        )

    def get_runtime_journal(self, job_id, *, limit=500, scope=None):
        _ = (job_id, limit, scope)
        return _Dump(
            {
                "job_id": str(job_id),
                "runtime_journal": [
                    {
                        "id": "rt-1",
                        "event": "approval_wait",
                        "step_id": "approval_review",
                        "status": "waiting",
                        "message": "The run paused for human approval.",
                        "reason": "A reviewer must approve the generated plan.",
                        "timestamp": "2026-05-18T07:00:10Z",
                    }
                ],
            }
        )


class _ApprovalQueryService:
    def get_current_gate(self, job_id, *, scope=None):
        _ = (job_id, scope)
        return _Dump(
            {
                "thread_id": "thread-42",
                "payload": {
                    "stage": "Review",
                    "message": "Please approve the generated implementation plan.",
                },
            }
        )

    def list_history(self, job_id, *, limit=100, scope=None):
        _ = (job_id, limit, scope)
        return _Dump(
            {
                "approvals": [
                    {
                        "id": "approval-1",
                        "approved": True,
                        "comment": "Looks good.",
                        "created_at": "2026-05-18T06:55:00Z",
                    }
                ]
            }
        )


def test_build_job_detail_contract_returns_null_without_workflow_run_projection():
    class _LegacyJobQueryService(_JobQueryService):
        get_workflow_run = None

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_LegacyJobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {},
            user_role="operator",
        )
    )

    assert payload["workflow_run"] is None


def test_build_job_detail_contract_returns_null_for_invalid_workflow_run_projection():
    class _LegacyJobQueryService(_JobQueryService):
        def get_workflow_run(self, *args, **kwargs):
            _ = (args, kwargs)
            return {"status": "legacy"}

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_LegacyJobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {},
            user_role="operator",
        )
    )

    assert payload["workflow_run"] is None


def test_build_job_detail_contract_returns_canonical_lifecycle_projection():
    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_JobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {
                "needs_user_approval": True,
                "status": "awaiting_approval",
            },
            user_role="operator",
        )
    )

    assert payload["project_id"] == "proj-42"
    assert payload["workflow_run"]["schema_version"] == "1"
    assert payload["workflow_run"]["status"] == "awaiting_approval"
    assert payload["runtime_journal"][0]["event"] == "approval_wait"


def test_failed_test_run_uses_public_workflow_progress_when_job_detail_rows_are_sparse():
    class _SparseFailedTestRunService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            _ = scope
            return _Dump(
                {
                    "id": str(job_id),
                    "status": "failed",
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "task_summary": "Run release audit",
                    "selected_workflow": None,
                    "started_at": None,
                    "finished_at": None,
                    "current_step_id": None,
                    "current_agent": None,
                    "runtime": {},
                    "failure_summary": {"present": True},
                }
            )

        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            _ = (job_id, initial_limit, scope)
            return _Dump(
                {
                    "job": {
                        "id": "42",
                        "project_id": "proj-42",
                        "thread_id": "thread-42",
                        "status": "failed",
                        "task_summary": "Run release audit",
                    },
                    "runtime": {},
                    "messages": [],
                    "events": [],
                    "decision_overrides": [],
                    "job_errors": [],
                }
            )

        def get_workflow_run(self, job_id, **_kwargs):
            return _Dump(
                {
                    "schema_version": "1",
                    "job_id": str(job_id),
                    "project_id": "proj-42",
                    "workflow_id": "extension.install-a.release_audit",
                    "status": "failed",
                    "terminal": True,
                    "active_steps": [],
                    "steps": [
                        {
                            "step_id": "update_stock",
                            "step_execution_id": "5d1c2b7a-0000-4000-8000-0000000005c0",
                            "group_id": "sync_records",
                            "occurrence": 1,
                            "display_name": "Update stock",
                            "step_kind": "connector",
                            "status": "failed",
                            "started_at": "2026-09-08T10:00:00Z",
                            "finished_at": "2026-09-08T10:00:02Z",
                            "child_job_id": None,
                        }
                    ],
                    "approval": None,
                    "result": None,
                    "error": {
                        "code": "unresolved_required_input",
                        "message": "The required input was not available.",
                        "step_id": "update_stock",
                        "step_execution_id": "5d1c2b7a-0000-4000-8000-0000000005c0",
                        "group_id": "sync_records",
                        "occurrence": 1,
                        "category": "workflow_step",
                        "retryable": False,
                        "details": {},
                    },
                    "queued_at": "2026-09-08T09:59:59Z",
                    "started_at": "2026-09-08T10:00:00Z",
                    "updated_at": "2026-09-08T10:00:02Z",
                    "finished_at": "2026-09-08T10:00:02Z",
                }
            )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_SparseFailedTestRunService(),
            approval_query_service=None,
            get_thread_state_values=lambda _thread_id: {},
            user_role="operator",
        )
    )

    assert payload["selected_workflow"] == "extension.install-a.release_audit"
    assert payload["started_at"] == "2026-09-08T10:00:00Z"
    assert payload["finished_at"] == "2026-09-08T10:00:02Z"
    assert payload["current_step_id"] == "update_stock"
    assert payload["current_group_id"] == "sync_records"
    assert payload["lifecycle"]["step"] == {"code": "update_stock", "label": "Update Stock"}
    execution_steps = payload["sections"]["execution_steps"]
    assert execution_steps[0]["scope_id"] == "update_stock"
    assert execution_steps[0]["status"] == "failed"
    assert execution_steps[0]["source"] == "workflow_run"
    assert execution_steps[0]["duration_ms"] == 2000


def test_job_detail_invocation_is_bounded_and_contains_no_raw_ingress():
    class _InvocationJobQueryService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            payload = (
                await super().get_job_status(
                    job_id, scope=scope, include_workflow_result=include_workflow_result
                )
            ).dump()
            payload["invocation"] = {
                "source_keys": [f"field-{index}" for index in range(5000)],
                "size_bytes": 999999,
                "sha256": "a" * 64,
                "truncated": True,
                "mapped_inputs": {"password": "[REDACTED]"},
            }
            return _Dump(payload)

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_InvocationJobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {},
            user_role="operator",
        )
    )

    encoded = json.dumps(payload["invocation"], sort_keys=True).encode("utf-8")
    assert len(encoded) <= 16 * 1024
    assert "original ingress" not in encoded.decode("utf-8")
    assert "ingress_diagnostic" not in payload["invocation"]
    diagnostics = payload["sections"]["diagnostics"]
    assert diagnostics["tabs"]["runtime"]["count"] == 1
    assert (
        diagnostics["tabs"]["runtime"]["rows"][0]["summary"] == "The run paused for human approval."
    )
    assert diagnostics["tabs"]["runtime"]["rows"][0]["facts"][0] == {
        "label": "Step",
        "value": "approval_review",
    }
    assert diagnostics["tabs"]["decisions"]["count"] == 2
    assert diagnostics["tabs"]["decisions"]["rows"][0]["title"]
    assert diagnostics["tabs"]["decisions"]["rows"][0]["raw"]
    assert payload["lifecycle"]["status"]["code"] == "awaiting_approval"
    assert payload["lifecycle"]["step"]["code"] == "approval_review"
    assert payload["lifecycle"]["runtime"]["count"] == 1
    assert payload["lifecycle"]["errors"]["count"] == 1
    assert payload["lifecycle"]["approval"]["state"] == "awaiting_approval"
    assert payload["lifecycle"]["approval"]["label"] == "Waiting for approval"
    assert payload["lifecycle"]["root_cause"]["message"] == "A human approval gate paused the run."
    assert payload["job"]["id"] == "42"
    assert payload["job"]["status"] == "awaiting_approval"
    assert payload["job"]["current_step_id"] == "approval_review"
    assert payload["job"]["current_agent"] == "planner"
    assert payload["status"] == payload["job"]["status"]
    assert payload["current_step_id"] == payload["job"]["current_step_id"]
    assert payload["current_agent"] == payload["job"]["current_agent"]


def test_job_detail_invocation_projects_owned_input_artifact_download() -> None:
    class _InvocationArtifactJobQueryService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            payload = (
                await super().get_job_status(
                    job_id, scope=scope, include_workflow_result=include_workflow_result
                )
            ).dump()
            payload["invocation"] = {
                "mapped_inputs": {"supplier": "demo"},
                "files": [
                    {
                        "artifact_id": "art-input",
                        "filename": "original-name.csv",
                        "content_type": "text/csv",
                        "size_bytes": 42,
                    }
                ],
            }
            return _Dump(payload)

    artifact = {
        "artifact_id": "art-input",
        "owner_scope": {"project_id": "proj-42", "job_id": "42"},
        "filename": "supplier-stock.csv",
        "content_type": "text/csv",
        "size_bytes": 4096,
        "persistence_status": "available",
        "object_key": "artifacts/art-input/supplier-stock.csv",
        "sha256": "a" * 64,
    }

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_InvocationArtifactJobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {},
            user_role="operator",
            resolve_artifact=lambda artifact_id: artifact if artifact_id == "art-input" else None,
        )
    )

    assert payload["invocation"]["files"] == [
        {
            "artifact_id": "art-input",
            "role": "input",
            "filename": "supplier-stock.csv",
            "content_type": "text/csv",
            "size_bytes": 4096,
            "available": True,
            "download_url": "/api/jobs/42/artifacts/art-input",
        }
    ]


def test_build_job_detail_contract_avoids_misleading_success_next_action():
    class _CompletedJobQueryService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            _ = scope
            return _Dump(
                {
                    "id": str(job_id),
                    "status": "completed",
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "task_summary": "Ship the successful run",
                    "current_step_id": "complete",
                    "current_agent": "planner",
                    "failure_summary": {
                        "next_action": {
                            "summary": "Review the diagnostics below, then decide whether to retry or escalate.",
                            "steps": [],
                        }
                    },
                }
            )

        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            _ = (job_id, initial_limit, scope)
            return _Dump(
                {
                    "job": {
                        "id": "42",
                        "project_id": "proj-42",
                        "thread_id": "thread-42",
                        "status": "completed",
                        "task_summary": "Ship the successful run",
                    },
                    "runtime": {
                        "current_step_id": "complete",
                        "active_agent": "planner",
                    },
                    "messages": [],
                    "events": [],
                    "decision_overrides": [],
                    "job_errors": [],
                    "project_name": "Demo project",
                    "job_failure_reason": "",
                }
            )

        def get_incident_drilldown(self, job_id, *, scope=None):
            _ = (job_id, scope)
            return _Dump(
                {
                    "job_id": str(job_id),
                    "root_cause": {
                        "category": "unknown",
                        "message": "No explicit root cause recorded.",
                        "source": "runtime",
                        "reason_code": "",
                        "attempt_no": 0,
                    },
                }
            )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_CompletedJobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {"status": "completed"},
            user_role="operator",
        )
    )

    diagnostics = payload["sections"]["diagnostics"]
    assert diagnostics["root_cause"]["message"] == "No explicit root cause recorded."
    assert diagnostics["root_cause"]["next_action"] == ""
    assert "worktrees" not in diagnostics.get("tabs", {})


def test_build_job_detail_contract_lifecycle_does_not_depend_on_ui_projection_copy(monkeypatch):
    original_project_job_detail = job_detail_contract_module.project_job_detail

    def _patched_project_job_detail(*args, **kwargs):
        projection = original_project_job_detail(*args, **kwargs)
        projection["hero_summary"] = {
            "status_label": "UI drifted label",
            "status_tone": "purple",
            "facts": [{"label": "Current step", "value": "Presentation copy only"}],
        }
        return projection

    monkeypatch.setattr(
        job_detail_contract_module, "project_job_detail", _patched_project_job_detail
    )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_JobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {
                "needs_user_approval": True,
                "status": "awaiting_approval",
            },
            user_role="operator",
        )
    )

    assert payload["hero_summary"]["status_label"] == "UI drifted label"
    assert payload["projection"]["hero_summary"]["status_label"] == "UI drifted label"
    assert payload["lifecycle"]["status"]["label"] == "Waiting for approval"
    assert payload["lifecycle"]["status"]["tone"] == "amber"
    assert payload["lifecycle"]["step"]["code"] == "approval_review"
    assert payload["lifecycle"]["step"]["label"] == "Approval Review"
    assert payload["projection"]["hero_summary"]["facts"][0] == {
        "label": "Current step",
        "value": "Approval Review",
    }


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ({"code": "query", "label": "Query"}, "Query"),
        ({"code": "complete", "label": "Complete"}, "Complete"),
        ({"code": "approval_review", "label": "Approval Review"}, "Approval Review"),
        ({"code": "", "label": ""}, "Not recorded"),
    ],
)
def test_job_detail_projection_uses_one_step_value_for_all_lifecycle_states(step, expected):
    projection = {
        "hero_summary": {"facts": [{"label": "Current step", "value": "stale-or-missing"}]}
    }

    normalized = job_detail_contract_module._projection_with_canonical_step(
        projection,
        {"step": step},
    )

    assert normalized["hero_summary"]["facts"][0]["value"] == expected


def test_terminal_job_detail_uses_persisted_step_when_status_and_runtime_are_empty():
    class _PersistedStepJobQueryService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            _ = scope
            return _Dump(
                {
                    "id": str(job_id),
                    "status": "completed",
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "task_summary": "Publish the final brief",
                    "current_step_id": "",
                }
            )

        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            _ = (job_id, initial_limit, scope)
            return _Dump(
                {
                    "job": {
                        "id": "42",
                        "project_id": "proj-42",
                        "thread_id": "thread-42",
                        "status": "completed",
                        "task_summary": "Publish the final brief",
                        "current_step_id": "publish_brief",
                    },
                    "runtime": {"current_step_id": ""},
                    "messages": [],
                    "events": [],
                    "decision_overrides": [],
                    "job_errors": [],
                    "project_name": "Demo project",
                    "job_failure_reason": "",
                }
            )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_PersistedStepJobQueryService(),
            approval_query_service=None,
            get_thread_state_values=None,
            user_role="operator",
        )
    )

    assert payload["current_step_id"] == "publish_brief"
    assert payload["job"]["current_step_id"] == "publish_brief"
    assert payload["runtime"]["current_step_id"] == "publish_brief"
    assert payload["lifecycle"]["step"] == {
        "code": "publish_brief",
        "label": "Publish Brief",
    }
    assert payload["step_label"] == "Publish Brief"
    assert payload["hero_summary"]["facts"][0] == {
        "id": "current_step",
        "label": "Current step",
        "label_key": "react.jobs.current_step",
        "value": "Publish Brief",
    }


def test_build_job_detail_contract_builds_machine_lifecycle_before_projection_rendering(
    monkeypatch,
):
    original_project_job_detail = job_detail_contract_module.project_job_detail
    call_count = 0
    passed_lifecycles: list[dict] = []

    def _patched_project_job_detail(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        passed_lifecycle = kwargs.get("lifecycle")
        if isinstance(passed_lifecycle, dict):
            passed_lifecycles.append(dict(passed_lifecycle))
        projection = original_project_job_detail(*args, **kwargs)
        projection["execution_steps"] = [{"id": "ui-only-step", "title": "UI drifted step"}]
        projection["cost_breakdown"] = {"total_cost_display": "$99.00", "total_cost_usd": 99.0}
        projection["status"] = "projection_override"
        projection["current_step_id"] = "projection_step"
        projection["unknown_projection_key"] = "should-not-leak"
        projection["diagnostics"] = {
            "tabs": {
                "runtime": {"count": 99, "rows": []},
                "errors": {"count": 88, "rows": []},
            },
            "root_cause": {
                "category": "UI drift",
                "message": "Projection-only root cause",
                "next_action": "Projection-only next action",
            },
        }
        return projection

    monkeypatch.setattr(
        job_detail_contract_module, "project_job_detail", _patched_project_job_detail
    )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_JobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {
                "needs_user_approval": True,
                "status": "awaiting_approval",
            },
            user_role="operator",
        )
    )

    assert call_count == 1
    assert passed_lifecycles
    assert passed_lifecycles[0]["runtime"]["count"] == 1
    assert passed_lifecycles[0]["errors"]["count"] == 1
    assert passed_lifecycles[0]["root_cause"]["message"] == "A human approval gate paused the run."
    assert "steps" not in payload["lifecycle"]
    assert payload["lifecycle"]["costs"]["total_cost_usd"] == 0.0123
    assert payload["lifecycle"]["runtime"]["count"] == 1
    assert payload["lifecycle"]["errors"]["count"] == 1
    assert payload["sections"]["execution_steps"][0]["id"] != "ui-only-step"
    assert payload["sections"]["cost_breakdown"]["total_cost_usd"] == 0.0123
    assert (
        payload["sections"]["diagnostics"]["root_cause"]["message"]
        == "A human approval gate paused the run."
    )
    assert payload["status"] == "awaiting_approval"
    assert payload["current_step_id"] == "approval_review"
    assert payload["job"]["status"] == "awaiting_approval"
    assert payload["job"]["current_step_id"] == "approval_review"
    assert payload["job"]["current_agent"] == "planner"
    assert payload["status_label"] == "Waiting for approval"
    assert payload["status_tone"] == "amber"
    assert payload["step_label"] == "Approval Review"
    assert payload["approval_state"] == "awaiting_approval"
    assert payload["attention_group"] == "needs_attention"
    assert "unknown_projection_key" not in payload
    assert "diagnostics" not in payload["projection"]
    assert "execution_steps" not in payload["projection"]
    assert "cost_breakdown" not in payload["projection"]
    assert payload["lifecycle"]["root_cause"]["next_action"] == (
        "Open the review workspace and decide whether to continue."
    )


def test_build_job_detail_contract_reuses_shared_sections_for_lifecycle_and_projection(
    monkeypatch,
):
    original_build_sections = job_detail_contract_module.build_job_detail_sections
    original_project_job_detail = job_detail_contract_module.project_job_detail
    section_calls = 0
    captured_projection_sections: list[dict] = []

    def _patched_build_sections(*args, **kwargs):
        nonlocal section_calls
        section_calls += 1
        sections = original_build_sections(*args, **kwargs)
        sections["execution_steps"] = [
            {"id": "shared-section-step", "title": "Shared section step", "status": "completed"}
        ]
        sections["selected_step"] = {"id": "shared-section-step", "title": "Shared section step"}
        return sections

    def _patched_project_job_detail(*args, **kwargs):
        sections = kwargs.get("sections")
        if isinstance(sections, dict):
            captured_projection_sections.append(dict(sections))
        return original_project_job_detail(*args, **kwargs)

    monkeypatch.setattr(
        job_detail_contract_module, "build_job_detail_sections", _patched_build_sections
    )
    monkeypatch.setattr(
        job_detail_contract_module, "project_job_detail", _patched_project_job_detail
    )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_JobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {
                "needs_user_approval": True,
                "status": "awaiting_approval",
            },
            user_role="operator",
        )
    )

    assert section_calls == 1
    assert captured_projection_sections
    assert captured_projection_sections[0]["execution_steps"][0]["id"] == "shared-section-step"
    assert payload["sections"]["execution_steps"][0]["id"] == "shared-section-step"
    assert payload["sections"]["selected_step"]["id"] == "shared-section-step"
    assert "execution_steps" not in payload
    assert "selected_step" not in payload
    assert "steps" not in payload["lifecycle"]


def test_project_job_detail_can_return_projection_only():
    projection = job_detail_contract_module.project_job_detail(
        job={
            "id": "42",
            "status": "completed",
            "project_id": "proj-42",
            "task_summary": "Ship the successful run",
            "started_at": "2026-05-18T07:00:00Z",
            "duration_ms": 120000,
        },
        runtime={"current_step_id": "complete"},
        sections={
            "execution_steps": [{"id": "step-1", "title": "Step one"}],
            "selected_step": {"id": "step-1", "title": "Step one"},
            "review_workspace": {"kind": "document_review"},
            "previous_decisions": [{"id": "approval-1"}],
            "cost_breakdown": {"total_cost_display": "$1.00"},
            "diagnostics": {"tabs": {"runtime": {"count": 1, "rows": []}}},
        },
        project_name="Demo project",
        user_role="operator",
        include_sections=False,
    )

    assert set(projection) == {
        "header",
        "hero_summary",
        "next_action",
        "job_actions",
        "user_role",
    }


def test_build_job_detail_contract_surfaces_section_warnings_for_soft_failures():
    class _FailingApprovalQueryService:
        def get_current_gate(self, job_id, *, scope=None):
            _ = (job_id, scope)
            raise RuntimeError("gate lookup failed")

        def list_history(self, job_id, *, limit=100, scope=None):
            _ = (job_id, limit, scope)
            raise RuntimeError("history lookup failed")

    def _raise_thread_state(_thread_id):
        raise RuntimeError("thread checkpoint unavailable")

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_JobQueryService(),
            approval_query_service=_FailingApprovalQueryService(),
            get_thread_state_values=_raise_thread_state,
            user_role="operator",
        )
    )

    warnings = payload["section_warnings"]
    assert {warning["section"] for warning in warnings} == {
        "approval_gate",
        "approval_history",
        "thread_state",
    }
    assert {warning["code"] for warning in warnings} == {
        "approval_gate_unavailable",
        "approval_history_unavailable",
        "thread_state_unavailable",
    }
    assert payload["approval_gate"] == {}
    assert payload["approval_history"] == []
    assert payload["thread_state"] == {}


def test_build_job_detail_contract_approval_state_uses_canonical_vocabulary():
    class _CompletedJobWithGateQueryService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            _ = scope
            return _Dump(
                {
                    "id": str(job_id),
                    "status": "completed",
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "task_summary": "Completed after review",
                    "current_step_id": "complete",
                }
            )

        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            _ = (job_id, initial_limit, scope)
            return _Dump(
                {
                    "job": {
                        "id": "42",
                        "project_id": "proj-42",
                        "thread_id": "thread-42",
                        "status": "completed",
                        "task_summary": "Completed after review",
                    },
                    "runtime": {
                        "current_step_id": "complete",
                    },
                    "messages": [],
                    "events": [],
                    "decision_overrides": [],
                    "job_errors": [],
                    "project_name": "Demo project",
                    "job_failure_reason": "",
                }
            )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_CompletedJobWithGateQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=None,
            user_role="operator",
        )
    )

    assert payload["sections"]["review_workspace"]["kind"] == "document_review"
    assert payload["lifecycle"]["approval"]["state"] == "review_available"
    assert payload["lifecycle"]["approval"]["label"] == "Review available"


@pytest.mark.parametrize(
    ("job_status", "conflict_code", "must_raise"),
    [
        ("failed", "approval_not_awaiting", False),
        ("resume_pending", "approval_not_awaiting", False),
        ("resume_pending", "multiple_approval_gates_unsupported", True),
        ("awaiting_approval", "approval_not_awaiting", True),
    ],
)
def test_job_detail_only_ignores_expected_absent_gate_after_approval(
    job_status, conflict_code, must_raise
):
    class _JobAfterApprovalQueryService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            _ = scope
            return _Dump(
                {
                    "id": str(job_id),
                    "status": job_status,
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "task_summary": "Failed after approval resume",
                }
            )

        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            _ = (job_id, initial_limit, scope)
            return _Dump(
                {
                    "job": {
                        "id": "42",
                        "project_id": "proj-42",
                        "thread_id": "thread-42",
                        "status": job_status,
                        "task_summary": "Failed after approval resume",
                    },
                    "runtime": {},
                    "messages": [],
                    "events": [],
                    "decision_overrides": [],
                    "job_errors": [],
                    "project_name": "Demo project",
                    "job_failure_reason": "resume failed",
                }
            )

    class _StaleGateApprovalQueryService:
        def get_current_gate(self, job_id, *, scope=None):
            _ = (job_id, scope)
            raise ServiceConflictError(
                "Job is not awaiting approval",
                payload={"code": conflict_code},
            )

        def list_history(self, job_id, *, limit=100, scope=None):
            _ = (job_id, limit, scope)
            return _Dump({"approvals": []})

    def detail():
        return build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_JobAfterApprovalQueryService(),
            approval_query_service=_StaleGateApprovalQueryService(),
            get_thread_state_values=None,
            user_role="operator",
        )

    if must_raise:
        coroutine = detail()
        with pytest.raises(ServiceConflictError) as error:
            asyncio.run(coroutine)
        assert error.value.payload["code"] == conflict_code
        return
    payload = asyncio.run(detail())

    assert payload["status"] == job_status
    assert payload["approval_gate"] == {}
    assert not any(
        warning.get("section") == "approval_gate" for warning in payload["section_warnings"]
    )


def test_build_job_detail_contract_skips_heavy_diagnostics_while_job_is_running():
    class _RunningJobQueryService(_JobQueryService):
        def __init__(self):
            self.heavy_calls: list[str] = []

        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            _ = scope
            return _Dump(
                {
                    "id": str(job_id),
                    "status": "in_progress",
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "task_summary": "Implement the requested change",
                    "current_step_id": "implementation",
                    "current_agent": "composer",
                    "failure_summary": {
                        "present": False,
                        "summary": "",
                        "next_action": "",
                    },
                }
            )

        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            _ = (job_id, initial_limit, scope)
            return _Dump(
                {
                    "job": {
                        "id": "42",
                        "project_id": "proj-42",
                        "thread_id": "thread-42",
                        "status": "in_progress",
                        "task_summary": "Implement the requested change",
                    },
                    "runtime": {
                        "current_step_id": "implementation",
                        "active_agent": "composer",
                    },
                    "messages": [],
                    "events": [],
                    "decision_overrides": [],
                    "job_errors": [],
                    "project_name": "Demo project",
                    "job_failure_reason": "",
                }
            )

        def get_causal_chain(self, job_id, *, scope=None):
            _ = (job_id, scope)
            self.heavy_calls.append("get_causal_chain")
            return super().get_causal_chain(job_id, scope=scope)

        def get_incident_drilldown(self, job_id, *, scope=None):
            _ = (job_id, scope)
            self.heavy_calls.append("get_incident_drilldown")
            return super().get_incident_drilldown(job_id, scope=scope)

    class _RecordingApprovalQueryService:
        def __init__(self):
            self.calls: list[str] = []

        def get_current_gate(self, job_id, *, scope=None):
            _ = (job_id, scope)
            self.calls.append("get_current_gate")
            return _Dump({})

        def list_history(self, job_id, *, limit=100, scope=None):
            _ = (job_id, limit, scope)
            self.calls.append("list_history")
            return _Dump({"approvals": []})

    job_query_service = _RunningJobQueryService()
    approval_query_service = _RecordingApprovalQueryService()

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=job_query_service,
            approval_query_service=approval_query_service,
            get_thread_state_values=None,
            user_role="operator",
        )
    )

    assert payload["status"] == "in_progress"
    assert payload["current_step_id"] == "implementation"
    assert payload["approval_gate"] == {}
    assert payload["approval_history"] == []
    assert payload["incident"] == {}
    assert "worktrees" not in payload
    assert "parallel_merges" not in payload
    assert payload["section_warnings"] == []
    assert approval_query_service.calls == []
    assert job_query_service.heavy_calls == []


def test_build_job_detail_contract_bounds_large_connector_results_without_mutating_truth():
    large_result = {
        "items": [
            {
                "sku": f"sku-{index}",
                "description": f"payload-marker-{index}-" + ("x" * 4096),
            }
            for index in range(128)
        ]
    }
    message = {
        "id": "msg-large-connector-result",
        "step_id": "catalog_sync",
        "from_agent": "connector_step",
        "skill": "catalog_list",
        "status": "pass",
        "summary": "Connector returned 128 catalog rows.",
        "timestamp": "2026-09-09T00:00:00Z",
        "artifacts": {"connector_result": large_result},
        "errors": [],
    }
    causal_chain = {
        "nodes": [
            {
                "node_id": "message:large",
                "title": "Catalog sync completed",
                "message": message,
            }
        ],
        "edges": [],
    }

    class _LargeResultJobQueryService(_JobQueryService):
        def __init__(self):
            self.causal_chain_calls = 0

        async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
            _ = scope
            return _Dump(
                {
                    "id": str(job_id),
                    "status": "completed",
                    "project_id": "proj-42",
                    "thread_id": "thread-42",
                    "task_summary": "Synchronize the complete product catalog",
                    "current_step_id": "complete",
                    "current_agent": "connector_step",
                }
            )

        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            _ = (job_id, initial_limit, scope)
            return _Dump(
                {
                    "job": {
                        "id": "42",
                        "project_id": "proj-42",
                        "thread_id": "thread-42",
                        "status": "completed",
                        "task_summary": "Synchronize the complete product catalog",
                    },
                    "runtime": {"current_step_id": "complete", "active_agent": "connector_step"},
                    "messages": [message],
                    "events": [],
                    "decision_overrides": [],
                    "job_errors": [],
                    "project_name": "Demo project",
                    "job_failure_reason": "",
                }
            )

        def get_causal_chain(self, job_id, *, scope=None):
            _ = (job_id, scope)
            self.causal_chain_calls += 1
            return _Dump(causal_chain)

    job_query_service = _LargeResultJobQueryService()
    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=job_query_service,
            approval_query_service=None,
            get_thread_state_values=lambda _thread_id: {
                "status": "completed",
                "agent_messages": [message],
            },
            user_role="operator",
        )
    )

    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    assert len(encoded.encode("utf-8")) < 300_000
    assert encoded.count("payload-marker-") < 32
    assert "sku-127" not in encoded
    assert "_flow_steward_projection" in encoded
    assert "full_value_persisted" not in encoded
    assert job_query_service.causal_chain_calls == 0
    assert len(large_result["items"]) == 128
    assert large_result["items"][-1]["sku"] == "sku-127"


def test_execution_step_primary_projection_omits_raw_provider_payload() -> None:
    source = {
        "id": "message-1",
        "step_id": "catalog_sync",
        "from_agent": "connector_step",
        "skill": "catalog_list",
        "status": "pass",
        "summary": "Catalog synchronized.",
        "artifacts": {"connector_result": {"items": [{"sku": "sku-1"}] * 8}},
        "errors": [],
    }

    steps = _build_message_steps([source])

    assert "raw" not in steps[0]
    assert steps[0]["summary"] == "Catalog synchronized."


def test_execution_step_projects_debuggable_inputs_outputs_timing_and_error() -> None:
    source = {
        "id": "message-2",
        "step_id": "catalog_sync",
        "from_agent": "connector_step",
        "skill": "acme_catalog",
        "status": "fail",
        "summary": "Catalog sync failed.",
        "timestamp": "2026-09-10T09:00:08Z",
        "artifacts": {
            "input_payload": {
                "sku": "VINYL-42",
                "credential": {
                    "__type__": "flow-steward.secret-ref.v1",
                    "ciphertext": "must-never-leak",
                },
            },
            "connector_invocation": {
                "response": {"result": {"imported": 0}},
            },
        },
        "errors": ["Acme Store returned HTTP 429 rate limit."],
        "envelope": {
            "timestamps": {
                "started_at": "2026-09-10T09:00:00Z",
                "completed_at": "2026-09-10T09:00:08Z",
            }
        },
    }

    steps = _build_message_steps([source])

    step = steps[0]
    assert step["inputs"] == {"sku": "VINYL-42", "credential": "[REDACTED]"}
    assert step["outputs"] == {"imported": 0}
    assert step["started_at"] == "2026-09-10T09:00:00Z"
    assert step["finished_at"] == "2026-09-10T09:00:08Z"
    assert step["duration_ms"] == 8000
    assert step["error"] == "Acme Store returned HTTP 429 rate limit."
    assert "must-never-leak" not in json.dumps(step)


def test_an_agent_steps_plain_text_answer_is_its_output_as_well_as_its_evidence() -> None:
    # A plain-text agent reply is stored under the skill's primary output key.
    # The step panel showed it as "Step output" evidence and, from the same
    # step, "No step outputs were recorded."
    source = {
        "id": "message-agent",
        "step_id": "ask",
        "from_agent": "acme_answerer",
        "skill": "acme_answer",
        "status": "pass",
        "summary": "Answered.",
        "artifacts": {
            "output": "Jupiter",
            "raw_output": "Jupiter",
            "_contract_errors": [],
            "source_artifact": {"artifact_id": "a-1"},
        },
        "errors": [],
    }

    step = _build_message_steps([source])[0]

    assert step["outputs"] == {"output": "Jupiter"}
    assert any(card["summary"] == "Jupiter" for card in step["evidence"])


def test_a_declared_state_patch_still_wins_over_agent_artifacts() -> None:
    source = {
        "id": "message-agent-patch",
        "step_id": "ask",
        "from_agent": "acme_answerer",
        "skill": "acme_answer",
        "status": "pass",
        "summary": "Answered.",
        "artifacts": {"answer": "Jupiter", "_state_patch": {"answer": "Jupiter", "n": 1}},
        "errors": [],
    }

    step = _build_message_steps([source])[0]

    assert step["outputs"] == {"answer": "Jupiter", "n": 1}


def test_connector_step_projects_operation_honest_timing_and_artifact_roles() -> None:
    source = {
        "id": "message-artifacts",
        "from_agent": "connector_step",
        "skill": "connector",
        "status": "pass",
        "summary": "Document converted.",
        "timestamp": "2026-09-10T09:00:00Z",
        "artifacts": {
            "step_id": "extract_invoice",
            "action_id": "convert_document",
            "extension_id": "acme.document-parser",
            "connector_id": "extension.acme.document-parser",
            "input_payload": {
                "connection_ref": "conn-secret-internal",
                "artifact_handle": "artifact:art-input",
                "do_ocr": False,
            },
            "connector_invocation": {
                "operation_id": "convert_document",
                "response": {
                    "result": {
                        "processing_time": 1.538,
                        "next_cursor": "opaque-provider-cursor",
                        "markdown_artifact_handle": "artifact:art-output",
                    }
                },
            },
        },
        "errors": [],
        "envelope": {
            "timestamps": {
                "started_at": "2026-09-10T09:00:00Z",
                "completed_at": "2026-09-10T09:00:00Z",
            }
        },
    }
    records = {
        "art-input": {
            "artifact_id": "art-input",
            "owner_scope": {"project_id": "proj-42", "job_id": "job-42"},
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2048,
            "persistence_status": "available",
            "object_key": "projects/proj-42/jobs/job-42/artifacts/art-input/invoice.pdf",
            "sha256": "a" * 64,
        },
        "art-output": {
            "artifact_id": "art-output",
            "owner_scope": {"project_id": "proj-42", "job_id": "job-42"},
            "filename": "invoice.md",
            "content_type": "text/markdown",
            "size_bytes": 4096,
            "persistence_status": "available",
            "object_key": "projects/proj-42/jobs/job-42/artifacts/art-output/invoice.md",
            "sha256": "b" * 64,
        },
    }

    steps = _build_message_steps(
        [source],
        project_id="proj-42",
        job_id="job-42",
        resolve_artifact=records.get,
    )

    step = steps[0]
    assert step["operation_id"] == "convert_document"
    assert step["operation_label"] == "Convert Document"
    assert step["duration_ms"] is None
    assert step["provider_processing_time"] == 1.538
    assert step["provider_processing_time_unit"] is None
    assert step["inputs"] == {"do_ocr": False}
    assert step["outputs"] == {"processing_time": 1.538}
    assert step["input_artifacts"] == [
        {
            "artifact_id": "art-input",
            "role": "input",
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2048,
            "available": True,
            "download_url": "/api/jobs/job-42/artifacts/art-input",
        }
    ]
    assert step["output_artifacts"] == [
        {
            "artifact_id": "art-output",
            "role": "output",
            "filename": "invoice.md",
            "content_type": "text/markdown",
            "size_bytes": 4096,
            "available": True,
            "download_url": "/api/jobs/job-42/artifacts/art-output",
        }
    ]
    assert "conn-secret-internal" not in json.dumps(step)
    assert "opaque-provider-cursor" not in json.dumps(step)


def test_mcp_connector_step_projects_the_concrete_operation_name() -> None:
    source = {
        "id": "message-mcp-operation",
        "from_agent": "connector_step",
        "skill": "connector",
        "status": "pass",
        "summary": (
            "Connector step 'mcp.mcp_7d5037bc0dee4ec9.update_spreadsheet' "
            "executed via tool_manifest"
        ),
        "timestamp": "2026-09-10T09:00:00Z",
        "artifacts": {
            "step_id": "add_stock_tabs_via_mcp",
            "tool_id": "mcp.mcp_7d5037bc0dee4ec9.update_spreadsheet",
            "function_name": "mcp.mcp_7d5037bc0dee4ec9.update_spreadsheet",
            "connector_id": "connector.mcp.mcp_7d5037bc0dee4ec9.update_spreadsheet",
            "input_payload": {"spreadsheetId": "sheet-42"},
        },
        "errors": [],
        "envelope": {
            "timestamps": {
                "started_at": "2026-09-10T09:00:00Z",
                "completed_at": "2026-09-10T09:00:03Z",
            }
        },
    }

    step = _build_message_steps([source])[0]

    assert step["operation_id"] == "mcp.mcp_7d5037bc0dee4ec9.update_spreadsheet"
    assert step["operation_label"] == "Update Spreadsheet"


def test_execution_steps_merge_durable_timing_into_rich_message_step() -> None:
    message = {
        "id": "message-compose-plan",
        "step_id": "compose_plan",
        "from_agent": "connector_step",
        "skill": "connector",
        "status": "pass",
        "summary": "Travel plan composed.",
        "timestamp": "2026-09-10T08:09:02.250000Z",
        "artifacts": {
            "step_id": "compose_plan",
            "step_display_name": "Compose travel plan",
            "input_payload": {"topic": "Vilnius"},
            "connector_invocation": {
                "operation_id": "invoke_tool",
                "response": {"result": {"travel_plan_brief": "Ready"}},
            },
        },
        "errors": [],
        "envelope": {
            "timestamps": {
                "started_at": "2026-09-10T08:09:02.250000Z",
                "completed_at": "2026-09-10T08:09:02.250000Z",
            }
        },
    }
    workflow_run = {
        "steps": [
            {
                "step_id": "compose_plan",
                "occurrence": 1,
                "display_name": "Compose travel plan",
                "status": "completed",
                "started_at": "2026-09-10T08:09:01Z",
                "finished_at": "2026-09-10T08:09:02.250000Z",
            }
        ]
    }

    steps, _selected = build_execution_steps(
        job={"id": "job-42", "project_id": "proj-42", "status": "completed"},
        messages=[message],
        workflow_run=workflow_run,
    )

    assert len(steps) == 1
    assert steps[0]["source"] == "message"
    assert steps[0]["scope_id"] == "compose_plan"
    assert steps[0]["inputs"] == {"topic": "Vilnius"}
    assert steps[0]["outputs"] == {"travel_plan_brief": "Ready"}
    assert steps[0]["started_at"] == "2026-09-10T08:09:01Z"
    assert steps[0]["finished_at"] == "2026-09-10T08:09:02.250000Z"
    assert steps[0]["duration_ms"] == 1250


def test_message_step_uses_canonical_top_level_timestamps_without_durable_row() -> None:
    message = {
        "id": "message-invoke-child",
        "from_agent": "invoke_workflow",
        "skill": "invoke_workflow",
        "status": "pass",
        "summary": "Child workflow completed.",
        "artifacts": {
            "step_id": "invoke-acme-store",
            "input_payload": {"supplier": "vinyl-feed", "batch": 4},
            "_invoke_workflow": {"child_job_id": "child-42"},
        },
        "timestamps": {
            "started_at": "2026-09-10T08:09:01Z",
            "completed_at": "2026-09-10T08:09:04.250000Z",
            "published_at": "2026-09-10T08:09:04.250000Z",
        },
        "errors": [],
    }

    steps, _selected = build_execution_steps(
        job={"id": "job-42", "project_id": "proj-42", "status": "completed"},
        messages=[message],
    )

    assert steps[0]["inputs"] == {"supplier": "vinyl-feed", "batch": 4}
    assert steps[0]["started_at"] == "2026-09-10T08:09:01Z"
    assert steps[0]["finished_at"] == "2026-09-10T08:09:04.250000Z"
    assert steps[0]["duration_ms"] == 3250


def test_execution_steps_merge_each_repeated_step_with_its_durable_occurrence() -> None:
    messages = [
        {
            "id": "message-push-batch-1",
            "group_id": "push_batches",
            "from_agent": "connector_step",
            "skill": "connector",
            "status": "pass",
            "summary": "First batch completed.",
            "timestamp": "2026-09-10T08:09:02Z",
            "artifacts": {"step_id": "push_batch", "input_payload": {"batch": 1}},
            "errors": [],
        },
        {
            "id": "message-push-batch-2",
            "group_id": "push_batches",
            "from_agent": "connector_step",
            "skill": "connector",
            "status": "pass",
            "summary": "Second batch completed.",
            "timestamp": "2026-09-10T08:09:05Z",
            "artifacts": {"step_id": "push_batch", "input_payload": {"batch": 2}},
            "errors": [],
        },
    ]
    workflow_run = {
        "steps": [
            {
                "step_id": "push_batch",
                "group_id": "push_batches",
                "occurrence": 1,
                "status": "completed",
                "started_at": "2026-09-10T08:09:01Z",
                "finished_at": "2026-09-10T08:09:02Z",
            },
            {
                "step_id": "push_batch",
                "group_id": "push_batches",
                "occurrence": 2,
                "status": "completed",
                "started_at": "2026-09-10T08:09:03Z",
                "finished_at": "2026-09-10T08:09:05Z",
            },
        ]
    }

    steps, _selected = build_execution_steps(
        job={"id": "job-42", "project_id": "proj-42", "status": "completed"},
        messages=messages,
        workflow_run=workflow_run,
    )

    assert len(steps) == 2
    assert [step["source"] for step in steps] == ["message", "message"]
    assert [step["scope_id"] for step in steps] == ["push_batch", "push_batch:2"]
    assert [step["started_at"] for step in steps] == [
        "2026-09-10T08:09:01Z",
        "2026-09-10T08:09:03Z",
    ]
    assert [step["finished_at"] for step in steps] == [
        "2026-09-10T08:09:02Z",
        "2026-09-10T08:09:05Z",
    ]
    assert [step["duration_ms"] for step in steps] == [1000, 2000]


def test_execution_steps_keep_recent_rich_messages_at_the_end_of_long_loop_history() -> None:
    workflow_steps = [
        {
            "step_id": "push_batch",
            "group_id": "push_batches",
            "occurrence": occurrence,
            "status": "completed",
            "started_at": f"2026-09-10T08:{occurrence // 60:02d}:{occurrence % 60:02d}Z",
            "finished_at": f"2026-09-10T08:{occurrence // 60:02d}:{occurrence % 60:02d}.500000Z",
        }
        for occurrence in range(1, 101)
    ]
    messages = [
        {
            "id": f"message-push-batch-{occurrence}",
            "group_id": "push_batches",
            "from_agent": "connector_step",
            "skill": "connector",
            "status": "pass",
            "summary": f"Batch {occurrence} completed.",
            "timestamp": f"2026-09-10T08:{occurrence // 60:02d}:{occurrence % 60:02d}.500000Z",
            "artifacts": {
                "step_id": "push_batch",
                "input_payload": {"batch": occurrence},
                "connector_invocation": {
                    "operation_id": "wc_batch_products",
                    "response": {"result": {"updated": 50}},
                },
            },
            "errors": [],
        }
        for occurrence in range(98, 101)
    ]

    steps, _selected = build_execution_steps(
        job={"id": "job-42", "project_id": "proj-42", "status": "completed"},
        messages=messages,
        workflow_run={"steps": workflow_steps},
    )

    assert len(steps) == 100
    assert [step["scope_id"] for step in steps[-3:]] == [
        "push_batch:98",
        "push_batch:99",
        "push_batch:100",
    ]
    assert [step["source"] for step in steps[-3:]] == ["message", "message", "message"]
    assert [step["inputs"] for step in steps[-3:]] == [
        {"batch": 98},
        {"batch": 99},
        {"batch": 100},
    ]
    assert [step["outputs"] for step in steps[-3:]] == [
        {"updated": 50},
        {"updated": 50},
        {"updated": 50},
    ]


def test_terminal_job_marks_unfinished_durable_step_as_interrupted() -> None:
    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "completed"},
        workflow_run={
            "steps": [
                {
                    "step_id": "fetch_orders",
                    "occurrence": 1,
                    "status": "in_progress",
                    "started_at": "2026-09-13T10:18:31Z",
                    "finished_at": None,
                },
                {
                    "step_id": "fetch_orders",
                    "occurrence": 2,
                    "status": "completed",
                    "started_at": "2026-09-13T10:19:16Z",
                    "finished_at": "2026-09-13T10:19:21Z",
                },
            ]
        },
    )

    assert [step["status"] for step in steps] == ["interrupted", "completed"]
    assert steps[0]["status_label"] == "Interrupted"


def test_disabled_step_the_run_skipped_is_a_skipped_row_not_a_completed_one() -> None:
    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "completed"},
        workflow_run={
            "steps": [
                {
                    "step_id": "transform",
                    "step_execution_id": "exec-transform",
                    "occurrence": 1,
                    "step_kind": "transform",
                    "status": "skipped",
                    "started_at": None,
                    "finished_at": "2026-09-13T10:18:31Z",
                },
                {
                    "step_id": "notify",
                    "occurrence": 1,
                    "status": "completed",
                    "started_at": "2026-09-13T10:18:32Z",
                    "finished_at": "2026-09-13T10:18:33Z",
                },
            ]
        },
    )

    by_step = {step["step_id"]: step for step in steps}
    assert (by_step["transform"]["status"], by_step["transform"]["status_label"]) == (
        "skipped",
        "Skipped",
    )
    assert by_step["notify"]["status"] == "completed"


@pytest.mark.parametrize(
    ("job_status", "durable", "expected"),
    [
        ("cancelled", "cancelled", ("cancelled", "Cancelled")),
        ("in_progress", "external_wait", ("in_progress", "In progress")),
    ],
)
def test_a_cancelled_or_externally_waiting_step_is_not_a_completed_row(
    job_status: str, durable: str, expected: tuple[str, str]
) -> None:
    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": job_status},
        workflow_run={
            "steps": [
                {
                    "step_id": "ship",
                    "step_execution_id": "exec-ship",
                    "occurrence": 1,
                    "step_kind": "connector",
                    "status": durable,
                    "started_at": "2026-09-13T10:18:31Z",
                    "finished_at": None,
                }
            ]
        },
    )

    expected_status, expected_label = expected
    assert steps[0]["status"] == expected_status
    assert steps[0]["status_label"] == expected_label


def test_recovered_message_matches_completed_durable_occurrence_by_time() -> None:
    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "completed"},
        messages=[
            {
                "id": "message-get-shop-recovered",
                "from_agent": "connector_step",
                "skill": "connector",
                "status": "pass",
                "timestamp": "2026-09-13T11:18:18.277495Z",
                "artifacts": {"step_id": "get_shop"},
                "envelope": {
                    "timestamps": {
                        "started_at": "2026-09-13T11:18:17.536838Z",
                        "completed_at": "2026-09-13T11:18:18.273937Z",
                    }
                },
                "errors": [],
            }
        ],
        workflow_run={
            "steps": [
                {
                    "step_id": "get_shop",
                    "occurrence": 1,
                    "status": "in_progress",
                    "started_at": "2026-09-13T11:17:32.833842Z",
                    "finished_at": None,
                },
                {
                    "step_id": "get_shop",
                    "occurrence": 2,
                    "status": "completed",
                    "started_at": "2026-09-13T11:18:17.536838Z",
                    "finished_at": "2026-09-13T11:18:18.287902Z",
                },
            ]
        },
    )

    message_step = next(step for step in steps if step["source"] == "message")
    interrupted_step = next(step for step in steps if step["status"] == "interrupted")

    assert message_step["scope_id"] == "get_shop:2"
    assert message_step["started_at"] == "2026-09-13T11:18:17.536838Z"
    assert message_step["finished_at"] == "2026-09-13T11:18:18.287902Z"
    assert interrupted_step["scope_id"] == "get_shop"
    assert interrupted_step["started_at"] == "2026-09-13T11:17:32.833842Z"
    assert interrupted_step["finished_at"] == ""


def test_execution_steps_reconcile_one_message_by_step_execution() -> None:
    """A message without its step id still names the execution it belongs to."""
    message = {
        "id": "message-invoke-weather",
        "step_execution_id": "8a4f3c1e-0000-4000-8000-00000000c0de",
        "from_agent": "invoke_workflow",
        "skill": "invoke_workflow",
        "status": "pass",
        "summary": "Child workflow completed.",
        "timestamp": "2026-09-10T08:09:04Z",
        "artifacts": {
            "_invoke_workflow": {"child_job_id": "child-42"},
            "weather_summary": "Sunny",
        },
        "errors": [],
        "envelope": {
            "timestamps": {
                "started_at": "2026-09-10T08:09:04Z",
                "completed_at": "2026-09-10T08:09:04Z",
            }
        },
    }
    workflow_run = {
        "steps": [
            {
                "step_id": "call_weather_child",
                "step_execution_id": "8a4f3c1e-0000-4000-8000-00000000c0de",
                "occurrence": 1,
                "display_name": "Call weather child",
                "status": "completed",
                "started_at": "2026-09-10T08:09:01Z",
                "finished_at": "2026-09-10T08:09:04Z",
            }
        ]
    }

    steps, _selected = build_execution_steps(
        job={"id": "job-42", "project_id": "proj-42", "status": "completed"},
        messages=[message],
        workflow_run=workflow_run,
    )

    assert len(steps) == 1
    assert steps[0]["source"] == "message"
    assert steps[0]["scope_id"] == "call_weather_child"
    assert steps[0]["duration_ms"] == 3000


def test_connector_step_exposes_complete_legacy_artifact_without_status() -> None:
    source = {
        "status": "pass",
        "timestamp": "2026-09-10T09:00:00Z",
        "artifacts": {
            "connector_invocation": {
                "operation_id": "convert_document",
                "response": {"result": {"markdown_artifact_handle": "artifact:art-output"}},
            }
        },
        "errors": [],
    }
    records = {
        "art-output": {
            "artifact_id": "art-output",
            "owner_scope": {"project_id": "proj-42", "job_id": "job-42"},
            "filename": "invoice.md",
            "content_type": "text/markdown",
            "size_bytes": 4096,
            "object_key": "artifacts/art-output/invoice.md",
            "sha256": "b" * 64,
        }
    }

    step = _build_message_steps(
        [source],
        project_id="proj-42",
        job_id="job-42",
        resolve_artifact=records.get,
    )[0]

    assert step["output_artifacts"][0]["available"] is True
    assert step["output_artifacts"][0]["download_url"] == ("/api/jobs/job-42/artifacts/art-output")


def test_contract_bounds_independent_heavy_raw_buckets_without_mutating_sources() -> None:
    heavy_rows = [
        {"id": index, "detail": f"bucket-{index}-" + ("x" * 4096)} for index in range(128)
    ]
    heavy_object = {"rows": heavy_rows}
    source_tail = heavy_rows[-1]["detail"]
    minimal_lifecycle = {
        "status": {"code": "completed", "label": "Completed", "tone": "green"},
        "step": {"code": "complete", "label": "Complete"},
        "approval": {"state": "none"},
    }

    payload = job_detail_contract_module._build_contract_payload(
        job={"id": "42", "project_id": "proj-42", "status": "completed"},
        status_payload={"id": "42", "project_id": "proj-42", "status": "completed"},
        runtime=heavy_object,
        projection={},
        lifecycle=minimal_lifecycle,
        sections={},
        approval_gate=heavy_object,
        approval_history=heavy_rows,
        causal_chain=heavy_object,
        cost_breakdown_raw=heavy_object,
        decision_overrides=heavy_rows,
        incident=heavy_object,
        job_errors=heavy_rows,
        runtime_journal=heavy_rows,
        section_warnings=[],
        thread_state=heavy_object,
        workflow_run=None,
    )

    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    assert len(encoded.encode("utf-8")) < 150_000
    for key in (
        "runtime",
        "approval_gate",
        "approval_history",
        "cost_breakdown_raw",
        "decision_overrides",
        "incident",
        "job_errors",
        "runtime_journal",
    ):
        assert "bucket-127" not in json.dumps(payload[key])
    assert heavy_rows[-1]["detail"] == source_tail
    assert len(heavy_rows) == 128


def test_contract_emits_execution_steps_once_in_canonical_sections() -> None:
    step = {
        "id": "step-1",
        "scope_id": "step-1",
        "status": "completed",
        "inputs": {"batch": 1},
        "outputs": {"updated": 25},
    }
    sections = {
        "execution_steps": [step],
        "selected_step": step,
        "diagnostics": {},
    }
    lifecycle = {
        "status": {"code": "completed", "label": "Completed", "tone": "green"},
        "step": {"code": "complete", "label": "Complete"},
        "steps": [step],
        "approval": {"state": "none"},
    }

    payload = job_detail_contract_module._build_contract_payload(
        job={"id": "42", "project_id": "proj-42", "status": "completed"},
        status_payload={"id": "42", "project_id": "proj-42", "status": "completed"},
        runtime={},
        projection={},
        lifecycle=lifecycle,
        sections=sections,
        approval_gate={},
        approval_history=[],
        causal_chain={},
        cost_breakdown_raw={},
        decision_overrides=[],
        incident={},
        job_errors=[],
        runtime_journal=[],
        section_warnings=[],
        thread_state={},
        workflow_run=None,
    )

    assert payload["sections"]["execution_steps"] == [step]
    assert "execution_steps" not in payload
    assert "steps" not in payload["lifecycle"]


def test_message_step_omits_full_raw_envelope_from_primary_projection() -> None:
    step = _build_message_steps(
        [
            {
                "id": "message-1",
                "group_id": "push_batches",
                "from_agent": "connector_step",
                "skill": "connector",
                "status": "pass",
                "summary": "Batch completed.",
                "timestamp": "2026-09-10T08:09:02Z",
                "artifacts": {
                    "step_id": "push_batch",
                    "connector_invocation": {
                        "operation_id": "wc_batch_products",
                        "response": {"result": {"rows": [{"id": 1}] * 100}},
                    },
                },
                "errors": [],
            }
        ]
    )[0]

    assert "raw" not in step
    assert step["operation_id"] == "wc_batch_products"


def test_contract_bounds_real_review_workspace_in_canonical_sections() -> None:
    items = [
        {
            "id": f"item-{index}",
            "title": f"Item {index}",
            "content": f"item-payload-{index}-" + ("x" * 4096),
        }
        for index in range(128)
    ]
    approval_gate = {
        "stage": "Review",
        "message": "Review the generated items.",
        "allowed_actions": ["approve", "override", "reject", "abort"],
        "items": items,
    }
    sections = build_job_detail_sections(
        job={"id": "42", "project_id": "proj-42", "status": "awaiting_approval"},
        approval_gate=approval_gate,
        user_role="operator",
    )
    minimal_lifecycle = {
        "status": {"code": "awaiting_approval", "label": "Awaiting approval", "tone": "amber"},
        "step": {"code": "review", "label": "Review"},
        "approval": {
            "state": "awaiting_approval",
            "gate": approval_gate,
            "history": [],
            "review_workspace": sections["review_workspace"],
        },
    }

    payload = job_detail_contract_module._build_contract_payload(
        job={"id": "42", "project_id": "proj-42", "status": "awaiting_approval"},
        status_payload={"id": "42", "project_id": "proj-42", "status": "awaiting_approval"},
        runtime={},
        projection={},
        lifecycle=minimal_lifecycle,
        sections=sections,
        approval_gate=approval_gate,
        approval_history=[],
        causal_chain={},
        cost_breakdown_raw={},
        decision_overrides=[],
        incident={},
        job_errors=[],
        runtime_journal=[],
        section_warnings=[],
        thread_state={},
        workflow_run=None,
    )

    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    assert len(encoded.encode("utf-8")) < 150_000
    assert "item-payload-127" not in encoded
    assert payload["sections"]["review_workspace"]["kind"] == "document_review"
    assert payload["approval_gate"]["allowed_actions"] == [
        "approve",
        "override",
        "reject",
        "abort",
    ]
    assert payload["lifecycle"]["approval"]["gate"]["allowed_actions"][-1] == "abort"
    assert items[-1]["id"] == "item-127"
    assert items[-1]["content"].endswith("x" * 4096)


def test_build_job_detail_contract_requests_bounded_diagnostic_histories() -> None:
    class _LimitRecordingJobQueryService(_JobQueryService):
        def __init__(self) -> None:
            self.runtime_limits: list[int] = []

        def get_runtime_journal(self, job_id, *, limit=500, scope=None):
            self.runtime_limits.append(limit)
            return super().get_runtime_journal(job_id, limit=limit, scope=scope)

    class _LimitRecordingApprovalQueryService(_ApprovalQueryService):
        def __init__(self) -> None:
            self.history_limits: list[int] = []

        def list_history(self, job_id, *, limit=100, scope=None):
            self.history_limits.append(limit)
            return super().list_history(job_id, limit=limit, scope=scope)

    job_query_service = _LimitRecordingJobQueryService()
    approval_query_service = _LimitRecordingApprovalQueryService()

    asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=job_query_service,
            approval_query_service=approval_query_service,
            get_thread_state_values=lambda _thread_id: {},
            user_role="operator",
        )
    )

    assert job_query_service.runtime_limits == [100]
    assert approval_query_service.history_limits == [100]


def test_compact_job_detail_returns_only_latest_execution_page() -> None:
    class _PagedJobQueryService(_JobQueryService):
        def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
            raise AssertionError("compact Job Details must not build the full log page")

        def list_messages(
            self,
            job_id,
            *,
            before_id="",
            newest=True,
            limit=50,
            scope=None,
        ):
            _ = (job_id, before_id, newest, limit, scope)
            return _Dump({"messages": [], "last_id": "", "has_more": False})

        def get_workflow_run(self, job_id, **_kwargs):
            return _Dump(
                {
                    "schema_version": "1",
                    "job_id": str(job_id),
                    "project_id": "proj-42",
                    "workflow_id": "workflow-42",
                    "status": "completed",
                    "terminal": True,
                    "active_steps": [],
                    "steps": [
                        {
                            "step_id": "batch",
                            "group_id": "push",
                            "occurrence": occurrence,
                            "display_name": "Push batch",
                            "step_kind": "connector",
                            "status": "completed",
                            "started_at": f"2026-05-18T07:{occurrence // 60:02d}:{occurrence % 60:02d}Z",
                            "finished_at": f"2026-05-18T07:{occurrence // 60:02d}:{occurrence % 60:02d}Z",
                            "child_job_id": None,
                        }
                        for occurrence in range(1, 121)
                    ],
                    "approval": None,
                    "result": {"outputs": {}, "artifacts": {}},
                    "error": None,
                    "queued_at": "2026-05-18T06:59:00Z",
                    "started_at": "2026-05-18T07:00:00Z",
                    "updated_at": "2026-05-18T07:03:00Z",
                    "finished_at": "2026-05-18T07:03:00Z",
                }
            )

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_PagedJobQueryService(),
            execution_step_limit=50,
            include_workflow_run=False,
        )
    )

    steps = payload["sections"]["execution_steps"]
    page = payload["sections"]["execution_steps_page"]
    assert len(steps) == 50
    assert steps[0]["scope_id"] == "batch:71"
    assert steps[-1]["scope_id"] == "batch:120"
    assert page["count"] == 50
    assert page["total_durable_steps"] == 120
    assert page["has_more"] is True
    assert page["next_cursor"]
    assert payload["sections"]["diagnostics"] == {}
    assert payload["lifecycle"]["runtime"] == {}
    assert payload["workflow_run"] is None


def test_execution_step_cursor_page_continues_before_previous_page() -> None:
    class _PagedJobQueryService(_JobQueryService):
        def list_messages(
            self,
            job_id,
            *,
            before_id="",
            newest=True,
            limit=50,
            scope=None,
        ):
            _ = (job_id, before_id, newest, limit, scope)
            return _Dump({"messages": [], "last_id": "", "has_more": False})

        def get_workflow_run(self, job_id, **_kwargs):
            payload = _JobQueryService.get_workflow_run(self, job_id, **_kwargs).dump()
            payload["steps"] = [
                {
                    "step_id": "batch",
                    "group_id": "push",
                    "occurrence": occurrence,
                    "display_name": "Push batch",
                    "step_kind": "connector",
                    "status": "completed",
                    "started_at": f"2026-05-18T07:00:{occurrence:02d}Z",
                    "finished_at": f"2026-05-18T07:00:{occurrence:02d}Z",
                    "child_job_id": None,
                }
                for occurrence in range(1, 13)
            ]
            return _Dump(payload)

    service = _PagedJobQueryService()
    first = asyncio.run(
        build_job_execution_steps_page(
            job_id="42",
            scope=None,
            job_query_service=service,
            limit=5,
        )
    )
    second = asyncio.run(
        build_job_execution_steps_page(
            job_id="42",
            scope=None,
            job_query_service=service,
            limit=5,
            cursor=first["page"]["next_cursor"],
        )
    )

    assert [row["scope_id"] for row in first["steps"]] == [
        "batch:8",
        "batch:9",
        "batch:10",
        "batch:11",
        "batch:12",
    ]
    assert [row["scope_id"] for row in second["steps"]] == [
        "batch:3",
        "batch:4",
        "batch:5",
        "batch:6",
        "batch:7",
    ]
    assert second["page"]["has_more"] is True


def test_execution_step_page_carries_the_run_error_for_the_overview() -> None:
    error = {
        "code": "projection_unavailable",
        "message": "Saved query projection is unavailable for new runs",
        "step_id": "query",
        "category": "configuration",
        "retryable": False,
        "details": {},
    }

    class _FailedJobQueryService(_JobQueryService):
        def list_messages(self, job_id, **_kwargs):
            _ = job_id
            return _Dump({"messages": [], "last_id": "", "has_more": False})

        def get_workflow_run(self, job_id, **_kwargs):
            payload = _JobQueryService.get_workflow_run(self, job_id, **_kwargs).dump()
            payload.update(status="failed", terminal=True, finished_at="2026-04-25T10:05:00Z")
            payload["error"] = dict(error)
            return _Dump(payload)

    page = asyncio.run(
        build_job_execution_steps_page(
            job_id="42", scope=None, job_query_service=_FailedJobQueryService(), limit=5
        )
    )

    assert page["run_error"]["code"] == "projection_unavailable"
    assert page["run_error"]["message"] == "Saved query projection is unavailable for new runs"
    assert page["run_error"]["step_id"] == "query"


def test_execution_step_first_page_carries_authorized_child_links() -> None:
    link = {
        "step_id": "hook-step",
        "step_execution_id": "execution-1",
        "invocation_key": "42:execution-1",
        "child_job_id": "child-1",
        "child_workflow_id": "client",
    }

    class _InvocationQueryService(_JobQueryService):
        def get_child_invocations(self, job_id, *, scope=None):
            assert job_id == "42"
            return {"invocations": [link], "truncated": True}

    page = asyncio.run(
        build_job_execution_steps_page(
            job_id="42", scope=None, job_query_service=_InvocationQueryService(), limit=5
        )
    )

    assert page["child_invocations"] == [link]
    assert page["child_invocations_truncated"] is True
    assert page["run_terminal"] is False


@pytest.mark.parametrize("with_message", [False, True])
@pytest.mark.parametrize("durable_status", ["completed", "failed"])
def test_execution_history_preserves_durable_container_and_occurrence_identity(
    with_message,
    durable_status,
) -> None:
    steps, _selected = build_execution_steps(
        job={"id": "42", "status": "completed"},
        workflow_run={
            "steps": [
                {
                    "step_id": "client-step",
                    "step_execution_id": "execution-3",
                    "for_each_id": "orders",
                    "group_id": "client-group",
                    "occurrence": 3,
                    "step_kind": "transform",
                    "status": durable_status,
                }
            ]
        },
        messages=[
            {
                "id": "message-3",
                "step_id": "client-step",
                "step_execution_id": "execution-3",
                "status": "pass",
                "from_agent": "transform",
                "skill": "transform",
                "artifacts": {"step_id": "client-step"},
            }
        ]
        if with_message
        else [],
    )

    assert steps[0]["for_each_id"] == "orders"
    assert steps[0]["occurrence"] == 3
    assert steps[0]["step_execution_id"] == "execution-3"
    assert steps[0]["status"] == durable_status


def test_contract_keeps_versioned_workflow_run_exact() -> None:
    workflow_run = {
        "schema_version": "1",
        "job_id": "42",
        "outputs": [{"sku": f"sku-{index}"} for index in range(5)],
    }

    payload = job_detail_contract_module._build_contract_payload(
        job={"id": "42", "project_id": "proj-42", "status": "completed"},
        status_payload={"id": "42", "project_id": "proj-42", "status": "completed"},
        runtime={},
        projection={},
        lifecycle={
            "status": {"code": "completed", "label": "Completed", "tone": "green"},
            "step": {"code": "complete", "label": "Complete"},
            "approval": {"state": "none"},
        },
        sections={},
        approval_gate={},
        approval_history=[],
        causal_chain={},
        cost_breakdown_raw={},
        decision_overrides=[],
        incident={},
        job_errors=[],
        runtime_journal=[],
        section_warnings=[],
        thread_state={},
        workflow_run=workflow_run,
    )

    assert payload["workflow_run"] == workflow_run
    assert payload["workflow_run"]["outputs"][-1] == {"sku": "sku-4"}


def test_contract_bounds_all_large_secret_bearing_decision_projections() -> None:
    token = "ghp_" + ("h" * 36)  # pragma: allowlist secret
    comment = ("x" * 980) + " " + token + " " + ("z" * 1_999_000)
    history = [
        {
            "stage": "Review",
            "approved": True,
            "comment": comment,
            "content_preview": comment,
        }
    ]
    decision_overrides = [
        {
            "id": "override-1",
            "domain": "workflow",
            "override_key": "workflow.mode",
            "reason": token + " " + ("r" * 1_999_000),
            "created_at": ("x" * 980) + " " + token + " " + ("t" * 1_999_000),
            "created_by": "operator",
            "original_value": {"mode": "automatic"},
            "override_value": {"mode": "manual"},
            "active": True,
        }
    ]
    sections = build_job_detail_sections(
        job={"id": "42", "project_id": "proj-42", "status": "completed"},
        approval_history=history,
        decision_overrides=decision_overrides,
        user_role="operator",
    )
    lifecycle = {
        "status": {"code": "completed", "label": "Completed", "tone": "green"},
        "step": {"code": "complete", "label": "Complete"},
        "approval": {"state": "approval_recorded", "gate": {}, "history": history},
    }

    payload = job_detail_contract_module._build_contract_payload(
        job={"id": "42", "project_id": "proj-42", "status": "completed"},
        status_payload={"id": "42", "project_id": "proj-42", "status": "completed"},
        runtime={},
        projection={},
        lifecycle=lifecycle,
        sections=sections,
        approval_gate={},
        approval_history=history,
        causal_chain={},
        cost_breakdown_raw={},
        decision_overrides=decision_overrides,
        incident={},
        job_errors=[],
        runtime_journal=[],
        section_warnings=[],
        thread_state={},
        workflow_run=None,
    )

    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    assert len(encoded.encode("utf-8")) < 100_000
    assert len(payload["sections"]["previous_decisions"]) == 2
    previous_decisions = payload["sections"]["previous_decisions"]
    approval_decision = next(row for row in previous_decisions if row["kind"] == "approval")
    assert len(approval_decision["summary"]) <= 700
    override_rows = payload["sections"]["diagnostics"]["tabs"]["overrides"]["rows"]
    assert len(override_rows) == 1
    assert override_rows[0]["source"] == "decision_override"
    assert len(override_rows[0]["summary"]) <= 220
    assert len(override_rows[0]["timestamp"]) <= 80
    assert not contains_detected_secret(encoded)
    assert history[0]["comment"] == comment
    assert decision_overrides[0]["reason"].startswith(token)


def test_job_detail_survives_an_unavailable_workflow_run_projection():
    """A missing checkpoint must degrade one section, not the whole job.

    The public projection is built from the graph checkpoint, which is briefly
    absent while a step transitions - exactly after an approval decision, when
    the operator who just decided is looking at the job. Failing the whole
    response hid status, logs and approvals behind a 503 for a job that plainly
    exists.
    """

    class _NoCheckpointJobQueryService(_JobQueryService):
        def get_workflow_run(self, *args, **kwargs):
            _ = (args, kwargs)
            raise ServiceError("Public workflow state is unavailable", status_code=503)

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=None,
            job_query_service=_NoCheckpointJobQueryService(),
            approval_query_service=_ApprovalQueryService(),
            get_thread_state_values=lambda _thread_id: {},
            user_role="operator",
        )
    )

    assert payload["workflow_run"] is None
    assert payload["id"] == "42", "the rest of the job must still be reported"
    warnings = [
        item
        for item in payload.get("section_warnings", [])
        if item.get("section") == "workflow_run"
    ]
    assert warnings, "the degraded section has to be named, not silently empty"
    assert warnings[0]["code"] == "workflow_run_unavailable"


def test_engine_stages_are_hidden_from_job_details_unless_they_failed() -> None:
    """A user sees the steps they declared, not the platform's own stages."""

    def stage(step_id: str, status: str = "completed") -> dict:
        return {
            "step_id": step_id,
            "occurrence": 1,
            "status": status,
            "started_at": "2026-10-07T10:00:00Z",
            "finished_at": "2026-10-07T10:00:01Z",
        }

    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "failed"},
        workflow_run={
            "steps": [
                stage("task_intake"),
                stage("kb_intake"),
                stage("workflow_routing"),
                stage("orchestrator"),
                stage("fetch_orders"),
                stage("orchestrator_validation", "failed"),
            ]
        },
    )

    assert [step["scope_id"] for step in steps] == ["fetch_orders", "orchestrator_validation"]
    assert steps[1]["status"] == "failed"


def test_a_step_without_an_agent_shows_its_type_tool_and_action_not_an_agent() -> None:
    """The inspector showed "Agent: Control Flow Step" and "Tool: Control Flow"."""
    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "completed"},
        messages=[
            {
                "id": "m1",
                "step_id": "start",
                "from_agent": "control_flow_step",
                "skill": "control_flow",
                "status": "pass",
                "timestamp": "2026-10-07T10:00:00Z",
                "artifacts": {},
            },
            {
                "id": "m2",
                "step_id": "sync",
                "from_agent": "connector_step",
                "skill": "shopify_products",
                "status": "pass",
                "timestamp": "2026-10-07T10:00:01Z",
                "artifacts": {"extension_id": "shopify", "action_id": "update_inventory"},
            },
            {
                "id": "m3",
                "step_id": "write",
                "from_agent": "seo_copywriter",
                "skill": "product_description",
                "status": "pass",
                "timestamp": "2026-10-07T10:00:02Z",
                "artifacts": {},
            },
        ],
    )

    by_step = {step["step_id"]: step for step in steps}
    assert by_step["start"]["step_type_label"] == "Control flow"
    assert by_step["start"]["agent_label"] == ""
    assert by_step["start"]["tool_label"] == ""
    assert by_step["sync"]["step_type_label"] == "Extension action"
    assert by_step["sync"]["tool_label"] == "shopify"
    assert by_step["sync"]["operation_id"] == "update_inventory"
    assert by_step["write"]["step_type_label"] == ""
    assert by_step["write"]["agent_label"] == "Seo Copywriter"


def test_a_step_reads_by_its_own_name_and_a_readable_action() -> None:
    """Two set-variable steps read "Control Flow"; an HTTP call read "Com/v1/search"."""
    steps = _build_message_steps(
        [
            {
                "id": "m1",
                "step_id": "start",
                "from_agent": "control_flow_step",
                "skill": "control_flow",
                "status": "pass",
                "timestamp": "2026-10-07T10:00:00Z",
                "artifacts": {"step_kind": "set_variable", "step_display_name": "Init"},
            },
            {
                "id": "m2",
                "step_id": "geocode",
                "from_agent": "http_request_step",
                "skill": "http_request",
                "status": "pass",
                "timestamp": "2026-10-07T10:00:01Z",
                "artifacts": {
                    "step_kind": "http_request",
                    "operation_id": "GET https://geocoding-api.open-meteo.com/v1/search",
                },
            },
        ]
    )

    assert steps[0]["title"] == "Init"
    # Without a name the step id tells two steps of one kind apart.
    assert steps[1]["title"] == "Geocode: Http Request"
    assert (
        _build_message_steps(
            [
                {
                    "id": "m3",
                    "step_id": "capital",
                    "group_id": "countries",
                    "from_agent": "control_flow_step",
                    "status": "pass",
                    "artifacts": {"step_display_name": "Mark capital"},
                }
            ]
        )[0]["title"]
        == "Countries: Mark capital"
    )
    assert steps[1]["operation_label"] == "GET geocoding-api.open-meteo.com/v1/search"


def test_a_failed_step_shows_no_bookkeeping_as_its_outputs() -> None:
    """A failed transform listed its step id and deadline as "Outputs"."""
    step = _build_message_steps(
        [
            {
                "id": "m1",
                "step_id": "shape",
                "from_agent": "transform_step",
                "skill": "transform",
                "status": "fail",
                "timestamp": "2026-10-07T10:00:00Z",
                "artifacts": {
                    "step_kind": "transform",
                    "step_id": "pick_fields",
                    "error": "transform_failed",
                    "transform": {"ok": False, "detail": "input_source_missing"},
                    "operation_deadline_epoch_seconds": 1791378310.03,
                    "input_payload": {"source": "state.geo.results"},
                },
            }
        ]
    )[0]

    assert step["outputs"] == {}
    assert step["inputs"] == {"source": "state.geo.results"}


def test_an_empty_step_result_is_shown_not_dropped() -> None:
    """A transform whose filter kept no rows showed only its result target."""
    step = _build_message_steps(
        [
            {
                "id": "m1",
                "step_id": "shape",
                "from_agent": "transform_step",
                "status": "pass",
                "artifacts": {
                    "step_kind": "transform",
                    "output_payload": {"result_target": "state.places", "value": []},
                },
            }
        ]
    )[0]

    assert step["outputs"] == {"result_target": "state.places", "value": []}


def test_a_step_output_list_keeps_its_recorded_preview() -> None:
    """Every list was cut to two items again: 25 rows read "11 items"."""
    recorded = [*(f"SKU-{index:03d}" for index in range(10)), "… 15 more items (25 in total)"]
    step = _build_message_steps(
        [
            {
                "id": "m1",
                "step_id": "loop",
                "from_agent": "control_flow_step",
                "status": "pass",
                "artifacts": {
                    "step_kind": "for_each",
                    "output_payload": {"state.skus": recorded, "batch_sizes": [10, 10, 5]},
                },
            }
        ]
    )[0]

    assert step["outputs"]["state.skus"] == recorded
    assert step["outputs"]["batch_sizes"] == [10, 10, 5]


@pytest.mark.parametrize(
    ("status", "response_target", "state_patch"),
    [
        ("fail", "state.user", {"user": {"id": 7}}),
        ("unknown", "state.user", {"user": {"id": 7}}),
        (None, "state.user", {"user": {"id": 7}}),
        ("pass", "state.user", None),
        ("pass", "state.user", {"other": {"id": 7}}),
        ("pass", "", {"user": {"id": 7}}),
        ("pass", "state.user[", {"user": {"id": 7}}),
    ],
)
def test_http_saved_output_requires_a_successful_recorded_target(
    status: str | None, response_target: str, state_patch: dict | None
) -> None:
    artifacts = {
        "step_kind": "http_request",
        "response_target": response_target,
        "http_request": {"response_mode": "inline"},
        "_state_patch": state_patch,
        "output_payload": {
            "status_code": 200,
            "content_type": "application/json",
            "body": {"id": 7},
        },
    }
    message = {
        "id": "m-http",
        "step_id": "fetch_user",
        "from_agent": "http_request_step",
        "artifacts": artifacts,
    }
    if status is not None:
        message["status"] = status
    step = _build_message_steps([message])[0]

    assert step.get("saved_output", {}) == {}


def test_http_saved_output_is_separate_from_the_response_diagnostics() -> None:
    step = _build_message_steps(
        [
            {
                "id": "m-http",
                "step_id": "fetch_user",
                "from_agent": "http_request_step",
                "status": "pass",
                "artifacts": {
                    "step_kind": "http_request",
                    "response_target": "state.user",
                    "http_request": {"response_mode": "inline"},
                    "_state_patch": {"user": {"id": 7}},
                    "output_payload": {
                        "status_code": 200,
                        "content_type": "application/json",
                        "body": {"id": 7},
                    },
                },
            }
        ]
    )[0]

    assert step["outputs"] == {
        "status_code": 200,
        "content_type": "application/json",
        "body": {"id": 7},
    }
    assert step["saved_output"] == {
        "target": "state.user",
        "value": {"id": 7},
        "is_file": False,
    }


@pytest.mark.parametrize("value", [None, [], 0, False, "", ["x"], "body"])
def test_http_saved_output_preserves_present_falsey_and_ordinary_values(value: object) -> None:
    step = _build_message_steps(
        [
            {
                "id": "m-http",
                "step_id": "fetch_user",
                "from_agent": "http_request_step",
                "status": "pass",
                "artifacts": {
                    "step_kind": "http_request",
                    "response_target": "state.user",
                    "http_request": {"response_mode": "inline"},
                    "_state_patch": {"user": value},
                    "output_payload": {"status_code": 200, "body": value},
                },
            }
        ]
    )[0]

    assert step["saved_output"] == {"target": "state.user", "value": value, "is_file": False}


def test_http_saved_output_redacts_and_bounds_the_saved_value() -> None:
    long_value = "x" * 70_000
    step = _build_message_steps(
        [
            {
                "id": "m-http",
                "step_id": "fetch_user",
                "from_agent": "http_request_step",
                "status": "pass",
                "artifacts": {
                    "step_kind": "http_request",
                    "response_target": "state.user",
                    "http_request": {"response_mode": "inline"},
                    "_state_patch": {"user": {"api_token": "private-token", "long": long_value}},
                    "output_payload": {"status_code": 200, "body": {}},
                },
            }
        ]
    )[0]
    encoded_saved = json.dumps(step["saved_output"], sort_keys=True)

    assert "private-token" not in encoded_saved
    assert "api_token" in encoded_saved
    assert len(encoded_saved) < 50_000


def test_http_artifact_saved_output_is_a_file_but_inline_artifact_text_is_not() -> None:
    records = {
        "art-7": {
            "artifact_id": "art-7",
            "owner_scope": {"project_id": "proj-42", "job_id": "job-42"},
            "filename": "body.txt",
            "content_type": "text/plain",
            "size_bytes": 13,
            "object_key": "artifacts/art-7/body.txt",
            "sha256": "a" * 64,
        }
    }
    artifact = _build_message_steps(
        [
            {
                "id": "m-artifact",
                "step_id": "download",
                "from_agent": "http_request_step",
                "status": "pass",
                "artifacts": {
                    "step_kind": "http_request",
                    "response_target": "state.download",
                    "http_request": {"response_mode": "artifact"},
                    "_state_patch": {"download": "artifact:art-7"},
                    "output_payload": {"status_code": 200, "file": "artifact:art-7"},
                },
            }
        ],
        project_id="proj-42",
        job_id="job-42",
        resolve_artifact=records.get,
    )[0]
    inline_text = _build_message_steps(
        [
            {
                "id": "m-inline",
                "step_id": "read_text",
                "from_agent": "http_request_step",
                "status": "pass",
                "artifacts": {
                    "step_kind": "http_request",
                    "response_target": "state.body",
                    "http_request": {"response_mode": "inline"},
                    "_state_patch": {"body": "artifact:art-7"},
                    "output_payload": {"status_code": 200, "body": "artifact:art-7"},
                },
            }
        ],
        project_id="proj-42",
        job_id="job-42",
        resolve_artifact=records.get,
    )[0]

    assert artifact["saved_output"] == {
        "target": "state.download",
        "value": "artifact:art-7",
        "is_file": True,
    }
    assert "file" not in artifact["outputs"]
    assert inline_text["saved_output"] == {
        "target": "state.body",
        "value": "artifact:art-7",
        "is_file": False,
    }
    assert inline_text["outputs"]["body"] == "artifact:art-7"
    assert artifact["output_artifacts"] == [
        {
            "artifact_id": "art-7",
            "role": "output",
            "filename": "body.txt",
            "content_type": "text/plain",
            "size_bytes": 13,
            "available": True,
            "download_url": "/api/jobs/job-42/artifacts/art-7",
        }
    ]
    assert inline_text["output_artifacts"] == []


_EXECUTION_ID = "3f0c9a52-0000-4000-8000-0000000000aa"


def _cursor_state(**step_fields) -> dict:
    step = {
        "step_id": "fetch_orders",
        "step_kind": "http_request",
        "display_name": "Fetch orders",
        "config": {"url": "https://example.test/orders"},
        **step_fields,
    }
    return step_run_state(step, step_execution_id=_EXECUTION_ID, job_id="job-1")


def test_the_step_in_flight_comes_from_the_run_cursor() -> None:
    steps, selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "in_progress"},
        runtime={"current_step_id": "fetch_orders"},
        thread_state=_cursor_state(group_id="orders"),
    )

    assert len(steps) == 1
    step = steps[0]
    assert step["source"] == "runtime"
    assert step["status"] == "in_progress"
    assert step["step_id"] == "fetch_orders"
    assert step["step_execution_id"] == _EXECUTION_ID
    assert step["group_id"] == "orders"
    assert step["attempt"] == 1
    assert step["title"] == "Orders: Fetch Orders"
    assert selected is not None
    assert selected["scope_id"] == "fetch_orders"


def test_a_recorded_message_of_the_active_step_is_not_listed_twice() -> None:
    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "in_progress"},
        runtime={"current_step_id": "fetch_orders"},
        thread_state=_cursor_state(),
        messages=[
            {
                "id": "m1",
                "step_id": "fetch_orders",
                "step_execution_id": _EXECUTION_ID,
                "attempt": 1,
                "from_agent": "http_request_step",
                "status": "pass",
                "timestamp": "2026-10-12T10:00:00Z",
                "artifacts": {},
            }
        ],
    )

    assert [step["source"] for step in steps] == ["message"]


def test_a_finished_run_does_not_list_its_last_cursor_step_as_running() -> None:
    steps, _selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "completed"},
        thread_state=_cursor_state(),
    )

    assert steps == []


def test_a_cursor_whose_definition_cannot_be_loaded_shows_no_active_step() -> None:
    state = {
        "runtime_schema_version": 2,
        "workflow_definition_ref": {"snapshot_id": "snap-missing", "revision": 1, "digest": "0"},
        "run_cursor": {"step_id": "fetch_orders", "step_execution_id": _EXECUTION_ID},
    }

    steps, selected = build_execution_steps(
        job={"id": "job-1", "project_id": "project-1", "status": "in_progress"},
        thread_state=state,
    )

    assert steps == []
    assert selected is None


def test_the_current_group_travels_with_the_step_it_came_from() -> None:
    status_payload = {"current_step_id": "push"}
    runtime = {"current_step_id": "pull", "current_group_id": "batches"}
    job = {"current_step_id": "pull", "current_group_id": "batches"}

    step_id = job_detail_contract_module._canonicalize_current_step(
        status_payload=status_payload, runtime=runtime, job=job
    )

    assert step_id == "push"
    for bucket in (status_payload, runtime, job):
        assert bucket["current_step_id"] == "push"
        assert bucket["current_group_id"] is None


@pytest.mark.parametrize("acknowledged", [True, False])
def test_compact_job_detail_preserves_acknowledgement_through_real_status_dto(acknowledged):
    acknowledged_at = datetime(2026, 10, 17, 17, 26, 48, tzinfo=UTC) if acknowledged else None
    actor = "handoff-review" if acknowledged else None
    store = MagicMock()
    store.get_job.return_value = {
        "id": "42",
        "project_id": "proj-42",
        "status": "failed",
        "task_summary": "Owned guardrail run",
        "acknowledged_at": acknowledged_at,
        "acknowledged_by": actor,
    }
    store.get_project.return_value = {"id": "proj-42", "account_id": DEFAULT_ACCOUNT_ID}
    store.get_job_cost_summary.return_value = {}
    store.get_project_workflows.return_value = []
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    service = JobQueryService(
        get_logger=lambda: store,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "failed",
        build_runtime_payload_for_api=AsyncMock(return_value={}),
    )

    class CompactQuery(_JobQueryService):
        async def get_job_status(self, job_id, **kwargs):
            return await service.get_job_status(job_id, **kwargs)

        def get_logs_page(self, *args, **kwargs):
            raise AssertionError("the browser's compact summary cannot read full job logs")

    payload = asyncio.run(
        build_job_detail_contract(
            job_id="42",
            scope=scope,
            job_query_service=CompactQuery(),
            execution_step_limit=0,
            include_workflow_run=False,
        )
    )

    assert payload["job"].get("acknowledged_at") == (
        acknowledged_at.isoformat() if acknowledged_at else None
    )
    assert payload["job"].get("acknowledged_by") == actor


def test_execution_page_publishes_typed_committed_trace_on_first_page_only():
    from tests.unit.test_workflow_run_trace import one_trace

    from core.application.jobs.workflow_run_models import PublicWorkflowTraceV1

    trace = PublicWorkflowTraceV1.model_validate(one_trace()).model_dump(mode="json")

    class TracedService(_JobQueryService):
        def get_workflow_run(self, job_id, **kwargs):
            payload = super().get_workflow_run(job_id, **kwargs).dump()
            payload["run_trace"] = trace
            return _Dump(payload)

    first = asyncio.run(
        build_job_execution_steps_page(job_id="42", scope=None, job_query_service=TracedService())
    )
    # A foreign run's trace never leaks through even if a provider supplies it.
    assert "run_trace" not in first
    trace["job_id"] = "42"
    trace["workflow_id"] = "workflow-42"
    first = asyncio.run(
        build_job_execution_steps_page(job_id="42", scope=None, job_query_service=TracedService())
    )
    assert first["run_trace"] == trace
    assert "_flow_steward_projection" not in json.dumps(first["run_trace"])


def test_execution_page_keeps_container_evidence_independent_of_step_pagination():
    evidence = {
        "schema_version": 1,
        "job_id": "42",
        "project_id": "proj-42",
        "workflow_id": "workflow-42",
        "snapshot_id": "archive",
        "revision": 1,
        "digest": "digest",
        "covered_from_start": True,
        "truncated": False,
        "last_sequence": 0,
        "checkpoint_terminal": True,
        "passes": [],
    }

    class ContainerService(_JobQueryService):
        def list_messages(self, job_id, **kwargs):
            return _Dump({"messages": [], "last_id": "", "has_more": False})

        def get_workflow_run(self, job_id, **kwargs):
            payload = super().get_workflow_run(job_id, **kwargs).dump()
            payload["container_progress"] = evidence
            payload["steps"] = [
                {
                    "step_id": "batch",
                    "occurrence": index,
                    "step_kind": "transform",
                    "status": "completed",
                    "started_at": f"2026-05-18T07:00:{index:02d}Z",
                    "finished_at": f"2026-05-18T07:00:{index:02d}Z",
                }
                for index in range(1, 13)
            ]
            return _Dump(payload)

    service = ContainerService()
    first = asyncio.run(
        build_job_execution_steps_page(job_id="42", scope=None, job_query_service=service, limit=5)
    )
    assert first["page"]["has_more"] is True
    assert first["container_progress"] == evidence
    second = asyncio.run(
        build_job_execution_steps_page(
            job_id="42",
            scope=None,
            job_query_service=service,
            limit=5,
            cursor=first["page"]["next_cursor"],
        )
    )
    assert second["steps"]
    assert "container_progress" not in second
    evidence["project_id"] = "foreign"
    foreign = asyncio.run(
        build_job_execution_steps_page(job_id="42", scope=None, job_query_service=service, limit=5)
    )
    assert "container_progress" not in foreign
