"""The extension runtime resolves records for the caller's account only (multi-account step C3)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.application.extensions.binding_service import ExtensionBindingService
from core.application.management.errors import ServiceNotFoundError
from core.application.management.requests import ExtensionActionRequest
from core.application.management.scope import ScopeContext
from core.infrastructure.config.account_scope import ACCOUNT_SCOPE_MISSING_CODE
from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

ACCOUNT_B = "22222222-2222-2222-2222-222222222222"
INSTALL_ID = "e8e80d3d-2904-4730-9298-675c44a77677"


@pytest.mark.parametrize("missing", [None, "", "  "])
def test_install_lookup_without_account_is_refused_not_answered_for_the_default(
    missing: Any, caplog: pytest.LogCaptureFixture
) -> None:
    store = MagicMock()

    with caplog.at_level(logging.WARNING):
        extension, bl = helpers.resolve_extension_runtime_record_by_install_id(
            INSTALL_ID, get_logger_only=lambda: store, account_id=missing
        )

    assert extension is None
    assert bl is store
    store.get_extension_by_install_id.assert_not_called()
    store.get_extension_by_install_id_for_runtime_gate.assert_not_called()
    assert ACCOUNT_SCOPE_MISSING_CODE in caplog.text


def test_extension_id_lookup_without_account_is_refused_before_any_store_or_bundle_read() -> None:
    store = MagicMock()

    with patch.object(
        helpers,
        "load_extension_from_filesystem",
        side_effect=AssertionError("an unscoped lookup must not read bundles"),
    ):
        extension, bl = helpers.resolve_extension_runtime_record(
            "acme.mailer", get_logger_only=lambda: store
        )

    assert extension is None
    assert bl is store
    store.get_extension.assert_not_called()


def test_lookup_with_account_reads_that_account_unchanged() -> None:
    store = MagicMock()
    store.get_extension.return_value = None

    with patch.object(helpers, "load_extension_from_filesystem", return_value=None):
        helpers.resolve_extension_runtime_record(
            "acme.mailer", get_logger_only=lambda: store, account_id=ACCOUNT_B
        )

    store.get_extension.assert_called_once_with("acme.mailer", account_id=ACCOUNT_B)


def test_helpers_no_longer_fall_back_to_the_default_account() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "core/infrastructure/extension_runtime/extension_runtime_helpers.py"
    ).read_text(encoding="utf-8")
    assert "DEFAULT_ACCOUNT_ID" not in source


def _service(calls: list[dict[str, Any]]) -> ExtensionBindingService:
    def resolve(extension_id: str, **kwargs: Any) -> tuple[None, None]:
        calls.append({"extension_id": extension_id, **kwargs})
        return None, None

    return ExtensionBindingService(
        get_logger=lambda: None,
        get_operation_store=lambda: None,
        resolve_extension_runtime_record=resolve,
        extension_identifier=lambda payload: "",
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
    )


def test_extension_action_resolves_the_record_for_the_caller_account() -> None:
    calls: list[dict[str, Any]] = []

    with pytest.raises(ServiceNotFoundError):
        _service(calls).execute_action(
            "acme.mailer",
            "send",
            ExtensionActionRequest(input={}),
            scope=ScopeContext(account_id=ACCOUNT_B),
        )

    assert calls == [{"extension_id": "acme.mailer", "account_id": ACCOUNT_B}]


def test_extension_query_resolves_the_record_for_the_caller_account() -> None:
    from core.application.management.requests import ExtensionQueryRequest

    calls: list[dict[str, Any]] = []

    with pytest.raises(ServiceNotFoundError):
        _service(calls).execute_query(
            "acme.mailer",
            "list",
            ExtensionQueryRequest(),
            scope=ScopeContext(account_id=ACCOUNT_B),
        )

    assert calls == [{"extension_id": "acme.mailer", "account_id": ACCOUNT_B}]


class _AccountsStore:
    def __init__(self, account_ids: list[str]) -> None:
        self.account_ids = account_ids

    def list_accounts(self, **_kwargs: Any) -> list[dict[str, str]]:
        return [{"account_id": account_id} for account_id in self.account_ids]


def _apply(account_id: str | None, store: Any) -> dict[str, Any]:
    from core.application.extensions import provider_runtime

    captured: dict[str, Any] = {}

    def record(_invocation: dict[str, Any], **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "core.application.extensions.response_envelope.process_project_state_updates",
        side_effect=record,
    ):
        provider_runtime._process_provider_project_state_updates(
            {"status": "succeeded", "response": {"ok": True}},
            extension={"extension_key": "acme.tasks"},
            db=store,
            account_id=account_id,
            project_id="project-1",
        )
    return captured


def test_provider_state_update_keeps_the_carried_account() -> None:
    assert _apply(ACCOUNT_B, _AccountsStore([]))["account_id"] == ACCOUNT_B


def test_in_flight_provider_response_without_account_uses_the_only_active_account() -> None:
    assert _apply(None, _AccountsStore([ACCOUNT_B]))["account_id"] == ACCOUNT_B


def test_provider_response_without_account_on_a_multi_account_install_gets_no_account() -> None:
    from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID

    captured = _apply(None, _AccountsStore([DEFAULT_ACCOUNT_ID, ACCOUNT_B]))

    # An empty account makes every state change refuse downstream
    # (project_state_scope_missing / host_mutation_scope_missing /
    # resume_waits_scope_missing); it is never the default account.
    assert captured["account_id"] == ""
