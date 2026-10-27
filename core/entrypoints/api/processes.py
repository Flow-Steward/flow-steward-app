from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.application.auth.enforce import require_roles
from core.application.monitoring.processes import (
    ProcessNotFoundError,
    ProcessNotTerminableError,
    ProcessTerminationPermissionError,
    list_process_payload,
    terminate_process,
)

router = APIRouter(prefix="/api", tags=["processes"])


class ProcessesPayload(BaseModel):
    jobs: list[dict[str, Any]]
    subprocesses: list[dict[str, Any]]
    suspected_orphans: list[dict[str, Any]]
    project_leases: list[dict[str, Any]]
    project_lease_evidence_unavailable: bool
    workers: list[dict[str, Any]]
    # AC17-R69 added this field. It defaults rather than being required: response-model
    # validation turns a payload that predates it into a 500 on an admin observability
    # endpoint, which is a worse failure than reporting no worker processes. The producer
    # always populates it, and tests/unit/test_process_monitoring.py pins that.
    worker_processes: list[dict[str, Any]] = Field(default_factory=list)
    reaped_worker_subprocess_count: int


@router.get(
    "/processes",
    response_model=ProcessesPayload,
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
    },
)
def list_processes(request: Request):
    """List active jobs with real worker subprocesses from /proc."""
    auth_user = getattr(request.state, "auth_user", None)
    if isinstance(auth_user, dict):
        try:
            require_roles(auth_user, {"admin", "operator"})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Operator role required") from exc
    return list_process_payload()


@router.post(
    "/processes/{pid}/kill",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
    },
)
def kill_process(request: Request, pid: int):
    """Gracefully kill an active Flow Steward subprocess by PID."""
    auth_user = getattr(request.state, "auth_user", None)
    if not isinstance(auth_user, dict):
        raise HTTPException(status_code=403, detail="Admin role required")
    try:
        require_roles(auth_user, {"admin"})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Admin role required") from exc
    try:
        return terminate_process(pid)
    except ProcessNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProcessNotTerminableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProcessTerminationPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
