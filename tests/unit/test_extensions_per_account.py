"""Extensions per account (ADR 0020, step C6), on the in-memory stores.

The code is installed once, in the installation account, by an installation
administrator. An administrator of another account enables it for that account, gives
it that account's settings and disables it again; nobody else's account sees it, and
installing, updating and removing the code stay installation-administrator actions.
The same rules on a real database are in
``tests/db/test_extensions_per_account_postgres.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.extensions.account_enablement import (
    ExtensionAccountEnablementService,
    InMemoryExtensionAccountEnablementStore,
    install_reaches_account,
    install_serves_other_accounts,
    validate_account_settings,
)
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.lifecycle_ports import require_global_extension_lifecycle_scope
from core.application.extensions.query_list import ExtensionQueryList
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

EXTENSION = "acme.crm-sync"
ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
ACCOUNT_B = "22222222-2222-2222-2222-222222222222"


def _scope(account_id: str, role: str = "admin") -> ScopeContext:
    return ScopeContext(account_id=account_id, principal_type=role, actor_id="ana")


@pytest.fixture
def world() -> dict[str, Any]:
    store = ExtensionInstallStore()
    store.account_enablements = InMemoryExtensionAccountEnablementStore()
    install = store.install(
        extension_id=EXTENSION, version="1.0.0", account_id=INSTALLATION_ACCOUNT_ID
    )
    return {
        "store": store,
        "install": install,
        "service": ExtensionAccountEnablementService(
            get_install_store=lambda: store,
            get_enablement_store=lambda: store.account_enablements,
        ),
    }


def test_an_account_admin_enables_disables_and_configures_for_their_account(world) -> None:
    service = world["service"]

    assert service.get(EXTENSION, scope=_scope(ACCOUNT_A))["state"] == "not_enabled"
    enabled = service.enable(EXTENSION, scope=_scope(ACCOUNT_A))
    configured = service.update_settings(EXTENSION, {"region": "eu"}, scope=_scope(ACCOUNT_A))
    disabled = service.disable(EXTENSION, scope=_scope(ACCOUNT_A))

    assert enabled["enabled"] is True
    assert enabled["install_id"] == world["install"].install_id
    assert enabled["enabled_by"] == "ana"
    assert configured["settings"] == {"region": "eu"}
    assert disabled["state"] == "disabled"
    assert disabled["settings"] == {"region": "eu"}
    assert service.get(EXTENSION, scope=_scope(ACCOUNT_B))["state"] == "not_enabled"


def test_only_an_administrator_of_the_account_changes_its_enablement(world) -> None:
    for role in ("operator", "viewer"):
        with pytest.raises(ServiceForbiddenError):
            world["service"].enable(EXTENSION, scope=_scope(ACCOUNT_A, role))


def test_the_installation_account_keeps_using_the_install_toggle(world) -> None:
    with pytest.raises(ServiceValidationError):
        world["service"].enable(EXTENSION, scope=_scope(INSTALLATION_ACCOUNT_ID))


def test_an_extension_the_installation_did_not_install_is_not_found(world) -> None:
    with pytest.raises(ServiceNotFoundError):
        world["service"].enable("acme.other", scope=_scope(ACCOUNT_A))


def test_an_extension_the_installation_turned_off_cannot_be_enabled(world) -> None:
    world["store"].set_state(
        extension_id=EXTENSION, account_id=INSTALLATION_ACCOUNT_ID, state="disabled"
    )

    with pytest.raises(ServiceValidationError):
        world["service"].enable(EXTENSION, scope=_scope(ACCOUNT_A))


def test_settings_are_a_small_object_without_secrets() -> None:
    assert validate_account_settings({"region": "eu", "batch": 50}) == {
        "region": "eu",
        "batch": 50,
    }
    for bad in ([], "x", {"api_key": "k"}, {"client_secret": "s"}, {"big": "x" * 70_000}):
        with pytest.raises(ServiceValidationError):
            validate_account_settings(bad)


def test_settings_refuse_a_secret_looking_key_at_any_depth() -> None:
    nested = {"region": "eu", "auth": {"api_key": "k"}}
    in_list = {"endpoints": [{"url": "https://x", "client_secret": "s"}]}
    for bad in (nested, in_list):
        with pytest.raises(ServiceValidationError, match="looks like a secret"):
            validate_account_settings(bad)
    assert validate_account_settings({"limits": {"batch": 50}, "tags": [{"name": "a"}]}) == {
        "limits": {"batch": 50},
        "tags": [{"name": "a"}],
    }


def test_an_account_reaches_the_install_only_through_its_own_enabled_row(world) -> None:
    store, service, install = world["store"], world["service"], world["install"]
    enablements = store.account_enablements

    assert install_reaches_account(install, INSTALLATION_ACCOUNT_ID, enablements)
    assert not install_reaches_account(install, ACCOUNT_A, enablements)
    service.enable(EXTENSION, scope=_scope(ACCOUNT_A))

    assert install_reaches_account(install, ACCOUNT_A, enablements)
    assert not install_reaches_account(install, ACCOUNT_B, enablements)
    assert not install_reaches_account(install, "", enablements)
    assert [row.extension_id for row in store.list_for_account(ACCOUNT_A)] == [EXTENSION]
    assert store.list_for_account(ACCOUNT_B) == []
    assert install_serves_other_accounts(install, enablements)


def test_the_code_lifecycle_stays_with_the_installation_administrator() -> None:
    require_global_extension_lifecycle_scope(_scope(INSTALLATION_ACCOUNT_ID))
    with pytest.raises(ServiceValidationError):
        require_global_extension_lifecycle_scope(_scope(ACCOUNT_A))


def _listing(world: dict[str, Any], scope: ScopeContext) -> dict[str, Any]:
    query = ExtensionQueryList(
        get_logger=lambda: None,
        get_install_store=lambda: world["store"],
        get_runtime_store=ExtensionRuntimeStore,
    )
    rows = query.list_extensions(scope=scope).extensions
    return {row.extension_id: row for row in rows}


def test_the_listing_shows_another_account_its_own_enablement(world) -> None:
    before = _listing(world, _scope(ACCOUNT_A))
    world["service"].enable(EXTENSION, scope=_scope(ACCOUNT_A))
    after = _listing(world, _scope(ACCOUNT_A))
    other = _listing(world, _scope(ACCOUNT_B))
    owner = _listing(world, _scope(INSTALLATION_ACCOUNT_ID))

    assert before[EXTENSION].account_enablement == "not_enabled"
    assert before[EXTENSION].state == "disabled"
    assert before[EXTENSION].install_id == ""
    assert after[EXTENSION].account_enablement == "enabled"
    assert after[EXTENSION].state == "active"
    assert after[EXTENSION].install_id == world["install"].install_id
    assert other[EXTENSION].account_enablement == "not_enabled"
    assert owner[EXTENSION].account_enablement == ""


def test_a_viewer_of_another_account_sees_only_what_it_enabled(world) -> None:
    assert _listing(world, _scope(ACCOUNT_B, "viewer")) == {}
    world["service"].enable(EXTENSION, scope=_scope(ACCOUNT_B))

    assert list(_listing(world, _scope(ACCOUNT_B, "viewer"))) == [EXTENSION]


class _Logger:
    """A runtime logger whose database says how each account reaches the install."""

    def __init__(self, access: dict[str, dict[str, Any]]) -> None:
        self.access = access
        self.looked_up: list[str] = []

    def get_extension_account_access(self, extension_key: str, *, account_id: str):
        return self.access.get(account_id)

    def get_extension(self, extension_key: str, *, account_id: str | None = None):
        self.looked_up.append(str(account_id))
        return {"extension_key": extension_key, "account_id": account_id, "enabled": True}


def test_the_runtime_resolves_the_installation_code_only_for_an_enabled_account(
    monkeypatch,
) -> None:
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    monkeypatch.setattr(helpers, "load_extension_from_filesystem", lambda _id: None)
    logger = _Logger(
        {
            ACCOUNT_A: {
                "access": "enabled",
                "owner_account_id": INSTALLATION_ACCOUNT_ID,
                "install_state": "active",
                "account_settings": {"region": "eu"},
            },
            ACCOUNT_B: {
                "access": "not_enabled",
                "owner_account_id": INSTALLATION_ACCOUNT_ID,
                "install_state": "active",
                "account_settings": {},
            },
        }
    )

    record_a, _ = helpers.resolve_extension_runtime_record(
        EXTENSION, get_logger_only=lambda: logger, account_id=ACCOUNT_A
    )
    record_b, _ = helpers.resolve_extension_runtime_record(
        EXTENSION, get_logger_only=lambda: logger, account_id=ACCOUNT_B
    )
    record_c, _ = helpers.resolve_extension_runtime_record(
        EXTENSION, get_logger_only=lambda: logger, account_id="33333333-3333-3333-3333-333333333333"
    )

    assert record_b is None
    assert record_c is None  # no install reaches it: not even the files on disk
    assert logger.looked_up == [INSTALLATION_ACCOUNT_ID]
    assert record_a is not None
    assert record_a["account_id"] == ACCOUNT_A
    assert record_a["install_account_id"] == INSTALLATION_ACCOUNT_ID
    assert record_a["account_settings"] == {"region": "eu"}


def _detail(world: dict[str, Any], scope: ScopeContext):
    from core.application.extensions.query_service import ExtensionQueryService

    service = ExtensionQueryService(
        get_logger=lambda: None,
        get_install_store=lambda: world["store"],
        get_runtime_store=ExtensionRuntimeStore,
    )
    return service.get_extension(EXTENSION, scope=scope)


def test_the_extension_page_shows_an_account_admin_what_to_enable(world) -> None:
    assert _detail(world, _scope(ACCOUNT_A)).account_enablement == "not_enabled"
    with pytest.raises(ServiceNotFoundError):
        _detail(world, _scope(ACCOUNT_A, "viewer"))

    world["service"].enable(EXTENSION, scope=_scope(ACCOUNT_A))
    enabled = _detail(world, _scope(ACCOUNT_A, "viewer"))

    assert enabled.account_enablement == "enabled"
    assert enabled.state == "active"
    assert _detail(world, _scope(INSTALLATION_ACCOUNT_ID)).account_enablement == ""


def test_infrastructure_names_the_installation_account_by_purpose() -> None:
    from core.infrastructure.config.platform_identity import is_installation_account

    assert is_installation_account(INSTALLATION_ACCOUNT_ID)
    assert not is_installation_account(ACCOUNT_A)
    assert not is_installation_account("")
    assert not is_installation_account(None)
