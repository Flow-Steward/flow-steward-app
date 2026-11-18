"""A section's history: configured values of one page of jobs (ADR 0075)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext
from core.application.projects.user_section_history import section_row_values
from core.application.projects.user_sections import ProjectUserSection

ACCOUNT = "acc-1"
PROJECT = "00000000-0000-0000-0000-000000000007"

SECTION = {
    "id": "invoices",
    "title": "Invoices",
    "workflow_id": "wf.invoices",
    "columns": [
        {"id": "status", "source": "job", "path": "status"},
        {"id": "sent", "source": "job", "path": "created_at"},
        {"id": "author", "source": "job", "path": "submitted_by"},
        {"id": "document", "source": "input", "path": "document", "format": "file"},
        {"id": "skus", "source": "input", "path": "lines[].sku", "format": "list"},
        {"id": "supplier", "source": "output", "path": "supplier"},
        {"id": "total", "source": "output", "path": "total", "format": "currency"},
    ],
}
HIDDEN = {**SECTION, "id": "draft", "visible": False}

STORED = {
    "inputs": {
        "document": {
            "artifact_id": "art-1",
            "filename": "inv-77.pdf",
            "content_type": "application/pdf",
            "storage_ref": "s3://bucket/key",
            "object_key": "key",
        },
        "lines": [{"sku": "A-1"}, {"sku": "B-2"}, {"qty": 3}],
        "api_key": {"secret_ref": "sec-9"},
    },
    "outputs": {"supplier": "ACME", "total": 0},
    "submitted_by": {"id": "operator-7", "kind": "person"},
}


def _section(**overrides) -> ProjectUserSection:
    return ProjectUserSection.model_validate({**SECTION, **overrides})


def test_a_finished_run_shows_inputs_outputs_and_author() -> None:
    row = {"id": "j1", "status": "completed", "queued_at": "2026-11-18T10:00:00Z"}

    values = section_row_values(_section(), row, STORED)

    assert values == {
        "status": "completed",
        "sent": "2026-11-18T10:00:00Z",
        "author": {"id": "operator-7", "kind": "person"},
        "document": {
            "artifact_id": "art-1",
            "filename": "inv-77.pdf",
            "content_type": "application/pdf",
        },
        "skus": ["A-1", "B-2"],
        "supplier": "ACME",
        # A real zero stays zero: absent and zero are different.
        "total": 0,
    }


@pytest.mark.parametrize("status", ["running", "queued", "failed", "cancelled"])
def test_an_unfinished_or_failed_run_shows_no_result(status) -> None:
    values = section_row_values(_section(), {"id": "j1", "status": status}, STORED)

    assert "supplier" not in values
    assert "total" not in values
    assert values["status"] == status


def test_an_old_job_without_author_or_outputs_leaves_them_absent() -> None:
    values = section_row_values(
        _section(), {"id": "j1", "status": "completed"}, {"inputs": {}, "outputs": {}}
    )

    assert values == {"status": "completed"}


def test_secrets_and_storage_details_never_reach_the_table() -> None:
    section = _section(
        columns=[{"id": "key", "source": "input", "path": "api_key"}],
    )

    values = section_row_values(section, {"id": "j1", "status": "completed"}, STORED)

    assert values == {"key": "[secret]"}
    assert "s3://" not in repr(section_row_values(_section(), {"status": "completed"}, STORED))


def _bl(*sections: dict, payloads: dict | None = None) -> MagicMock:
    bl = MagicMock()
    bl.get_project.return_value = {
        "id": PROJECT,
        "account_id": ACCOUNT,
        "config": {"user_sections": list(sections), "other": 1},
    }
    bl.list_jobs_for_attention.return_value = [
        {"id": "j1", "project_id": PROJECT, "status": "completed"},
        {"id": "j2", "project_id": PROJECT, "status": "failed"},
    ]
    bl.count_jobs_for_attention.return_value = 2
    bl.read_job_section_payloads.return_value = payloads or {"j1": STORED, "j2": STORED}
    return bl


def _service(bl: MagicMock) -> JobQueryService:
    return JobQueryService(
        get_logger=lambda: bl,
        serialize_attention_job_row=lambda row: {**row, "job_id": row.get("id")},
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "running",
        build_runtime_payload_for_api=AsyncMock(return_value={}),
    )


def _scope(role: str = "operator", **fields) -> ScopeContext:
    return ScopeContext(account_id=ACCOUNT, actor_id=f"{role}-1", principal_type=role, **fields)


def test_section_history_uses_the_section_workflow_without_test_runs_in_rows_and_count():
    bl = _bl(SECTION)

    payload = _service(bl).list_jobs(project_id=PROJECT, section_id="invoices", scope=_scope())

    for call in (bl.list_jobs_for_attention, bl.count_jobs_for_attention):
        kwargs = call.call_args.kwargs
        assert kwargs["workflow_id"] == "wf.invoices"
        assert kwargs["exclude_task_source"] == "workflow_test_run"
    jobs = payload.dump()["jobs"]
    assert jobs[0]["section_values"]["supplier"] == "ACME"
    assert "supplier" not in jobs[1]["section_values"]
    assert jobs[0]["submitted_by"] == {"id": "operator-7", "kind": "person"}


def test_section_values_are_read_once_per_page_not_per_row():
    bl = _bl(SECTION)

    _service(bl).list_jobs(project_id=PROJECT, section_id="invoices", scope=_scope())

    bl.read_job_section_payloads.assert_called_once_with(job_ids=["j1", "j2"], account_id=ACCOUNT)
    bl.get_job.assert_not_called()


def test_the_plain_job_list_keeps_its_shape():
    bl = _bl(SECTION)

    jobs = _service(bl).list_jobs(project_id=PROJECT, scope=_scope()).dump()["jobs"]

    assert jobs[0].get("section_values") is None
    bl.read_job_section_payloads.assert_not_called()
    assert "exclude_task_source" not in bl.list_jobs_for_attention.call_args.kwargs


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"section_id": "invoices"}, ServiceValidationError),
        ({"project_id": PROJECT, "section_id": "missing"}, ServiceNotFoundError),
        ({"project_id": PROJECT, "section_id": "draft"}, ServiceNotFoundError),
        (
            {"project_id": PROJECT, "section_id": "invoices", "workflow_id": "wf.other"},
            ServiceValidationError,
        ),
        (
            {"project_id": PROJECT, "section_id": "invoices", "task_source": "workflow_test_run"},
            ServiceValidationError,
        ),
        (
            {"project_id": PROJECT, "section_id": "invoices", "group_by": "attention"},
            ServiceValidationError,
        ),
    ],
)
def test_section_history_refuses_what_it_cannot_answer(kwargs, error):
    bl = _bl(SECTION, HIDDEN)

    with pytest.raises(error):
        _service(bl).list_jobs(scope=_scope(), **kwargs)
    bl.list_jobs_for_attention.assert_not_called()


def test_an_admin_reads_the_history_of_a_hidden_section():
    bl = _bl(SECTION, HIDDEN)

    _service(bl).list_jobs(project_id=PROJECT, section_id="draft", scope=_scope("admin"))

    assert bl.list_jobs_for_attention.call_args.kwargs["workflow_id"] == "wf.invoices"


def test_a_token_limited_to_other_workflows_cannot_read_the_section():
    bl = _bl(SECTION)

    with pytest.raises(ServiceForbiddenError):
        _service(bl).list_jobs(
            project_id=PROJECT,
            section_id="invoices",
            scope=_scope(allowed_workflow_ids=frozenset({"wf.receipts"})),
        )
    bl.list_jobs_for_attention.assert_not_called()


def test_an_unlinked_workflow_keeps_its_section_history_readable():
    """ADR 0075: unlinking blocks new runs, not reading the history."""
    bl = _bl(SECTION)
    bl.get_project_workflows.return_value = []

    payload = _service(bl).list_jobs(project_id=PROJECT, section_id="invoices", scope=_scope())

    assert payload.dump()["total"] == 2
