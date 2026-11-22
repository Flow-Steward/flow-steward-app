"""The operator review queue (ADR 0075, item 10): neighbours, deep review, rights.

The SQL itself is exercised against PostgreSQL in
``tests/integration/test_operator_review_queue_postgres.py``; here the service
passes the list's own filters and checks what each caller may see.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.query_services_approval import ApprovalQueryService
from core.application.management.scope import ScopeContext
from core.application.registry.workflow_operator_form import JOB_STATUS_OPTIONS
from core.application.workflows import approval_waiting_propagation
from core.application.workflows.approval_review import open_review
from core.infrastructure.db.job_queries_store import _build_attention_where
from tests.unit.test_operator_job_list import (  # the section's workflow registry fixture
    ACCOUNT,
    PROJECT,
    WORKFLOW,
    _bl,
    _list,
    _registry,  # noqa: F401
    _scope,
    _service,
)

CHILD_WORKFLOW = "wf.recognize"


# --- Status option and SQL --------------------------------------------------------------


def test_awaiting_review_is_an_option_of_the_status_filter():
    assert JOB_STATUS_OPTIONS[0] == "awaiting_review"


def test_awaiting_review_reads_the_invoke_chain_with_its_depth():
    sql, params = _build_attention_where(
        column_exists=lambda *_args: True,
        project_id=PROJECT,
        project_ids=None,
        status=None,
        workflow_id=WORKFLOW,
        search=None,
        account_id=ACCOUNT,
        include_trashed=False,
        value_filters=[
            {
                "source": "job",
                "key": "status",
                "type": "choice_multi",
                "value": ("awaiting_review", "failed"),
                "review_chain_depth": 7,
            }
        ],
    )

    sql = " ".join(sql.split())
    assert "LOWER(COALESCE(j.status, '')) = ANY(%s)" in sql
    assert "WITH RECURSIVE review_chain(" in sql
    assert "invoke_parent_job_id" in sql and "invoke_children" in sql
    assert "NOT parent.id = ANY(chain.path)" in sql, "a cycle in the links stops the walk"
    assert params[-2:] == [["failed"], 7]


def test_only_an_awaiting_review_filter_carries_the_chain_depth(monkeypatch):
    monkeypatch.setattr(approval_waiting_propagation, "invoke_chain_max_depth", lambda: 9)
    from core.application.management import query_services_job_core_mixin as mixin

    monkeypatch.setattr(mixin, "invoke_chain_max_depth", lambda: 9)
    bl = _bl()

    _list(bl, filters=json.dumps({"state": ["awaiting_review"]}))

    (spec,) = bl.list_jobs_for_attention.call_args.kwargs["value_filters"]
    assert spec["value"] == ("awaiting_review",)
    assert spec["review_chain_depth"] == 9


# --- Neighbours -----------------------------------------------------------------------------


def _section_job(job_id: str, *, project: str = PROJECT, workflow: str = WORKFLOW) -> dict:
    return {"id": job_id, "project_id": project, "selected_workflow": workflow}


def test_around_answers_the_position_with_the_lists_own_filters():
    bl = _bl()
    bl.get_job.return_value = _section_job("j2")
    bl.list_job_neighbours.return_value = {
        "job_id": "j2",
        "in_list": False,
        "position": None,
        "total": 46,
        "previous_job_id": "j1",
        "next_job_id": "j3",
    }

    view = _list(
        bl,
        around="j2",
        filters=json.dumps({"state": ["awaiting_review"]}),
        search="acme",
        status=["awaiting_approval"],
    ).dump()

    assert view == {
        "job_id": "j2",
        "in_list": False,
        "position": None,
        "total": 46,
        "previous_job_id": "j1",
        "next_job_id": "j3",
    }
    kwargs = bl.list_job_neighbours.call_args.kwargs
    assert kwargs["job_id"] == "j2"
    assert kwargs["project_id"] == PROJECT
    assert kwargs["workflow_id"] == WORKFLOW
    assert kwargs["exclude_task_source"] == "workflow_test_run"
    assert kwargs["search"] == "acme"
    assert "awaiting_approval" in kwargs["status"]
    assert kwargs["value_filters"][0]["value"] == ("awaiting_review",)
    bl.list_jobs_for_attention.assert_not_called()
    bl.count_jobs_for_attention.assert_not_called()


def test_around_a_job_outside_the_callers_list_is_not_found():
    bl = _bl()
    bl.get_job.return_value = _section_job("foreign")
    bl.list_job_neighbours.return_value = None

    with pytest.raises(ServiceNotFoundError):
        _list(bl, around="foreign")


@pytest.mark.parametrize(
    "job",
    [
        _section_job("x", workflow="wf.other"),  # another workflow, same project
        _section_job("x", project="project-other"),  # same workflow, another project
        None,
    ],
)
def test_around_a_job_of_another_section_is_not_found_and_not_positioned(job):
    bl = _bl()
    bl.get_job.return_value = job
    bl.list_job_neighbours.return_value = {"job_id": "x", "in_list": False, "total": 1}

    with pytest.raises(ServiceNotFoundError):
        _list(bl, around="x")
    bl.list_job_neighbours.assert_not_called()


def test_around_cannot_be_grouped():
    bl = _bl()

    with pytest.raises(ServiceValidationError):
        _service(bl).list_jobs(
            project_id=PROJECT, group_by="attention", around="j1", scope=_scope()
        )


# --- Rows: which job holds the review ----------------------------------------------------


def _target(job_id: str, workflow: str) -> dict[str, Any]:
    return {
        "review_job_id": job_id,
        "project_id": PROJECT,
        "selected_workflow": workflow,
        "status": "awaiting_approval",
        "account_id": ACCOUNT,
        "tenant_id": DEFAULT_TENANT_ID,
        "workspace_id": "ws-1",
    }


def test_rows_name_the_review_job_once_per_page_and_only_when_readable(monkeypatch):
    monkeypatch.setattr(approval_waiting_propagation, "invoke_chain_max_depth", lambda: 5)
    bl = _bl()
    bl.list_jobs_for_attention.return_value = [
        {"id": "a", "project_id": PROJECT, "status": "awaiting_child"},
        {"id": "b", "project_id": PROJECT, "status": "awaiting_child"},
        {"id": "c", "project_id": PROJECT, "status": "awaiting_approval"},
        {"id": "d", "project_id": PROJECT, "status": "completed"},
    ]
    bl.read_job_operator_payloads.return_value = {}
    bl.read_awaiting_review_targets.return_value = {
        "a": _target("a-child", CHILD_WORKFLOW),
        "b": _target("b-child", "wf.secret-review"),
        "c": _target("c", WORKFLOW),
    }
    scope = _scope(allowed_workflow_ids=frozenset({WORKFLOW, CHILD_WORKFLOW}))

    jobs = _list(bl, scope=scope).dump()["jobs"]

    bl.read_awaiting_review_targets.assert_called_once_with(job_ids=["a", "b", "c"], max_depth=5)
    assert [job.get("review_job_id") for job in jobs] == ["a-child", None, "c", None]


# --- The review page's read: rights on the job holding the approval -----------------


def _captured_review() -> dict[str, Any]:
    review = open_review(
        {
            "fields": [
                {
                    "name": "total",
                    "type": "number",
                    "required": True,
                    "editable": True,
                    "from": "t",
                },
                {"name": "confidence", "type": "number", "editable": False, "from": "c"},
            ]
        },
        resolve=lambda selector: ({"t": 31.85, "c": 0.62}[selector], ""),
    )
    assert review is not None
    return review


def _jobs() -> dict[str, dict[str, Any]]:
    base = {"project_id": PROJECT, "account_id": ACCOUNT}
    return {
        "a": {**base, "id": "a", "status": "awaiting_child", "selected_workflow": WORKFLOW},
        "b": {**base, "id": "b", "status": "awaiting_child", "selected_workflow": "wf.middle"},
        "c": {
            **base,
            "id": "c",
            "status": "awaiting_approval",
            "thread_id": "thread-c",
            "selected_workflow": CHILD_WORKFLOW,
        },
        "done": {**base, "id": "done", "status": "completed", "selected_workflow": WORKFLOW},
    }


def _approvals(jobs: dict[str, dict[str, Any]] | None = None) -> ApprovalQueryService:
    rows = jobs or _jobs()
    store = MagicMock()
    store.get_job.side_effect = lambda job_id: rows.get(job_id)
    store.get_project.return_value = {"id": PROJECT, "account_id": ACCOUNT}
    store.read_awaiting_review_targets.side_effect = lambda job_ids, max_depth: {
        job_id: {"review_job_id": "c"} for job_id in job_ids if job_id in {"a", "b"}
    }
    payload = {
        "approval_id": "approval-c",
        "allowed_actions": ["approve", "reject"],
        "approve_label": "Export",
        "review": _captured_review(),
    }
    snapshot = SimpleNamespace(
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(id="interrupt-c", value=payload)])],
        values={},
    )
    return ApprovalQueryService(
        get_logger=lambda: store,
        get_thread_state_snapshot=lambda _thread_id: snapshot,
        approval_fallback_payload=lambda _job: {},
        resolve_artifact=lambda _handle: None,
    )


def _person(*workflows: str) -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        actor_id="person-1",
        principal_type="user",
        allowed_workflow_ids=frozenset(workflows),
    )


def test_a_deep_review_is_decided_on_the_job_that_holds_it():
    view = _approvals().get_review("a", scope=_person(), can_decide=True).dump()

    assert view["waiting"] is True
    assert view["review_job_id"] == "c"
    assert view["access"] == "decide"
    gate = view["gate"]
    assert gate["job_id"] == "c" and gate["approval_id"] == "approval-c"
    assert gate["payload"]["approve_label"] == "Export"
    assert {field["name"] for field in gate["review"]["fields"]} == {"total", "confidence"}
    assert gate["review"]["values"] == {"total": 31.85, "confidence": 0.62}


def test_a_section_reads_only_its_own_jobs_and_their_deep_reviews():
    service = _approvals()

    view = service.get_review(
        "a", scope=_person(), can_decide=True, project_id=PROJECT, workflow_id=WORKFLOW
    ).dump()
    assert view["review_job_id"] == "c"

    # A job of another workflow in the same project, opened from this section.
    with pytest.raises(ServiceNotFoundError):
        service.get_review("b", scope=_person(), project_id=PROJECT, workflow_id=WORKFLOW)
    # The job holding the deep approval is not this section's root job either.
    with pytest.raises(ServiceNotFoundError):
        service.get_review("c", scope=_person(), project_id=PROJECT, workflow_id=WORKFLOW)
    # The same workflow in another project.
    with pytest.raises(ServiceNotFoundError):
        service.get_review("a", scope=_person(), project_id="project-other", workflow_id=WORKFLOW)


def test_a_reader_gets_fields_and_values_without_the_right_to_decide():
    view = _approvals().get_review("a", scope=_person(), can_decide=False).dump()

    assert view["access"] == "read"
    assert view["gate"]["review"]["values"]["total"] == 31.85


def test_without_read_on_the_child_no_fields_values_files_or_job_id_leave_the_server():
    scope = _person(WORKFLOW)  # may read the section's job A, not the child C

    view = _approvals().get_review("a", scope=scope, can_decide=True).dump()

    assert view == {
        "job_id": "a",
        "status": "awaiting_child",
        "waiting": True,
        "review_job_id": None,
        "access": "none",
        "gate": None,
        # The section's own job is readable: what it submitted, nothing of the child.
        "job_values": {
            "status": "awaiting_child",
            "submitted_by": None,
            "created_at": None,
            "finished_at": None,
            "duration_ms": None,
            "inputs": {},
            "outputs": None,
        },
        "decision": None,
    }
    assert "31.85" not in json.dumps(view)


def test_the_child_approval_itself_stays_closed_to_a_parent_scoped_caller():
    from core.application.management.errors import ServiceForbiddenError

    with pytest.raises(ServiceForbiddenError):
        _approvals().get_current_gate("c", scope=_person(WORKFLOW))


def test_a_job_that_waits_for_nothing_has_no_review():
    view = _approvals().get_review("done", scope=_person(), can_decide=True).dump()

    assert view["waiting"] is False
    assert view["gate"] is None and view["access"] is None


def test_a_review_decided_meanwhile_is_no_longer_waiting():
    rows = _jobs()
    rows["c"] = {**rows["c"], "status": "in_progress"}

    view = _approvals(rows).get_review("a", scope=_person(), can_decide=True).dump()

    assert view["waiting"] is False and view["gate"] is None


def test_the_section_job_itself_needs_read_access():
    from core.application.management.errors import ServiceForbiddenError

    with pytest.raises(ServiceForbiddenError):
        _approvals().get_review("a", scope=_person(CHILD_WORKFLOW), can_decide=True)


# --- The built-in status filter: "awaiting review" without admin setup -------------


def test_the_built_in_status_filter_takes_awaiting_review_without_a_configured_filter(
    monkeypatch, request
):
    from core.application.management import query_services_job_core_mixin as mixin
    from tests.unit.test_operator_job_list import COLUMNS, _workflow_row

    monkeypatch.setattr(mixin, "invoke_chain_max_depth", lambda: 6)
    request.getfixturevalue("_registry")[WORKFLOW] = _workflow_row(
        list={"columns": COLUMNS, "filters": []}
    )
    bl = _bl()

    _list(bl, status=["awaiting_review", "failed"])

    for call in (bl.list_jobs_for_attention, bl.count_jobs_for_attention):
        kwargs = call.call_args.kwargs
        assert kwargs["status"] is None, "no stored status alone: one predicate reads the chain"
        (spec,) = kwargs["value_filters"]
        assert spec["source"] == "job" and spec["key"] == "status"
        assert "awaiting_review" in spec["value"] and "failed" in spec["value"]
        assert spec["review_chain_depth"] == 6


def test_a_plain_status_filter_stays_a_status_filter():
    bl = _bl()

    _list(bl, status=["failed"])

    kwargs = bl.list_jobs_for_attention.call_args.kwargs
    assert kwargs["status"] and "value_filters" not in kwargs


def test_awaiting_review_combines_with_acknowledged_as_alternatives():
    from core.infrastructure.db.job_queries_store import _status_filter_sql

    sql, params = _status_filter_sql(["acknowledged", "awaiting_review"], review_chain_depth=3)

    assert "j.acknowledged_at IS NOT NULL" in sql
    assert " OR " in sql
    assert params == [3]
