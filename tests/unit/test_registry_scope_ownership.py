"""Ownership-check invariants for the registry admin surface.

Remediation epic 2026-07-29, WS-02.

`CLAUDE-P3-01` — `row_in_account_scope` returned `True` for any non-dict row so
that `MagicMock` test doubles would pass. A fail-open branch inside a production
ownership check is a cross-account hole waiting for the first store that returns
a row object instead of a dict. It now fails closed, and the store doubles that
relied on it return real rows.

`CLAUDE-P1-01` / `CLAUDE-P3-16` — the agent history/clone/restore routes gained
account scoping in `c0ae854`, but the audit's fix also required replacing the
hardcoded `author="admin"` / `"admin_restore"` with the resolved actor, so the
changelog attributes a cross-account restore to whoever actually performed it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.application.management.scope import normalize_scope
from core.entrypoints.admin._registry_scope import (
    assert_row_in_account_scope,
    row_in_account_scope,
)

ACCOUNT_A = "00000000-0000-0000-0000-0000000000aa"
ACCOUNT_B = "00000000-0000-0000-0000-0000000000bb"
ADMIN_ROUTES = Path(__file__).resolve().parents[2] / "core" / "entrypoints" / "admin"


def scope_for(account_id: str):
    return normalize_scope(account_id=account_id)


class TestRowInAccountScopeFailsClosed:
    def test_a_row_owned_by_another_account_is_out_of_scope(self):
        assert row_in_account_scope({"account_id": ACCOUNT_B}, scope_for(ACCOUNT_A)) is False

    def test_a_row_owned_by_the_callers_account_is_in_scope(self):
        assert row_in_account_scope({"account_id": ACCOUNT_A}, scope_for(ACCOUNT_A)) is True

    def test_a_platform_global_row_is_visible_to_every_scoped_admin(self):
        assert row_in_account_scope({"account_id": ""}, scope_for(ACCOUNT_A)) is True
        assert row_in_account_scope({"agent_id": "release_writer"}, scope_for(ACCOUNT_A)) is True

    def test_an_owned_row_is_refused_when_the_caller_has_no_account(self):
        assert row_in_account_scope({"account_id": ACCOUNT_B}, normalize_scope()) is False

    def test_a_missing_row_is_out_of_scope(self):
        assert row_in_account_scope(None, scope_for(ACCOUNT_A)) is False

    @pytest.mark.parametrize(
        "row",
        [
            object(),
            "release_writer",
            ["release_writer"],
            42,
        ],
    )
    def test_a_non_mapping_row_fails_closed(self, row):
        """CLAUDE-P3-01: the fail-open branch that existed for MagicMock doubles."""
        assert row_in_account_scope(row, scope_for(ACCOUNT_A)) is False

    def test_a_mock_style_double_no_longer_slips_through(self):
        from unittest.mock import MagicMock

        assert row_in_account_scope(MagicMock(), scope_for(ACCOUNT_A)) is False


class TestAssertRowInAccountScope:
    def test_a_cross_account_row_raises_404_not_403(self):
        """404 rather than 403 is the platform's deliberate anti-enumeration choice."""
        from fastapi import HTTPException

        _raises_input_80_1 = scope_for(ACCOUNT_A)
        with pytest.raises(HTTPException) as excinfo:
            assert_row_in_account_scope(
                {"account_id": ACCOUNT_B}, _raises_input_80_1, detail="Agent 'x' not found"
            )

        assert excinfo.value.status_code == 404

    def test_the_detail_does_not_reveal_that_the_resource_exists(self):
        from fastapi import HTTPException

        _raises_input_90_1 = scope_for(ACCOUNT_A)
        with pytest.raises(HTTPException) as excinfo:
            assert_row_in_account_scope(
                {"account_id": ACCOUNT_B, "display_name": "Victim Agent", "prompts": {"a": "b"}},
                _raises_input_90_1,
                detail="Agent 'release_writer' not found",
            )

        detail = str(excinfo.value.detail)
        assert "Victim Agent" not in detail
        assert "prompts" not in detail

    def test_an_in_scope_row_is_returned_unchanged(self):
        row = {"account_id": ACCOUNT_A, "agent_id": "release_writer"}

        assert assert_row_in_account_scope(row, scope_for(ACCOUNT_A), detail="nope") is row


class TestAuditAuthorsResolveToTheActor:
    """CLAUDE-P3-16: the changelog must record who acted, not a constant."""

    def test_no_admin_route_hardcodes_its_audit_author(self):
        offenders = []
        for path in sorted(ADMIN_ROUTES.glob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r'author\s*=\s*"admin"', line):
                    offenders.append(f"{path.name}:{number}")

        assert not offenders, f"hardcoded audit authors remain: {offenders}"

    def test_agent_restore_attributes_the_change_to_the_caller(self):
        source = (ADMIN_ROUTES / "agents_history_routes.py").read_text(encoding="utf-8")

        assert 'author="admin_restore"' not in source
        assert 'author=f"{get_scope(request).actor_name}_restore"' in source

    def test_every_agent_history_route_asserts_account_scope(self):
        """Each handler scopes inline or forwards caller scope to the clone service."""
        source = (ADMIN_ROUTES / "agents_history_routes.py").read_text(encoding="utf-8")
        registration = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "register_agent_history_routes"
        )
        handlers = {
            node.name: node for node in registration.body if isinstance(node, ast.FunctionDef)
        }

        assert set(handlers) == {
            "clone_agent",
            "get_agent_history",
            "get_agent_revision",
            "get_agent_diff",
            "restore_agent_revision",
        }
        for name, handler in handlers.items():
            body = ast.get_source_segment(source, handler)
            assert body is not None
            if name == "clone_agent":
                assert "scope = get_scope(request)" in body
                calls = [
                    node
                    for node in ast.walk(handler)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "clone_agent_command"
                ]
                assert len(calls) == 1
                assert any(
                    keyword.arg == "scope"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "scope"
                    for keyword in calls[0].keywords
                )
            else:
                assert "assert_in_scope(source, get_scope(request)" in body, name

    def test_clone_route_delegated_scope_refuses_cross_account_before_persistence(self):
        from fastapi import APIRouter, FastAPI
        from fastapi.testclient import TestClient

        from core.application.management.command_services_agent import AgentCommandService
        from core.entrypoints.admin.agents_history_routes import register_agent_history_routes

        db = MagicMock()
        db.get_agent.return_value = {
            "agent_id": "source",
            "account_id": ACCOUNT_B,
            "display_name": "Private source agent",
        }
        reload_registry = MagicMock()
        service = AgentCommandService(get_db=lambda: db, reload_registry=reload_registry)
        clone_command = MagicMock(wraps=service.clone_agent)
        router = APIRouter(prefix="/agents")
        register_agent_history_routes(
            router,
            get_db=lambda: db,
            get_registry=MagicMock(),
            get_agent_validation=MagicMock(),
            get_scope=lambda _request: scope_for(ACCOUNT_A),
            assert_in_scope=MagicMock(),
            clone_agent_command=clone_command,
        )
        app = FastAPI()
        app.include_router(router)

        with TestClient(app) as client:
            response = client.post("/agents/source/clone", json={"agent_id": "source_copy"})

        assert response.status_code == 404
        assert response.json() == {"detail": "Agent 'source' not found"}
        assert clone_command.call_args.kwargs["scope"].account_id == ACCOUNT_A
        db.save_agent.assert_not_called()
        reload_registry.assert_not_called()
