"""Approval with typed review fields (ADR 0075, item 9).

The person sees initial values captured when the gate opened, may correct the
editable ones, and on approve the checked values become the step's typed
outputs that later steps map.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_WORKSPACE_ID
from core.application.jobs.public_approval_serializer import serialize_approval_history_entry
from core.application.management.approval_decision_service import approval_decision_fingerprint
from core.application.management.approval_repository import (
    ApprovalRepository,
    build_approval_resume_value,
    checked_review_decision,
)
from core.application.management.command_models_api_inputs import ApprovalDecisionInput
from core.application.management.command_services_approval import ApprovalCommandService
from core.application.management.errors import ServiceConflictError, ServiceError
from core.application.management.query_services_approval import ApprovalQueryService
from core.application.management.scope import ScopeContext
from core.application.orchestration.agent_messages import build_agent_message
from core.application.orchestration.graph.builder_approval_gate import (
    _registry_approval_gate_node,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _build_state_based_approval_update,
)
from core.application.orchestration.invoke_workflow_step_node_common import (
    _resolve_invoke_input,
)
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION, RunCursor, begin_step
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.workflows.approval_review import (
    REVIEW_VALUES_MAX_BYTES,
    ReviewDecisionError,
    decide_review,
    open_review,
    public_review,
    review_config_issues,
    review_source_issues,
)
from core.application.workflows.workflow_definition_snapshot import definition_digest
from core.application.workflows.workflow_portability_rewrite import (
    WorkflowPortabilityRewriteMixin,
)
from core.application.workflows.workflow_step_config_contracts import validate_step_config_keys
from tests._workflow_definitions import edge, node

LONG_TEXT = "Clause. " * 400  # well over the 1000-character content limit

REVIEW = {
    "original": "state.files",
    "fields_from_workflow": "accounting_export",
    "fields": [
        {
            "name": "supplier",
            "type": "string",
            "required": True,
            "editable": True,
            "from": "message.step.recognize.artifacts.supplier",
        },
        {
            "name": "total",
            "type": "number",
            "required": True,
            "editable": True,
            "from": "message.step.recognize.artifacts.total",
        },
        {
            "name": "document_date",
            "type": "string",
            "format": "date",
            "editable": True,
            "from": "message.step.recognize.artifacts.document_date",
        },
        {
            "name": "lines",
            "type": "array",
            "editable": True,
            "from": "message.step.recognize.artifacts.lines",
            "items": {
                "type": "object",
                "properties": [
                    {"name": "item", "type": "string"},
                    {"name": "qty", "type": "number"},
                ],
            },
        },
        {"name": "notes", "editable": True, "from": "message.step.recognize.artifacts.notes"},
        {
            "name": "confidence",
            "type": "number",
            "editable": False,
            "from": "message.step.recognize.artifacts.confidence",
        },
    ],
}


@pytest.fixture(autouse=True)
def _isolated_definitions():
    clear_run_definition_cache()
    yield
    clear_run_definition_cache()


def _definition(approval: dict) -> dict:
    return {
        "steps": [
            {"step_id": "recognize", "step_kind": "transform"},
            {
                "step_id": "check",
                "step_kind": "approval",
                "display_name": "Check before accounting",
                "override": {"approval": approval},
            },
            {"step_id": "export", "step_kind": "invoke_workflow"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "execution_graph": {
            "schema_version": 1,
            "entry": node("recognize"),
            "edges": [
                edge("recognize", node("check")),
                edge("check", node("export"), "approved"),
                edge("check", node("recognize"), "rejected", rework=True),
                edge("export", node("done")),
            ],
        },
        "settings": {},
    }


def _recognized(**artifacts: Any) -> dict:
    return build_agent_message(
        from_agent="reader",
        to_agent="orchestrator",
        skill="recognize",
        status="pass",
        correlation_id=uuid.uuid4().hex,
        artifacts=artifacts,
        summary="recognized",
        published_at=datetime.now(UTC).isoformat(),
        step_id="recognize",
        step_execution_id=str(uuid.uuid4()),
        attempt=1,
    )


def _state(approval: dict | None = None, **overrides: Any) -> dict:
    definition = _definition(approval if approval is not None else {"review": REVIEW})
    ref = {
        "snapshot_id": f"test:{uuid.uuid4()}",
        "revision": 1,
        "digest": definition_digest(definition),
    }
    prime_run_definition(ref, definition)
    cursor = begin_step(RunCursor(), "check")
    state = {
        "task_id": "task-1",
        "task_summary": "Invoice",
        "status": "in_progress",
        "workflow_id": "wf",
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "run_cursor": cursor.dump(),
        "current_step_id": "check",
        "needs_user_approval": True,
        "user_approved": None,
        "review_feedback": None,
        "files": [{"artifact_id": "art-1", "filename": "invoice.pdf"}],
        "agent_messages": [
            _recognized(
                supplier="ACME",
                total="31,85",
                document_date="2026-11-03",
                lines=[{"item": "Paper", "qty": "2"}],
                notes=LONG_TEXT,
                confidence=0.62,
            )
        ],
    }
    state.update(overrides)
    return state


def _gate(state: dict, response: Any) -> tuple[Any, dict]:
    with patch("langgraph.types.interrupt", return_value=response) as interrupted:
        result = _registry_approval_gate_node(state)
    return result, interrupted.call_args.args[0]


def _opened_payload(state: dict | None = None) -> dict:
    with (
        patch("langgraph.types.interrupt", side_effect=RuntimeError("opened")) as interrupted,
        pytest.raises(RuntimeError, match="opened"),
    ):
        _registry_approval_gate_node(state or _state())
    return interrupted.call_args.args[0]


def _decision(**overrides: Any) -> ApprovalDecisionInput:
    return ApprovalDecisionInput(**{"approved": True, "approval_id": "approval-1", **overrides})


# --- Definition ----------------------------------------------------------------


def test_review_and_labels_are_known_approval_config_keys() -> None:
    issues = validate_step_config_keys(
        step_kind="approval",
        override={"approval": {"review": REVIEW, "approve_label": "Go", "reject_label": "Back"}},
        step_path="workflow.steps.check",
    )

    assert issues == []
    assert review_config_issues(REVIEW, path="r") == []


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ({"name": "token", "type": "secret", "editable": True}, "approval_review_field_secret"),
        ({"name": "total", "type": "number", "required": True}, "field_source_required"),
        ({"name": "comment", "editable": True}, "approval_review_field_name_reserved"),
        ({"name": "two words", "editable": True}, "approval_review_field_name_invalid"),
        ({"name": "scan", "type": "file", "editable": True, "from": "state.f"}, "file_editable"),
        ({"name": "kind", "type": "select", "editable": True}, "options_missing"),
        ({"name": "when", "format": "weekday", "editable": True}, "format_unknown"),
    ],
)
def test_definition_issues_name_the_field_path(field: dict, code: str) -> None:
    issues = review_config_issues({"fields": [field]}, path="review")

    assert [issue["path"].startswith("review.fields[0]") for issue in issues] == [True]
    assert code in issues[0]["code"]


def test_duplicate_names_and_unknown_keys_are_issues() -> None:
    issues = review_config_issues(
        {
            "fields": [{"name": "total", "editable": True}, {"name": "total", "editable": True}],
            "layout": "wide",
        },
        path="review",
    )

    assert {issue["code"] for issue in issues} == {
        "workflow.approval_review_field_duplicate",
        "workflow.approval_review_key_unknown",
    }


def test_source_issues_follow_the_known_source_type() -> None:
    types = {
        "message.step.recognize.artifacts.total": "text",
        "message.step.recognize.artifacts.free": "json",
        "state.files": "number",
    }

    def source_type(selector: str, _name: str) -> tuple[str | None, str]:
        if selector in types:
            return types[selector], ""
        return None, "source_not_available"

    from core.application.registry.io_resolver import IOResolver

    issues = review_source_issues(
        {
            "original": "state.files",
            "fields": [
                {
                    "name": "total",
                    "type": "number",
                    "from": "message.step.recognize.artifacts.total",
                },
                {"name": "free", "type": "number", "from": "message.step.recognize.artifacts.free"},
                {"name": "gone", "from": "message.step.missing.artifacts.gone"},
            ],
        },
        path="review",
        source_type=source_type,
        compatible=IOResolver._check_type_compat,
    )

    assert [(issue["code"], issue["path"]) for issue in issues] == [
        ("workflow.approval_review_source_type_mismatch", "review.fields[0].from"),
        ("workflow.approval_review_source_unavailable", "review.fields[2].from"),
        ("workflow.approval_review_original_not_file", "review.original"),
    ]


# --- Opening the review ----------------------------------------------------------


def test_gate_captures_schema_editable_fields_and_full_typed_values() -> None:
    payload = _opened_payload()

    review = payload["review"]
    assert review["editable"] == ["supplier", "total", "document_date", "lines", "notes"]
    assert [field["name"] for field in review["fields"]] == [
        "supplier",
        "total",
        "document_date",
        "lines",
        "notes",
        "confidence",
    ]
    notes = next(field for field in review["fields"] if field["name"] == "notes")
    assert notes["type"] == "paragraph"
    assert review["values"]["supplier"] == "ACME"
    assert review["values"]["lines"] == [{"item": "Paper", "qty": 2}]
    assert review["values"]["confidence"] == 0.62
    # Not truncated like the 1000-character content.
    assert review["values"]["notes"] == LONG_TEXT.strip()
    # A value that fails its type stays as found, marked for the person.
    assert review["values"]["total"] == "31,85"
    assert "total" in review["invalid"]
    assert review["original"] == [{"artifact_id": "art-1", "filename": "invoice.pdf"}]
    assert payload["approval_id"]


def test_labels_and_plain_approval_payload() -> None:
    labelled = _opened_payload(_state({"review": REVIEW, "approve_label": "Export"}))
    plain = _opened_payload(_state({"message": "Check it"}))

    assert labelled["approve_label"] == "Export"
    assert "review" not in plain
    assert "approve_label" not in plain


def test_open_review_bounds_values_and_skips_secret_fields() -> None:
    huge = "x" * (REVIEW_VALUES_MAX_BYTES + 10)
    review = open_review(
        {
            "fields": [
                {"name": "body", "editable": True, "from": "state.body"},
                {"name": "key", "type": "secret", "from": "state.key"},
            ]
        },
        resolve=lambda selector: ({"state.body": huge, "state.key": "sk-1"}[selector], ""),
    )

    assert review is not None
    assert [field["name"] for field in review["fields"]] == ["body"]
    assert review["values"] == {"body": None}
    assert "body" in review["invalid"]


# --- Deciding --------------------------------------------------------------------


def test_approve_without_edits_returns_the_initial_values() -> None:
    review = {
        "fields": [{"name": "total", "type": "number", "editable": True}],
        "editable": ["total"],
        "values": {"total": 318.5},
        "invalid": {},
    }

    values, changes = decide_review(review, None, approved=True)

    assert values == {"total": 318.5}
    assert changes == []


def test_edits_of_non_editable_or_unknown_fields_are_refused() -> None:
    review = _opened_payload()["review"]

    with pytest.raises(ReviewDecisionError) as not_editable:
        decide_review(review, {"confidence": 0.99}, approved=True)
    with pytest.raises(ReviewDecisionError) as unknown:
        decide_review(review, {"vat": 1}, approved=True)

    assert (not_editable.value.code, not_editable.value.field) == (
        "approval_review_field_not_editable",
        "confidence",
    )
    assert unknown.value.code == "approval_review_field_unknown"


@pytest.mark.parametrize("typed", ["318,50", "318,5O"])
def test_a_number_with_a_decimal_comma_is_refused_naming_the_field(typed: str) -> None:
    review = _opened_payload()["review"]

    with pytest.raises(ReviewDecisionError) as refused:
        decide_review(review, {"total": typed}, approved=True)

    assert refused.value.field == "total"
    assert refused.value.code == "approval_review_field_invalid"


def test_corrected_values_are_checked_and_the_diff_recorded() -> None:
    review = _opened_payload()["review"]

    values, changes = decide_review(
        review, {"total": 318.5, "lines": [{"item": "Paper", "qty": "3"}]}, approved=True
    )

    assert values["total"] == 318.5
    assert values["lines"] == [{"item": "Paper", "qty": 3}]
    assert values["confidence"] == 0.62
    assert changes == [
        {"field": "total", "before": "31,85", "after": 318.5},
        {
            "field": "lines",
            "before": [{"item": "Paper", "qty": 2}],
            "after": [{"item": "Paper", "qty": 3}],
        },
    ]


def test_invalid_shape_and_missing_required_value_are_refused() -> None:
    review = _opened_payload()["review"]

    with pytest.raises(ReviewDecisionError) as bad_date:
        decide_review(review, {"total": 1, "document_date": "03.11.2026"}, approved=True)
    with pytest.raises(ReviewDecisionError) as missing:
        decide_review(review, {"total": 1, "supplier": ""}, approved=True)

    assert bad_date.value.field == "document_date"
    assert (missing.value.code, missing.value.field) == (
        "approval_review_field_required",
        "supplier",
    )


def test_a_rejection_keeps_edits_unchecked() -> None:
    review = _opened_payload()["review"]

    values, changes = decide_review(review, {"total": "31,85?"}, approved=False)

    assert values["total"] == "31,85?"
    assert changes == [{"field": "total", "before": "31,85", "after": "31,85?"}]


def test_definition_change_while_open_does_not_change_the_captured_schema() -> None:
    payload = _opened_payload()
    # The same step later loses editability of total; the open review keeps its rules.
    edited_review = json.loads(json.dumps(REVIEW))
    edited_review["fields"][1]["editable"] = False
    _opened_payload(_state({"review": edited_review}))

    checked = checked_review_decision(payload, _decision(values={"total": 318.5}))

    assert checked is not None
    assert checked[0]["total"] == 318.5


def test_old_gate_without_captured_schema_is_decided_the_old_way() -> None:
    old_payload = {"approval_id": "approval-1", "allowed_actions": ["approve", "reject"]}

    assert checked_review_decision(old_payload, _decision()) is None
    with pytest.raises(ServiceError) as refused:
        checked_review_decision(old_payload, _decision(values={"total": 1}))

    assert refused.value.status_code == 422
    assert refused.value.payload["code"] == "approval_review_not_supported"


def test_a_refused_value_is_a_422_naming_the_field() -> None:
    payload = _opened_payload()

    with pytest.raises(ServiceError) as refused:
        checked_review_decision(payload, _decision(values={"confidence": 1}))

    assert refused.value.status_code == 422
    assert refused.value.payload == {
        "error": "Review field 'confidence' is not editable",
        "code": "approval_review_field_not_editable",
        "field": "confidence",
    }


# --- Persistence, resume and idempotency -------------------------------------------


class _Cursor:
    rowcount = 1

    def __init__(self, writes: list) -> None:
        self._writes = writes

    def execute(self, _sql: str, params: tuple) -> None:
        self._writes.append(params)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _Conn:
    def __init__(self) -> None:
        self.writes: list = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.writes)


def _persist(payload: dict, decision: ApprovalDecisionInput, *, meta: dict | None = None) -> dict:
    conn = _Conn()
    ApprovalRepository().persist_decision(
        bl=SimpleNamespace(conn=conn),
        transaction_conn=None,
        row={"id": "job-1", "project_id": "project-1", "task_id": "T-1", "meta": meta or {}},
        thread_id="thread-1",
        approval_id="approval-1",
        payload=payload,
        decision=decision,
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="alice"),
        record_in_progress=lambda **_kwargs: None,
    )
    return json.loads(conn.writes[0][0])


def test_approve_persists_checked_values_for_resume_and_the_diff_with_the_actor() -> None:
    payload = _opened_payload()

    meta = _persist(payload, _decision(values={"total": 318.5}))

    resume_value = meta["workflow_resume"]["resume_value"]
    assert resume_value["review_values"]["total"] == 318.5
    assert resume_value["review_values"]["notes"] == LONG_TEXT.strip()
    entry = meta["approvals"][0]
    assert entry["actor"] == "alice"
    assert entry["review_changes"] == [{"field": "total", "before": "31,85", "after": 318.5}]
    assert serialize_approval_history_entry(entry)["review_changes"] == entry["review_changes"]


def test_plain_approval_resume_value_is_unchanged() -> None:
    meta = _persist({"approval_id": "approval-1"}, _decision(comment="ok"))

    assert meta["workflow_resume"]["resume_value"] == {
        "reason": "approval",
        "approved": True,
        "comment": "ok",
    }
    assert "review_changes" not in meta["approvals"][0]
    assert build_approval_resume_value(_decision()) == {"approved": True, "comment": ""}


def test_second_decision_gets_approval_already_resolved() -> None:
    payload = _opened_payload()

    with pytest.raises(ServiceConflictError) as conflict:
        _persist(
            payload,
            _decision(values={"total": 1}),
            meta={"approvals": [{"approval_id": "approval-1", "approved": True}]},
        )

    assert conflict.value.payload["code"] == "approval_already_resolved"


def test_same_key_with_other_values_has_another_fingerprint() -> None:
    plain = approval_decision_fingerprint(_decision())
    edited = approval_decision_fingerprint(_decision(values={"total": 318.5}))
    other = approval_decision_fingerprint(_decision(values={"total": 31.85}))

    assert len({plain, edited, other}) == 3
    assert approval_decision_fingerprint(_decision(values={"total": 318.5})) == edited


# --- The graph node: outputs reach the next step --------------------------------------


def test_restart_resumes_with_corrected_values_that_invoke_maps() -> None:
    state = _state()
    payload = _opened_payload(state)
    meta = _persist(payload, _decision(values={"total": 318.5}))
    # A restarted worker resumes the graph with the persisted resume value.
    result, _ = _gate(state, meta["workflow_resume"]["resume_value"])

    message = result.update["agent_messages"][-1]
    assert message["artifacts"]["decision"] == "approved"
    assert message["artifacts"]["total"] == 318.5
    after = {**state, **result.update}
    value, error = _resolve_invoke_input(after, "message.step.check.artifacts.total", "total")
    assert (value, error) == (318.5, "")
    assert _resolve_invoke_input(after, "message.step.check.artifacts.notes", "notes")[0] == (
        LONG_TEXT.strip()
    )


def test_reject_comment_reaches_the_rejected_branch() -> None:
    result, _ = _gate(_state(), {"approved": False, "comment": "Total is 318,50"})

    message = result.update["agent_messages"][-1]
    assert message["artifacts"]["decision"] == "rejected"
    assert message["artifacts"]["comment"] == "Total is 318,50"
    assert "total" not in message["artifacts"]
    assert message["artifacts"]["_outcome"] == {"name": "rejected"}


def test_test_run_resumes_from_state_with_checked_values() -> None:
    state = _state(task_source="workflow_test_run")
    update = _build_state_based_approval_update(
        approved=True,
        action=None,
        comment="",
        resume_value={"approved": True, "comment": "", "review_values": {"total": 318.5}},
        state_values=state,
    )

    result = _registry_approval_gate_node({**state, **update})

    assert update["approval_review_values"] == {"total": 318.5}
    assert result.update["agent_messages"][-1]["artifacts"]["total"] == 318.5
    assert result.update["approval_review_values"] is None


# --- Tokens --------------------------------------------------------------------------


def _command_service() -> tuple[ApprovalCommandService, MagicMock]:
    cursor = MagicMock()
    cursor.fetchone.return_value = (
        "project-1",
        DEFAULT_ACCOUNT_ID,
        DEFAULT_WORKSPACE_ID,
        "workflow-1",
        "job-1",
    )
    bl = SimpleNamespace(conn=MagicMock())
    bl.conn.cursor.return_value.__enter__.return_value = cursor
    service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: bl,
        enqueue_workflow_resume=MagicMock(),
        workflow_resume_repository_factory=lambda _bl: MagicMock(),
    )
    claim = MagicMock(side_effect=ServiceConflictError("stop here", payload={"code": "stop"}))
    service._claim_and_record_decision = claim
    return service, claim


def test_machine_token_values_are_refused_with_422() -> None:
    service, claim = _command_service()

    with pytest.raises(ServiceError) as refused:
        service.resume_task(
            thread_id="thread-1",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="robot",
                principal_type="service_account",
                auth_source="machine",
            ),
            decision=_decision(values={"total": 318.5}),
            idempotency_key="key-1",
        )

    assert refused.value.status_code == 422
    assert refused.value.payload["code"] == "approval_values_machine_token"
    claim.assert_not_called()


def test_machine_token_may_still_decide_without_values() -> None:
    service, claim = _command_service()

    with pytest.raises(ServiceConflictError):
        service.resume_task(
            thread_id="thread-1",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="robot",
                principal_type="service_account",
                auth_source="machine",
            ),
            decision=_decision(),
            idempotency_key="key-1",
        )

    claim.assert_called_once()


def test_personal_token_values_reach_the_decision_with_the_owner() -> None:
    service, claim = _command_service()

    with pytest.raises(ServiceConflictError):
        service.resume_task(
            thread_id="thread-1",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="alice",
                principal_type="user",
                auth_source="pat",
            ),
            decision=_decision(values={"total": 318.5}),
            idempotency_key="key-1",
        )

    assert claim.call_args.kwargs["decision"].values == {"total": 318.5}
    assert claim.call_args.kwargs["scope"].actor_name == "alice"


# --- Public projection -------------------------------------------------------------


def test_approval_query_returns_the_public_review_with_owned_original() -> None:
    payload = _opened_payload()
    store = MagicMock()
    job = {
        "id": "job-1",
        "status": "awaiting_approval",
        "thread_id": "thread-1",
        "task_summary": "Invoice",
        "project_id": "project-1",
    }
    store.get_job.return_value = job
    store.get_project.return_value = {"id": "project-1", "account_id": DEFAULT_ACCOUNT_ID}
    snapshot = MagicMock()
    snapshot.tasks = [MagicMock(interrupts=[MagicMock(id="interrupt-1", value=payload)])]
    artifact = {
        "artifact_id": "art-1",
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1200,
        "owner_scope": {"project_id": "project-1", "job_id": "job-1"},
    }
    service = ApprovalQueryService(
        get_logger=lambda: store,
        get_thread_state_snapshot=lambda _thread_id: snapshot,
        approval_fallback_payload=lambda _job: {},
        resolve_artifact=lambda handle: artifact if handle == "art-1" else None,
    )

    gate = service.get_current_gate("job-1").dump()

    assert "review" not in gate["payload"]
    review = gate["review"]
    assert set(review) == {"fields", "values", "invalid", "original"}
    total = next(field for field in review["fields"] if field["name"] == "total")
    assert total == {
        "name": "total",
        "label": "Total",
        "type": "number",
        "required": True,
        "editable": True,
        "options": [],
        "items": None,
        "properties": None,
        "min_items": None,
        "max_items": None,
        "format": None,
        "description": "",
    }
    assert review["values"]["notes"] == LONG_TEXT.strip()
    assert review["original"] == [
        {
            "artifact_id": "art-1",
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1200,
        }
    ]


def test_public_review_masks_secret_references_and_hides_foreign_files() -> None:
    from core.infrastructure.security.secret_reference import SECRET_REFERENCE_MARKER

    review = {
        "fields": [{"name": "note", "type": "object", "editable": False}],
        "editable": [],
        "values": {"note": {"key": {"__type__": SECRET_REFERENCE_MARKER, "name": "k"}}},
        "invalid": {},
        "original": [{"artifact_id": "foreign"}],
    }

    public = public_review(review, original=[])

    assert public is not None
    assert public["values"] == {"note": {"key": "[REDACTED]"}}
    assert public["original"] == []


def test_token_gate_response_keeps_the_review() -> None:
    from core.entrypoints.api.jobs import _token_approval_gate_response

    flattened = _token_approval_gate_response(
        {"job_id": "job-1", "payload": {"message": "Check"}, "review": {"fields": []}}
    )

    assert flattened["review"] == {"fields": []}
    assert "payload" not in flattened


# --- Builder outputs and portability ----------------------------------------------------


def test_approval_outputs_are_typed_step_outputs_for_mapping() -> None:
    from core.application.registry.io_resolver import IOResolver

    resolver = IOResolver(SimpleNamespace())
    outputs = resolver.get_step_declared_outputs(
        {"step_id": "check", "step_kind": "approval", "override": {"approval": {"review": REVIEW}}}
    )

    by_name = {row["name"]: row for row in outputs}
    assert by_name["total"]["type"] == "number"
    assert by_name["lines"]["type"] == "array"
    assert by_name["notes"]["type"] == "paragraph"
    assert by_name["decision"]["step_id"] == "check"
    assert by_name["comment"]["type"] == "paragraph"


class _Portability(WorkflowPortabilityRewriteMixin):
    pass


@pytest.mark.parametrize(
    ("resolution_index", "expected"),
    [
        (
            {
                ("child_workflow", "accounting_export"): {
                    "action": "rename",
                    "target_ref": "accounting_export_copy",
                }
            },
            "accounting_export_copy",
        ),
        ({}, None),
    ],
)
def test_import_rebinds_or_drops_the_fields_hint_and_keeps_fields(
    resolution_index: dict, expected: str | None
) -> None:
    step = {
        "step_id": "check",
        "step_kind": "approval",
        "override": {"approval": {"review": REVIEW}},
    }

    rewritten = _Portability()._rewrite_step(step, resolution_index)

    review = rewritten["override"]["approval"]["review"]
    assert review.get("fields_from_workflow") == expected
    assert review["fields"] == REVIEW["fields"]
    assert REVIEW["fields_from_workflow"] == "accounting_export"


def test_readiness_resolves_review_sources_like_an_invoke_mapping() -> None:
    from core.application.registry.contract_validation_workflow_readiness import (
        _approval_review_issues,
    )

    type_map = {
        "message.step.recognize.artifacts.total": "number",
        "message.step.recognize.artifacts.supplier": "text",
        "inputs.files": "array",
        "inputs.notes": "array",
    }
    resolver = SimpleNamespace(get_available_type_map_for_step=lambda *_args: type_map)
    workflow = {
        "settings": {
            "callable": {
                "expected_inputs": [
                    {"name": "files", "type": "array", "items": {"type": "file"}},
                    {"name": "notes", "type": "array", "items": {"type": "string"}},
                ]
            }
        }
    }

    def issues(review: dict) -> list[tuple[str, str]]:
        step = {
            "step_id": "check",
            "step_kind": "approval",
            "override": {"approval": {"review": review}},
        }
        return [
            (issue["code"], issue["path"])
            for issue in _approval_review_issues(
                step, "workflow.steps.check", workflow=workflow, workflow_id="wf", resolver=resolver
            )
        ]

    path = "workflow.steps.check.override.approval.review"
    assert (
        issues(
            {
                "original": "state.inputs.files",
                "fields": [
                    {
                        "name": "total",
                        "type": "number",
                        "editable": True,
                        "from": "message.step.recognize.artifacts.total",
                    },
                    {
                        "name": "supplier",
                        "editable": True,
                        "from": "message.step.recognize.artifacts.supplier",
                    },
                    {
                        "name": "free",
                        "editable": True,
                        "from": "message.agent.reader.artifacts.free",
                    },
                ],
            }
        )
        == []
    )
    assert issues(
        {
            "original": "state.inputs.notes",
            "fields": [
                {
                    "name": "total",
                    "type": "boolean",
                    "editable": True,
                    "from": "message.step.recognize.artifacts.total",
                },
                {"name": "files", "type": "array", "editable": True, "from": "inputs.files"},
            ],
        }
    ) == [
        ("workflow.approval_review_source_type_mismatch", f"{path}.fields[0].from"),
        ("workflow.approval_review_source_unavailable", f"{path}.fields[1].from"),
        ("workflow.approval_review_original_not_file", f"{path}.original"),
    ]
