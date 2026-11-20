"""Exact-only secret scopes and who may change, bind and approve catalog secrets (ADR 0057)."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from core.application.auth.scope_contracts import EXACT_ONLY_SCOPES, scope_granted
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.secrets import authority, repository, workflow_gate
from core.application.secrets.projection import projection_fingerprint
from core.application.secrets.service import SecretCatalogService
from core.infrastructure.db import platform_logger_common

WILDCARDS = ("admin:all", "*:*", "secrets:*", "*:bind", "*:write")
PROJECT_ID = "00000000-0000-0000-0000-000000000007"


@pytest.mark.parametrize("scope", sorted(EXACT_ONLY_SCOPES))
@pytest.mark.parametrize("wildcard", WILDCARDS)
def test_no_wildcard_grants_a_secret_scope(scope, wildcard) -> None:
    assert scope_granted({wildcard}, scope) is False
    assert platform_logger_common._scope_granted({wildcard}, scope) is False


@pytest.mark.parametrize("scope", sorted(EXACT_ONLY_SCOPES))
def test_the_exact_scope_still_grants_next_to_wildcards(scope) -> None:
    assert scope_granted({scope, *WILDCARDS}, scope) is True
    assert platform_logger_common._scope_granted({scope}, scope) is True


def test_both_scope_checks_name_the_same_exact_scopes() -> None:
    assert platform_logger_common._EXACT_ONLY_SCOPES == EXACT_ONLY_SCOPES


def test_other_scopes_keep_their_wildcards() -> None:
    assert scope_granted({"admin:all"}, "workflows:write") is True
    assert scope_granted({"workflows:*"}, "workflows:write") is True


def _scope(**fields) -> ScopeContext:
    return ScopeContext(account_id="acct", **fields)


def test_an_interactive_operator_writes_binds_and_approves() -> None:
    person = _scope(principal_type="operator", auth_source="local")
    authority.require_secret_write(person)
    authority.require_secret_bind(person)
    authority.require_approval_authority(person)


def test_a_viewer_does_none_of_it() -> None:
    viewer = _scope(principal_type="viewer", auth_source="local")
    for check in (
        authority.require_secret_write,
        authority.require_secret_bind,
        authority.require_approval_authority,
    ):
        with pytest.raises(ServiceForbiddenError):
            check(viewer)


@pytest.mark.parametrize("source", ["pat", "machine", "token", "oauth"])
def test_a_token_needs_the_exact_scope_whatever_its_role(source) -> None:
    broad = _scope(principal_type="admin", auth_source=source, token_scopes=frozenset(WILDCARDS))
    with pytest.raises(ServiceForbiddenError):
        authority.require_secret_bind(broad)
    with pytest.raises(ServiceForbiddenError):
        authority.require_secret_write(broad)
    exact = _scope(
        principal_type="operator",
        auth_source=source,
        token_scopes=frozenset({"secrets:bind", "secrets:write"}),
    )
    authority.require_secret_bind(exact)
    authority.require_secret_write(exact)
    with pytest.raises(ServiceForbiddenError):
        authority.require_approval_authority(exact)


@pytest.mark.parametrize(
    "fields",
    [
        {"principal_type": "service_account", "auth_source": "local"},
        {"principal_type": "admin", "auth_source": ""},
        {"principal_type": "admin", "auth_source": "mcp_dev"},
        {"principal_type": "system_actor", "auth_source": "local"},
    ],
)
def test_only_a_trusted_interactive_session_counts_as_a_person(fields) -> None:
    scope = _scope(**fields)
    assert authority.is_interactive_human(scope) is False
    with pytest.raises(ServiceForbiddenError):
        authority.require_approval_authority(scope)


@pytest.mark.parametrize(
    ("allowed_workflow_ids", "visible"),
    [
        (frozenset({"allowed"}), ["allowed"]),
        (frozenset(), ["allowed", "other"]),
    ],
)
def test_workflow_approval_views_apply_workflow_scope(
    monkeypatch, allowed_workflow_ids, visible
) -> None:
    service = SecretCatalogService(lambda: None)
    cur = Mock()
    cur.fetchall.return_value = [("allowed", "Allowed"), ("other", "Other")]
    cur.fetchone.return_value = (1,)
    monkeypatch.setattr(service, "_transaction", lambda: nullcontext((object(), cur)))
    monkeypatch.setattr(
        repository,
        "project_scope",
        Mock(return_value={"project_id": PROJECT_ID, "workspace_id": "workspace"}),
    )
    workflow_row = Mock(return_value={})
    monkeypatch.setattr(workflow_gate, "workflow_row", workflow_row)
    monkeypatch.setattr(workflow_gate, "current_definition", Mock(return_value={}))
    monkeypatch.setattr(
        service,
        "_workflow_status_view",
        lambda _cur, _scope, _found: {"secret_inputs": ["authorization"]},
    )
    machine = _scope(
        principal_type="operator",
        auth_source="pat",
        token_scopes=frozenset({"read:*"}),
        allowed_project_ids=frozenset({PROJECT_ID}),
        allowed_workflow_ids=allowed_workflow_ids,
    )

    statuses = service.project_workflow_statuses(PROJECT_ID, machine)

    assert [status["display_name"] for status in statuses] == [name.title() for name in visible]
    assert [call.kwargs["workflow_id"] for call in workflow_row.call_args_list] == visible
    if allowed_workflow_ids:
        workflow_row.reset_mock()
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.workflow_approval_status("other", machine, project_id=PROJECT_ID)
        workflow_row.assert_not_called()


def test_connection_approval_read_and_write_apply_owning_workspace(monkeypatch) -> None:
    service = SecretCatalogService(lambda: None)
    cur = Mock()
    monkeypatch.setattr(service, "_transaction", lambda: nullcontext((object(), cur)))
    monkeypatch.setattr(
        repository,
        "lock_connection",
        Mock(return_value={"connection_id": "connection", "project_id": PROJECT_ID}),
    )
    project_scope = Mock(return_value={"project_id": PROJECT_ID, "workspace_id": "owner"})
    monkeypatch.setattr(repository, "project_scope", project_scope)
    current = {"bindings": {"authorization": "reference"}, "release": {"release_id": "release"}}
    monkeypatch.setattr(
        "core.application.secrets.service.connection_projection_for",
        Mock(return_value=current),
    )
    status = Mock(return_value={"status": "approved"})
    monkeypatch.setattr(service, "_status_view", status)
    record = Mock()
    monkeypatch.setattr(repository, "record_connection_approval", record)
    monkeypatch.setattr(service, "_audit", Mock())
    external = _scope(
        principal_type="operator",
        auth_source="external",
        allowed_workspace_ids=frozenset({"other"}),
    )
    approvals = {"connection": projection_fingerprint(current)}

    with pytest.raises(ServiceForbiddenError, match="Access denied"):
        service.connection_approval_status("connection", external, project_id=PROJECT_ID)
    with pytest.raises(ServiceForbiddenError, match="Access denied"):
        service.approve_connections(external, approvals=approvals, project_id=PROJECT_ID)
    status.assert_not_called()
    record.assert_not_called()
    project_scope.assert_called_with(cur, account_id="acct", project_id=PROJECT_ID)

    owner = external.model_copy(update={"allowed_workspace_ids": frozenset({"owner"})})
    assert service.connection_approval_status("connection", owner, project_id=PROJECT_ID) == {
        "status": "approved"
    }
    assert service.approve_connections(owner, approvals=approvals, project_id=PROJECT_ID) == [
        {"status": "approved"}
    ]
    record.assert_called_once()

    project_scope.reset_mock()
    project_only = external.model_copy(
        update={
            "allowed_workspace_ids": frozenset(),
            "allowed_project_ids": frozenset({PROJECT_ID}),
        }
    )
    assert service.connection_approval_status(
        "connection", project_only, project_id=PROJECT_ID
    ) == {"status": "approved"}
    project_scope.assert_not_called()
