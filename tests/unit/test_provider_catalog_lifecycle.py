"""The provider catalog follows the extension through the real service paths.

Testing the helpers alone proved they behave; it did not prove the services call
them, in the right order, with a rollback that puts the catalog back. Two of the
three transitions were wrong precisely there — enable never restored the row it
had written, and the public uninstall never touched it at all — so these go
through `ExtensionCommandService.enable_extension`, `.disable_extension` and the
uninstall service, and read what was written to the catalog.

Everything is a synthetic `acme.*` bundle.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import ScopeContext

_ADMIN_SCOPE = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin")

NOTIFIER_DESCRIPTOR: dict[str, Any] = {
    "extension_id": "acme.mailer",
    "kind": "notification_provider",
    "display_name": "Acme Mailer",
    "description": "Synthetic notification provider.",
    "features": ["notify"],
    "entrypoint": {"command": ["python3", "main.py"]},
    "health": {"command": ["python3", "health.py"]},
}


class _Catalog:
    """The provider half of the platform logger, recording what was written."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes: list[dict[str, Any]] = []
        self.save_error: Exception | None = None
        self.read_error: Exception | None = None

    def get_provider(self, provider_key: str) -> dict[str, Any] | None:
        if self.read_error is not None:
            raise self.read_error
        return dict(self.rows[provider_key]) if provider_key in self.rows else None

    def save_provider(self, row: dict[str, Any]) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.writes.append(dict(row))
        self.rows[str(row["provider_key"])] = dict(row)


def _install_record(state: str = "disabled") -> Any:
    return SimpleNamespace(
        extension_id="acme.mailer",
        install_id="install-acme-mailer",
        release_id="release-acme-mailer",
        account_id=DEFAULT_ACCOUNT_ID,
        state=state,
        visibility="account",
        descriptor_source="extension_scan",
        provenance={},
    )


def _services(catalog: _Catalog, *, state: str = "disabled"):
    from core.application.extensions.command_service import ExtensionCommandService

    record = _install_record(state)
    store = MagicMock()
    store.get.return_value = record
    store.set_state.return_value = record
    store.disable.return_value = _install_record("disabled")
    runtime_store = MagicMock()
    runtime_store.get_release.return_value = SimpleNamespace(
        extension_id="acme.mailer",
        release_id="release-acme-mailer",
        descriptor=dict(NOTIFIER_DESCRIPTOR),
        descriptor_sha256="a" * 64,
        provenance={},
        source_kind="extension_scan",
    )
    runtime_store.list_projections.return_value = []
    logger = MagicMock()
    logger.get_provider.side_effect = catalog.get_provider
    logger.save_provider.side_effect = catalog.save_provider
    service = ExtensionCommandService(
        get_logger=lambda: logger,
        get_install_store=lambda: store,
        get_runtime_store=lambda: runtime_store,
    )
    return service, store, runtime_store, record


def test_enabling_registers_the_declared_provider(monkeypatch) -> None:
    catalog = _Catalog()
    service, _store, _runtime_store, _record = _services(catalog)
    monkeypatch.setattr(
        "core.application.extensions.command_lifecycle_activation.validate_extension_activation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.application.extensions.command_lifecycle_activation.require_extension_health_ready",
        lambda *args, **kwargs: None,
    )

    service.enable_extension("acme.mailer", scope=_ADMIN_SCOPE)

    assert catalog.rows["acme.mailer"]["enabled"] is True
    assert catalog.rows["acme.mailer"]["provider_type_key"] == "notification_provider"


def test_a_failed_enable_leaves_no_provider_row_behind(monkeypatch) -> None:
    """The row this enable created must not outlive it."""
    catalog = _Catalog()
    service, _store, runtime_store, _record = _services(catalog)
    monkeypatch.setattr(
        "core.application.extensions.command_lifecycle_activation.validate_extension_activation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.application.extensions.command_lifecycle_activation.require_extension_health_ready",
        lambda *args, **kwargs: None,
    )
    runtime_store.set_projection_active_for_install.side_effect = RuntimeError("projection failed")

    with pytest.raises(RuntimeError, match="projection failed"):
        service.enable_extension("acme.mailer", scope=_ADMIN_SCOPE)

    # There was no row before, so the restored state is a row that cannot deliver.
    assert catalog.rows["acme.mailer"]["enabled"] is False


def test_a_failed_enable_restores_the_row_that_was_already_there(monkeypatch) -> None:
    catalog = _Catalog()
    catalog.rows["acme.mailer"] = {"provider_key": "acme.mailer", "enabled": True}
    service, _store, runtime_store, _record = _services(catalog)
    monkeypatch.setattr(
        "core.application.extensions.command_lifecycle_activation.validate_extension_activation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.application.extensions.command_lifecycle_activation.require_extension_health_ready",
        lambda *args, **kwargs: None,
    )
    runtime_store.set_projection_active_for_install.side_effect = RuntimeError("projection failed")

    with pytest.raises(RuntimeError):
        service.enable_extension("acme.mailer", scope=_ADMIN_SCOPE)

    assert catalog.rows["acme.mailer"]["enabled"] is True


def test_disabling_turns_the_provider_off_before_the_extension() -> None:
    """The order is the point: the reverse leaves a saveable, undeliverable route.

    Asserted from inside the extension-disable call, which is the only place the
    intermediate state is observable.
    """
    catalog = _Catalog()
    catalog.rows["acme.mailer"] = {"provider_key": "acme.mailer", "enabled": True}
    service, store, _runtime_store, _record = _services(catalog, state="active")
    seen: dict[str, Any] = {}

    def _disable(**_kwargs: Any) -> Any:
        seen["provider_enabled_at_disable"] = catalog.rows["acme.mailer"]["enabled"]
        return _install_record()

    store.disable.side_effect = _disable

    service.disable_extension("acme.mailer", scope=_ADMIN_SCOPE)

    assert seen["provider_enabled_at_disable"] is False
    assert catalog.rows["acme.mailer"]["enabled"] is False


def test_a_failed_disable_puts_the_provider_back() -> None:
    catalog = _Catalog()
    catalog.rows["acme.mailer"] = {"provider_key": "acme.mailer", "enabled": True}
    service, store, _runtime_store, _record = _services(catalog, state="active")
    store.disable.side_effect = RuntimeError("store unavailable")

    with pytest.raises(RuntimeError, match="store unavailable"):
        service.disable_extension("acme.mailer", scope=_ADMIN_SCOPE)

    assert catalog.rows["acme.mailer"]["enabled"] is True


def test_a_read_failure_stops_the_change_rather_than_guessing() -> None:
    """An unreadable row is not an absent one; disabling on that guess is wrong."""
    from core.application.extensions.provider_registration import (
        ProviderRegistrationError,
        provider_state_snapshot,
    )

    catalog = _Catalog()
    catalog.read_error = RuntimeError("catalog unreachable")
    logger = MagicMock()
    logger.get_provider.side_effect = catalog.get_provider

    with pytest.raises(ProviderRegistrationError, match="could not read provider"):
        provider_state_snapshot(logger, NOTIFIER_DESCRIPTOR)


def test_a_failed_restore_is_reported_not_swallowed() -> None:
    from core.application.extensions.provider_registration import restore_provider_state

    catalog = _Catalog()
    catalog.save_error = RuntimeError("catalog unavailable")
    logger = MagicMock()
    logger.save_provider.side_effect = catalog.save_provider
    reported: list[str] = []

    restored = restore_provider_state(logger, NOTIFIER_DESCRIPTOR, None, on_error=reported.append)

    assert restored is False
    assert reported
    assert "acme.mailer" in reported[0]


class _Transaction:
    """A cursor and its transaction, recording what committed and what rolled back."""

    def __init__(self, catalog: _Catalog) -> None:
        self.catalog = catalog
        self.staged: list[tuple[str, tuple]] = []
        self.committed = False

    # -- cursor -------------------------------------------------------------
    def __enter__(self) -> _Transaction:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def cursor(self) -> _Transaction:
        return self

    def execute(self, statement: str, params: tuple = ()) -> None:
        self.staged.append((statement, params))

    # -- transaction --------------------------------------------------------
    def commit(self) -> None:
        self.committed = True
        for statement, params in self.staged:
            if "UPDATE providers" in statement and "enabled = false" in statement:
                key = params[0]
                if key in self.catalog.rows:
                    self.catalog.rows[key]["enabled"] = False

    def rollback(self) -> None:
        self.staged.clear()


def test_the_provider_row_is_disabled_inside_the_removal_transaction() -> None:
    """Atomicity is the point: the write has to be on the removal's cursor.

    Disabling it around the outside and restoring on failure produced the
    opposite half-state — a step failing after the removal had already committed
    left an enabled provider on an extension that was gone.
    """
    from core.application.extensions.provider_registration import disable_provider_row_cur

    catalog = _Catalog()
    catalog.rows["acme.mailer"] = {"provider_key": "acme.mailer", "enabled": True}
    transaction = _Transaction(catalog)

    applied = disable_provider_row_cur(transaction, NOTIFIER_DESCRIPTOR)

    assert applied is True
    # Staged, not yet visible: it belongs to the transaction.
    assert catalog.rows["acme.mailer"]["enabled"] is True
    transaction.commit()
    assert catalog.rows["acme.mailer"]["enabled"] is False


def test_a_rolled_back_removal_leaves_the_provider_untouched() -> None:
    """Extension and provider move together or neither does."""
    from core.application.extensions.provider_registration import disable_provider_row_cur

    catalog = _Catalog()
    catalog.rows["acme.mailer"] = {"provider_key": "acme.mailer", "enabled": True}
    transaction = _Transaction(catalog)

    disable_provider_row_cur(transaction, NOTIFIER_DESCRIPTOR)
    transaction.rollback()
    transaction.commit()

    assert catalog.rows["acme.mailer"]["enabled"] is True


def test_an_extension_that_declares_no_provider_stages_nothing() -> None:
    from core.application.extensions.provider_registration import disable_provider_row_cur

    catalog = _Catalog()
    transaction = _Transaction(catalog)

    assert disable_provider_row_cur(transaction, {"extension_id": "acme.x", "kind": "widget"}) is (
        False
    )
    assert transaction.staged == []


def test_the_removal_path_disables_the_provider_on_its_own_cursor() -> None:
    """Read from the source: the write must be inside each transaction block.

    A unit test cannot open the real transaction, so what is pinned is that the
    removal path calls the cursor-scoped helper — the property that makes it atomic.
    """
    from pathlib import Path

    source = Path("core/application/extensions/uninstall_persistence_service.py").read_text(
        encoding="utf-8"
    )

    assert source.count("disable_provider_row_cur(cur, target.extension)") == 1
    removal_block = source.split("def remove_installed_target", 1)[1].split(
        "def _execute_oauth_revocations", 1
    )[0]
    assert "disable_provider_row_cur" in removal_block


def test_the_uninstall_service_no_longer_unwinds_the_provider_outside_the_transaction() -> None:
    """The outer restore is what created the reverse half-state; it must be gone."""
    from pathlib import Path

    source = Path("core/application/extensions/uninstall_service.py").read_text(encoding="utf-8")

    assert "provider_deactivated_for_uninstall" not in source
    assert "restore_provider_state" not in source


def test_enable_registers_from_the_install_record_when_no_release_resolves() -> None:
    """An install can be resolvable without a release, and still declare a provider.

    Found on a live stand: an extension installed from an archive enabled with
    HTTP 200 and never appeared in the provider catalog, because the release row
    did not resolve from the record and the descriptor read was empty. That is the
    silent failure this whole mechanism exists to prevent, so the install record —
    which carries the same typed `kind` — is the fallback.
    """
    from core.application.extensions.command_service import ExtensionCommandService

    catalog = _Catalog()
    record = _install_record()
    store = MagicMock()
    store.get.return_value = record
    store.set_state.return_value = record
    runtime_store = MagicMock()
    # No release resolves for this install.
    runtime_store.get_release.return_value = None
    runtime_store.list_projections.return_value = []
    logger = MagicMock()
    logger.get_provider.side_effect = catalog.get_provider
    logger.save_provider.side_effect = catalog.save_provider
    logger.get_extension.return_value = dict(NOTIFIER_DESCRIPTOR)

    service = ExtensionCommandService(
        get_logger=lambda: logger,
        get_install_store=lambda: store,
        get_runtime_store=lambda: runtime_store,
    )
    with (
        patch(
            "core.application.extensions.command_lifecycle_activation.validate_extension_activation",
            return_value=None,
        ),
        patch(
            "core.application.extensions.command_lifecycle_activation.require_extension_health_ready",
            return_value=None,
        ),
    ):
        service.enable_extension("acme.mailer", scope=_ADMIN_SCOPE)

    assert catalog.rows["acme.mailer"]["enabled"] is True
    logger.get_extension.assert_called_with("acme.mailer")


def test_disable_uses_the_install_record_provider_when_no_release_resolves() -> None:
    """Enable and disable must resolve the same provider without a release row."""
    from core.application.extensions.command_service import ExtensionCommandService

    catalog = _Catalog()
    record = _install_record()
    store = MagicMock()
    store.get.return_value = record
    store.set_state.return_value = _install_record("active")
    store.disable.return_value = _install_record("disabled")
    runtime_store = MagicMock()
    runtime_store.get_release.return_value = None
    runtime_store.list_projections.return_value = []
    logger = MagicMock()
    logger.get_provider.side_effect = catalog.get_provider
    logger.save_provider.side_effect = catalog.save_provider
    logger.get_extension.return_value = dict(NOTIFIER_DESCRIPTOR)

    service = ExtensionCommandService(
        get_logger=lambda: logger,
        get_install_store=lambda: store,
        get_runtime_store=lambda: runtime_store,
    )
    with (
        patch(
            "core.application.extensions.command_lifecycle_activation.validate_extension_activation",
            return_value=None,
        ),
        patch(
            "core.application.extensions.command_lifecycle_activation.require_extension_health_ready",
            return_value=None,
        ),
    ):
        service.enable_extension("acme.mailer", scope=_ADMIN_SCOPE)
        assert catalog.rows["acme.mailer"]["enabled"] is True
        service.disable_extension("acme.mailer", scope=_ADMIN_SCOPE)

    assert catalog.rows["acme.mailer"]["enabled"] is False


def test_the_fallback_reads_only_manifest_typed_fields() -> None:
    """It must not grow into knowledge of one store's record shape.

    A whole install record carries far more than a provider row is built from;
    narrowing it to the declared fields keeps the fallback a projection of the
    contract rather than of whatever a particular row happens to contain.
    """
    from core.application.extensions.provider_registration import (
        INSTALL_RECORD_PROVIDER_FIELDS,
        typed_provider_source,
    )

    record = {
        "extension_id": "acme.mailer",
        "kind": "notification_provider",
        "display_name": "Acme Mailer",
        "description": "Synthetic.",
        "features": ["notify"],
        "install_id": "install-1",
        "bundle_root": "/app/extensions/acme_mailer",
        "config": {"private_note": "must-not-travel"},
        "health_status": "unknown",
    }

    typed = typed_provider_source(record)

    assert set(typed) <= set(INSTALL_RECORD_PROVIDER_FIELDS)
    assert "config" not in typed
    assert "bundle_root" not in typed
    assert typed_provider_source(None) == {}
    assert "flowsteward" not in str(INSTALL_RECORD_PROVIDER_FIELDS)
