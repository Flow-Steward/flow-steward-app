from __future__ import annotations

import importlib


def test_attachment_policy_defaults_are_loaded_from_example_env(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.delenv("FS_ALLOWED_ATTACHMENT_MIMES", raising=False)
    monkeypatch.delenv("FS_ALLOWED_ATTACHMENT_EXTENSIONS", raising=False)

    reloaded = importlib.reload(config_module)

    assert (
        reloaded._example_env_value("FS_ALLOWED_ATTACHMENT_MIMES")
        == "image/png,image/jpeg,image/webp,image/gif,application/pdf,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "application/vnd.ms-excel,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "text/plain,text/markdown,text/csv,application/octet-stream"
    )
    assert reloaded.CONFIG["allowed_attachment_mimes"] == {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/octet-stream",
    }
    assert reloaded.CONFIG["allowed_attachment_extensions"] == {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".pdf",
        ".xlsx",
        ".xls",
        ".docx",
        ".txt",
        ".md",
        ".csv",
    }


def test_attachment_policy_config_reads_env_overrides(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.setenv("FS_ATTACHMENT_MAX_SIZE_BYTES", "12345")
    monkeypatch.setenv("FS_ATTACHMENT_MAX_FILES", "7")
    monkeypatch.setenv("FS_ALLOWED_ATTACHMENT_MIMES", "text/plain, image/png")
    monkeypatch.setenv("FS_ALLOWED_ATTACHMENT_EXTENSIONS", "txt,png")

    reloaded = importlib.reload(config_module)

    assert reloaded.CONFIG["attachment_max_size_bytes"] == 12345
    assert reloaded.CONFIG["attachment_max_files"] == 7
    assert reloaded.CONFIG["allowed_attachment_mimes"] == {"text/plain", "image/png"}
    assert reloaded.CONFIG["allowed_attachment_extensions"] == {".txt", ".png"}

    monkeypatch.delenv("FS_ATTACHMENT_MAX_SIZE_BYTES", raising=False)
    monkeypatch.delenv("FS_ATTACHMENT_MAX_FILES", raising=False)
    monkeypatch.delenv("FS_ALLOWED_ATTACHMENT_MIMES", raising=False)
    monkeypatch.delenv("FS_ALLOWED_ATTACHMENT_EXTENSIONS", raising=False)
    importlib.reload(config_module)


def test_extension_payload_redaction_defaults_are_loaded_from_example_env(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_REDACT_KEYS", raising=False)
    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_REDACT_EXACT_KEYS", raising=False)
    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_REDACT_KEY_FRAGMENTS", raising=False)

    reloaded = importlib.reload(config_module)

    assert reloaded.CONFIG["extension_payload_redact_keys"] == {
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api_secret",
        "access_token",
        "refresh_token",
        "authorization",
        "credential",
        "private_key",
        "client_secret",
        "webhook_secret",
        "signing_key",
    }
    assert reloaded.CONFIG["extension_payload_redact_exact_keys"] == {
        "db_dsn",
        "dsn",
        "project_connections",
        "project_providers",
        "watch",
        "watch_config",
        "config",
        "raw_config",
        "secret_refs",
        "credentials",
        "auth",
        "callback_url",
    }
    assert reloaded.CONFIG["extension_payload_redact_key_fragments"] == (
        "secret",
        "token",
        "password",
        "api_key",
        "credential",
        "dsn",
        "connection",
        "watch",
    )


def test_extension_payload_redaction_config_reads_env_overrides(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.setenv("FS_EXTENSION_DEBUG_PAYLOADS", "1")
    monkeypatch.setenv("FS_EXTENSION_PAYLOAD_REDACT_KEYS", "password, signing_key")
    monkeypatch.setenv("FS_EXTENSION_PAYLOAD_REDACT_EXACT_KEYS", "config, raw_config")
    monkeypatch.setenv("FS_EXTENSION_PAYLOAD_REDACT_KEY_FRAGMENTS", "secret,dsn")
    monkeypatch.setenv("FS_EXTENSION_PAYLOAD_REDACT_MAX_DEPTH", "11")
    monkeypatch.setenv("FS_EXTENSION_PAYLOAD_STRING_TRUNCATE_LENGTH", "321")
    monkeypatch.setenv("FS_TRACE_REDACT_MAX_LENGTH", "654")

    reloaded = importlib.reload(config_module)

    assert reloaded.CONFIG["extension_debug_payloads"] is True
    assert reloaded.CONFIG["extension_payload_redact_keys"] == {
        "password",
        "signing_key",
    }
    assert reloaded.CONFIG["extension_payload_redact_exact_keys"] == {
        "config",
        "raw_config",
    }
    assert reloaded.CONFIG["extension_payload_redact_key_fragments"] == ("secret", "dsn")
    assert reloaded.CONFIG["extension_payload_redact_max_depth"] == 11
    assert reloaded.CONFIG["extension_payload_string_truncate_length"] == 321
    assert reloaded.CONFIG["trace_redact_max_length"] == 654

    monkeypatch.delenv("FS_EXTENSION_DEBUG_PAYLOADS", raising=False)
    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_REDACT_KEYS", raising=False)
    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_REDACT_EXACT_KEYS", raising=False)
    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_REDACT_KEY_FRAGMENTS", raising=False)
    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_REDACT_MAX_DEPTH", raising=False)
    monkeypatch.delenv("FS_EXTENSION_PAYLOAD_STRING_TRUNCATE_LENGTH", raising=False)
    monkeypatch.delenv("FS_TRACE_REDACT_MAX_LENGTH", raising=False)
    importlib.reload(config_module)


def test_runtime_defaults_use_registry_values(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    reloaded = importlib.reload(config_module)

    assert reloaded._registry_default("FS_DEFAULT_PROVIDER", "fallback") == ""
    assert reloaded._registry_default("FS_DEFAULT_MODEL", "fallback") == ""
    assert reloaded._registry_default("FS_DEFAULT_TEMPERATURE", "fallback") == "0.1"
    assert reloaded._registry_default("SENTRY_DSN", "fallback") == ""
    assert reloaded._registry_default("FS_LOG_LEVEL", "fallback") == "INFO"
    assert reloaded._registry_default("FS_OBSERVABILITY_DSN", "fallback") == ""


def test_runtime_db_dsn_uses_fs_values(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.setenv("FS_LOAD_REPO_DOTENV", "0")
    monkeypatch.setenv("FS_DB_DSN", "postgres://flow-steward-db")
    monkeypatch.setenv("FS_OBSERVABILITY_DSN", "postgres://flow-steward-observability")

    reloaded = importlib.reload(config_module)

    assert reloaded._db_dsn_env() == "postgres://flow-steward-db"
    assert reloaded._observability_dsn_env() == "postgres://flow-steward-observability"
    assert reloaded.CONFIG["fs_db_dsn"] == "postgres://flow-steward-db"
    assert reloaded.CONFIG["fs_observability_dsn"] == "postgres://flow-steward-observability"


def test_runtime_config_does_not_load_provider_api_keys(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.setenv("FS_LOAD_REPO_DOTENV", "0")
    monkeypatch.setenv("GOOGLE_API_KEY", "canonical-google-key")
    monkeypatch.setenv("GEMINI_API_KEY", "unsupported-alias-key")

    reloaded = importlib.reload(config_module)

    assert "google_api_key" not in reloaded.CONFIG

    monkeypatch.delenv("GOOGLE_API_KEY")
    reloaded = importlib.reload(config_module)

    assert "google_api_key" not in reloaded.CONFIG


def test_runtime_config_skips_repo_dotenv_autoload_in_production(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("FS_LOAD_REPO_DOTENV", raising=False)

    reloaded = importlib.reload(config_module)

    assert reloaded._should_autoload_repo_dotenv() is False


def test_runtime_config_autoloads_repo_dotenv_when_environment_is_unset(monkeypatch):
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("FS_RUNTIME_ENVIRONMENT", raising=False)
    monkeypatch.delenv("FS_STACK_ENVIRONMENT", raising=False)
    monkeypatch.delenv("FS_LOAD_REPO_DOTENV", raising=False)

    reloaded = importlib.reload(config_module)

    assert reloaded._should_autoload_repo_dotenv() is True


def test_public_workflow_settings_resolve_after_dotenv_bootstrap(monkeypatch):
    import core.infrastructure.config.public_workflow as public_workflow_module
    import core.infrastructure.config.runtime_config as config_module

    monkeypatch.delenv("FS_RETENTION_JOBS_DAYS", raising=False)
    importlib.reload(public_workflow_module)
    monkeypatch.setenv("FS_RETENTION_JOBS_DAYS", "45")

    reloaded = importlib.reload(config_module)

    assert reloaded.CONFIG["idempotency_result_retention_days"] == 45


def test_task_upload_admission_clamps_capacities_and_body_timeout(monkeypatch):
    from core.infrastructure.config.task_upload import task_upload_admission_settings

    monkeypatch.setenv("FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS", "100")
    monkeypatch.setenv("FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS_PER_ACCOUNT", "3")
    monkeypatch.setenv("FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS_PER_PRINCIPAL", "10")
    monkeypatch.setenv("FS_TASK_UPLOAD_BODY_READ_TIMEOUT_SECONDS", "1")
    settings = task_upload_admission_settings()
    assert (
        settings.total_capacity,
        settings.per_account_capacity,
        settings.per_principal_capacity,
    ) == (64, 3, 3)
    assert settings.body_read_timeout_seconds == 5

    monkeypatch.setenv("FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS", "0")
    monkeypatch.setenv("FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS_PER_ACCOUNT", "invalid")
    monkeypatch.setenv("FS_TASK_UPLOAD_BODY_READ_TIMEOUT_SECONDS", "9999")
    settings = task_upload_admission_settings()
    assert (
        settings.total_capacity,
        settings.per_account_capacity,
        settings.per_principal_capacity,
    ) == (1, 1, 1)
    assert settings.body_read_timeout_seconds == 3600
