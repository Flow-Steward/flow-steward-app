"""Tests for ResolvedPrincipal and resolve_to_principal."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from datetime import UTC

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.resolve import (
    _normalize_principal_dict,
    normalize_principal,
    resolve_to_principal,
)
from core.application.management.scope import scope_from_principal

USER_ID = "00000000-0000-0000-0000-000000000201"
USER_ID_ALT = "00000000-0000-0000-0000-000000000202"
SERVICE_ACCOUNT_ID = "00000000-0000-0000-0000-000000000301"
PROJECT_ID = "00000000-0000-0000-0000-000000000401"
PROJECT_ID_ALT = "00000000-0000-0000-0000-000000000402"

# ---------------------------------------------------------------------------
# ResolvedPrincipal construction
# ---------------------------------------------------------------------------


def test_resolved_principal_defaults():
    p = ResolvedPrincipal()
    assert p.id == ""
    assert p.username == ""
    assert p.account_id == DEFAULT_ACCOUNT_ID
    assert p.principal_type == "user"
    assert p.roles == []
    assert p.scopes == []
    assert p.allowed_project_ids == []
    assert p.allowed_workflow_ids == []
    assert p.is_active is True


def test_resolved_principal_from_session_user():
    p = ResolvedPrincipal(
        id=USER_ID,
        username="alice",
        display_name="Alice Smith",
        roles=["admin", "operator"],
        auth_source="local",
        principal_type="user",
        account_id="acc_42",
    )
    assert p.id == USER_ID
    assert p.username == "alice"
    assert p.account_id == "acc_42"
    assert p.principal_type == "user"
    assert p.auth_source == "local"
    assert "admin" in p.roles


def test_resolved_principal_from_service_account():
    p = ResolvedPrincipal(
        id=SERVICE_ACCOUNT_ID,
        username="svc-ci",
        auth_source="machine",
        principal_type="service_account",
        scopes=["admin:all"],
        allowed_project_ids=[PROJECT_ID, PROJECT_ID_ALT],
        allowed_workflow_ids=["weekly_digest"],
        account_id="acc_42",
    )
    assert p.principal_type == "service_account"
    assert p.auth_source == "machine"
    assert p.allowed_project_ids == [PROJECT_ID, PROJECT_ID_ALT]
    assert p.allowed_workflow_ids == ["weekly_digest"]


def test_resolved_principal_from_pat():
    p = ResolvedPrincipal(
        id=USER_ID_ALT,
        username="bob",
        roles=["operator"],
        auth_source="pat",
        principal_type="user",
        account_id="acc_42",
    )
    assert p.auth_source == "pat"
    assert p.principal_type == "user"


def test_resolved_principal_from_external():
    p = ResolvedPrincipal(
        id=USER_ID,
        username="carol@example.com",
        display_name="Carol",
        roles=["admin"],
        auth_source="external",
        principal_type="user",
    )
    assert p.auth_source == "external"
    assert p.account_id == DEFAULT_ACCOUNT_ID


def test_to_scope_dict_produces_valid_scope_input():
    p = ResolvedPrincipal(
        id=USER_ID,
        username="alice",
        account_id="acc_42",
        principal_type="user",
        allowed_project_ids=[PROJECT_ID, PROJECT_ID_ALT],
        allowed_workflow_ids=["weekly_digest"],
    )
    scope = scope_from_principal(p.to_scope_dict())
    assert scope.account_id == "acc_42"
    assert PROJECT_ID in scope.allowed_project_ids
    assert PROJECT_ID_ALT in scope.allowed_project_ids
    assert "weekly_digest" in scope.allowed_workflow_ids


# ---------------------------------------------------------------------------
# _normalize_principal_dict
# ---------------------------------------------------------------------------


def test_normalize_session_user_dict():
    raw = {
        "id": USER_ID,
        "username": "alice",
        "display_name": "Alice",
        "is_active": True,
        "roles": ["admin"],
        "auth_source": "local",
        "account_id": "acc_42",
    }
    p = _normalize_principal_dict(raw)
    assert isinstance(p, ResolvedPrincipal)
    assert p.id == USER_ID
    assert p.username == "alice"
    assert p.account_id == "acc_42"
    # interactive_principal_type() derives principal_type from roles for local/proxy
    # auth users so that application permissions can gate admin-only UI features.
    # A user with roles=["admin"] correctly gets principal_type="admin".
    assert p.principal_type == "admin"
    assert p.allowed_project_ids == []
    assert p.allowed_workflow_ids == []


def test_normalize_machine_token_dict():
    raw = {
        "id": SERVICE_ACCOUNT_ID,
        "service_account_key": "svc-deploy",
        "is_active": True,
        "auth_source": "machine",
        "principal_type": "service_account",
        "scopes": ["admin:all"],
        "allowed_project_ids": [PROJECT_ID, PROJECT_ID_ALT],
        "allowed_workflow_ids": ["weekly_digest", "weekly_report"],
        "account_id": "acc_42",
    }
    p = _normalize_principal_dict(raw)
    assert p.username == "svc-deploy"
    assert p.principal_type == "service_account"
    assert p.allowed_project_ids == [PROJECT_ID, PROJECT_ID_ALT]
    assert p.allowed_workflow_ids == ["weekly_digest", "weekly_report"]
    assert p.scopes == ["admin:all"]


def test_normalize_pat_dict():
    raw = {
        "id": USER_ID_ALT,
        "username": "bob",
        "is_active": True,
        "roles": ["operator"],
        "auth_source": "pat",
        "principal_type": "user",
        "account_id": "acc_99",
    }
    p = _normalize_principal_dict(raw)
    assert p.id == USER_ID_ALT
    assert p.username == "bob"
    assert p.auth_source == "pat"
    assert p.account_id == "acc_99"


def test_normalize_missing_account_id_defaults():
    raw = {"id": USER_ID, "username": "alice", "auth_source": "local"}
    p = _normalize_principal_dict(raw)
    assert p.account_id == DEFAULT_ACCOUNT_ID


def test_normalize_external_dict_sets_default_account_and_principal_type():
    raw = {
        "id": USER_ID,
        "username": "external-admin",
        "display_name": "External Admin",
        "roles": ["admin"],
        "auth_source": "external",
    }
    p = normalize_principal(raw)
    assert p is not None
    assert p.auth_source == "external"
    # External auth with roles=["admin"] derives principal_type as "admin".
    assert p.principal_type == "admin"
    assert p.account_id == DEFAULT_ACCOUNT_ID


def test_normalize_coerces_invalid_project_ids():
    raw = {
        "id": SERVICE_ACCOUNT_ID,
        "auth_source": "machine",
        "principal_type": "service_account",
        "allowed_project_ids": [PROJECT_ID, "", None, PROJECT_ID_ALT],
    }
    p = _normalize_principal_dict(raw)
    assert p.allowed_project_ids == [PROJECT_ID, PROJECT_ID_ALT]


def test_normalize_coerces_empty_workflow_ids():
    raw = {
        "id": SERVICE_ACCOUNT_ID,
        "auth_source": "machine",
        "principal_type": "service_account",
        "allowed_workflow_ids": ["weekly_digest", "", None, "weekly_report"],
    }
    p = _normalize_principal_dict(raw)
    assert p.allowed_workflow_ids == ["weekly_digest", "weekly_report"]


def test_resolve_to_principal_normalizes_session_user_path():
    from unittest.mock import MagicMock, patch

    raw = {
        "id": USER_ID,
        "username": "alice",
        "display_name": "Alice",
        "roles": ["operator"],
        "is_active": True,
        "must_change_password": True,
    }
    with patch("core.application.auth.resolve.resolve_bearer_principal", return_value=raw):
        principal = resolve_to_principal(MagicMock(), "token-123")

    assert principal is not None
    assert principal.username == "alice"
    assert principal.auth_source == ""
    assert principal.account_id == DEFAULT_ACCOUNT_ID
    assert principal.must_change_password is True


def test_resolve_to_principal_normalizes_pat_path():
    from unittest.mock import MagicMock, patch

    raw = {
        "id": USER_ID_ALT,
        "username": "bob",
        "roles": ["operator"],
        "scopes": ["read:*"],
        "auth_source": "pat",
        "account_id": "acc_ops",
        "allowed_project_ids": [PROJECT_ID],
        "allowed_workflow_ids": ["wf_demo"],
    }
    with patch("core.application.auth.resolve.resolve_bearer_principal", return_value=raw):
        principal = resolve_to_principal(MagicMock(), "token-123")

    assert principal is not None
    assert principal.auth_source == "pat"
    assert principal.account_id == "acc_ops"
    assert principal.allowed_project_ids == [PROJECT_ID]
    assert principal.allowed_workflow_ids == ["wf_demo"]


def test_resolve_to_principal_normalizes_service_account_path():
    from datetime import datetime
    from unittest.mock import MagicMock, patch

    raw = {
        "principal_type": "service_account",
        "service_account_id": SERVICE_ACCOUNT_ID,
        "service_account_key": "svc-ci",
        "scopes": ["command:*"],
        "auth_source": "machine",
        "account_id": "acc_ops",
        "expires_at": datetime(2026, 3, 24, 8, 0, tzinfo=UTC),
    }
    with patch("core.application.auth.resolve.resolve_bearer_principal", return_value=raw):
        principal = resolve_to_principal(MagicMock(), "token-123")

    assert principal is not None
    assert principal.principal_type == "service_account"
    assert principal.service_account_id == SERVICE_ACCOUNT_ID
    assert principal.service_account_key == "svc-ci"
    assert principal.expires_at is not None


# ---------------------------------------------------------------------------
# End-to-end: ResolvedPrincipal → scope_from_principal → guards
# ---------------------------------------------------------------------------


def test_principal_to_scope_to_guard_pipeline():
    """Verify the full pipeline works without any Request object."""
    import pytest

    from core.application.management.errors import ServiceForbiddenError
    from core.application.management.guards import require_project_access, require_workflow_access

    # Restricted service account
    p = ResolvedPrincipal(
        id=SERVICE_ACCOUNT_ID,
        username="svc-ci",
        auth_source="machine",
        principal_type="service_account",
        account_id="acc_42",
        allowed_project_ids=[PROJECT_ID],
        allowed_workflow_ids=["weekly_digest"],
    )
    scope = scope_from_principal(p.to_scope_dict())

    project_ok = {"id": PROJECT_ID, "account_id": "acc_42"}
    project_denied = {"id": PROJECT_ID_ALT, "account_id": "acc_42"}

    # Allowed project
    require_project_access(scope, project_ok)  # no exception

    # Denied project
    with pytest.raises(ServiceForbiddenError):
        require_project_access(scope, project_denied)

    # Allowed workflow
    require_workflow_access(scope, "weekly_digest")  # no exception

    # Denied workflow
    with pytest.raises(ServiceForbiddenError):
        require_workflow_access(scope, "weekly_report")


def test_unrestricted_principal_accesses_any_project_in_account():
    """Unrestricted token (empty allowed_project_ids) can access all projects within account."""
    from core.application.management.guards import require_project_access

    p = ResolvedPrincipal(
        id=USER_ID,
        username="alice",
        auth_source="local",
        principal_type="user",
        account_id="acc_42",
    )
    scope = scope_from_principal(p.to_scope_dict())

    for pid in [PROJECT_ID, PROJECT_ID_ALT, "00000000-0000-0000-0000-000000000499"]:
        require_project_access(scope, {"id": pid, "account_id": "acc_42"})


def test_cross_account_always_denied():
    """Even unrestricted tokens cannot cross account boundaries."""
    import pytest

    from core.application.management.errors import ServiceForbiddenError
    from core.application.management.guards import require_project_access

    p = ResolvedPrincipal(
        id=USER_ID,
        username="alice",
        auth_source="local",
        principal_type="user",
        account_id="acc_42",
    )
    scope = scope_from_principal(p.to_scope_dict())

    with pytest.raises(ServiceForbiddenError):
        require_project_access(scope, {"id": PROJECT_ID, "account_id": "acc_other"})


def test_viewer_role_ceiling_allows_every_user_selectable_read_category():
    from core.application.auth.principal import enforce_token_scope_within_caller_authority

    caller = {
        "principal_type": "user",
        "auth_source": "local",
        "roles": ["viewer"],
    }
    enforce_token_scope_within_caller_authority(
        caller,
        requested_scopes=[
            "jobs.read",
            "projects.read",
            "registry.read",
            "workflows.read",
            "reports.read",
            "notify.read",
        ],
        requested_project_ids=[],
        requested_workflow_ids=[],
    )


def test_operator_role_ceiling_allows_bounded_write_categories():
    from core.application.auth.principal import enforce_token_scope_within_caller_authority

    caller = {
        "principal_type": "user",
        "auth_source": "local",
        "roles": ["operator"],
    }
    enforce_token_scope_within_caller_authority(
        caller,
        requested_scopes=[
            "jobs.write",
            "projects.write",
            "workflows.write",
            "reports.write",
        ],
        requested_project_ids=[],
        requested_workflow_ids=[],
    )


def test_operator_role_ceiling_does_not_gain_admin_only_categories():
    import pytest

    from core.application.auth.principal import (
        TokenAuthorityError,
        enforce_token_scope_within_caller_authority,
    )

    caller = {
        "principal_type": "user",
        "auth_source": "local",
        "roles": ["operator"],
    }
    for scope in ("registry.write", "notify:dispatch"):
        with pytest.raises(TokenAuthorityError):
            enforce_token_scope_within_caller_authority(
                caller,
                requested_scopes=[scope],
                requested_project_ids=[],
                requested_workflow_ids=[],
            )
