"""Test-only platform module that proves Core's scoped roles seam reaches a module.

Core owns the limit table, the check and the port a module calls (ADR 0076 section 8,
ADR 0082). This stub uses the port the way a real module would and does nothing
else: one read-only route that lists the account's project grants through
``container.scoped_roles``, behind the ``scoped_roles`` entitlement and a module
route rule, and one ``container.feature_endpoints`` registration. It has no write
route and no page.

The account always comes from the authenticated principal, never from the request.
FastAPI is imported inside :func:`register`, which only the app role calls; the route
annotations are evaluated there, so this file does not use postponed annotations.
"""

from collections.abc import Mapping
from typing import Annotated, Any

FEATURE = "scoped_roles"
PERMISSION = "users.manage"


def register(app: Any, container: Any) -> None:
    """App role entrypoint: mount the one route and register the feature endpoint."""
    from fastapi import APIRouter, Depends, HTTPException, Request

    prefix = f"/api/modules/{container.metadata['platform_module']['id']}/scoped-roles"
    require_user = container.auth_helpers["require_authenticated_user"]
    require_feature = container.auth_helpers["require_entitlement"](FEATURE)
    scoped_roles = container.scoped_roles

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

    @router.get("/grants")
    def list_grants(
        account_id: Annotated[str, Depends(caller_account)],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        rows = scoped_roles.list_grants(account_id, user_id=user_id)
        return {"grants": [dict(row) for row in rows or ()]}

    app.include_router(router)
    container.registries["module_route_permissions"].register(
        methods=("GET",), pattern="scoped-roles/grants", permission=PERMISSION
    )
    container.feature_endpoints.register(FEATURE, prefix)
