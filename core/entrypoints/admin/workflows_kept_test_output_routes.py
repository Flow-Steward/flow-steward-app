"""Authenticated builder transport for kept test outputs (test data, not structure)."""

from contextlib import contextmanager
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.application.workflows.workflow_kept_test_outputs import (
    KeptTestOutputError,
    WorkflowKeptTestOutputService,
)
from core.entrypoints.admin.workflows_common import _get_db, router
from core.entrypoints.admin.workflows_scope import (
    _assert_workflow_mutation_allowed,
    _resolve_active_workflow_scope_target,
)


class KeptTestOutputSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outputs: dict[str, Any] = Field(default_factory=dict)
    source_kind: Literal["run", "manual"] = "manual"
    source_job_id: str = Field(default="", max_length=200)


def _actor(request: Request) -> str:
    user = getattr(request.state, "auth_user", None)
    if not isinstance(user, dict):
        return ""
    return str(user.get("username") or user.get("id") or "").strip()


def _step_kind(workflow: dict[str, Any], step_id: str) -> str | None:
    for step in workflow.get("steps") or []:
        if isinstance(step, dict) and str(step.get("step_id") or "").strip() == step_id:
            return str(step.get("step_kind") or "agent").strip()
    return None


@contextmanager
def _kept_output_request(request: Request, workflow_id: str, *, write: bool = False):
    db = _get_db()
    workflow, scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not workflow:
        raise HTTPException(404, "Workflow not found.")
    if write:
        _assert_workflow_mutation_allowed(workflow)
    try:
        yield (
            WorkflowKeptTestOutputService(db, actor=_actor(request)),
            workflow,
            str(workflow.get("account_id") or scope.account_id),
        )
    except KeptTestOutputError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/{workflow_id}/kept-test-outputs")
def list_kept_test_outputs(workflow_id: str, request: Request):
    with _kept_output_request(request, workflow_id) as (service, _workflow, account_id):
        return service.list(account_id=account_id, workflow_id=workflow_id)


@router.put("/{workflow_id}/kept-test-outputs/{step_id}")
def save_kept_test_output(
    workflow_id: str, step_id: str, body: KeptTestOutputSave, request: Request
):
    with _kept_output_request(request, workflow_id, write=True) as (
        service,
        workflow,
        account_id,
    ):
        step_kind = _step_kind(workflow, step_id)
        if step_kind is None:
            raise HTTPException(404, "Step not found in the applied workflow.")
        return service.keep(
            account_id=account_id,
            workflow_id=workflow_id,
            step_id=step_id,
            step_kind=step_kind,
            outputs=body.outputs,
            source_kind=body.source_kind,
            source_job_id=body.source_job_id,
        )


@router.delete("/{workflow_id}/kept-test-outputs/{step_id}")
def delete_kept_test_output(workflow_id: str, step_id: str, request: Request):
    with _kept_output_request(request, workflow_id, write=True) as (
        service,
        _workflow,
        account_id,
    ):
        return service.clear(account_id=account_id, workflow_id=workflow_id, step_id=step_id)


__all__ = [
    "delete_kept_test_output",
    "list_kept_test_outputs",
    "save_kept_test_output",
]
