"""The LLM provider panel learns whether a key is saved and whether it worked.

Seen on a stand: a half-copied Ollama key was saved, then saved twice more
unchanged, and the panel looked the same every time. The status endpoint says
which secrets are saved (names, never values) and how the last check ended.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.settings import llm_provider_connection_status as status_module
from core.application.settings import llm_provider_extension_repository as store
from core.entrypoints.api.llm_provider_extensions import create_api_llm_provider_extensions_router

_SECRET = "half-of-a-key.and-the-other-half"  # pragma: allowlist secret - test sentinel
_SAVED_AT = {"api_key": "2026-10-04T10:42:00+00:00"}  # pragma: allowlist secret - a timestamp
_CONTRACT = SimpleNamespace(
    required_provider_secrets=["api_key"],
    optional_provider_secrets=["runtime_bearer_token"],
)


class _Cursor:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self.log = log
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params):
        self.log.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple]] = []

    def cursor(self, **_kwargs):
        return _Cursor(self.log)


def _use_cases(monkeypatch, *, install_config: dict, credentials: dict | None):
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_a: (
            {"install_id": "pi_ollama", "config_jsonb": {"stale": "record"}},
            _CONTRACT,
            "pi_ollama",
            "acme.ollama",
            "Ollama",
        ),
    )
    monkeypatch.setattr(
        store,
        "get_install_current_credential_snapshot",
        lambda *_a, **_k: ("ref", "cipher" if credentials is not None else ""),
    )
    monkeypatch.setattr(
        "core.application.auth.secrets.decrypt_secret",
        lambda _cipher: json.dumps(credentials or {}),
    )
    _fresh_install_config(monkeypatch, install_config)
    return use_cases


def _fresh_install_config(monkeypatch, install_config: dict) -> None:
    """The status reads the install's config fresh, not from a cached record."""
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        status_module, "read_install_config", lambda *_a, **_k: dict(install_config)
    )


def test_status_names_saved_secrets_and_the_last_check(monkeypatch) -> None:
    use_cases = _use_cases(
        monkeypatch,
        install_config={
            status_module.SAVED_SECRETS_KEY: _SAVED_AT,
            "llm_provider_last_check": {
                "status": "authentication_error",
                "checked_at": "2026-10-04T10:43:00+00:00",
            },
        },
        credentials={"api_key": _SECRET, "runtime_bearer_token": "  "},
    )

    payload = use_cases.connection_status("pi_ollama", "acc-1")

    assert payload == {
        "install_id": "pi_ollama",
        "saved_secrets": [{"name": "api_key", "saved_at": "2026-10-04T10:42:00+00:00"}],
        "last_check": {
            "status": "authentication_error",
            "checked_at": "2026-10-04T10:43:00+00:00",
        },
    }


def test_status_with_nothing_saved_and_never_checked(monkeypatch) -> None:
    use_cases = _use_cases(monkeypatch, install_config={}, credentials=None)

    payload = use_cases.connection_status("pi_ollama", "acc-1")

    assert payload["saved_secrets"] == []
    assert payload["last_check"] is None


def test_status_route_never_returns_secret_values(monkeypatch) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    class Logger:
        conn = object()

        @staticmethod
        def get_extension_install_for_operator(install_id, *, account_id):
            return {"install_id": install_id}

    monkeypatch.setattr(
        LlmProviderExtensionUseCases,
        "_resolve_install",
        lambda self, *_a: (
            {
                "install_id": "pi_ollama",
                "config_jsonb": {
                    store.INSTALL_CREDENTIAL_REF_KEY: "llmext_cred_pi_ollama",
                    status_module.SAVED_SECRETS_KEY: _SAVED_AT,
                },
            },
            _CONTRACT,
            "pi_ollama",
            "acme.ollama",
            "Ollama",
        ),
    )
    monkeypatch.setattr(
        store, "get_install_current_credential_snapshot", lambda *_a, **_k: ("ref", "c")
    )
    _fresh_install_config(
        monkeypatch,
        {
            store.INSTALL_CREDENTIAL_REF_KEY: "llmext_cred_pi_ollama",
            status_module.SAVED_SECRETS_KEY: _SAVED_AT,
        },
    )
    monkeypatch.setattr(
        "core.application.auth.secrets.decrypt_secret",
        lambda _cipher: json.dumps({"api_key": _SECRET}),
    )
    app = FastAPI()
    app.include_router(
        create_api_llm_provider_extensions_router(
            get_logger_only=Logger,
            require_authenticated_user=lambda request, required_roles=None: None,
            auth_admin_roles={"admin"},
            api_error_response=lambda exc, **_kwargs: (_ for _ in ()).throw(exc),
        )
    )

    response = TestClient(app).get("/api/llm-provider-extensions/pi_ollama/status")

    assert response.status_code == 200
    assert response.json()["saved_secrets"] == [
        {"name": "api_key", "saved_at": "2026-10-04T10:42:00+00:00"}
    ]
    for fragment in (_SECRET, _SECRET[:8], _SECRET.split(".")[1], "llmext_cred"):
        assert fragment not in response.text


def test_install_config_is_read_fresh_from_the_connection() -> None:
    class Cursor(_Cursor):
        def fetchone(self):
            return {"config_jsonb": {"llm_provider_last_check": {"status": "ok"}}}

    class Conn(_Conn):
        def cursor(self, **_kwargs):
            return Cursor(self.log)

    conn = Conn()

    config = status_module.read_install_config(conn, account_id="acc-1", extension_install_id="pi")

    assert config == {"llm_provider_last_check": {"status": "ok"}}
    assert conn.log[0][1] == ("acc-1", "pi")


def test_check_outcomes_and_saved_names_are_written_to_the_install() -> None:
    conn = _Conn()

    status_module.record_last_check(
        conn, account_id="acc-1", extension_install_id="pi", status="ok"
    )
    status_module.clear_last_check(conn, account_id="acc-1", extension_install_id="pi")
    status_module.record_saved_secrets(
        conn, account_id="acc-1", extension_install_id="pi", names=["api_key", " ", "api_key"]
    )

    (record_sql, record_params), (clear_sql, _), (saved_sql, saved_params) = conn.log
    assert "'{llm_provider_last_check}'" in record_sql
    assert record_params == ("ok", "acc-1", "pi")
    assert "- 'llm_provider_last_check'" in clear_sql
    assert "'{llm_provider_saved_secrets}'" in saved_sql
    assert saved_params == (["api_key"], "acc-1", "pi")
    assert (
        status_module.record_saved_secrets(
            conn, account_id="acc-1", extension_install_id="pi", names=[]
        )
        is False
    )


def test_a_failed_check_is_recorded_without_masking_the_failure(monkeypatch, caplog) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    recorded: list[str] = []
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        status_module, "record_last_check", lambda *_a, status, **_k: recorded.append(status)
    )
    LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)._record_failed_check(
        "acc-1", "pi", "authentication_error"
    )
    assert recorded == ["authentication_error"]

    def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(status_module, "record_last_check", broken)
    LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)._record_failed_check(
        "acc-1", "pi", "timeout"
    )
    assert "Could not record the failed LLM provider check" in caplog.text


@pytest.mark.parametrize("secrets", [{"api_key": _SECRET}, {}])
def test_saving_reports_whether_anything_changed(monkeypatch, secrets) -> None:
    from tests.unit.test_llm_provider_catalog_ux import _saving

    use_cases, _stored = _saving(monkeypatch, existing_config={})
    monkeypatch.setattr(
        "core.infrastructure.db.model_provider_store.upsert_secret_ref", lambda *_a, **_k: None
    )
    monkeypatch.setattr("core.application.auth.secrets.encrypt_secret", lambda value: "cipher")
    saved_names: list[list[str]] = []
    cleared: list[str] = []
    monkeypatch.setattr(
        status_module,
        "record_saved_secrets",
        lambda *_a, names, **_k: saved_names.append(list(names)),
    )
    monkeypatch.setattr(
        status_module,
        "clear_last_check",
        lambda *_a, extension_install_id, **_k: cleared.append(extension_install_id),
    )

    result = use_cases.save_credentials(
        "pi_acme", "acc-1", secrets=secrets, connection_config={"upstream_base_url": ""}
    )

    assert result["changed"] is bool(secrets)
    assert result["verification_invalidated"] is bool(secrets)
    assert saved_names == ([["api_key"]] if secrets else [])
    assert cleared == (["pi_acme"] if secrets else [])
    assert _SECRET not in json.dumps(result)
