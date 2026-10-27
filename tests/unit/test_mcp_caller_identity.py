from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers


def _context(*authorization_values: str):
    headers = Headers(
        raw=[(b"authorization", value.encode("ascii")) for value in authorization_values]
    )
    return SimpleNamespace(request=SimpleNamespace(headers=headers))


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (("Bearer caller-token",), "caller-token"),
        (("bEaReR caller.token_+/=",), "caller.token_+/="),
        ((), ""),
        (("caller-token",), ""),
        (("Bearer",), ""),
        (("Bearer ",), ""),
        (("Bearer first second",), ""),
        (("Bearer first, Bearer second",), ""),
        (("Bearer first", "Bearer second"), ""),
    ],
)
def test_transport_bearer_parser_accepts_only_one_strict_value(values, expected) -> None:
    from core.entrypoints.mcp.server_caller_identity import transport_caller_bearer

    assert transport_caller_bearer(_context(*values)) == expected


def test_auth_enabled_requires_verified_context_and_verified_token_wins(monkeypatch) -> None:
    from core.entrypoints.mcp import server_caller_identity as identity

    observed: list[str] = []
    middleware = identity.McpCallerIdentityMiddleware(auth_enabled=True)

    async def call_next(_ctx):
        observed.append(identity.current_caller_bearer())
        return {"ok": True}

    asyncio.run(middleware(_context("Bearer raw-token"), call_next))
    assert observed == [""]

    monkeypatch.setattr(
        identity,
        "get_access_token",
        lambda: SimpleNamespace(token="verified-token"),
    )
    observed.clear()
    asyncio.run(middleware(_context("Bearer raw-token"), call_next))
    assert observed == ["verified-token"]


def test_missing_bearer_has_no_service_or_session_fallback(monkeypatch) -> None:
    from core.entrypoints.mcp import server_caller_identity as identity

    monkeypatch.setenv("MCP_BEARER_TOKEN", "service-token")
    monkeypatch.setenv("FS_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("FS_SESSION", "session-token")
    observed: list[str] = []

    async def call_next(_ctx):
        observed.append(identity.current_caller_bearer())
        return {"ok": True}

    asyncio.run(identity.McpCallerIdentityMiddleware(auth_enabled=False)(_context(), call_next))

    assert observed == [""]
    assert identity.current_caller_bearer() == ""


def test_request_bearer_resets_in_finally_after_handler_error() -> None:
    from core.entrypoints.mcp import server_caller_identity as identity

    async def call_next(_ctx):
        assert identity.current_caller_bearer() == "request-token"
        raise RuntimeError("handler failed")

    _raises_input_85_1 = identity.McpCallerIdentityMiddleware(auth_enabled=False)(
        _context("Bearer request-token"), call_next
    )
    with pytest.raises(RuntimeError, match="handler failed"):
        asyncio.run(_raises_input_85_1)

    assert identity.current_caller_bearer() == ""


def test_parallel_requests_keep_bearers_isolated_and_reset() -> None:
    from core.entrypoints.mcp import server_caller_identity as identity

    middleware = identity.McpCallerIdentityMiddleware(auth_enabled=False)
    both_entered = asyncio.Event()
    entered = 0

    async def invoke(token: str) -> tuple[str, str]:
        nonlocal entered

        async def call_next(_ctx):
            nonlocal entered
            first = identity.current_caller_bearer()
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await asyncio.sleep(0)
            return first, identity.current_caller_bearer()

        return await middleware(_context(f"Bearer {token}"), call_next)

    async def run_parallel():
        return await asyncio.gather(invoke("token-a"), invoke("token-b"))

    assert asyncio.run(run_parallel()) == [
        ("token-a", "token-a"),
        ("token-b", "token-b"),
    ]
    assert identity.current_caller_bearer() == ""
