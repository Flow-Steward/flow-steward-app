"""Tools and notification reactions reach an extension through C6 enablement.

ADR 0020, C6: an account that enabled the installation's install of an extension
uses that install (its bundle row belongs to the installation account). The tool
resolver and the notification-reaction validator used to look the bundle up in the
caller's own account and reported "not found". A failed access lookup is reported as
unavailable for an account that would need another account's install, while an
account's own install keeps working.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.management.command_services_event_webhook_mixin import (
    _reaction_extension_for_account,
)
from core.application.orchestration.tools.tool_resolver import _resolve_extension_record
from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

EXTENSION = "acme.crm-sync"
ENABLED = "11111111-1111-1111-1111-111111111111"
NOT_ENABLED = "22222222-2222-2222-2222-222222222222"


class _Logger:
    """Bundle rows exist only in the installation account; access per account."""

    def __init__(self, *, access_fails: bool = False, owners: set[str] | None = None) -> None:
        self.access_fails = access_fails
        self.owners = owners if owners is not None else {INSTALLATION_ACCOUNT_ID}
        self.looked_up: list[str | None] = []

    def get_extension_account_access(self, extension_key: str, *, account_id: str):
        if self.access_fails:
            raise RuntimeError("database unavailable")
        if account_id == INSTALLATION_ACCOUNT_ID:
            access = "owner"
        elif account_id == ENABLED:
            access = "enabled"
        else:
            access = "not_enabled"
        return {
            "access": access,
            "owner_account_id": INSTALLATION_ACCOUNT_ID,
            "install_state": "active",
            "account_settings": {},
        }

    def get_extension(self, extension_key: str, *, account_id: str | None = None):
        self.looked_up.append(account_id)
        if account_id not in self.owners:
            return None
        return {"extension_key": extension_key, "account_id": account_id, "enabled": True}


def _manifest() -> Any:
    return SimpleNamespace(
        tool_id="crm_lookup",
        config={},
        implementation=SimpleNamespace(to_dict=lambda: {"extension_key": EXTENSION}),
    )


def test_tool_of_an_enabled_account_uses_the_installation_install() -> None:
    bl = _Logger()
    record, _ = _resolve_extension_record(
        _manifest(), runtime_context={"logger": bl, "account_id": ENABLED}
    )
    assert record["enabled"] is True
    assert bl.looked_up == [INSTALLATION_ACCOUNT_ID]


def test_tool_of_an_account_that_did_not_enable_it_is_not_found() -> None:
    bl = _Logger()
    with pytest.raises(RuntimeError, match="not found"):
        _resolve_extension_record(
            _manifest(), runtime_context={"logger": bl, "account_id": NOT_ENABLED}
        )
    assert bl.looked_up == []


def test_notification_reaction_of_an_enabled_account_finds_the_extension() -> None:
    bl = _Logger()
    assert _reaction_extension_for_account(bl, EXTENSION, ENABLED) is not None
    assert _reaction_extension_for_account(bl, EXTENSION, NOT_ENABLED) is None
    assert _reaction_extension_for_account(bl, EXTENSION, "") is None


def test_failed_access_lookup_is_unavailable_for_an_account_without_its_own_install() -> None:
    bl = _Logger(access_fails=True)
    with pytest.raises(helpers.ExtensionAccessUnavailableError):
        helpers.extension_install_account(bl, EXTENSION, ENABLED)


def test_failed_access_lookup_keeps_the_owners_own_install(monkeypatch) -> None:
    monkeypatch.setattr(helpers, "load_extension_from_filesystem", lambda _id: None)
    bl = _Logger(access_fails=True)

    lookup, access = helpers.extension_install_account(bl, EXTENSION, INSTALLATION_ACCOUNT_ID)
    assert lookup == INSTALLATION_ACCOUNT_ID
    assert access["access"] == "owner"

    record, _ = helpers.resolve_extension_runtime_record(
        EXTENSION, get_logger_only=lambda: bl, account_id=INSTALLATION_ACCOUNT_ID
    )
    assert record is not None
