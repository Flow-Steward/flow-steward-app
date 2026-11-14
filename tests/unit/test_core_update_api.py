from __future__ import annotations

import asyncio
from threading import Event

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.application.auth.request_principal_runtime import (
    require_authenticated_user,
    roles_intersect,
)
from core.application.release.update_notice import CoreUpdateStatus
from core.entrypoints.api.system_core_updates import create_system_core_updates_router


def admin(request: Request, *, required_roles=None):
    role = request.headers.get("x-test-role")
    return require_authenticated_user(
        request,
        required_roles=required_roles,
        request_auth_user_payload_fn=lambda _request: {"roles": [role]} if role else None,
        roles_intersect_fn=roles_intersect,
        err_authentication_required="Authentication required",
    )


def available():
    return CoreUpdateStatus(
        status="update_available",
        current_version="0.9.17",
        latest_version="0.9.18",
        release_url="https://github.com/Flow-Steward/flow-steward-app/releases/tag/v0.9.18",
    )


@pytest.mark.parametrize("role", ["operator", "viewer", None])
def test_non_admins_cannot_trigger_release_discovery(role):
    calls = []

    def provider():
        calls.append("discovered")
        return available()

    app = FastAPI()
    app.include_router(
        create_system_core_updates_router(
            require_authenticated_user=admin, status_provider=provider
        )
    )
    response = TestClient(app).get(
        "/api/system/core-updates", headers={"x-test-role": role} if role else {}
    )
    assert response.status_code == 403
    assert calls == []


def test_admin_receives_only_the_notice_contract_with_private_cache_policy():
    app = FastAPI()
    app.include_router(
        create_system_core_updates_router(
            require_authenticated_user=admin, status_provider=available
        )
    )
    response = TestClient(app).get("/api/system/core-updates", headers={"x-test-role": "admin"})
    assert response.status_code == 200
    assert response.json() == available().model_dump()
    assert response.headers["cache-control"] == "private, no-store"


def test_cold_release_check_does_not_block_other_http_requests():
    entered, release = Event(), Event()

    def slow_provider():
        entered.set()
        release.wait(2)
        return available()

    app = FastAPI()
    app.include_router(
        create_system_core_updates_router(
            require_authenticated_user=admin, status_provider=slow_provider
        )
    )

    @app.get("/health/live")
    async def health():
        return {"ok": True}

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(
                client.get("/api/system/core-updates", headers={"x-test-role": "admin"})
            )
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert (await asyncio.wait_for(client.get("/health/live"), 1)).status_code == 200
                assert not pending.done()
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(check())
