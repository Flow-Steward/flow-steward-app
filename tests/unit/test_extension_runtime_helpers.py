"""Unit tests for extension runtime helper resolution paths."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID

PROJECT_ROOT = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", Path(__file__).resolve().parents[2]))


def test_runtime_row_merge_uses_host_owned_filesystem_install_metadata() -> None:
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        merge_extension_runtime_rows,
    )

    host_metadata = {
        "runtime_grants": {
            "resources": {
                "provider_secrets": {
                    "provider_key": "acme.notification-provider",
                    "secret_keys": ["api_token"],
                }
            }
        }
    }

    merged = merge_extension_runtime_rows(
        {
            "extension_key": "acme.notification-provider",
            "install_metadata": {"runtime_grants": {"resources": {"provider_secrets": {}}}},
        },
        {
            "extension_key": "acme.notification-provider",
            "install_metadata": host_metadata,
        },
    )

    assert merged is not None
    assert merged["install_metadata"] == host_metadata


def test_filesystem_loader_attaches_host_owned_install_metadata(tmp_path: Path) -> None:
    from core.infrastructure.extension_runtime.extension_filesystem_loader import (
        load_extension_from_filesystem,
    )

    bundle = tmp_path / "acme_notification_provider"
    bundle.mkdir()
    host_metadata = {
        "runtime_grants": {
            "resources": {
                "provider_secrets": {
                    "provider_key": "acme.notification-provider",
                    "secret_keys": ["api_token"],
                }
            }
        },
    }
    compiled = {
        "extension_key": "acme.notification-provider",
        "bundle_root": str(bundle),
    }

    with (
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_compile.compile_extension_bundle",
            return_value=compiled,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_manifest.default_extensions_root",
            return_value=str(tmp_path),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_manifest.extension_host_metadata",
            return_value={"install_metadata": host_metadata},
        ),
    ):
        loaded = load_extension_from_filesystem(
            "acme.notification-provider",
            canonical_folder_name="acme_notification_provider",
        )

    assert loaded is not None
    assert loaded["install_metadata"] == host_metadata


def test_compiled_manifest_projects_normalized_contract_ui_surfaces(tmp_path):
    """Built here: the subject is the projection, not any shipped bundle's contract."""
    from core.application.extensions.lifecycle import validate_extension_descriptor_file
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        build_extension_compiled_manifest,
    )
    from tests.support.synthetic_extension_bundle import write_contract_v2_bundle

    bundle_root = write_contract_v2_bundle(tmp_path)
    validation = validate_extension_descriptor_file(
        bundle_root / "extension-descriptor.yaml",
        platform_version="1.0.0",
    )
    assert validation["valid"] is True, validation.get("errors")
    persisted_descriptor = validation["normalized"]
    normalized_contract = persisted_descriptor["extension_contract_v2_normalized"]
    operation_manifest = normalized_contract["operation_manifest"]
    connection_types = normalized_contract["connection_types"]
    assert "operation_manifest" not in persisted_descriptor
    assert "connection_types" not in persisted_descriptor

    compiled = build_extension_compiled_manifest(
        {
            "extension_key": persisted_descriptor["extension_id"],
            "compiled_manifest": persisted_descriptor,
        }
    )

    assert compiled["operation_manifest"] == operation_manifest
    assert compiled["connection_types"] == connection_types


def test_get_extension_by_install_id_materializes_active_global_release_for_consumer_account():
    from core.infrastructure.db.logger_extensions_accounts_common import (
        PlatformLoggerExtensionsAccountsCommonMixin,
    )
    from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
        PlatformLoggerExtensionsAccountsQueryMixin,
    )

    descriptor = {
        "extension_id": "acme.document-converter",
        "kind": "notification_provider",
        "entrypoint": {"command": ["python", "main.py"]},
        "action_definitions": [{"action_id": "convert_file"}],
    }

    class Cursor:
        description = ()

        def __init__(self) -> None:
            self.query = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, query, params) -> None:
            self.query = query
            self.params = params

        def fetchone(self):
            return {
                "extension_id": "acme.document-converter",
                "install_account_id": "owner_account",
                "visibility": "global",
                "state": "active",
                "install_id": "install_global",
                "release_id": "release_acme_documents",
                "release_extension_id": "acme.document-converter",
                "config_jsonb": {},
                "descriptor_jsonb": descriptor,
            }

    class Logger(
        PlatformLoggerExtensionsAccountsCommonMixin,
        PlatformLoggerExtensionsAccountsQueryMixin,
    ):
        def __init__(self) -> None:
            self._cursor = Cursor()
            self.conn = self

        def cursor(self, **_kwargs):
            return self._cursor

        def _table_exists(self, table_name: str) -> bool:
            return table_name in {"extension_installs", "extension_releases"}

    logger = Logger()

    extension = logger.get_extension_by_install_id("install_global", account_id="consumer_account")

    assert extension is not None
    assert extension["account_id"] == "consumer_account"
    assert extension["kind"] == "notification_provider"
    assert extension["release_id"] == "release_acme_documents"
    assert extension["compiled_manifest"] == descriptor
    assert extension["entrypoint"] == {"command": ["python", "main.py"]}
    assert "ei.state = 'active'" in logger._cursor.query
    assert logger._cursor.params == ["install_global", "consumer_account"]


def test_runtime_gate_lookup_materializes_disabled_install_metadata() -> None:
    from core.infrastructure.db.logger_extensions_accounts_common import (
        PlatformLoggerExtensionsAccountsCommonMixin,
    )
    from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
        PlatformLoggerExtensionsAccountsQueryMixin,
    )

    class Cursor:
        description = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, query, params) -> None:
            self.query = str(query)
            self.params = params

        @staticmethod
        def fetchone():
            return {
                "extension_id": "fixture.extension-oauth",
                "install_account_id": DEFAULT_ACCOUNT_ID,
                "visibility": "account",
                "state": "disabled",
                "install_id": "install-fixture-disabled",
                "release_id": "release-fixture",
                "release_extension_id": "fixture.extension-oauth",
                "config_jsonb": {},
                "descriptor_jsonb": {
                    "extension_id": "fixture.extension-oauth",
                    "entrypoint": {"command": ["python", "main.py"]},
                },
            }

    class Logger(
        PlatformLoggerExtensionsAccountsCommonMixin,
        PlatformLoggerExtensionsAccountsQueryMixin,
    ):
        def __init__(self) -> None:
            self._cursor = Cursor()
            self.conn = self

        def cursor(self, **_kwargs):
            return self._cursor

        @staticmethod
        def _table_exists(table_name: str) -> bool:
            return table_name in {"extension_installs", "extension_releases"}

    logger = Logger()
    extension = logger.get_extension_by_install_id_for_runtime_gate(
        "install-fixture-disabled",
        account_id=DEFAULT_ACCOUNT_ID,
    )

    assert extension is not None
    assert extension["enabled"] is False
    assert "ei.state = 'active'" not in logger._cursor.query


def test_operator_install_lookup_materializes_disabled_account_owned_release():
    from core.infrastructure.db.logger_extensions_accounts_common import (
        PlatformLoggerExtensionsAccountsCommonMixin,
    )
    from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
        PlatformLoggerExtensionsAccountsQueryMixin,
    )

    descriptor = {"extension_id": "acme.remote-llm", "manifest_version": 2}

    class Cursor:
        description = ()

        def __init__(self) -> None:
            self.query = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, query, params) -> None:
            self.query = query
            self.params = params

        def fetchone(self):
            return {
                "extension_id": "acme.remote-llm",
                "install_account_id": "owner_account",
                "visibility": "account",
                "state": "disabled",
                "install_id": "install_disabled",
                "release_id": "release_llm",
                "release_extension_id": "acme.remote-llm",
                "descriptor_sha256": "a" * 64,
                "config_jsonb": {},
                "descriptor_jsonb": descriptor,
            }

    class Logger(
        PlatformLoggerExtensionsAccountsCommonMixin,
        PlatformLoggerExtensionsAccountsQueryMixin,
    ):
        def __init__(self) -> None:
            self._cursor = Cursor()
            self.conn = self

        def cursor(self, **_kwargs):
            return self._cursor

        def _table_exists(self, table_name: str) -> bool:
            return table_name in {"extension_installs", "extension_releases"}

    logger = Logger()

    extension = logger.get_extension_install_for_operator(
        "install_disabled",
        account_id="owner_account",
    )

    assert extension is not None
    assert extension["account_id"] == "owner_account"
    assert extension["enabled"] is False
    assert extension["descriptor_sha256"] == "a" * 64
    assert "er.descriptor_sha256" in logger._cursor.query
    assert "ei.state = 'active'" not in logger._cursor.query
    assert "ei.account_id = %s" in logger._cursor.query
    assert "ei.visibility = 'global'" not in logger._cursor.query
    assert logger._cursor.params == ["install_disabled", "owner_account"]


def test_get_extension_by_install_id_rejects_release_owned_by_another_extension():
    from core.infrastructure.db.logger_extensions_accounts_common import (
        PlatformLoggerExtensionsAccountsCommonMixin,
    )
    from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
        PlatformLoggerExtensionsAccountsQueryMixin,
    )

    class Cursor:
        description = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, query, params) -> None:
            self.query = query
            self.params = params

        def fetchone(self):
            return {
                "extension_id": "acme.document-converter",
                "release_extension_id": "acme.foreign",
                "install_account_id": "owner_account",
                "visibility": "global",
                "state": "active",
                "install_id": "install_global",
                "release_id": "release_foreign",
                "config_jsonb": {},
                "descriptor_jsonb": {
                    "entrypoint": {"command": ["python", "foreign.py"]},
                },
            }

    class Logger(
        PlatformLoggerExtensionsAccountsCommonMixin,
        PlatformLoggerExtensionsAccountsQueryMixin,
    ):
        def __init__(self) -> None:
            self._cursor = Cursor()
            self.conn = self

        def cursor(self, **_kwargs):
            return self._cursor

        def _table_exists(self, table_name: str) -> bool:
            return table_name in {"extension_installs", "extension_releases"}

    logger = Logger()

    assert (
        logger.get_extension_by_install_id("install_global", account_id="consumer_account") is None
    )
    assert "er.extension_id AS release_extension_id" in logger._cursor.query


def test_get_extension_by_install_id_rejects_descriptor_without_matching_identity():
    from core.infrastructure.db.logger_extensions_accounts_common import (
        PlatformLoggerExtensionsAccountsCommonMixin,
    )
    from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
        PlatformLoggerExtensionsAccountsQueryMixin,
    )

    class Cursor:
        description = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, query, params) -> None:
            self.query = query
            self.params = params

        def fetchone(self):
            return {
                "extension_id": "acme.document-converter",
                "release_extension_id": "acme.document-converter",
                "install_account_id": "owner_account",
                "visibility": "global",
                "state": "active",
                "install_id": "install_global",
                "release_id": "release_acme_documents",
                "config_jsonb": {},
                "descriptor_jsonb": {"entrypoint": {"command": ["python", "foreign.py"]}},
            }

    class Logger(
        PlatformLoggerExtensionsAccountsCommonMixin,
        PlatformLoggerExtensionsAccountsQueryMixin,
    ):
        def __init__(self) -> None:
            self._cursor = Cursor()
            self.conn = self

        def cursor(self, **_kwargs):
            return self._cursor

        def _table_exists(self, table_name: str) -> bool:
            return table_name in {"extension_installs", "extension_releases"}

    assert Logger().get_extension_by_install_id("install_global", account_id="consumer") is None


def test_resolve_extension_runtime_record_supports_install_id_lookup():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    db_extension = {
        "extension_key": "mailer",
        "compiled_manifest": {"extension_id": "mailer"},
        "ui_manifest": {"pages": [{"page_id": "overview"}]},
    }
    fake_logger = MagicMock()
    fake_logger.get_extension_by_install_id.return_value = db_extension
    fake_logger.get_extension.return_value = None

    with patch.object(helpers, "load_extension_from_filesystem", return_value=None):
        extension, bl = helpers.resolve_extension_runtime_record(
            "pi_test_install",
            get_logger_only=lambda: fake_logger,
        )

    assert bl is fake_logger
    assert extension is not None
    assert extension["extension_key"] == "mailer"
    fake_logger.get_extension_by_install_id.assert_called_once_with(
        "pi_test_install",
        account_id=DEFAULT_ACCOUNT_ID,
    )
    fake_logger.get_extension.assert_not_called()


def test_resolve_extension_runtime_record_by_install_id_supports_uuid_without_filesystem_lookup():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    install_id = "e8e80d3d-2904-4730-9298-675c44a77677"
    db_extension = {
        "extension_key": "acme.dataset-connector",
        "compiled_manifest": {"extension_id": "acme.dataset-connector"},
        "ui_manifest": {},
    }
    fake_logger = MagicMock()
    fake_logger.get_extension_by_install_id.return_value = db_extension

    with patch.object(
        helpers,
        "load_extension_from_filesystem",
        side_effect=AssertionError("install resolution must not use the filesystem"),
    ):
        extension, bl = helpers.resolve_extension_runtime_record_by_install_id(
            install_id,
            get_logger_only=lambda: fake_logger,
        )

    assert bl is fake_logger
    assert extension is not None
    assert extension["extension_key"] == "acme.dataset-connector"
    fake_logger.get_extension_by_install_id.assert_called_once_with(
        install_id,
        account_id=DEFAULT_ACCOUNT_ID,
    )
    fake_logger.get_extension.assert_not_called()


def test_resolve_extension_runtime_record_by_install_id_attaches_host_install_metadata():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    install_id = "e8e80d3d-2904-4730-9298-675c44a77677"
    db_extension = {
        "extension_key": "acme.notification-provider",
        "compiled_manifest": {"extension_id": "acme.notification-provider"},
    }
    host_metadata = {
        "install_metadata": {
            "runtime_grants": {
                "resources": {
                    "provider_secrets": {
                        "provider_key": "acme.notification-provider",
                        "secret_keys": ["api_token"],
                    }
                }
            }
        },
    }
    fake_logger = MagicMock()
    fake_logger.get_extension_by_install_id.return_value = db_extension

    with (
        patch.object(
            helpers,
            "load_extension_from_filesystem",
            side_effect=AssertionError("install resolution must not use the filesystem"),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_manifest.extension_host_metadata",
            return_value=host_metadata,
        ) as host_metadata_fn,
    ):
        extension, _ = helpers.resolve_extension_runtime_record_by_install_id(
            install_id,
            get_logger_only=lambda: fake_logger,
        )

    assert extension is not None
    assert extension["install_metadata"] == host_metadata["install_metadata"]
    host_metadata_fn.assert_called_once_with(extension)


def test_resolve_extension_runtime_record_by_install_id_preserves_exact_install_identity(
    monkeypatch,
):
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    requested_install_id = "pi_global_disabled"

    class Logger:
        @staticmethod
        def get_extension_by_install_id_for_runtime_gate(
            install_id: str,
            *,
            account_id: str,
        ):
            assert install_id == requested_install_id
            assert account_id == "acc_alpha"
            return {
                "extension_key": "acme.email",
                "compiled_manifest": {"extension_id": "acme.email"},
                "install_id": requested_install_id,
                "release_id": "release_global",
                "enabled": False,
            }

    class CompetingInstallStore:
        @staticmethod
        def get(*, extension_id: str, account_id: str):
            assert extension_id == "acme.email"
            assert account_id == "acc_alpha"
            return SimpleNamespace(
                install_id="pi_account_active",
                release_id="release_account",
                state="active",
            )

    monkeypatch.setattr(
        "core.application.extensions.service_support._get_default_extension_install_store",
        lambda get_logger: CompetingInstallStore(),
    )

    extension, _ = helpers.resolve_extension_runtime_record_by_install_id(
        requested_install_id,
        get_logger_only=Logger,
        account_id="acc_alpha",
    )

    assert extension is not None
    assert extension["install_id"] == requested_install_id
    assert extension["release_id"] == "release_global"
    assert extension["enabled"] is False


def test_resolve_extension_runtime_record_rejects_noncanonical_id_without_filesystem_lookup():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    fake_logger = MagicMock()

    with patch.object(
        helpers,
        "load_extension_from_filesystem",
        side_effect=AssertionError("noncanonical ids must not reach filesystem lookup"),
    ):
        extension, bl = helpers.resolve_extension_runtime_record(
            "conn_mariadb_connection",
            get_logger_only=lambda: fake_logger,
        )

    assert extension is None
    assert bl is fake_logger
    fake_logger.get_extension.assert_not_called()


def test_resolve_extension_runtime_record_skips_install_lookup_for_extension_id():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    fake_logger = MagicMock()
    fake_logger.get_extension.return_value = None

    with patch.object(helpers, "load_extension_from_filesystem", return_value=None):
        extension, bl = helpers.resolve_extension_runtime_record(
            "acme.missing",
            get_logger_only=lambda: fake_logger,
        )

    assert extension is None
    assert bl is fake_logger
    fake_logger.get_extension_by_install_id.assert_not_called()
    fake_logger.get_extension.assert_called_once()


def test_resolve_extension_runtime_record_passes_account_scope_to_install_lookup():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    db_extension = {
        "extension_key": "mailer",
        "account_id": "acc_alpha",
        "compiled_manifest": {"extension_id": "mailer"},
        "ui_manifest": {},
    }
    fake_logger = MagicMock()
    fake_logger.get_extension_by_install_id.return_value = db_extension

    with patch.object(helpers, "load_extension_from_filesystem", return_value=None):
        extension, _ = helpers.resolve_extension_runtime_record(
            "pi_test_install",
            get_logger_only=lambda: fake_logger,
            account_id="acc_alpha",
        )

    assert extension is not None
    fake_logger.get_extension_by_install_id.assert_called_once_with(
        "pi_test_install",
        account_id="acc_alpha",
    )


def test_resolve_extension_runtime_record_hydrates_canonical_install_identity(monkeypatch):
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    class Logger:
        conn = object()

        @staticmethod
        def get_extension(extension_id: str, *, account_id: str):
            assert extension_id == "acme.email"
            assert account_id == "acc_alpha"
            return {
                "extension_id": extension_id,
                "compiled_manifest": {"extension_id": extension_id},
            }

    class InstallStore:
        @staticmethod
        def get(*, extension_id: str, account_id: str):
            assert extension_id == "acme.email"
            assert account_id == "acc_alpha"
            return SimpleNamespace(install_id="pi_email_alpha", state="active")

    monkeypatch.setattr(
        "core.application.extensions.service_support._get_default_extension_install_store",
        lambda get_logger: InstallStore(),
    )
    monkeypatch.setattr(helpers, "load_extension_from_filesystem", lambda _extension_id: None)

    extension, _ = helpers.resolve_extension_runtime_record(
        "acme.email",
        get_logger_only=Logger,
        account_id="acc_alpha",
    )

    assert extension is not None
    assert extension["extension_install_id"] == "pi_email_alpha"
    assert extension["install_id"] == "pi_email_alpha"
    assert extension["enabled"] is True


def test_extension_operation_failure_status_maps_missing_template_to_not_found():
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        extension_operation_failure_status,
    )

    assert (
        extension_operation_failure_status(
            {
                "status": "failed",
                "detail": "Template 'missing' was not found",
                "response": {"error_code": "template_not_found"},
            }
        )
        == 404
    )


def test_extension_operation_failure_status_maps_required_input_to_bad_request():
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        extension_operation_failure_status,
    )

    assert (
        extension_operation_failure_status(
            {
                "status": "failed",
                "detail": "provider_secret_value_required",
                "response": {"error": "provider_secret_value_required"},
            }
        )
        == 400
    )


@pytest.mark.parametrize("error_code", ["bot_token_required", "provider_secret_required"])
def test_extension_operation_failure_status_maps_typed_required_code_to_bad_request(
    error_code: str,
):
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        extension_operation_failure_status,
    )

    assert (
        extension_operation_failure_status(
            {
                "status": "failed",
                "detail": "Extension action failed",
                "response": {"ok": False, "error_code": error_code},
            }
        )
        == 400
    )


@pytest.mark.parametrize(
    "error_code",
    ["upstream_unavailable", "external_effect_timeout_unknown", "timeout_unknown"],
)
def test_extension_operation_failure_status_keeps_upstream_or_ambiguous_codes_as_bad_gateway(
    error_code: str,
):
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        extension_operation_failure_status,
    )

    assert (
        extension_operation_failure_status(
            {
                "status": "failed",
                "detail": "Extension action failed",
                "response": {"ok": False, "error_code": error_code},
            }
        )
        == 502
    )


def test_resolve_extension_runtime_record_never_retries_without_account_scope():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    class BrokenLogger:
        def get_extension(self, _extension_id: str, *, account_id: str | None = None):
            assert account_id == "acc_alpha"
            raise TypeError("scoped runtime lookup failed")

    with (
        patch.object(
            helpers,
            "load_extension_from_filesystem",
            side_effect=AssertionError("storage failure must not become filesystem fallback"),
        ),
        pytest.raises(TypeError, match="scoped runtime lookup failed"),
    ):
        helpers.resolve_extension_runtime_record(
            "acme.private",
            get_logger_only=BrokenLogger,
            account_id="acc_alpha",
        )


def test_resolve_extension_runtime_record_does_not_overlay_install_release_with_filesystem():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    db_extension = {
        "extension_key": "mailer",
        "compiled_manifest": {"extension_id": "mailer"},
        "ui_manifest": {},
    }
    fake_logger = MagicMock()
    fake_logger.get_extension_by_install_id.return_value = db_extension
    fake_logger.get_extension.return_value = None

    with patch.object(
        helpers,
        "load_extension_from_filesystem",
        side_effect=AssertionError("install-bound release must not load filesystem overlay"),
    ) as load_fs:
        extension, _ = helpers.resolve_extension_runtime_record(
            "pi_test_install",
            get_logger_only=lambda: fake_logger,
        )

    load_fs.assert_not_called()
    assert extension is not None
    assert extension["ui_manifest"] == {}


def test_resolve_extension_runtime_record_does_not_fallback_for_missing_explicit_install():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    fake_logger = MagicMock()
    fake_logger.get_extension_by_install_id.return_value = None
    fake_logger.get_extension.return_value = {
        "extension_key": "acme.document-converter",
    }

    with patch.object(
        helpers,
        "load_extension_from_filesystem",
        side_effect=AssertionError("explicit install lookup must fail closed"),
    ) as load_fs:
        extension, _ = helpers.resolve_extension_runtime_record(
            "pi_missing_install",
            get_logger_only=lambda: fake_logger,
        )

    assert extension is None
    load_fs.assert_not_called()
    fake_logger.get_extension.assert_not_called()


def test_resolve_extension_runtime_record_requires_storage_for_explicit_install():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    with patch.object(
        helpers,
        "load_extension_from_filesystem",
        side_effect=AssertionError("explicit install lookup must not use filesystem"),
    ) as load_fs:
        extension, logger = helpers.resolve_extension_runtime_record(
            "pi_unavailable_install",
            get_logger_only=lambda: None,
        )

    assert extension is None
    assert logger is None
    load_fs.assert_not_called()


def test_extension_query_provider_uses_explicit_logger_dependency(monkeypatch):
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    logger = MagicMock()
    extension = {"install_id": "pi_db_only", "runtime_mode": "local"}
    resolve_record = MagicMock(return_value=(extension, logger))
    monkeypatch.setattr(helpers, "resolve_extension_runtime_record", resolve_record)
    monkeypatch.setattr(helpers, "build_extension_compiled_manifest", lambda _row: {})

    provider = helpers.get_extension_query_provider(
        "pi_db_only",
        get_logger_only=lambda: logger,
    )

    assert callable(provider)
    assert resolve_record.call_args.kwargs["get_logger_only"]() is logger


def test_operation_backed_resource_query_hydrates_selected_connection_target(
    monkeypatch,
):
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    logger = MagicMock()
    extension = {
        "extension_key": "fixture.resource-catalog",
        "runtime_mode": "local",
    }
    monkeypatch.setattr(
        helpers,
        "resolve_extension_runtime_record",
        lambda *_args, **_kwargs: (extension, logger),
    )
    monkeypatch.setattr(helpers, "build_extension_compiled_manifest", lambda _row: {})
    resolve_target = MagicMock(
        return_value={
            "connection_id": "resource-1",
            "secrets": {"access_token": "selected-token"},
        }
    )
    monkeypatch.setattr(
        "core.application.extensions.operation_targets._direct_action_connection_target",
        resolve_target,
    )
    captured: dict = {}

    def run_query(_extension, **kwargs):
        captured.update(kwargs)
        return {
            "status": "succeeded",
            "response": {"ok": True, "result": {"items": [], "total": 0}},
        }

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_query",
        run_query,
    )
    provider = helpers.get_extension_query_provider("resource-install")

    result = provider(
        {
            "query_id": "list_resources",
            "scope": {
                "account_id": "account-1",
                "project_id": "project-1",
                "connection_id": "resource-1",
            },
            "query_manifest": {
                "operation_id": "list_resources",
                "connection_type_id": "resource_account",
            },
            "target": {
                "connection": {
                    "secrets": {"access_token": "caller-token-must-be-ignored"},
                }
            },
        }
    )

    assert result == {"items": [], "next_cursor": "", "total": 0}
    assert captured["target"] == {
        "connection_id": "resource-1",
        "connection": {
            "connection_id": "resource-1",
            "secrets": {"access_token": "selected-token"},
        },
    }
    assert resolve_target.call_args.kwargs["extension_install_id"] == "resource-install"
    # The declared set, not one arbitrarily chosen name: an operation with no
    # policy constrains nothing, and one with several constrains all of them.
    assert resolve_target.call_args.kwargs["required_connection_type_ids"] == frozenset()
    assert "caller-token-must-be-ignored" not in str(captured)


@pytest.mark.parametrize(
    ("row_patch", "scope_patch"),
    [
        ({"project_id": "project-other"}, {}),
        ({"extension_install_id": "install-other"}, {}),
        ({"connection_type": "wrong_account"}, {}),
    ],
)
def test_resource_query_rejects_wrong_scope_or_connection_requirements_before_oauth(
    monkeypatch,
    row_patch,
    scope_patch,
):
    from core.application.management.errors import ServiceValidationError
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    row = {
        "connection_id": "resource-1",
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": "fixture.resource-catalog",
        "extension_install_id": "resource-install",
        "connection_type": "resource_account",
        "enabled": True,
        "connection_config": {
            "oauth": {
                "lifecycle_status": "connected",
            }
        },
        "runtime_status": "ready",
    }
    row.update(row_patch)
    logger = MagicMock()
    logger.get_project_connection.return_value = row
    extension = {
        "extension_key": "fixture.resource-catalog",
        "runtime_mode": "local",
        "operation_manifest": [
            {
                "operation_id": "search_resources",
                "connection_type_ids": ["resource_account"],
                "required_oauth_scopes": ["resources.read"],
            }
        ],
    }
    monkeypatch.setattr(
        helpers,
        "resolve_extension_runtime_record",
        lambda *_args, **_kwargs: (extension, logger),
    )
    monkeypatch.setattr(helpers, "build_extension_compiled_manifest", lambda _row: {})
    resolve_oauth = MagicMock(return_value={"access_token": "must-not-resolve"})
    monkeypatch.setattr(
        "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime",
        resolve_oauth,
    )
    run_query = MagicMock()
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_query",
        run_query,
    )
    provider = helpers.get_extension_query_provider("resource-install")
    scope = {
        "account_id": "account-1",
        "project_id": "project-1",
        "connection_id": "resource-1",
        **scope_patch,
    }

    with pytest.raises(ServiceValidationError):
        provider(
            {
                "query_id": "resource_by_id",
                "scope": scope,
                "query_manifest": {
                    "operation_id": "search_resources",
                    "connection_type_id": "resource_account",
                },
            }
        )

    resolve_oauth.assert_not_called()
    run_query.assert_not_called()


def test_resource_query_request_rejects_caller_supplied_runtime_target() -> None:
    from pydantic import ValidationError

    from core.entrypoints.admin.workflows_routes_extension import ResourceQueryRequest

    with pytest.raises(ValidationError):
        ResourceQueryRequest.model_validate(
            {
                "install_id": "fixture-install",
                "query_id": "fixture_document_by_id",
                "project_id": "project-1",
                "connection_id": "drive-1",
                "target": {"connection": {"secrets": {"access_token": "caller-token"}}},
            }
        )


def test_extension_runtime_helpers_do_not_import_http_entrypoints() -> None:
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    source = Path(helpers.__file__).read_text(encoding="utf-8")

    assert "core.entrypoints" not in source


def test_log_host_extension_invocation_merges_context_and_structured_fields():
    from core.infrastructure.extension_runtime.extension_invocation_audit import (
        log_host_extension_invocation,
    )

    bl = MagicMock()

    log_host_extension_invocation(
        bl=bl,
        extension={"extension_key": "mailer"},
        extension_id="mailer",
        invocation_type="action:send",
        request={"input": {"to": "alice@example.com"}},
        invocation={
            "status": "failed",
            "detail": "permission denied",
            "audit_context": {
                "job_id": "11111111-1111-1111-1111-111111111121",
                "workflow_id": "wf.audit",
                "trace_id": "trace-audit",
            },
        },
        runtime_context={
            "account_id": "acc_default",
            "actor_type": "user",
            "actor_id": "jane",
            "job_id": "11111111-1111-1111-1111-111111111142",
            "workflow_id": "wf.runtime",
            "group_id": "group-1",
            "step_id": "step-2",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
            "project_id": 7,
            "source": "runtime-source",
        },
        request_context={
            "source": "explicit-source",
            "tool_id": "tool.demo",
            "connector_install_id": "pi_install_1",
        },
        action_id="send",
    )

    kwargs = bl.log_extension_invocation.call_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["account_id"] == "acc_default"
    assert kwargs["actor_type"] == "user"
    assert kwargs["actor_id"] == "jane"
    assert kwargs["job_id"] == "11111111-1111-1111-1111-111111111142"
    assert kwargs["workflow_id"] == "wf.runtime"
    assert kwargs["group_id"] == "group-1"
    assert kwargs["step_id"] == "step-2"
    assert kwargs["step_execution_id"] == "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"
    assert kwargs["action_id"] == "send"
    assert kwargs["request_context"]["source"] == "explicit-source"
    assert kwargs["request_context"]["tool_id"] == "tool.demo"
    assert kwargs["request_context"]["connector_install_id"] == "pi_install_1"
    assert kwargs["request_context"]["job_id"] == "11111111-1111-1111-1111-111111111142"
    assert kwargs["request_context"]["project_id"] == 7
    assert kwargs["request_context"]["trace_id"] == "trace-audit"


def test_log_host_extension_invocation_warns_and_swallows_logger_failures(caplog):
    from core.infrastructure.extension_runtime.extension_invocation_audit import (
        log_host_extension_invocation,
    )

    bl = MagicMock()
    bl.log_extension_invocation.side_effect = RuntimeError("db unavailable")

    with caplog.at_level(logging.WARNING):
        log_host_extension_invocation(
            bl=bl,
            extension={"extension_key": "mailer"},
            extension_id="mailer",
            invocation_type="invoke",
            request={"request": {"action": "ping"}},
            invocation={"status": "succeeded", "response": {"ok": True}},
            runtime_context={"account_id": "acc_default"},
        )

    assert "extension invocation audit persistence failed" in caplog.text


@pytest.mark.parametrize(
    "operation",
    [
        "action:get_resource",
        "action:search_resources",
        "action:test_connection",
        "action:download_resource",
        "action:upload_resource",
        "action:read_table",
        "action:write_table",
        "action:append_table_rows",
    ],
)
def test_extension_invocation_audit_persists_only_safe_projection(operation):
    from core.infrastructure.extension_runtime.extension_invocation_audit import (
        log_host_extension_invocation,
    )

    canary = "PRIVATE_EXTENSION_CANARY_alice@example.test"
    bl = MagicMock()
    log_host_extension_invocation(
        bl=bl,
        extension={"extension_key": "fixture.resource-catalog"},
        extension_id="fixture.resource-catalog",
        invocation_type=operation,
        request={
            "context": {"project_id": "project-1"},
            "target": {
                "connection_id": "resource-1",
                "connection": {
                    "secret_refs": {"access_token": canary},
                    "secrets": {"access_token": canary},
                },
            },
            "input": {
                "connection_ref": "resource-1",
                "message_id": "msg-1",
                "file_id": "file-1",
                "spreadsheet_id": "spreadsheet-1",
                "sheet_id": 7,
                "destination_folder_id": "folder-1",
                "filters": {"subject": canary, "text": canary},
                "name": canary,
                "filename": canary,
                "sheet_title": canary,
                "range": canary,
                "table_range": canary,
                "inline_rows": [[canary]],
            },
        },
        invocation={
            "status": "failed",
            "detail": canary,
            "response": {
                "ok": False,
                "error_code": "provider_unavailable",
                "error": canary,
                "result": {
                    "email_address": canary,
                    "message": {
                        "id": "msg-1",
                        "subject": canary,
                        "snippet": canary,
                        "text_body": canary,
                        "html_body": canary,
                        "headers": [{"name": "From", "value": canary}],
                    },
                    "file_id": "file-1",
                    "spreadsheet_id": "spreadsheet-1",
                    "sheet_id": 7,
                    "row_count": 20,
                    "column_count": 3,
                    "size": 4096,
                    "name": canary,
                    "filename": canary,
                    "sheet_title": canary,
                    "appended_range": canary,
                    "preview": [{"customer": canary}],
                },
                "errors": [{"code": "provider_unavailable", "message": canary}],
            },
        },
        runtime_context={"account_id": "account-1"},
    )

    persisted = bl.log_extension_invocation.call_args.kwargs
    serialized = json.dumps(
        {
            "request_jsonb": persisted["request"],
            "response_jsonb": persisted["response"],
            "error_text": persisted["error_text"],
        },
        sort_keys=True,
    )
    assert canary not in serialized
    assert "access_token" not in serialized
    assert persisted["request"]["target"] == {"connection_id": "resource-1"}
    assert persisted["request"]["input"] == {
        "connection_ref": "resource-1",
        "destination_folder_id": "folder-1",
        "file_id": "file-1",
        "message_id": "msg-1",
        "sheet_id": 7,
        "spreadsheet_id": "spreadsheet-1",
    }
    assert persisted["response"]["response"]["error_code"] == "provider_unavailable"
    assert persisted["response"]["response"]["result"] == {
        "column_count": 3,
        "file_id": "file-1",
        "message": {"id": "msg-1"},
        "row_count": 20,
        "sheet_id": 7,
        "size": 4096,
        "spreadsheet_id": "spreadsheet-1",
    }


def test_dataset_page_extension_audit_is_bounded_and_redacts_item_identity() -> None:
    """Dataset-page audits must never retain raw rows or more than ten failures."""
    from core.infrastructure.extension_runtime.extension_invocation_audit import (
        log_host_extension_invocation,
    )

    bl = MagicMock()
    rows = [
        {"customer_id": f"customer-{index}", "email": f"customer-{index}@private.example"}
        for index in range(1000)
    ]
    log_host_extension_invocation(
        bl=bl,
        extension={"extension_key": "fixture.provider"},
        extension_id="fixture.provider",
        invocation_type="action:send_page",
        request={"action_id": "send_page", "input_payload": {"rows": rows}},
        invocation={
            "status": "failed",
            "response": {
                "ok": False,
                "errors": _dataset_audit_errors(25),
                "result": {"rows": rows, "failed_count": 25},
            },
        },
        runtime_context={"account_id": "account-1"},
        request_context={
            "source": "connector_step_runtime",
            "dataset_page": {
                "dataset_handle": "dst_orders_2026",
                "page_ordinal": 7,
                "item_count": 1000,
                "digest": "a" * 64,
            },
        },
    )

    persisted = bl.log_extension_invocation.call_args.kwargs
    envelope = {
        "request": persisted["request"],
        "response": persisted["response"],
        "request_context": persisted["request_context"],
    }
    serialized = json.dumps(envelope, sort_keys=True)
    assert len(serialized.encode("utf-8")) < 16 * 1024
    assert "private.example" not in serialized
    assert "customer-17" not in serialized
    assert persisted["request_context"]["dataset_page"]["page_ordinal"] == 7
    errors = persisted["response"]["response"]["errors"]
    assert len(errors) == 10
    assert (
        errors[0]
        == {
            "page_relative_index": 0,
            "hashed_item_key": "65a8f95e999b098c",  # pragma: allowlist secret - deterministic diagnostic hash
            "provider_code": "invalid_email",
            "field": "email",
            "message": "[redacted provider message]",
        }
    )


def test_dataset_page_extension_audit_caps_provider_controlled_count_fields() -> None:
    """Durable audit projection has a hard byte and field-count boundary."""
    from core.infrastructure.extension_runtime.extension_invocation_audit import (
        log_host_extension_invocation,
    )

    bl = MagicMock()
    counters = {f"provider_metric_{index}_count": index for index in range(5000)}
    log_host_extension_invocation(
        bl=bl,
        extension={"extension_key": "fixture.provider"},
        extension_id="fixture.provider",
        invocation_type="action:send_page",
        request={"action_id": "send_page"},
        invocation={
            "status": "succeeded",
            "response": {"ok": True, "result": counters},
        },
        runtime_context={"account_id": "account-1"},
        request_context={
            "source": "connector_step_runtime",
            "dataset_page": {
                "dataset_handle": "dst_orders_2026",
                "page_ordinal": 7,
                "item_count": 1000,
                "digest": "a" * 64,
            },
        },
    )

    persisted = bl.log_extension_invocation.call_args.kwargs
    envelope = {
        "request": persisted["request"],
        "response": persisted["response"],
        "request_context": persisted["request_context"],
    }
    assert len(json.dumps(envelope, sort_keys=True).encode("utf-8")) <= 16 * 1024
    counts = persisted["response"]["response"]["result"]
    assert len(counts) <= 64


def _dataset_audit_errors(count: int) -> list[dict]:
    return [
        {
            "index": index,
            "item_key": f"customer-{index}",
            "provider_code": "invalid_email",
            "field": "email",
            "message": f"Invalid email customer-{index}@private.example",
        }
        for index in range(count)
    ]
