"""Test-only platform module that proves Core's approval seams reach a module (ADR 0082).

Core owns the tables its decision check reads, the check, the journal writes and the
ports a module calls (section 3a). This stub uses them the way a real module would
and does nothing else: one read-only route that lists a step's assigned approvers
through ``container.approval_assignments``, behind the ``approval_assignees``
entitlement and a module route rule, and one ``container.feature_endpoints``
registration. It has no write route, no report and no page.

The account always comes from the authenticated principal, never from the request.
FastAPI is imported inside :func:`register`, which only the app role calls; the route
annotations are evaluated there, so this file does not use postponed annotations.
"""

from collections.abc import Mapping
from typing import Annotated, Any

FEATURE = "approval_assignees"
PERMISSION = "workflows.manage"


def register(app: Any, container: Any) -> None:
    """App role entrypoint: mount the one route and register the feature endpoint."""
    from fastapi import APIRouter, Depends, HTTPException, Request

    prefix = f"/api/modules/{container.metadata['platform_module']['id']}/approval-steps"
    require_user = container.auth_helpers["require_authenticated_user"]
    require_feature = container.auth_helpers["require_entitlement"](FEATURE)
    assignments = container.approval_assignments

    def caller_account(request: Request) -> str:
        try:
            user = require_user(request)
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail="Authentication required") from exc
        account_id = str(user.get("account_id") or "") if isinstance(user, Mapping) else ""
        if not account_id:
            raise HTTPException(status_code=403, detail="The caller has no account")
        return account_id

    # Authentication first, so an anonymous caller gets 401 rather than 403.
    router = APIRouter(
        prefix=prefix, dependencies=[Depends(caller_account), Depends(require_feature)]
    )

    @router.get("/{workflow_id}/{step_id}/approvers")
    def list_approvers(
        workflow_id: str, step_id: str, account_id: Annotated[str, Depends(caller_account)]
    ) -> dict[str, Any]:
        try:
            rows = assignments.list_approvers(workflow_id, step_id, account_id=account_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"approvers": [dict(row) for row in rows or ()]}

    app.include_router(router)
    container.registries["module_route_permissions"].register(
        methods=("GET",), pattern="approval-steps/[^/]+/[^/]+/approvers", permission=PERMISSION
    )
    container.feature_endpoints.register(FEATURE, prefix)
