"""Direct extension execution must allow its artifact callback to reach the API."""

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import MagicMock

import anyio
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router


@pytest.mark.parametrize("route", ["action", "invoke"])
def test_direct_execution_allows_concurrent_artifact_callback(route):
    async def exercise():
        app = FastAPI()
        loop = asyncio.get_running_loop()
        request_context = ContextVar("request_context", default="missing")
        observed = {}
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 1

        @app.middleware("http")
        async def set_context(request, call_next):
            token = request_context.set("request-value")
            try:
                return await call_next(request)
            finally:
                request_context.reset(token)

        @app.put("/callback")
        async def callback(request: Request):
            content = await request.body()
            await run_in_threadpool(observed.__setitem__, "callback_body", content)
            return {"uploaded": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            def execute(*args, **kwargs):
                observed["args"] = args
                observed["kwargs"] = kwargs
                observed["context"] = request_context.get()
                upload = asyncio.run_coroutine_threadsafe(
                    client.put("/callback", content=b"artifact"), loop
                )
                response = upload.result(timeout=3)
                assert response.status_code == 200
                return SimpleNamespace(dump=lambda: {"status": "succeeded"})

            app.include_router(
                create_reactions_extensions_router(
                    event_query_service=MagicMock,
                    event_command_service=MagicMock,
                    job_query_service=MagicMock,
                    extension_query_service=MagicMock,
                    extension_binding_service=lambda: SimpleNamespace(execute_action=execute),
                    tool_execution_service=lambda: SimpleNamespace(invoke_extension=execute),
                    command_dispatch_service=MagicMock,
                    request_scope=lambda request, **kwargs: kwargs,
                    service_error_response=lambda e, request: JSONResponse(
                        {"error": str(e)}, status_code=500
                    ),
                    api_error_response=lambda e, request: JSONResponse(
                        {"error": type(e).__name__}, status_code=500
                    ),
                )
            )
            path = (
                "/api/extensions/demo/action/create"
                if route == "action"
                else "/api/extensions/demo/invoke"
            )
            body = (
                {"context": {"project_id": "project-1"}}
                if route == "action"
                else {"request": {"project_id": "project-1"}}
            )
            response = await client.post(path, json=body, headers={"Idempotency-Key": "key-1"})
            assert response.status_code == 200, response.text
            assert response.json() == {"status": "succeeded"}
            assert observed["callback_body"] == b"artifact"
            assert observed["context"] == "request-value"
            assert observed["kwargs"]["scope"] == {"project_id": "project-1"}
            if route == "action":
                assert observed["kwargs"]["idempotency_key"] == "key-1"

    async def with_restored_limiter():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous_tokens = limiter.total_tokens
        try:
            await exercise()
        finally:
            limiter.total_tokens = previous_tokens

    asyncio.run(with_restored_limiter())
