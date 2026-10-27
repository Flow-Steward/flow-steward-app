from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import Mock

from core.application.workflows.test_run_query_service import WorkflowTestRunQueryService


class _Repository:
    def __init__(self, row):
        self.row = row
        self.thread_ids: list[str] = []

    def find_by_thread_id(self, thread_id: str):
        self.thread_ids.append(thread_id)
        return self.row


def test_query_service_returns_a_copy_of_durable_job_state() -> None:
    row = {"id": "job-1", "status": "resume_pending"}
    repository = _Repository(row)
    service = WorkflowTestRunQueryService(repository=repository)

    result = service.get_durable_job_state(" thread-1 ")

    assert result == row
    assert result is not row
    assert repository.thread_ids == ["thread-1"]


def test_query_service_rejects_blank_thread_without_persistence_lookup() -> None:
    repository = _Repository({"id": "job-1"})
    service = WorkflowTestRunQueryService(repository=repository)

    assert service.get_durable_job_state("  ") == {}
    assert repository.thread_ids == []


def test_query_service_projects_queued_and_terminal_job_state_without_execution_runtime() -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "thread_id": "thread-1",
            "project_id": "project-1",
            "task_id": "task-1",
            "task_summary": "Validate",
            "status": "pending",
            "meta": {
                "selected_workflow_id": "wf-demo",
                "execution_id": "exec-1",
                "task_envelope": {"inputs": {"release": "1.0"}},
            },
        }
    )
    service = WorkflowTestRunQueryService(repository=repository)

    result = service.get_public_state("thread-1")

    assert result["status"] == "queued"
    assert result["final_status"] == "queued"
    assert result["job_id"] == "job-1"
    assert result["thread_id"] == "thread-1"
    assert result["workflow_id"] == "wf-demo"
    assert result["project_id"] == "project-1"
    assert result["execution_id"] == "exec-1"
    assert result["task_envelope"] == {"inputs": {"release": "1.0"}}


def test_query_service_surfaces_worker_failure_as_terminal_get_state() -> None:
    repository = _Repository(
        {
            "id": "job-2",
            "thread_id": "thread-2",
            "project_id": "project-1",
            "status": "failed",
            "error": "step execution failed",
            "final_report": "Compilation failed",
            "meta": {"selected_workflow_id": "wf-demo", "execution_id": "exec-2"},
        }
    )

    result = WorkflowTestRunQueryService(repository=repository).get_public_state("thread-2")

    assert result["status"] == "failed"
    assert result["final_status"] == "failed"
    assert result["error"] == "step execution failed"
    assert result["final_report"] == "Compilation failed"


def test_query_service_exposes_only_the_current_awaiting_approval_identity() -> None:
    row = {
        "id": "job-approval",
        "thread_id": "thread-approval",
        "project_id": "project-1",
        "status": "awaiting_approval",
        "meta": {
            "workflow_test_run": True,
            "state_approval_id": "interrupt-current-7",
        },
    }
    repository = _Repository(row)
    service = WorkflowTestRunQueryService(repository=repository)

    awaiting = service.get_public_state("thread-approval")
    row["status"] = "completed"
    resolved = service.get_public_state("thread-approval")

    assert awaiting["approval_id"] == "interrupt-current-7"
    assert awaiting["needs_user_approval"] is True
    assert resolved["approval_id"] is None
    assert resolved["needs_user_approval"] is False


def test_query_service_leaves_missing_approval_identity_read_only() -> None:
    row = {
        "id": "job-awaiting",
        "thread_id": "thread-awaiting",
        "status": "awaiting_approval",
        "meta": {"workflow_test_run": True},
    }
    repository = Mock(wraps=_Repository(row))
    repository.persist_recovered_state_approval = Mock(
        return_value={"state_approval_id": "unexpected-recovered-id"}
    )
    checkpoint_reader = Mock(
        return_value=SimpleNamespace(
            values={
                "status": "awaiting_approval",
                "needs_user_approval": True,
                "approval_context": "Approve the release candidate.",
            }
        )
    )
    service = WorkflowTestRunQueryService(repository=repository)
    # A leftover runtime attribute cannot give a durable query a checkpoint/write port.
    object.__setattr__(service, "get_thread_state_snapshot", checkpoint_reader)

    for _ in range(2):
        awaiting = service.get_public_state("thread-awaiting")
        assert awaiting["approval_id"] is None
        assert awaiting["needs_user_approval"] is True
    assert row["meta"] == {"workflow_test_run": True}
    checkpoint_reader.assert_not_called()
    repository.persist_recovered_state_approval.assert_not_called()
    assert [field.name for field in fields(service)] == ["repository"]


def _completed_row(meta: dict) -> dict:
    return {
        "id": "job-9",
        "thread_id": "thread-9",
        "project_id": "project-1",
        "status": "completed",
        "selected_workflow": "wf_named_result",
        "meta": meta,
    }


def test_a_completed_run_reports_the_outputs_it_produced() -> None:
    """FLOW-002: every polling GET returned empty results for a run that had them."""
    repository = _Repository(
        _completed_row(
            {
                "workflow_outputs": {
                    "outputs": {"named_result": 15},
                    "artifacts": {"report": "artifact:report-9"},
                }
            }
        )
    )

    state = WorkflowTestRunQueryService(repository=repository).get_public_state("thread-9")

    assert state["status"] == "completed"
    assert state["outputs"] == {"named_result": 15}
    assert state["artifacts"] == {"report": "artifact:report-9"}
    assert state["workflow_outputs"]["outputs"] == {"named_result": 15}


def test_outputs_persisted_beside_the_contract_are_read_too() -> None:
    """Runs persist these in more than one shape; the projection reads what is there."""
    repository = _Repository(
        _completed_row({"outputs": {"named_result": 15}, "artifacts": {"report": "artifact:r"}})
    )

    state = WorkflowTestRunQueryService(repository=repository).get_public_state("thread-9")

    assert state["outputs"] == {"named_result": 15}
    assert state["artifacts"] == {"report": "artifact:r"}


def test_outputs_stored_as_json_text_are_read_as_an_object() -> None:
    repository = _Repository(
        _completed_row({"workflow_outputs": '{"outputs": {"named_result": 15}}'})
    )

    state = WorkflowTestRunQueryService(repository=repository).get_public_state("thread-9")

    assert state["outputs"] == {"named_result": 15}


def test_a_queued_run_reports_empty_results_rather_than_inventing_them() -> None:
    repository = _Repository({"id": "job-1", "thread_id": "t", "status": "pending", "meta": {}})

    state = WorkflowTestRunQueryService(repository=repository).get_public_state("t")

    assert state["status"] == "queued"
    assert state["outputs"] == {}
    assert state["artifacts"] == {}
    assert state["workflow_outputs"] == {}


_WITHHELD_VALUE = "ALPHA-111"


def test_completed_state_is_redacted_recursively() -> None:
    """Durable state is what the run produced; it is not a safe public projection."""
    repository = _Repository(
        _completed_row(
            {
                "workflow_outputs": {
                    "outputs": {
                        "named_result": 15,
                        "connection": {"host": "mail.test", "password": _WITHHELD_VALUE},
                        "rows": [{"api_key": _WITHHELD_VALUE, "sku": "SKU-1"}],
                    },
                    "artifacts": {"report": "artifact:r", "signing_secret": _WITHHELD_VALUE},
                }
            }
        )
    )

    state = WorkflowTestRunQueryService(repository=repository).get_public_state("thread-9")

    serialized = json.dumps(state)
    assert _WITHHELD_VALUE not in serialized
    assert state["outputs"]["named_result"] == 15, "ordinary results stay available"
    assert state["outputs"]["connection"]["host"] == "mail.test"
    assert state["outputs"]["rows"][0]["sku"] == "SKU-1"
    assert state["artifacts"]["report"] == "artifact:r"


def test_a_secret_aliased_into_an_innocuous_key_is_redacted_here_too() -> None:
    repository = _Repository(
        _completed_row(
            {
                "materialized_input_sensitive_keys": ["login"],
                "workflow_outputs": {"outputs": {"login": _WITHHELD_VALUE, "topic": "ok"}},
            }
        )
    )

    state = WorkflowTestRunQueryService(repository=repository).get_public_state("thread-9")

    assert _WITHHELD_VALUE not in json.dumps(state)
    assert state["outputs"]["topic"] == "ok"


def test_public_error_text_is_scrubbed() -> None:
    row = _completed_row({})
    row["status"] = "failed"
    row["error"] = f"connection refused using password={_WITHHELD_VALUE}"
    repository = _Repository(row)

    state = WorkflowTestRunQueryService(repository=repository).get_public_state("thread-9")

    assert _WITHHELD_VALUE not in str(state["error"])


# ---------------------------------------------------------------------------
# The envelope
#
# Outputs and artifacts were redacted; `task_envelope` was returned straight
# from run metadata. It carries the materialized step inputs, so a secret
# aliased into an innocuous key travelled out through the field next to the one
# that was carefully cleaned.
# ---------------------------------------------------------------------------


def test_the_task_envelope_is_redacted_like_every_other_public_field() -> None:
    from core.application.workflows.test_run_query_service import (
        WorkflowTestRunQueryService,
    )
    from core.infrastructure.security.prompt_safety import REDACTED_CREDENTIAL_MARKER

    payload = WorkflowTestRunQueryService._public_payload(
        {"id": "job-1", "status": "succeeded", "task_summary": "run"},
        {
            # Provenance is recorded at the run level when step inputs are
            # materialised; the envelope is one of the places the value lands.
            "materialized_input_sensitive_keys": ["login"],
            "task_envelope": {"inputs": {"login": "ENVELOPE-SECRET-7781"}},
        },
    )

    assert "ENVELOPE-SECRET-7781" not in json.dumps(payload)
    # Checked structurally: json.dumps escapes the marker's em dash, so a
    # substring assertion on the rendered text would pass for the wrong reason.
    assert payload["task_envelope"]["inputs"]["login"] == REDACTED_CREDENTIAL_MARKER


def test_an_ordinary_envelope_value_is_still_readable() -> None:
    from core.application.workflows.test_run_query_service import (
        WorkflowTestRunQueryService,
    )

    payload = WorkflowTestRunQueryService._public_payload(
        {"id": "job-1", "status": "succeeded"},
        {"task_envelope": {"inputs": {"region": "eu-west-1"}}},
    )

    assert payload["task_envelope"]["inputs"]["region"] == "eu-west-1"


def test_runtime_redactor_hides_a_secret_reference_under_a_neutral_name() -> None:
    from core.application.workflows.platform_test_run_common import redact_test_run_payload
    from core.infrastructure.security.prompt_safety import REDACTED_CREDENTIAL_MARKER
    from core.infrastructure.security.secret_reference import SecretReference

    reference = SecretReference(name="login", ciphertext="opaque-ciphertext").to_wire()

    payload = redact_test_run_payload({"login": reference})

    assert payload["login"] == REDACTED_CREDENTIAL_MARKER
    assert "opaque-ciphertext" not in json.dumps(payload)
