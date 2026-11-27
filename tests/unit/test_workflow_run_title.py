"""Run titles (A56): a job says which run it is without opening its inputs.

A schedule's name or a webhook's description labels the runs it starts; a
workflow's ``settings.run_title`` renders over the run's inputs. The host writes
``meta.run_title`` once, when the job is created, and every job surface reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import core.infrastructure.runtime.control.scans_scheduled_jobs as scheduled_mod
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs import task_enqueue_runtime, task_scheduling
from core.application.management.query_service_runtime import (
    serialize_attention_job_row,
    serialize_awaiting_approval_job_row,
)
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext
from core.application.registry.workflow_contract import interface_from_definition
from core.application.reports.job_detail_contract_projection import _build_job_bucket
from core.application.reports.ui_projection import _build_header
from core.application.workflows.workflow_entrypoint_launch import WorkflowEntrypointLaunchService
from core.application.workflows.workflow_run_title import (
    RUN_LABEL_MAX_CHARS,
    RUN_TITLE_MAX_CHARS,
    compose_run_title,
    job_run_title,
    render_run_title,
    run_title_issues,
    stamp_run_title,
)
from core.infrastructure.runtime.control.config import ControlConfig


def _workflow(run_title: Any = "{{ inputs.site_url }}", **settings: Any) -> dict[str, Any]:
    return {
        "workflow_id": "scrape",
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": ["ui", "schedule"],
                "input_mode": "fields",
                "expected_inputs": [
                    {"name": "site_url", "type": "string", "required": True},
                    {"name": "run_name", "type": "string"},
                    {"name": "login", "type": "secret"},
                    {"name": "pages", "type": "integer"},
                ],
            },
            "run_title": run_title,
            **settings,
        },
    }


def _loader(row: Any):
    calls: list[tuple[str, str]] = []

    def load(workflow_id: str, account_id: str) -> Any:
        calls.append((workflow_id, account_id))
        return row

    load.calls = calls  # type: ignore[attr-defined]
    return load


# --- rendering ---------------------------------------------------------------


def test_render_substitutes_declared_inputs_as_plain_text() -> None:
    rendered = render_run_title(
        "{{ run_name }}{{inputs.run_name}} · {{ inputs.site_url }} ({{ inputs.pages }})",
        {"run_name": "Supplier A", "site_url": "https://a.example/x", "pages": 3},
    )
    # A placeholder that is not inputs.<name> renders as nothing; nothing is evaluated.
    assert rendered == "Supplier A · https://a.example/x (3)"


def test_render_never_shows_a_secret_input_reference_or_structured_value() -> None:
    rendered = render_run_title(
        "{{ inputs.login }}|{{ inputs.api_token }}|{{ inputs.ref }}|{{ inputs.obj }}|{{ inputs.site_url }}",
        {
            "login": "hunter2",
            "api_token": "tok-123",
            "ref": {"__type__": "secret_ref", "name": "SCRAPER_KEY"},
            "obj": {"a": 1},
            "site_url": "https://a.example",
        },
        secret_inputs={"login"},
    )
    assert "hunter2" not in rendered
    assert "tok-123" not in rendered
    assert "SCRAPER_KEY" not in rendered
    assert rendered == "||||https://a.example"


def test_render_collapses_whitespace_and_bounds_every_part() -> None:
    long_value = "word " * 100
    rendered = render_run_title(
        "{{ inputs.a }}\n\t{{ inputs.b }}", {"a": "x\x00y", "b": long_value}
    )
    assert "\n" not in rendered and "\x00" not in rendered
    assert len(rendered) <= RUN_TITLE_MAX_CHARS
    assert rendered.endswith("…")


def test_a_template_whose_values_are_all_empty_renders_nothing() -> None:
    assert render_run_title("{{ inputs.site_url }} · {{ inputs.run_name }}", {}) == ""
    assert render_run_title("", {"site_url": "x"}) == ""
    assert render_run_title("x" * 301, {}) == ""


def test_compose_joins_label_and_rendered_title() -> None:
    assert compose_run_title("Supplier A", "https://a.example") == "Supplier A · https://a.example"
    assert compose_run_title("", "https://a.example") == "https://a.example"
    assert compose_run_title("Nightly", "") == "Nightly"
    assert compose_run_title("Same", "Same") == "Same"
    assert compose_run_title("", "") == ""
    assert len(compose_run_title("L" * 500, "R" * 500)) <= RUN_LABEL_MAX_CHARS + RUN_TITLE_MAX_CHARS


# --- validation --------------------------------------------------------------


def _issues(template: Any, **settings: Any) -> list[str]:
    workflow = _workflow(template, **settings)
    return [
        issue["code"]
        for issue in run_title_issues(workflow["settings"], interface_from_definition(workflow))
    ]


def test_a_template_over_declared_inputs_and_plain_text_is_valid() -> None:
    assert _issues("{{ inputs.run_name }} · {{ inputs.site_url }}") == []
    assert _issues("Nightly import") == []
    assert _issues("") == []
    assert _issues(None) == []
    assert run_title_issues({}, None) == []


def test_validation_names_each_refused_placeholder() -> None:
    assert _issues("{{ inputs.login }}") == ["workflow.run_title.secret_input"]
    assert _issues("{{ inputs.api_key }}") == ["workflow.run_title.secret_input"]
    assert _issues("{{ inputs.region }}") == ["workflow.run_title.unknown_input"]
    assert _issues("{{ steps.fetch.output }}") == ["workflow.run_title.placeholder_invalid"]
    assert _issues(["not", "text"]) == ["workflow.run_title.not_text"]
    assert _issues("x" * 301) == ["workflow.run_title.too_long"]


def test_issues_point_at_the_setting() -> None:
    workflow = _workflow("{{ inputs.region }}")
    issue = run_title_issues(workflow["settings"], interface_from_definition(workflow))[0]
    assert issue["path"] == "workflow.settings.run_title"
    assert issue["severity"] == "error"


def test_passthrough_inputs_accept_any_name_but_never_a_secret_one() -> None:
    interface = SimpleNamespace(input_mode="passthrough", expected_inputs=[], input_schema={})
    assert run_title_issues({"run_title": "{{ inputs.anything }}"}, interface) == []
    codes = [i["code"] for i in run_title_issues({"run_title": "{{ inputs.password }}"}, interface)]
    assert codes == ["workflow.run_title.secret_input"]


def test_readiness_refuses_a_broken_run_title() -> None:
    from core.application.registry import contract_validation_workflow_readiness as readiness

    workflow = _workflow("{{ inputs.login }}")
    db = SimpleNamespace(get_workflow=lambda _workflow_id: workflow)
    result = readiness._validate_workflow_readiness(db, "scrape")
    assert "workflow.run_title.secret_input" in [issue["code"] for issue in result["errors"]]


# --- stamping a new job --------------------------------------------------------


def test_stamp_writes_label_and_rendered_title_and_drops_a_callers_title() -> None:
    load = _loader(_workflow())
    meta = stamp_run_title(
        {"run_title": "forged", "run_label": "  Supplier A\n"},
        workflow_id="scrape",
        account_id="account-1",
        inputs={"site_url": "https://a.example", "login": "hunter2"},
        load_workflow=load,
    )
    assert meta["run_label"] == "Supplier A"
    assert meta["run_title"] == "Supplier A · https://a.example"
    assert load.calls == [("scrape", "account-1")]


def test_stamp_without_label_or_template_adds_nothing() -> None:
    meta = stamp_run_title(
        {"run_title": "forged", "run_label": "   "},
        workflow_id="plain",
        account_id="",
        inputs={},
        load_workflow=_loader({"settings": {}}),
    )
    assert meta == {}


def test_a_title_never_blocks_a_run() -> None:
    def broken(_workflow_id: str, _account_id: str) -> Any:
        raise RuntimeError("registry down")

    meta = stamp_run_title(
        {"run_label": "Nightly"},
        workflow_id="scrape",
        account_id="",
        inputs={},
        load_workflow=broken,
    )
    assert meta == {"run_label": "Nightly", "run_title": "Nightly"}


def test_stamp_reads_only_the_registry_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.application.registry.registry_manager import RegistryManager

    monkeypatch.setattr(RegistryManager, "current", classmethod(lambda cls: None))
    meta = stamp_run_title({}, workflow_id="scrape", account_id="", inputs={"site_url": "x"})
    assert meta == {}

    store = SimpleNamespace(get_workflow=lambda _workflow_id: _workflow())
    monkeypatch.setattr(
        RegistryManager, "current", classmethod(lambda cls: SimpleNamespace(db_store=store))
    )
    meta = stamp_run_title({}, workflow_id="scrape", account_id="", inputs={"site_url": "x"})
    assert meta == {"run_title": "x"}


def test_enqueue_job_stamps_the_title_before_the_job_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        task_enqueue_runtime,
        "_enqueue_job_unadmitted",
        lambda **kwargs: captured.update(kwargs) or "job-1",
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_run_title._running_registry_workflow",
        lambda _workflow_id, _account_id: _workflow(),
    )

    job_id = task_enqueue_runtime.enqueue_job(
        task_id="t-1",
        task_summary="Workflow run: scrape",
        task_source="manual_ui",
        project_id="project-1",
        project_name="Demo",
        project_config={"account_id": DEFAULT_ACCOUNT_ID},
        extra_state={"workflow_id": "scrape", "inputs": {"site_url": "https://b.example"}},
        meta=None,
    )

    assert job_id == "job-1"
    assert captured["meta"]["run_title"] == "https://b.example"


def test_enqueue_job_leaves_meta_alone_when_nothing_names_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        task_enqueue_runtime,
        "_enqueue_job_unadmitted",
        lambda **kwargs: captured.update(kwargs) or "job-2",
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_run_title._running_registry_workflow",
        lambda _workflow_id, _account_id: None,
    )
    task_enqueue_runtime.enqueue_job(
        task_id="t-2",
        task_summary="Ad hoc",
        task_source="manual_ui",
        project_id="project-1",
        project_name="Demo",
        project_config={},
        extra_state={"workflow_id": "plain"},
        meta=None,
    )
    # No title; only who started the run, which every job names.
    assert set(captured["meta"]) == {"submitted_by"}


def test_enqueue_scheduled_job_stamps_the_schedule_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        task_scheduling,
        "_enqueue_scheduled_job_unadmitted",
        lambda **kwargs: captured.update(kwargs) or {"status": "enqueued"},
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_run_title._running_registry_workflow",
        lambda _workflow_id, _account_id: _workflow(),
    )
    task_scheduling.enqueue_scheduled_job(
        task_source="schedule",
        workflow_id="scrape",
        account_id="account-1",
        extra_state={"workflow_id": "scrape", "inputs": {"site_url": "https://c.example"}},
        meta={"run_label": "Supplier C", "scheduled_job_id": "s-1"},
    )
    assert captured["meta"]["run_title"] == "Supplier C · https://c.example"
    assert captured["meta"]["scheduled_job_id"] == "s-1"


def _cfg() -> ControlConfig:
    return ControlConfig(
        watchdog_enabled=True,
        watchdog_interval=30.0,
        watchdog_auto_apply=True,
        watchdog_stuck_seconds=120,
        watchdog_orphan_grace_seconds=120,
        watchdog_pickup_grace_seconds=45,
        watchdog_max_journal_entries=200,
        scheduled_jobs_enabled=True,
        scheduled_jobs_interval=30.0,
        scheduled_jobs_batch_limit=20,
        dashboard_rollup_enabled=True,
        dashboard_rollup_interval=300.0,
        dashboard_rollup_backfill_days=180,
        dashboard_rollup_lookback_days=3,
        security_posture_enabled=True,
        security_posture_interval=600.0,
        workflow_knowledge_sync_enabled=True,
        workflow_knowledge_sync_interval=120.0,
        workflow_knowledge_sync_changelog_limit=200,
    )


@pytest.mark.parametrize(("name", "expected"), [("Supplier A", "Supplier A"), ("", None)])
def test_a_scheduled_run_carries_its_schedule_name_as_run_label(name: str, expected: Any) -> None:
    captured: dict[str, Any] = {}
    now = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(get_project_run_config=lambda _project_id: {"project_name": "Demo"}),
        {
            "id": "schedule-1",
            "project_id": "project-1",
            "account_id": "account-1",
            "workflow_id": "scrape",
            "name": name,
            "inputs": {"site_url": "https://a.example"},
            "cron_expr": "* * * * *",
            "next_run_at": now,
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
        },
        _cfg(),
        now_utc=now,
        _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        _enqueue_scheduled_job_fn=lambda _bl, **kwargs: (
            captured.update(kwargs) or {"status": "enqueued", "job_id": "job-1"}
        ),
        _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
    )
    assert captured["meta"].get("run_label") == expected


@pytest.mark.parametrize(
    ("description", "expected"), [(" Shop orders ", "Shop orders"), ("", None)]
)
def test_a_webhook_run_carries_the_webhook_description_as_run_label(
    description: str, expected: Any
) -> None:
    captured: dict[str, Any] = {}
    service = WorkflowEntrypointLaunchService(
        enqueue_workflow_start=lambda **kwargs: captured.update(kwargs) or "job-9",
        now_iso=lambda: "2026-11-14T10:00:00Z",
    )
    service.launch(
        entry_key="orders",
        task_id="webhook:orders:r-1",
        project_id="project-1",
        run_config={},
        workflow_id="scrape",
        request_id="r-1",
        idempotency_key="",
        workflow_inputs={"site_url": "https://a.example"},
        workflow_interface={},
        files=[],
        request_context={},
        idempotency_identities=[],
        primary_idempotency_identity={},
        entry_label=description,
    )
    assert captured["job_meta"].get("run_label") == expected


# --- reading the title -------------------------------------------------------------


def test_job_run_title_reads_a_column_or_meta() -> None:
    assert job_run_title({"run_title": "A"}) == "A"
    assert job_run_title({"meta": {"run_title": "B"}}) == "B"
    assert job_run_title({"meta": '{"run_title": "C"}'}) == "C"
    assert job_run_title({"meta": "not json"}) == ""
    assert job_run_title(None) == ""


def _identity(value: Any, **_kwargs: Any) -> str:
    return str(value or "")


def test_jobs_list_row_shows_the_run_title_instead_of_the_generated_summary() -> None:
    row = {
        "id": "job-1",
        "status": "completed",
        "task_summary": "Workflow run: scrape",
        "task_summary_key": "react.jobs.generated_workflow_run_summary",
        "task_summary_params": {"workflow_id": "scrape"},
        "run_title": "Supplier A · https://a.example",
    }
    payload = serialize_attention_job_row(
        row, truncate_text_fn=_identity, build_display_summary_fn=lambda value: f"short {value}"
    )
    assert payload["run_title"] == "Supplier A · https://a.example"
    assert payload["display_summary"] == "Supplier A · https://a.example"
    assert payload["task_summary_key"] is None

    untitled = serialize_attention_job_row(
        {**row, "run_title": None},
        truncate_text_fn=_identity,
        build_display_summary_fn=lambda value: f"short {value}",
    )
    assert untitled["run_title"] is None
    assert untitled["task_summary_key"] == "react.jobs.generated_workflow_run_summary"


def test_approvals_queue_row_shows_the_run_title() -> None:
    payload = serialize_awaiting_approval_job_row(
        {"id": "job-1", "task_summary": "Workflow run: scrape", "run_title": "Supplier A"},
        truncate_text_fn=_identity,
        build_display_summary_fn=_identity,
    )
    assert payload["display_summary"] == "Supplier A"
    assert payload["run_title"] == "Supplier A"


def test_job_page_payload_and_header_carry_the_run_title() -> None:
    job = {
        "id": "job-1",
        "task_summary": "Workflow run: scrape",
        "meta": {"run_title": "Supplier A"},
    }
    bucket = _build_job_bucket(job=job, status_payload={}, runtime={}, lifecycle={})
    assert bucket["run_title"] == "Supplier A"
    assert bucket["display_summary"] == "Supplier A"
    header = _build_header(job, "Demo")
    assert header["task_title"] == "Supplier A"
    assert header["run_title"] == "Supplier A"


def test_child_invocation_links_carry_the_child_run_title() -> None:
    link = {
        "child_job_id": "child",
        "child_workflow_id": "client",
        "step_id": "invoke",
        "step_execution_id": "execution-1",
        "invocation_key": "parent:execution-1",
    }
    jobs = {
        "parent": {
            "id": "parent",
            "account_id": DEFAULT_ACCOUNT_ID,
            "selected_workflow": "author",
            "meta": {"invoke_children": {"child": link}},
        },
        "child": {
            "id": "child",
            "account_id": DEFAULT_ACCOUNT_ID,
            "selected_workflow": "client",
            "meta": {
                "invoke_parent_job_id": "parent",
                "invoke_parent_step_id": "invoke",
                "run_title": "https://b.example",
            },
        },
    }
    store = SimpleNamespace(get_job=lambda job_id: jobs.get(str(job_id)))
    service = JobQueryService(
        get_logger=lambda: store,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda *_args: "completed",
        build_runtime_payload_for_api=lambda *_args: {},
    )
    result = service.get_child_invocations(
        "parent", scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    )
    assert result["invocations"][0]["child_run_title"] == "https://b.example"


def test_a_restart_keeps_the_run_label_of_the_run_it_repeats() -> None:
    import inspect

    from core.application.workflows import serve_workflow_helpers

    source = inspect.getsource(serve_workflow_helpers)
    assert '"run_label",' in source


# --- schedule names ------------------------------------------------------------------


def test_a_schedule_name_is_one_trimmed_line_and_refused_when_too_long() -> None:
    from core.application.management.command_services_project_run_requests_mixin import (
        SCHEDULE_NAME_MAX_CHARS,
        _schedule_name,
    )
    from core.application.management.errors import ServiceValidationError

    assert _schedule_name("  Supplier\tA\n daily ") == "Supplier A daily"
    assert _schedule_name(None) == ""
    with pytest.raises(ServiceValidationError):
        _schedule_name("x" * (SCHEDULE_NAME_MAX_CHARS + 1))


def test_the_schedule_api_accepts_and_bounds_a_name() -> None:
    from pydantic import ValidationError

    from core.entrypoints.admin.project_schedules import (
        ApiScheduledJobCreateRequest,
        ApiScheduledJobUpdateRequest,
    )

    created = ApiScheduledJobCreateRequest(workflow_id="scrape", cron_expr="0 9 * * *", name="A")
    assert created.name == "A"
    assert ApiScheduledJobCreateRequest(workflow_id="scrape", cron_expr="0 9 * * *").name == ""
    assert ApiScheduledJobUpdateRequest(workflow_id="scrape", cron_expr="0 9 * * *").name is None
    with pytest.raises(ValidationError):
        ApiScheduledJobCreateRequest(workflow_id="scrape", cron_expr="0 9 * * *", name="x" * 121)


def test_editing_a_schedule_keeps_its_name_unless_a_new_one_is_given() -> None:
    from core.application.management.command_models import (
        ProjectScheduledJobCreateInput,
        ProjectScheduledJobUpdateInput,
    )

    # update_scheduled_job overlays the update's non-None fields on the stored row.
    current = {"workflow_id": "scrape", "cron_expr": "0 9 * * *", "name": "Supplier A"}
    values = {
        name: current[name]
        for name in ProjectScheduledJobCreateInput.model_fields
        if name in current
    }
    update = ProjectScheduledJobUpdateInput(workflow_id="scrape", cron_expr="0 10 * * *")
    values.update({name: value for name, value in update.dump().items() if value is not None})
    assert ProjectScheduledJobCreateInput(**values).name == "Supplier A"


def test_run_title_failure_keeps_internal_loader_details_out_of_logs(caplog):
    def unavailable_workflow(_workflow_id, _account_id):
        raise RuntimeError("example workflow storage diagnostic")

    with caplog.at_level("DEBUG", logger="core.application.workflows.workflow_run_title"):
        meta = stamp_run_title(
            {"run_label": "Keep this label"},
            workflow_id="example-workflow",
            account_id="example-account",
            inputs={},
            load_workflow=unavailable_workflow,
        )

    assert meta["run_title"] == "Keep this label"
    assert any("Run title not rendered" in record.getMessage() for record in caplog.records)
    assert "example workflow storage diagnostic" not in caplog.text
