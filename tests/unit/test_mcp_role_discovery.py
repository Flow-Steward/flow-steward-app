"""ADR 0076 §4: ``tools/list`` shows the tools the caller's role allows in the token's account.

Discovery is presentation only; the application API still decides every call.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from core.application.auth.effective_permissions import effective_permissions
from core.application.auth.permissions import SYSTEM_ROLE_PERMISSIONS
from core.entrypoints.api.auth_users_introspection import introspection_payload
from core.entrypoints.mcp.server_api_client import AppApiClient
from core.entrypoints.mcp.server_discovery import (
    PERMISSIONS_PATH,
    CallerPermissionLookup,
    McpToolDiscoveryMiddleware,
    visible_mcp_tool_names,
)
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES, tool_allowed_for_permissions
from tests.unit.test_route_permission_parity import middleware_decision

ALL_TOOLS = sorted(MCP_TOOL_SCOPES)
_SECRET_MARK = "fs_pat_secret_value_never_logged"  # pragma: allowlist secret


def _role_keys(role: str) -> list[str]:
    person = {"id": f"{role}-1", "username": role, "roles": [role], "auth_source": "local"}
    return sorted(
        effective_permissions(person, role_permissions=SYSTEM_ROLE_PERMISSIONS).permissions
    )


def _request(state: object) -> Any:
    """A stand-in request carrying only ``state``."""
    return cast(Any, SimpleNamespace(state=state))


def _introspection(permissions: list[str] | None) -> dict:
    principal: dict = {"id": "u-1", "principal_type": "user", "scopes": ["admin:all"]}
    if permissions is not None:
        principal["permissions"] = permissions
    return {"active": True, "principal": principal}


class _FakeApi:
    """The app's introspection route, answering per bearer; counts calls per bearer."""

    def __init__(self, by_bearer: dict[str, object]) -> None:
        self.by_bearer = by_bearer
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == PERMISSIONS_PATH
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        self.calls.append(bearer)
        answer = self.by_bearer.get(bearer)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": "nope"})
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer)

    def lookup(self, **kwargs) -> CallerPermissionLookup:
        client = AppApiClient(base_url="http://fs-app", transport=httpx.MockTransport(self.handler))
        return CallerPermissionLookup(client=client, **kwargs)


def _list_tools(monkeypatch, middleware, *, token: str, scopes: list[str]) -> list[str]:
    from mcp.types import ListToolsResult, Tool

    monkeypatch.setattr(
        "mcp.server.auth.middleware.auth_context.get_access_token",
        lambda: SimpleNamespace(token=token, scopes=scopes),
    )
    result = ListToolsResult(
        tools=[Tool.model_validate({"name": name, "inputSchema": {}}) for name in ALL_TOOLS]
    )

    async def _next(_ctx):
        return result

    listed = asyncio.run(middleware(SimpleNamespace(method="tools/list"), _next))
    return sorted(tool.name for tool in listed.tools)


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
def test_a_role_sees_exactly_the_tools_its_permissions_allow(monkeypatch, role) -> None:
    keys = _role_keys(role)
    api = _FakeApi({f"tok-{role}": _introspection(keys)})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    listed = _list_tools(monkeypatch, middleware, token=f"tok-{role}", scopes=["admin:all"])

    expected = sorted(tool for tool in ALL_TOOLS if tool_allowed_for_permissions(tool, set(keys)))
    assert listed == expected
    if role == "admin":
        assert listed == ALL_TOOLS
    else:
        assert listed != ALL_TOOLS


def test_viewer_list_is_narrower_than_operator_list_which_is_narrower_than_admin() -> None:
    def allowed(role: str) -> set[str]:
        keys = set(_role_keys(role))
        return {tool for tool in ALL_TOOLS if tool_allowed_for_permissions(tool, keys)}

    assert allowed("viewer") < allowed("operator") < allowed("admin")
    assert "job_cancel" not in allowed("viewer")
    assert "job_list" in allowed("viewer")


def test_role_filter_applies_after_the_token_scope_filter(monkeypatch) -> None:
    api = _FakeApi({"tok": _introspection(_role_keys("admin"))})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    listed = _list_tools(monkeypatch, middleware, token="tok", scopes=["jobs.read"])

    assert listed == sorted(visible_mcp_tool_names(["jobs.read"], auth_enabled=True))


def test_the_token_account_role_decides_not_the_persons_other_accounts(monkeypatch) -> None:
    """Admin in account B, viewer in account A: a token bound to A lists the viewer's tools.

    The app resolves the acting account's role for the bearer (ADR 0085); its
    introspection of the A token carries the viewer's keys, and discovery follows it.
    """
    viewer_in_a = {"id": "p-1", "username": "pat", "roles": ["viewer"], "account_id": "acc-a"}
    viewer_in_a.update(auth_source="pat", scopes=["admin:all"])
    allowed, state = middleware_decision(PERMISSIONS_PATH, "GET", viewer_in_a, public=False)
    assert allowed
    a_payload = introspection_payload(_request(state), cast(Any, state).auth_user)
    assert a_payload["principal"]["account_id"] == "acc-a"
    assert a_payload["principal"]["permissions"] == _role_keys("viewer")

    api = _FakeApi(
        {"tok-account-a": a_payload, "tok-account-b": _introspection(_role_keys("admin"))}
    )
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    in_a = _list_tools(monkeypatch, middleware, token="tok-account-a", scopes=["admin:all"])
    in_b = _list_tools(monkeypatch, middleware, token="tok-account-b", scopes=["admin:all"])

    viewer_keys = set(_role_keys("viewer"))
    assert in_a == sorted(t for t in ALL_TOOLS if tool_allowed_for_permissions(t, viewer_keys))
    assert in_b == ALL_TOOLS


@pytest.mark.parametrize("failure", [503, 401, httpx.ConnectError("down"), "malformed"])
def test_a_failed_lookup_falls_back_to_the_scope_only_list(monkeypatch, caplog, failure) -> None:
    answer = {"active": False} if failure == "malformed" else failure
    api = _FakeApi({_SECRET_MARK: answer})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    with caplog.at_level(logging.WARNING, logger="platform.mcp.discovery"):
        listed = _list_tools(monkeypatch, middleware, token=_SECRET_MARK, scopes=["read:*"])

    assert listed == sorted(visible_mcp_tool_names(["read:*"], auth_enabled=True))
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert _SECRET_MARK not in caplog.text


def test_a_failed_lookup_is_not_cached() -> None:
    api = _FakeApi({"tok": 503})
    lookup = api.lookup()

    assert asyncio.run(lookup.permissions("tok")) is None
    api.by_bearer["tok"] = _introspection(["jobs.read"])
    assert asyncio.run(lookup.permissions("tok")) == frozenset({"jobs.read"})
    assert api.calls == ["tok", "tok"]


def test_a_principal_without_role_permissions_keeps_the_scope_only_list(monkeypatch) -> None:
    """Service accounts are not role based: introspection carries no permission keys."""
    api = _FakeApi({"sa-token": _introspection(None)})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    listed = _list_tools(monkeypatch, middleware, token="sa-token", scopes=["admin:all"])

    assert listed == ALL_TOOLS


def test_a_role_without_permissions_sees_no_tools(monkeypatch) -> None:
    api = _FakeApi({"tok": _introspection([])})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    assert _list_tools(monkeypatch, middleware, token="tok", scopes=["admin:all"]) == []


def test_the_cache_is_per_token_and_expires() -> None:
    now = [100.0]
    api = _FakeApi(
        {"tok-1": _introspection(["jobs.read"]), "tok-2": _introspection(["projects.read"])}
    )
    lookup = api.lookup(ttl_seconds=30, clock=lambda: now[0])

    assert asyncio.run(lookup.permissions("tok-1")) == frozenset({"jobs.read"})
    assert asyncio.run(lookup.permissions("tok-1")) == frozenset({"jobs.read"})
    assert asyncio.run(lookup.permissions("tok-2")) == frozenset({"projects.read"})
    assert api.calls == ["tok-1", "tok-2"]

    now[0] += 31
    api.by_bearer["tok-1"] = _introspection(["jobs.write"])
    assert asyncio.run(lookup.permissions("tok-1")) == frozenset({"jobs.write"})
    assert api.calls == ["tok-1", "tok-2", "tok-1"]


def test_the_cache_is_bounded_and_never_keeps_the_raw_token() -> None:
    api = _FakeApi({f"tok-{i}": _introspection(["jobs.read"]) for i in range(5)})
    lookup = api.lookup(max_entries=2)

    for i in range(5):
        asyncio.run(lookup.permissions(f"tok-{i}"))

    assert len(lookup._entries) == 2
    assert not any(key.startswith("tok-") for key in lookup._entries)
    asyncio.run(lookup.permissions("tok-0"))
    assert api.calls.count("tok-0") == 2


def test_no_bearer_means_no_lookup(monkeypatch) -> None:
    api = _FakeApi({})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    listed = _list_tools(monkeypatch, middleware, token="", scopes=["read:*"])

    assert listed == sorted(visible_mcp_tool_names(["read:*"], auth_enabled=True))
    assert api.calls == []


def test_auth_disabled_lists_every_tool_without_a_lookup(monkeypatch) -> None:
    api = _FakeApi({})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=False, permission_lookup=api.lookup())

    listed = _list_tools(monkeypatch, middleware, token="tok", scopes=[])

    assert listed == ALL_TOOLS
    assert api.calls == []


def test_other_methods_are_untouched_and_never_look_up() -> None:
    api = _FakeApi({})
    middleware = McpToolDiscoveryMiddleware(auth_enabled=True, permission_lookup=api.lookup())

    async def _next(_ctx):
        return {"content": []}

    result = asyncio.run(middleware(SimpleNamespace(method="tools/call"), _next))

    assert result == {"content": []}
    assert api.calls == []


def test_introspection_names_role_permissions_only_for_role_based_principals() -> None:
    request = _request(SimpleNamespace(auth_source="pat"))
    person = {
        "id": "u-1",
        "roles": ["operator"],
        "auth_source": "pat",
        "scopes": ["read:*"],
        "permissions": ["projects.read", "jobs.read", "jobs.read"],
    }
    assert introspection_payload(request, person)["principal"]["permissions"] == [
        "jobs.read",
        "projects.read",
    ]

    machine = {"id": "sa-1", "principal_type": "service_account", "auth_source": "machine"}
    machine_request = _request(SimpleNamespace(auth_source="machine"))
    assert "permissions" not in introspection_payload(machine_request, machine)["principal"]
