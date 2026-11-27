"""Machine and token principals carry their account explicitly (ADR 0083, security notes).

Principal normalisation no longer fills in the installation account: an empty account
is no account and is refused. Every principal Core builds for a service account, a
personal or machine token and its own system actor names its account itself, so a
normal service-account token keeps working.
"""

from __future__ import annotations

from core.application.auth.auth_http_runtime import mcp_dev_system_principal
from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.resolve import normalize_principal
from tests.unit.test_platform_logger_mixin_coverage import _make_logger
from tests.unit.test_route_permission_parity import middleware_decision

_ACCOUNT = "acc_beta"


def _machine_row(*, token_account: str, service_account: str) -> dict:
    return {
        "token_id": 10,
        "token_account_id": token_account,
        "token_label": "ci",
        "token_scopes_jsonb": ["read:*", "command:*"],
        "token_allowed_project_ids_jsonb": [],
        "token_allowed_workflow_ids_jsonb": [],
        "expires_at": None,
        "revoked": False,
        "service_account_id": 9,
        "service_account_account_id": service_account,
        "service_account_key": "ci",
        "display_name": "CI",
        "description": "",
        "service_scopes_jsonb": ["read:*", "command:*"],
        "service_allowed_project_ids_jsonb": [],
        "service_allowed_workflow_ids_jsonb": [],
        "is_active": True,
    }


def _resolved_machine_principal(row: dict) -> dict:
    logger, conn = _make_logger()
    conn.fetchone_results = [row, {"is_active": True}]
    principal = logger.resolve_machine_token("fs_svc_normal")
    assert principal is not None
    return principal


def test_a_service_account_token_carries_its_account() -> None:
    principal = _resolved_machine_principal(
        _machine_row(token_account=_ACCOUNT, service_account=_ACCOUNT)
    )

    assert principal["account_id"] == _ACCOUNT
    normalized = normalize_principal(principal)
    assert normalized is not None
    assert normalized.account_id == _ACCOUNT
    assert normalized.principal_type == "service_account"


def test_a_service_account_row_without_an_account_names_the_installation_account() -> None:
    principal = _resolved_machine_principal(_machine_row(token_account="", service_account=""))

    assert principal["account_id"] == INSTALLATION_ACCOUNT_ID
    assert normalize_principal(principal) is not None


def test_a_normal_service_account_token_still_passes_the_middleware() -> None:
    principal = _resolved_machine_principal(
        _machine_row(token_account=_ACCOUNT, service_account=_ACCOUNT)
    )

    allowed, state = middleware_decision(
        "/api/jobs",
        "GET",
        principal,
        public=False,
        real_normalize=True,
        active_accounts=frozenset({_ACCOUNT}),
    )

    assert allowed
    assert state.auth_principal.account_id == _ACCOUNT


def test_a_token_principal_without_an_account_is_refused() -> None:
    """The fail-closed side: a token principal that names no account never signs in."""
    principal = _resolved_machine_principal(
        _machine_row(token_account=_ACCOUNT, service_account=_ACCOUNT)
    )
    principal["account_id"] = ""

    assert normalize_principal(principal) is None
    allowed, _state = middleware_decision(
        "/api/jobs",
        "GET",
        principal,
        public=False,
        real_normalize=True,
        active_accounts=frozenset({_ACCOUNT}),
    )
    assert not allowed


def test_a_personal_token_principal_carries_its_account() -> None:
    principal = {
        "principal_type": "user",
        "id": "u-1",
        "username": "pat@acme.test",
        "account_id": _ACCOUNT,
        "scopes": ["read:*"],
        "roles": ["viewer"],
        "is_active": True,
        "auth_source": "pat",
    }

    allowed, state = middleware_decision(
        "/api/jobs",
        "GET",
        principal,
        public=False,
        real_normalize=True,
        active_accounts=frozenset({_ACCOUNT}),
    )

    assert allowed
    assert state.auth_principal.account_id == _ACCOUNT


def test_cores_system_actor_names_the_installation_account(monkeypatch) -> None:
    from core.application.auth import auth_http_runtime

    monkeypatch.setattr(auth_http_runtime, "mcp_dev_proxy_enabled", lambda: True)

    from types import SimpleNamespace

    request = SimpleNamespace(headers={auth_http_runtime.MCP_DEV_AUTH_HEADER: "1"})
    principal = mcp_dev_system_principal(request)

    assert principal is not None
    assert principal.account_id == INSTALLATION_ACCOUNT_ID
    assert "account_id" in principal.model_fields_set
