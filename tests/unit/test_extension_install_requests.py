"""Requests to install an extension from a company account (ADR 0084), in memory.

An account administrator of a company account asks for a catalog extension; the
installation administrator installs it through the catalog install call and approves,
which enables it for that account, or declines with a reason. Nobody else reads or
answers a request, another account's request is answered as missing, and every change
is written to the audit trail. The same rules on PostgreSQL are in
``tests/db/test_extension_install_requests_postgres.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.extensions.account_enablement import (
    ExtensionAccountEnablement,
    ExtensionAccountEnablementService,
    InMemoryExtensionAccountEnablementStore,
)
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.install_requests import (
    ExtensionInstallRequestService,
    InMemoryExtensionInstallRequestStore,
    can_request_extension_installs,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

EXTENSION = "acme.crm-sync"
OTHER = "acme.mailer"
ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
ACCOUNT_B = "22222222-2222-2222-2222-222222222222"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
INSTALLER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CATALOG = {EXTENSION: "CRM sync", OTHER: "Mailer"}


def _scope(account_id: str, role: str = "admin", **extra: Any) -> ScopeContext:
    user = INSTALLER if account_id == INSTALLATION_ACCOUNT_ID else USER_A
    values: dict[str, Any] = {
        "account_id": account_id,
        "principal_type": role,
        "actor_id": "ana",
        "user_id": user,
    }
    values.update(extra)
    return ScopeContext(**values)


class _AuditLogger:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_action(self, **values: Any) -> None:
        self.rows.append(values)


@pytest.fixture
def world() -> dict[str, Any]:
    installs = ExtensionInstallStore()
    installs.account_enablements = InMemoryExtensionAccountEnablementStore()
    requests = InMemoryExtensionInstallRequestStore(account_names={ACCOUNT_A: "Company A"})
    audit = _AuditLogger()
    service = ExtensionInstallRequestService(
        get_store=lambda: requests,
        get_install_store=lambda: installs,
        get_enablement_service=lambda: ExtensionAccountEnablementService(
            get_install_store=lambda: installs,
            get_enablement_store=lambda: installs.account_enablements,
        ),
        catalog_name=CATALOG.get,
        get_audit_logger=lambda: audit,
    )
    return {"installs": installs, "requests": requests, "audit": audit, "service": service}


def _install(world: dict[str, Any], extension_id: str = EXTENSION) -> Any:
    return world["installs"].install(
        extension_id=extension_id, version="1.0.0", account_id=INSTALLATION_ACCOUNT_ID
    )


def test_an_account_admin_requests_and_sees_it_pending(world) -> None:
    service = world["service"]

    created = service.create(EXTENSION, note="We need it for orders", scope=_scope(ACCOUNT_A))
    listed = service.list_requests(scope=_scope(ACCOUNT_A))

    assert created["status"] == "pending"
    assert created["extension_name"] == "CRM sync"
    assert created["note"] == "We need it for orders"
    assert created["requested_by"] == USER_A
    assert listed["scope"] == "account"
    assert [item["id"] for item in listed["items"]] == [created["id"]]
    assert world["audit"].rows[-1]["action_type"] == "extension_install_request_created"
    assert world["audit"].rows[-1]["meta"]["audit_category"] == "extensions"


def test_one_pending_request_per_account_and_extension(world) -> None:
    service = world["service"]
    service.create(EXTENSION, scope=_scope(ACCOUNT_A))

    with pytest.raises(ServiceConflictError) as refused:
        service.create(EXTENSION, scope=_scope(ACCOUNT_A))

    assert refused.value.payload["code"] == "extension_install_already_requested"
    # Another extension, and the same extension from another account, are separate.
    service.create(OTHER, scope=_scope(ACCOUNT_A))
    service.create(EXTENSION, scope=_scope(ACCOUNT_B))


def test_a_cancelled_or_declined_request_can_be_made_again(world) -> None:
    service = world["service"]
    first = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    service.cancel(first["id"], scope=_scope(ACCOUNT_A))
    second = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    service.decline(second["id"], reason="Not this year", scope=_scope(INSTALLATION_ACCOUNT_ID))

    third = service.create(EXTENSION, scope=_scope(ACCOUNT_A))

    statuses = [item["status"] for item in service.list_requests(scope=_scope(ACCOUNT_A))["items"]]
    assert statuses == ["pending", "declined", "cancelled"]
    assert third["id"] not in {first["id"], second["id"]}


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_only_an_account_administrator_requests_cancels_and_lists(world, role) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))

    with pytest.raises(ServiceForbiddenError):
        service.create(OTHER, scope=_scope(ACCOUNT_A, role))
    with pytest.raises(ServiceForbiddenError):
        service.cancel(request["id"], scope=_scope(ACCOUNT_A, role))
    with pytest.raises(ServiceForbiddenError):
        service.list_requests(scope=_scope(ACCOUNT_A, role))


def test_a_caller_without_an_account_or_scope_is_refused(world) -> None:
    service = world["service"]

    with pytest.raises(ServiceForbiddenError):
        service.create(EXTENSION, scope=None)
    with pytest.raises(ServiceForbiddenError):
        service.list_requests(scope=None)
    with pytest.raises(ServiceForbiddenError):
        service.create(EXTENSION, scope=_scope(""))


def test_an_admin_token_without_admin_scope_is_refused(world) -> None:
    token = _scope(ACCOUNT_A, token_scopes=frozenset({"read:*"}))

    with pytest.raises(ServiceForbiddenError):
        world["service"].create(EXTENSION, scope=token)
    assert can_request_extension_installs(token) is False


def test_the_installation_account_installs_directly_and_never_requests(world) -> None:
    with pytest.raises(ServiceValidationError) as refused:
        world["service"].create(EXTENSION, scope=_scope(INSTALLATION_ACCOUNT_ID))

    assert refused.value.payload["code"] == "extension_install_request_installation_account"
    assert can_request_extension_installs(_scope(INSTALLATION_ACCOUNT_ID)) is False
    assert can_request_extension_installs(_scope(ACCOUNT_A)) is True
    assert can_request_extension_installs(_scope(ACCOUNT_A, "operator")) is False
    assert can_request_extension_installs(None) is False


def test_an_installed_extension_is_enabled_not_requested(world) -> None:
    _install(world)

    with pytest.raises(ServiceConflictError) as refused:
        world["service"].create(EXTENSION, scope=_scope(ACCOUNT_A))

    assert refused.value.payload["code"] == "extension_already_installed"


def test_only_catalog_extensions_can_be_requested(world) -> None:
    service = world["service"]

    with pytest.raises(ServiceNotFoundError):
        service.create("acme.unknown", scope=_scope(ACCOUNT_A))
    with pytest.raises(ServiceValidationError):
        service.create("not an id", scope=_scope(ACCOUNT_A))
    with pytest.raises(ServiceValidationError):
        service.create(EXTENSION, note="x" * 501, scope=_scope(ACCOUNT_A))


def test_an_unreachable_catalog_refuses_the_request(world) -> None:
    def _unreachable(_extension_id: str) -> str | None:
        raise ServiceUnavailableError("The extension catalog cannot be reached now")

    service = ExtensionInstallRequestService(
        get_store=lambda: world["requests"],
        get_install_store=lambda: world["installs"],
        get_enablement_service=lambda: None,
        catalog_name=_unreachable,
    )
    with pytest.raises(ServiceUnavailableError):
        service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    assert world["requests"].list_pending() == []


def test_the_installation_admin_lists_every_pending_request(world) -> None:
    service = world["service"]
    a = service.create(EXTENSION, note="please", scope=_scope(ACCOUNT_A))
    b = service.create(OTHER, scope=_scope(ACCOUNT_B))
    service.cancel(b["id"], scope=_scope(ACCOUNT_B))

    listed = service.list_requests(scope=_scope(INSTALLATION_ACCOUNT_ID))

    assert listed["scope"] == "installation"
    assert [item["id"] for item in listed["items"]] == [a["id"]]
    assert listed["items"][0]["account_name"] == "Company A"
    assert listed["items"][0]["note"] == "please"


def test_another_account_never_sees_or_cancels_a_request(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))

    assert service.list_requests(scope=_scope(ACCOUNT_B))["items"] == []
    with pytest.raises(ServiceNotFoundError):
        service.cancel(request["id"], scope=_scope(ACCOUNT_B))
    with pytest.raises(ServiceNotFoundError):
        service.cancel(str(uuid.uuid4()), scope=_scope(ACCOUNT_A))
    assert world["requests"].get(request["id"]).status == "pending"


def test_an_account_admin_cannot_approve_or_decline(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    _install(world)

    for scope in (
        _scope(ACCOUNT_A),
        _scope(ACCOUNT_B),
        _scope(INSTALLATION_ACCOUNT_ID, "operator"),
    ):
        with pytest.raises(ServiceForbiddenError) as refused:
            service.approve(request["id"], scope=scope)
        assert refused.value.payload["code"] == "installation_admin_required"
        with pytest.raises(ServiceForbiddenError):
            service.decline(request["id"], scope=scope)
    assert world["requests"].get(request["id"]).status == "pending"
    assert (
        world["installs"].account_enablements.get(account_id=ACCOUNT_A, extension_id=EXTENSION)
        is None
    )


def test_approving_after_the_install_enables_it_for_the_asking_account(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    install = _install(world)  # the page's catalog install call

    approved = service.approve(request["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))

    enablements = world["installs"].account_enablements
    row = enablements.get(account_id=ACCOUNT_A, extension_id=EXTENSION)
    assert approved["status"] == "approved"
    assert row is not None and row.enabled and row.install_id == install.install_id
    assert enablements.get(account_id=ACCOUNT_B, extension_id=EXTENSION) is None
    assert world["requests"].get(request["id"]).decided_by == INSTALLER
    assert world["audit"].rows[-1]["action_type"] == "extension_install_request_approved"
    # The account sees the outcome on its own list.
    assert service.list_requests(scope=_scope(ACCOUNT_A))["items"][0]["status"] == "approved"


def test_a_failed_install_keeps_the_request_pending(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))

    # The catalog install failed, so nothing is installed: approving is refused.
    with pytest.raises(ServiceConflictError) as refused:
        service.approve(request["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))

    assert refused.value.payload["code"] == "extension_not_installed"
    assert world["requests"].get(request["id"]).status == "pending"
    assert world["audit"].rows[-1]["action_type"] == "extension_install_request_created"


def test_an_install_that_is_turned_off_keeps_the_request_pending(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    _install(world)
    world["installs"].disable(extension_id=EXTENSION, account_id=INSTALLATION_ACCOUNT_ID)

    with pytest.raises(ServiceValidationError):
        service.approve(request["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))

    assert world["requests"].get(request["id"]).status == "pending"


def test_an_extension_installed_meanwhile_is_only_enabled(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    install = _install(world)
    # The company had turned it off earlier, with its own settings.
    world["installs"].account_enablements.save(
        ExtensionAccountEnablement(
            account_id=ACCOUNT_A,
            extension_id=EXTENSION,
            install_id=install.install_id,
            state="disabled",
            settings={"region": "eu"},
        )
    )

    service.approve(request["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))

    row = world["installs"].account_enablements.get(account_id=ACCOUNT_A, extension_id=EXTENSION)
    assert row.enabled and row.settings == {"region": "eu"}


def test_declining_keeps_the_reason_and_installs_nothing(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))

    declined = service.decline(
        request["id"], reason="We use another tool", scope=_scope(INSTALLATION_ACCOUNT_ID)
    )

    assert declined["status"] == "declined"
    assert declined["decline_reason"] == "We use another tool"
    assert world["installs"].get(extension_id=EXTENSION, account_id=INSTALLATION_ACCOUNT_ID) is None
    assert world["audit"].rows[-1]["action_type"] == "extension_install_request_declined"
    with pytest.raises(ServiceValidationError):
        service.decline(
            service.create(OTHER, scope=_scope(ACCOUNT_A))["id"],
            reason="x" * 501,
            scope=_scope(INSTALLATION_ACCOUNT_ID),
        )


def test_an_answered_request_cannot_be_answered_again(world) -> None:
    service = world["service"]
    request = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    service.cancel(request["id"], scope=_scope(ACCOUNT_A))
    _install(world)

    with pytest.raises(ServiceConflictError):
        service.approve(request["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))
    with pytest.raises(ServiceConflictError):
        service.decline(request["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))
    with pytest.raises(ServiceConflictError):
        service.cancel(request["id"], scope=_scope(ACCOUNT_A))
    with pytest.raises(ServiceNotFoundError):
        service.approve("not-a-request", scope=_scope(INSTALLATION_ACCOUNT_ID))


def test_every_change_is_audited_with_its_account(world) -> None:
    service = world["service"]
    first = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    service.cancel(first["id"], scope=_scope(ACCOUNT_A))
    second = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    service.decline(second["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))
    third = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    _install(world)
    service.approve(third["id"], scope=_scope(INSTALLATION_ACCOUNT_ID))

    actions = [row["action_type"] for row in world["audit"].rows]
    assert actions == [
        "extension_install_request_created",
        "extension_install_request_cancelled",
        "extension_install_request_created",
        "extension_install_request_declined",
        "extension_install_request_created",
        "extension_install_request_approved",
    ]
    assert {row["payload"]["account_id"] for row in world["audit"].rows} == {ACCOUNT_A}
    assert {row["agent"] for row in world["audit"].rows} == {"ana"}


def test_a_failing_audit_write_does_not_undo_the_decision(world) -> None:
    def _broken() -> Any:
        raise RuntimeError("audit store down")

    service = ExtensionInstallRequestService(
        get_store=lambda: world["requests"],
        get_install_store=lambda: world["installs"],
        get_enablement_service=lambda: None,
        catalog_name=CATALOG.get,
        get_audit_logger=_broken,
    )
    created = service.create(EXTENSION, scope=_scope(ACCOUNT_A))
    assert created["status"] == "pending"


def test_enable_on_request_is_the_installation_admins_only(world) -> None:
    _install(world)
    enablement = ExtensionAccountEnablementService(
        get_install_store=lambda: world["installs"],
        get_enablement_store=lambda: world["installs"].account_enablements,
    )

    with pytest.raises(ServiceForbiddenError):
        enablement.enable_on_request(EXTENSION, account_id=ACCOUNT_B, scope=_scope(ACCOUNT_A))
    with pytest.raises(ServiceForbiddenError):
        enablement.enable_on_request(EXTENSION, account_id=ACCOUNT_B, scope=None)
    with pytest.raises(ServiceValidationError):
        enablement.enable_on_request(
            EXTENSION, account_id=INSTALLATION_ACCOUNT_ID, scope=_scope(INSTALLATION_ACCOUNT_ID)
        )
    with pytest.raises(ServiceNotFoundError):
        enablement.enable_on_request(
            "acme.missing", account_id=ACCOUNT_B, scope=_scope(INSTALLATION_ACCOUNT_ID)
        )
    enabled = enablement.enable_on_request(
        EXTENSION, account_id=ACCOUNT_B, scope=_scope(INSTALLATION_ACCOUNT_ID)
    )
    assert enabled["enabled"] is True
