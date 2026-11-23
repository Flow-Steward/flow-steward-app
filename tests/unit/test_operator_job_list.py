"""A workflow's operator jobs list: column values and configured filters (ADR 0075)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext
from core.application.projects import operator_sections
from core.application.registry.workflow_operator_form import OperatorListColumn
from core.application.registry.workflow_operator_list import operator_row_values
from core.application.workflows.workflow_start_runtime import build_filter_values
from core.infrastructure.db.job_queries_store import _build_attention_where

ACCOUNT = "acc-1"
PROJECT = "00000000-0000-0000-0000-000000000007"
WORKFLOW = "wf.invoices"

COLUMNS = [
    {"id": "status", "source": "job", "path": "status"},
    {"id": "sent", "source": "job", "path": "created_at"},
    {"id": "author", "source": "job", "path": "submitted_by"},
    {"id": "document", "source": "input", "path": "document", "format": "file_links"},
    {"id": "skus", "source": "input", "path": "lines[].sku", "format": "list"},
    {"id": "supplier", "source": "output", "path": "supplier"},
    {"id": "total", "source": "output", "path": "total", "format": "currency"},
]
FILTERS = [
    {"id": "tags", "source": "input", "path": "tags", "type": "choice_multi"},
    {"id": "kind", "source": "input", "path": "kind", "type": "choice"},
    {"id": "total", "source": "output", "path": "total", "type": "number_range"},
    {"id": "signed", "source": "input", "path": "signed_on", "type": "date_range"},
    {"id": "urgent", "source": "input", "path": "urgent", "type": "boolean"},
    {"id": "note", "source": "input", "path": "note", "type": "search"},
    {"id": "state", "source": "job", "path": "status", "type": "choice_multi"},
]
TAG_OPTIONS = [f"t{index}" for index in range(1, 41)]
CALLABLE = {
    "enabled": True,
    "trigger_channels": ["ui"],
    "expected_inputs": [
        {"name": "document", "type": "file"},
        {
            "name": "lines",
            "type": "array",
            "items": {"type": "object", "properties": [{"name": "sku", "type": "string"}]},
        },
        {"name": "tags", "type": "array", "items": {"type": "select", "options": TAG_OPTIONS}},
        {"name": "kind", "type": "select", "options": ["contract", "invoice"]},
        {"name": "signed_on", "type": "string", "format": "date"},
        {"name": "urgent", "type": "boolean"},
        {"name": "note", "type": "paragraph"},
        {"name": "api_key", "type": "secret"},
    ],
    "expected_outputs": [
        {"name": "supplier", "type": "string"},
        {"name": "total", "type": "number"},
    ],
}

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
        "api_key": {"secret_ref": "sec-9"},  # pragma: allowlist secret
    },
    "outputs": {"supplier": "ACME", "total": 0},
    "submitted_by": {"id": "operator-7", "kind": "person"},
}


def _columns(rows: list[dict[str, Any]] | None = None) -> list[OperatorListColumn]:
    return [OperatorListColumn.model_validate(row) for row in rows or COLUMNS]


# --- Column values ----------------------------------------------------------------


def test_a_finished_run_shows_inputs_outputs_and_author() -> None:
    row = {"id": "j1", "status": "completed", "queued_at": "2026-11-18T10:00:00Z"}

    values = operator_row_values(_columns(), row, STORED)

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
    values = operator_row_values(_columns(), {"id": "j1", "status": status}, STORED)

    assert "supplier" not in values
    assert "total" not in values
    assert values["status"] == status


def test_an_old_job_without_author_or_outputs_leaves_them_absent() -> None:
    values = operator_row_values(
        _columns(), {"id": "j1", "status": "completed"}, {"inputs": {}, "outputs": {}}
    )

    assert values == {"status": "completed"}


def test_the_accepted_request_projection_shows_a_file_by_its_name() -> None:
    """Jobs keep a display-safe projection of their inputs (meta.invocation.mapped_inputs)."""
    projected = {
        "inputs": {
            "company": "Acme OÜ",
            "document": {
                "sha256": "abc",
                "filename": "inv-1.pdf",
                "size_bytes": 10,
                "content_type": "application/pdf",
            },
        },
    }

    values = operator_row_values(_columns(), {"id": "j1", "status": "running"}, projected)

    assert values["document"] == {
        "filename": "inv-1.pdf",
        "content_type": "application/pdf",
        "size_bytes": 10,
    }


def test_file_columns_carry_the_stored_artifact_for_download() -> None:
    columns = _columns(
        [
            {"id": "document", "source": "input", "path": "document", "format": "file_links"},
            {"id": "report", "source": "output", "path": "report_pdf", "format": "file_links"},
        ]
    )
    stored = {
        "inputs": {"document": {"sha256": "abc", "filename": "inv-1.pdf", "size_bytes": 10}},
        "input_files": [{"filename": "inv-1.pdf", "artifact_id": "art-in"}],
        "outputs": {},
        "output_files": {"report_pdf": {"artifact_id": "art-out", "filename": "report.pdf"}},
    }

    values = operator_row_values(columns, {"id": "j1", "status": "completed"}, stored)

    assert values["document"]["artifact_id"] == "art-in"
    assert values["report"] == {"artifact_id": "art-out", "filename": "report.pdf"}


def test_several_files_of_one_input_each_get_their_artifact() -> None:
    columns = _columns([{"id": "files", "source": "input", "path": "files"}])
    stored = {
        "inputs": {"files": [{"filename": "p1.jpg"}, {"filename": "p2.jpg"}]},
        "input_files": [
            {"filename": "p1.jpg", "artifact_id": "a1"},
            {"filename": "p2.jpg", "artifact_id": "a2"},
        ],
    }

    values = operator_row_values(columns, {"status": "running"}, stored)

    assert [item["artifact_id"] for item in values["files"]] == ["a1", "a2"]


def test_pages_with_the_same_filename_each_get_their_own_artifact() -> None:
    """Stored files name their input in upload order; the n-th page is the n-th copy."""
    columns = _columns([{"id": "pages", "source": "input", "path": "pages"}])
    stored = {
        "inputs": {
            "pages": [{"filename": "scan.jpg"}, {"filename": "scan.jpg"}, {"filename": "x.jpg"}],
            "cover": {"filename": "scan.jpg"},
        },
        "input_files": [
            {"filename": "scan.jpg", "artifact_id": "cover-1", "field_name": "cover"},
            {"filename": "scan.jpg", "artifact_id": "a1", "field_name": "pages"},
            {"filename": "scan.jpg", "artifact_id": "a2", "field_name": "pages"},
            {"filename": "x.jpg", "artifact_id": "a3", "field_name": "pages"},
        ],
    }

    values = operator_row_values(
        [*columns, *_columns([{"id": "cover", "source": "input", "path": "cover"}])],
        {"status": "running"},
        stored,
    )

    assert [item["artifact_id"] for item in values["pages"]] == ["a1", "a2", "a3"]
    assert values["cover"]["artifact_id"] == "cover-1"


def test_an_older_run_without_input_names_uses_each_stored_copy_once() -> None:
    columns = _columns([{"id": "pages", "source": "input", "path": "pages"}])
    stored = {
        "inputs": {"pages": [{"filename": "scan.jpg"}, {"filename": "scan.jpg"}]},
        "input_files": [
            {"filename": "scan.jpg", "artifact_id": "a1"},
            {"filename": "scan.jpg", "artifact_id": "a2"},
        ],
    }

    values = operator_row_values(columns, {"status": "running"}, stored)

    assert [item["artifact_id"] for item in values["pages"]] == ["a1", "a2"]


def test_secrets_and_storage_details_never_reach_the_table() -> None:
    columns = _columns([{"id": "key", "source": "input", "path": "api_key"}])

    values = operator_row_values(columns, {"id": "j1", "status": "completed"}, STORED)

    assert values == {"key": "[secret]"}
    assert "s3://" not in repr(operator_row_values(_columns(), {"status": "completed"}, STORED))


# --- The jobs list in operator view ---------------------------------------------------------


def _workflow_row(**settings: Any) -> dict[str, Any]:
    return {
        "workflow_id": WORKFLOW,
        "display_name": "Invoices",
        "account_id": ACCOUNT,
        "revision": 5,
        "enabled": True,
        "settings": {
            "callable": CALLABLE,
            "operator": {
                "published": True,
                "list": {"columns": COLUMNS, "filters": FILTERS},
                **settings,
            },
        },
    }


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    rows = {WORKFLOW: _workflow_row()}

    def load(workflow_id: str):
        row = rows.get(workflow_id)
        return operator_sections.operator_workflow_from_row(row) if row else None

    monkeypatch.setattr(operator_sections, "load_operator_workflow", load)
    return rows


def _bl(payloads: dict | None = None) -> MagicMock:
    bl = MagicMock()
    bl.get_project.return_value = {"id": PROJECT, "account_id": ACCOUNT, "config": {}}
    bl.list_jobs_for_attention.return_value = [
        {"id": "j1", "project_id": PROJECT, "status": "completed"},
        {"id": "j2", "project_id": PROJECT, "status": "failed"},
    ]
    bl.count_jobs_for_attention.return_value = 2
    bl.read_job_operator_payloads.return_value = payloads or {"j1": STORED, "j2": STORED}
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


def _list(bl: MagicMock, **kwargs: Any):
    params: dict[str, Any] = {"project_id": PROJECT, "workflow_id": WORKFLOW, "operator_view": True}
    params.update(kwargs)
    scope = params.pop("scope", None) or _scope()
    return _service(bl).list_jobs(scope=scope, **params)


def test_the_operator_list_shows_one_workflow_without_test_runs_in_rows_and_count():
    bl = _bl()

    payload = _list(bl)

    for call in (bl.list_jobs_for_attention, bl.count_jobs_for_attention):
        kwargs = call.call_args.kwargs
        assert kwargs["workflow_id"] == WORKFLOW
        assert kwargs["project_id"] == PROJECT
        assert kwargs["exclude_task_source"] == "workflow_test_run"
        assert "value_filters" not in kwargs
    jobs = payload.dump()["jobs"]
    assert jobs[0]["operator_values"]["supplier"] == "ACME"
    assert "supplier" not in jobs[1]["operator_values"]
    assert jobs[0]["submitted_by"] == {"id": "operator-7", "kind": "person"}


def test_column_values_are_read_once_per_page_not_per_row():
    bl = _bl()

    _list(bl)

    bl.read_job_operator_payloads.assert_called_once_with(job_ids=["j1", "j2"], account_id=ACCOUNT)
    bl.get_job.assert_not_called()


def test_the_plain_job_list_keeps_its_shape():
    bl = _bl()

    jobs = _service(bl).list_jobs(project_id=PROJECT, scope=_scope()).dump()["jobs"]

    assert jobs[0].get("operator_values") is None
    bl.read_job_operator_payloads.assert_not_called()
    assert "exclude_task_source" not in bl.list_jobs_for_attention.call_args.kwargs


def _value_filters(bl: MagicMock) -> list[dict[str, Any]]:
    listed = bl.list_jobs_for_attention.call_args.kwargs["value_filters"]
    assert bl.count_jobs_for_attention.call_args.kwargs["value_filters"] == listed
    return listed


def test_configured_filters_reach_the_query_with_checked_values():
    bl = _bl()
    requested = {
        "tags": ["t30", "t2"],
        "kind": "invoice",
        "total": {"min": 10, "max": "250.5"},
        "signed": {"from": "2026-01-01"},
        "urgent": True,
        "note": "  late   delivery ",
        "state": ["completed", "failed"],
    }

    _list(bl, filters=json.dumps(requested))

    assert _value_filters(bl) == [
        {
            "id": "tags",
            "source": "input",
            "key": "tags",
            "type": "choice_multi",
            "value": ("t30", "t2"),
        },
        {"id": "kind", "source": "input", "key": "kind", "type": "choice", "value": ("invoice",)},
        {
            "id": "total",
            "source": "output",
            "key": "total",
            "type": "number_range",
            "value": (10.0, 250.5),
        },
        {
            "id": "signed",
            "source": "input",
            "key": "signed_on",
            "type": "date_range",
            "value": ("2026-01-01", None),
        },
        {"id": "urgent", "source": "input", "key": "urgent", "type": "boolean", "value": True},
        {
            "id": "note",
            "source": "input",
            "key": "note",
            "type": "search",
            "value": "late delivery",
        },
        {
            "id": "state",
            "source": "job",
            "key": "status",
            "type": "choice_multi",
            "value": ("completed", "failed"),
        },
    ]


def test_a_null_filter_value_means_not_filtered():
    bl = _bl()

    _list(bl, filters=json.dumps({"tags": None}))

    assert "value_filters" not in bl.list_jobs_for_attention.call_args.kwargs


@pytest.mark.parametrize(
    "requested",
    [
        {"ghost": "x"},
        {"meta.filter_values.tags": ["t1"]},
        {"tags": ["unknown-option"]},
        {"kind": ["contract", "invoice"]},
        {"tags": []},
        {"total": {"min": "ten"}},
        {"total": {}},
        {"total": {"min": 1, "above": 2}},
        {"signed": {"from": "01.02.2026"}},
        {"urgent": "maybe"},
        {"note": ""},
        {"state": ["exploded"]},
    ],
)
def test_unknown_filters_and_unfit_values_are_refused_with_422(requested):
    bl = _bl()

    with pytest.raises(ServiceValidationError) as refused:
        _list(bl, filters=json.dumps(requested))

    assert refused.value.status_code == 422
    bl.list_jobs_for_attention.assert_not_called()


def test_an_unknown_filter_names_itself_in_the_error():
    with pytest.raises(ServiceValidationError) as refused:
        _list(_bl(), filters=json.dumps({"ghost": "x"}))

    assert refused.value.payload["code"] == "unknown_filter"
    assert refused.value.payload["filter_id"] == "ghost"


def test_a_filters_parameter_must_be_a_json_object():
    with pytest.raises(ServiceValidationError) as refused:
        _list(_bl(), filters="[1, 2]")

    assert refused.value.status_code == 422


def test_filters_need_the_operator_view():
    with pytest.raises(ServiceValidationError):
        _service(_bl()).list_jobs(
            project_id=PROJECT, workflow_id=WORKFLOW, filters='{"tags": ["t1"]}', scope=_scope()
        )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"project_id": ""}, ServiceValidationError),
        ({"workflow_id": ""}, ServiceValidationError),
        ({"workflow_id": "wf.missing"}, ServiceNotFoundError),
        ({"task_source": "workflow_test_run"}, ServiceValidationError),
        ({"group_by": "attention"}, ServiceValidationError),
        (
            {"scope": _scope(allowed_workflow_ids=frozenset({"wf.receipts"}))},
            ServiceForbiddenError,
        ),
    ],
)
def test_the_operator_list_refuses_what_it_cannot_answer(kwargs, error):
    bl = _bl()

    with pytest.raises(error):
        _list(bl, **kwargs)
    bl.list_jobs_for_attention.assert_not_called()


def test_another_accounts_workflow_is_refused(_registry):
    _registry[WORKFLOW] = {**_workflow_row(), "account_id": "acc-other"}
    bl = _bl()

    with pytest.raises(ServiceForbiddenError):
        _list(bl)
    bl.list_jobs_for_attention.assert_not_called()


def test_an_unpublished_or_unlinked_workflow_keeps_its_history_readable(_registry):
    """ADR 0075: unpublishing or unlinking blocks new runs, not reading the history."""
    _registry[WORKFLOW] = _workflow_row(published=False)
    bl = _bl()
    bl.get_project_workflows.return_value = []

    payload = _list(bl)

    assert payload.dump()["total"] == 2


# --- Full values for filters ----------------------------------------------------------------


def test_filter_values_keep_long_text_and_long_lists_whole():
    note = "x" * 2100 + " needle"
    tags = [f"t{index}" for index in range(1, 31)]

    values = build_filter_values({"note": note, "tags": tags, "total": 12.5, "urgent": False})

    assert values["note"] == note
    assert values["tags"][29] == "t30"
    assert values["total"] == 12.5
    assert values["urgent"] is False


def test_filter_values_never_hold_secrets_files_or_objects():
    from core.infrastructure.security.secret_reference import SecretReference

    reference = SecretReference(name="mail_login", ciphertext="cipher").to_wire()

    values = build_filter_values(
        {
            "api_token": "plain-token",
            "password": "hunter2",  # pragma: allowlist secret
            "mail_login": reference,
            "customer_code": "declared",
            "document": {"filename": "a.pdf", "artifact_id": "art"},
            "files": [{"filename": "a.pdf"}],
            "lines": [{"sku": "A"}],
            "region": "eu",
        },
        declared_secret_inputs={"customer_code"},
    )

    assert values == {"region": "eu"}
    assert "cipher" not in json.dumps(values)


def test_every_accepted_run_writes_filter_values_next_to_the_invocation():
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

    enqueue_task = MagicMock(return_value="job-1")
    note = "y" * 3000 + " needle"

    enqueue_workflow_start(
        enqueue_task_fn=enqueue_task,
        task_id="task-1",
        task_summary="Run",
        task_source="manual_ui",
        project_id=PROJECT,
        project_name="p",
        project_config={},
        selected_workflow_id=WORKFLOW,
        workflow_selection_source="explicit_request",
        entry_path="manual_ui",
        input_contract="direct_inputs",
        source_system="ui",
        workflow_secret_ref_fingerprints=[],
        inputs={"note": note, "tags": TAG_OPTIONS[:30], "password": "hunter2"},
        job_meta={"filter_values": {"forged": True}},
    )

    meta = enqueue_task.call_args.kwargs["meta"]
    assert meta["filter_values"] == {"note": note, "tags": TAG_OPTIONS[:30]}
    assert "filter_values" not in meta["invocation"]
    # The display projection stays bounded; the filter values do not.
    assert meta["invocation"]["mapped_inputs"]["note"] != note
    assert "hunter2" not in json.dumps(meta)


# --- SQL ----------------------------------------------------------------------------------


def _where(*filters: dict[str, Any]) -> tuple[str, list[Any]]:
    return _build_attention_where(
        column_exists=lambda *_args: True,
        project_id=PROJECT,
        project_ids=None,
        status=None,
        workflow_id=WORKFLOW,
        search=None,
        account_id=ACCOUNT,
        include_trashed=False,
        exclude_task_source="workflow_test_run",
        value_filters=list(filters),
    )


def test_input_filters_read_full_values_and_output_filters_read_outputs():
    sql, params = _where(
        {"source": "input", "key": "note", "type": "search", "value": "50%_off"},
        {"source": "output", "key": "total", "type": "number_range", "value": (1.0, None)},
    )

    assert "j.meta -> 'filter_values'" in sql
    assert "j.meta -> 'workflow_outputs' -> 'outputs'" in sql
    assert "mapped_inputs" not in sql
    assert "'note'" not in sql and "'total'" not in sql, "names are parameters"
    assert "%50\\%\\_off%" in params
    assert params[-3:] == ["total", "total", 1.0]


def test_choice_filters_match_a_select_or_any_element_of_a_multi_select():
    sql, params = _where(
        {"source": "input", "key": "tags", "type": "choice_multi", "value": ("t30",)}
    )

    assert "?| %s::text[]" in sql
    assert "= ANY(%s::text[])" in sql
    assert params[-5:] == ["tags", "tags", ["t30"], "tags", ["t30"]]


def test_boolean_date_and_status_filters():
    sql, params = _where(
        {"source": "input", "key": "urgent", "type": "boolean", "value": False},
        {
            "source": "input",
            "key": "signed_on",
            "type": "date_range",
            "value": ("2026-01-01", "2026-12-31"),
        },
        {"source": "job", "key": "status", "type": "choice", "value": ("failed",)},
    )

    assert "= %s::jsonb" in sql
    assert "LEFT(" in sql
    assert "LOWER(COALESCE(j.status, '')) = ANY(%s)" in sql
    assert params[-9:] == [
        "urgent",
        "false",
        "signed_on",
        "signed_on",
        "2026-01-01",
        "signed_on",
        "signed_on",
        "2026-12-31",
        ["failed"],
    ]
