"""The operator's view of one job of a section (ADR 0075): values, result and decided review."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs.public_approval_serializer import serialize_approval_history_entry
from core.application.management.approval_repository import ApprovalRepository
from core.application.management.command_models_api_inputs import ApprovalDecisionInput
from core.application.management.query_services_approval import ApprovalQueryService
from core.application.management.scope import ScopeContext
from core.application.registry.workflow_operator_list import operator_job_values
from core.application.workflows.approval_review_history import last_review_decision

REVIEW = {
    "fields": [
        {"name": "supplier", "type": "string", "label": "Supplier", "editable": True},
        {"name": "total", "type": "number", "editable": True},
        {"name": "api_key", "type": "string", "editable": True},
    ],
    "editable": ["supplier", "total", "api_key"],
    "values": {
        "supplier": "ACME",
        "total": 31.85,
        "api_key": "key-123",  # pragma: allowlist secret
    },
    "invalid": {},
}


class _Cursor:
    def __init__(self, writes: list) -> None:
        self._writes = writes

    def execute(self, _sql: str, params: Any = None) -> None:
        self._writes.append(params)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _decided_meta(decision: ApprovalDecisionInput) -> dict:
    writes: list = []
    ApprovalRepository().persist_decision(
        bl=SimpleNamespace(conn=SimpleNamespace(cursor=lambda: _Cursor(writes))),
        transaction_conn=None,
        row={"id": "child-1", "project_id": "project-1", "task_id": "T-1", "meta": {}},
        thread_id="thread-1",
        approval_id="approval-1",
        payload={"approval_id": "approval-1", "review": dict(REVIEW)},
        decision=decision,
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="Olga"),
        record_in_progress=lambda **_kwargs: None,
    )
    return json.loads(writes[0][0])


def _approve() -> ApprovalDecisionInput:
    return ApprovalDecisionInput(
        approved=True, approval_id="approval-1", values={"total": 318.5, "api_key": "key-9"}
    )


def test_an_approved_review_keeps_its_accepted_values_masked_in_the_history() -> None:
    meta = _decided_meta(_approve())

    entry = meta["approvals"][0]
    assert entry["review"]["values"] == {
        "supplier": "ACME",
        "total": 318.5,
        "api_key": "[REDACTED]",
    }
    assert [field["name"] for field in entry["review"]["fields"]] == [
        "supplier",
        "total",
        "api_key",
    ]
    public = serialize_approval_history_entry(entry)
    assert public["review"]["values"]["total"] == 318.5
    assert "key-9" not in json.dumps(meta["approvals"])


def test_a_rejection_keeps_no_accepted_values() -> None:
    meta = _decided_meta(
        ApprovalDecisionInput(approved=False, approval_id="approval-1", comment="wrong")
    )

    assert "review" not in meta["approvals"][0]
    # It is still a decision on the review, so the operator's view finds it.
    assert meta["approvals"][0]["typed_review"] is True


def test_a_rejection_shows_who_rejected_it_and_why_but_none_of_its_edits() -> None:
    meta = _decided_meta(
        ApprovalDecisionInput(
            approved=False,
            approval_id="approval-1",
            comment="Wrong supplier: the invoice is from Baltic Paper.",
            values={"total": 999.0},
        )
    )
    # The rejected edit never reaches the workflow.
    assert "review_values" not in meta["workflow_resume"]["resume_value"]
    jobs = _chain(meta)
    bl = SimpleNamespace(get_job=jobs.get)

    decision = last_review_decision(
        bl, "root-1", jobs["root-1"], can_read=lambda _row: True, max_depth=3
    )

    assert decision is not None
    assert decision["access"] == "read"
    assert decision["approved"] is False
    assert decision["actor"] == "Olga"
    assert decision["comment"] == "Wrong supplier: the invoice is from Baltic Paper."
    assert decision["values"] == {}
    assert decision["changes"] == []
    assert "999" not in json.dumps(decision)


def test_a_plain_approval_without_fields_is_not_a_review_decision() -> None:
    writes: list = []
    ApprovalRepository().persist_decision(
        bl=SimpleNamespace(conn=SimpleNamespace(cursor=lambda: _Cursor(writes))),
        transaction_conn=None,
        row={"id": "child-1", "project_id": "project-1", "task_id": "T-1", "meta": {}},
        thread_id="thread-1",
        approval_id="approval-1",
        payload={"approval_id": "approval-1"},
        decision=ApprovalDecisionInput(approved=False, approval_id="approval-1", comment="no"),
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="Olga"),
        record_in_progress=lambda **_kwargs: None,
    )
    meta = json.loads(writes[0][0])

    assert "typed_review" not in meta["approvals"][0]
    jobs = _chain(meta)
    assert (
        last_review_decision(
            SimpleNamespace(get_job=jobs.get),
            "root-1",
            jobs["root-1"],
            can_read=lambda _row: True,
            max_depth=3,
        )
        is None
    )


def _chain(child_meta: dict, *, child_parent: str = "root-1") -> dict[str, dict]:
    return {
        "root-1": {
            "id": "root-1",
            "status": "completed",
            "project_id": "project-1",
            "meta": {"invoke_children": {"child-1": {"step_execution_id": "x"}}},
        },
        "child-1": {
            "id": "child-1",
            "status": "completed",
            "project_id": "project-1",
            "meta": {**child_meta, "invoke_parent_job_id": child_parent},
        },
    }


def test_the_decision_of_an_invoke_child_is_read_from_the_root() -> None:
    jobs = _chain(_decided_meta(_approve()))
    bl = SimpleNamespace(get_job=jobs.get)

    decision = last_review_decision(
        bl, "root-1", jobs["root-1"], can_read=lambda _row: True, max_depth=3
    )

    assert decision is not None
    assert decision["access"] == "read"
    assert decision["job_id"] == "child-1"
    assert decision["approved"] is True
    assert decision["actor"] == "Olga"
    assert decision["values"]["total"] == 318.5
    assert decision["changes"] == [
        {"field": "total", "before": 31.85, "after": 318.5},
        {"field": "api_key", "before": "[REDACTED]", "after": "[REDACTED]"},
    ]


def test_a_child_the_caller_cannot_read_says_only_that_it_was_decided() -> None:
    jobs = _chain(_decided_meta(_approve()))
    bl = SimpleNamespace(get_job=jobs.get)

    decision = last_review_decision(
        bl,
        "root-1",
        jobs["root-1"],
        can_read=lambda row: row.get("id") != "child-1",
        max_depth=3,
    )

    assert decision == {"access": "none"}


def test_a_child_that_does_not_name_the_parent_is_not_followed() -> None:
    jobs = _chain(_decided_meta(_approve()), child_parent="someone-else")
    bl = SimpleNamespace(get_job=jobs.get)

    assert (
        last_review_decision(bl, "root-1", jobs["root-1"], can_read=lambda _r: True, max_depth=3)
        is None
    )


def test_job_values_show_inputs_with_their_files_and_outputs_only_on_success() -> None:
    payload = {
        "inputs": {"doc_type": "invoice", "pages": [{"filename": "p1.jpg"}]},
        "input_files": [{"artifact_id": "a1", "filename": "p1.jpg", "field_name": "pages"}],
        "outputs": {"total": 318.5},
        "output_files": {"report": {"artifact_id": "r1", "filename": "report.pdf"}},
        "submitted_by": {"id": "u1", "name": "Olga"},
    }

    done = operator_job_values(
        {
            "status": "completed",
            "queued_at": datetime(2026, 11, 19, 10, 42, tzinfo=UTC),
            "started_at": datetime(2026, 11, 19, 10, 42, 1, tzinfo=UTC),
            "finished_at": datetime(2026, 11, 19, 10, 42, 4, tzinfo=UTC),
            "duration_ms": 3100,
        },
        payload,
    )
    running = operator_job_values({"status": "running"}, payload)

    assert done["inputs"]["pages"] == [{"filename": "p1.jpg", "artifact_id": "a1"}]
    assert done["outputs"] == {
        "total": 318.5,
        "report": {"artifact_id": "r1", "filename": "report.pdf"},
    }
    assert done["submitted_by"] == {"id": "u1", "name": "Olga"}
    assert running["outputs"] is None
    # The header shows when it was sent and how long it ran.
    assert done["created_at"] == "2026-11-19T10:42:00+00:00"
    assert done["finished_at"] == "2026-11-19T10:42:04+00:00"
    assert done["duration_ms"] == 3100
    assert (running["created_at"], running["finished_at"], running["duration_ms"]) == (
        None,
        None,
        None,
    )


def test_the_review_read_of_a_finished_job_carries_its_values_and_decision() -> None:
    jobs = _chain(_decided_meta(_approve()))
    store = MagicMock()
    store.get_job.side_effect = jobs.get
    store.get_project.return_value = {"id": "project-1", "account_id": DEFAULT_ACCOUNT_ID}
    store.read_job_operator_payloads.return_value = {
        "root-1": {"inputs": {"doc_type": "invoice"}, "outputs": {"total": 318.5}}
    }
    service = ApprovalQueryService(
        get_logger=lambda: store,
        get_thread_state_snapshot=lambda _thread_id: None,
        approval_fallback_payload=lambda _job: {},
    )

    view = service.get_review("root-1").dump()

    assert view["waiting"] is False
    assert view["job_values"]["inputs"] == {"doc_type": "invoice"}
    assert view["job_values"]["outputs"] == {"total": 318.5}
    assert view["decision"]["job_id"] == "child-1"
    assert view["decision"]["values"]["api_key"] == "[REDACTED]"
