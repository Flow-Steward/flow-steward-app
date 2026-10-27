from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.entrypoints.api.llm_provider_extensions import create_api_llm_provider_extensions_router


def _client() -> TestClient:
    class Logger:
        conn = object()

        @staticmethod
        def get_extension_install_for_operator(install_id, *, account_id):
            return {
                "install_id": install_id,
                "extension_id": "fixture.provider",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
                "runtime_mode": "remote_https",
                "remote_base_url": "https://runtime.example.com",
                "config_jsonb": {},
            }

    app = FastAPI()
    app.include_router(
        create_api_llm_provider_extensions_router(
            get_logger_only=Logger,
            require_authenticated_user=lambda request, required_roles=None: None,
            auth_admin_roles={"admin"},
            api_error_response=lambda exc, **_kwargs: (_ for _ in ()).throw(exc),
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_initial_credential_save_keeps_discoverable_deterministic_ref() -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        credential_ref_for_save,
    )

    assert (
        credential_ref_for_save("llmext_cred_install", credentials_changed=True, has_sources=False)
        == "llmext_cred_install"
    )
    assert credential_ref_for_save(
        "llmext_cred_install", credentials_changed=True, has_sources=True
    ).startswith("llmext_cred_install_")


def test_provider_listing_batch_fetches_account_models_once(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store

    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_args, **_kwargs: [
            {"id": "source-1", "credential_secret_ref": "secret-1"},
            {"id": "source-2", "credential_secret_ref": None},
        ],
    )
    model_queries: list[str | None] = []

    def list_models(*_args, provider_source_id=None, **_kwargs):
        model_queries.append(provider_source_id)
        return [
            {"id": "model-1", "provider_source_id": "source-1"},
            {"id": "model-2", "provider_source_id": "source-2"},
        ]

    monkeypatch.setattr(store, "list_models", list_models)

    response = _client().get("/api/llm-provider-extensions")

    assert response.status_code == 200
    assert model_queries == [None]
    sources = response.json()["sources"]
    assert [model["id"] for model in sources[0]["models"]] == ["model-1"]
    assert [model["id"] for model in sources[1]["models"]] == ["model-2"]


def test_provider_listing_restores_safe_connection_state_and_expires_old_evidence(
    monkeypatch,
) -> None:
    from core.application.settings import llm_provider_extension_repository as store

    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_args, **_kwargs: [
            {
                "id": "source-1",
                "extension_install_id": "install-1",
                "credential_secret_ref": "secret-1",
                "connection_config_jsonb": {"upstream_base_url": "https://upstream.example.com"},
                "remote_base_url": "https://runtime.example.com",
            }
        ],
    )
    monkeypatch.setattr(
        store,
        "list_models",
        lambda *_args, **_kwargs: [
            {
                "id": "model-1",
                "provider_source_id": "source-1",
                "verification_status": "passed",
                "verified_at": datetime.now(UTC) - timedelta(days=31),
            }
        ],
    )

    response = _client().get("/api/llm-provider-extensions")

    assert response.status_code == 200
    source = response.json()["sources"][0]
    assert source["remote_base_url"] == "https://runtime.example.com"
    assert source["connection_config"] == {"upstream_base_url": "https://upstream.example.com"}
    assert "connection_config_jsonb" not in source
    assert source["models"][0]["verification_status"] == "stale"
    assert source["models"][0]["verification_error_safe"] == "verification_expired"


def test_refresh_rejects_malformed_json_before_use_case(monkeypatch) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    calls: list[str] = []

    async def refresh_models(self, install_id, account_id, *, connection_config):
        calls.append(install_id)
        return {"status": "ok"}

    monkeypatch.setattr(LlmProviderExtensionUseCases, "refresh_models", refresh_models)

    response = _client().post(
        "/api/llm-provider-extensions/install-1/refresh-models",
        content="{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code in {400, 422}
    assert calls == []


def test_refresh_reports_a_rejected_key_as_its_category(monkeypatch) -> None:
    """Discovery with a rejected key answers 502 with the category the panel maps.

    The panel turns ``authentication_error`` into a sentence about the key; a
    generic message here would leave it with nothing to say.
    """
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    def refresh_models(self, install_id, account_id, *, connection_config):
        raise LlmProviderExtensionError(502, "authentication_error")

    monkeypatch.setattr(LlmProviderExtensionUseCases, "refresh_models", refresh_models)

    response = _client().post(
        "/api/llm-provider-extensions/install-1/refresh-models",
        json={"connection_config": {}},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "authentication_error"}


@pytest.mark.parametrize("operation", ["credentials", "refresh-models"])
def test_provider_requests_reject_unknown_top_level_fields_before_use_case(
    monkeypatch, operation
) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    calls: list[str] = []

    def save_credentials(self, install_id, account_id, *, secrets, connection_config):
        calls.append(install_id)
        return {"status": "ok"}

    async def refresh_models(self, install_id, account_id, *, connection_config):
        calls.append(install_id)
        return {"status": "ok"}

    monkeypatch.setattr(LlmProviderExtensionUseCases, "save_credentials", save_credentials)
    monkeypatch.setattr(LlmProviderExtensionUseCases, "refresh_models", refresh_models)

    response = _client().post(
        f"/api/llm-provider-extensions/install-1/{operation}",
        json={"unexpected": True},
    )

    assert response.status_code == 422
    assert calls == []


def test_credentials_route_passes_remote_runtime_url_separately(monkeypatch) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    calls: list[dict[str, object]] = []

    def save_credentials(
        self,
        install_id,
        account_id,
        *,
        secrets,
        connection_config,
        remote_base_url,
    ):
        calls.append(
            {
                "install_id": install_id,
                "secrets": secrets,
                "connection_config": connection_config,
                "remote_base_url": remote_base_url,
            }
        )
        return {"status": "saved"}

    monkeypatch.setattr(LlmProviderExtensionUseCases, "save_credentials", save_credentials)

    response = _client().post(
        "/api/llm-provider-extensions/install-1/credentials",
        json={
            "secrets": {},
            "connection_config": {"upstream_base_url": "https://upstream.example"},
            "remote_base_url": "https://runtime.example",
        },
    )

    assert response.status_code == 200
    assert calls == [
        {
            "install_id": "install-1",
            "secrets": {},
            "connection_config": {"upstream_base_url": "https://upstream.example"},
            "remote_base_url": "https://runtime.example",
        }
    ]


def test_all_provider_use_cases_run_through_threadpool(monkeypatch) -> None:
    from fastapi import concurrency

    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    threadpool_calls: list[str] = []
    use_case_calls: list[str] = []

    async def run_in_threadpool(func, *args, **kwargs):
        threadpool_calls.append(getattr(func, "__name__", type(func).__name__))
        return func(*args, **kwargs)

    monkeypatch.setattr(concurrency, "run_in_threadpool", run_in_threadpool)
    monkeypatch.setattr(
        LlmProviderExtensionUseCases,
        "list_sources",
        lambda *_args, **_kwargs: use_case_calls.append("list") or {"sources": []},
    )
    monkeypatch.setattr(
        LlmProviderExtensionUseCases,
        "save_credentials",
        lambda *_args, **_kwargs: use_case_calls.append("save") or {"status": "saved"},
    )
    monkeypatch.setattr(
        LlmProviderExtensionUseCases,
        "refresh_models",
        lambda *_args, **_kwargs: use_case_calls.append("refresh") or {"status": "ok"},
    )
    monkeypatch.setattr(
        LlmProviderExtensionUseCases,
        "set_source_state",
        lambda *_args, **_kwargs: use_case_calls.append("source") or {"status": "ok"},
    )
    monkeypatch.setattr(
        LlmProviderExtensionUseCases,
        "set_model_state",
        lambda *_args, **_kwargs: use_case_calls.append("model") or {"status": "ok"},
    )
    client = _client()

    responses = [
        client.get("/api/llm-provider-extensions"),
        client.post(
            "/api/llm-provider-extensions/install-1/credentials",
            json={"secrets": {"api_key": "redacted"}},
        ),
        client.post(
            "/api/llm-provider-extensions/install-1/refresh-models",
            json={"connection_config": {}},
        ),
        client.post("/api/llm-provider-extensions/sources/source-1/disable"),
        client.post("/api/llm-provider-extensions/models/model-1/disable"),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200]
    assert use_case_calls == ["list", "save", "refresh", "source", "model"]
    assert len(threadpool_calls) == 5


def test_credentials_reject_secret_or_unknown_connection_config(monkeypatch) -> None:
    """Secrets may only travel through encrypted ``secret_refs``."""
    from core.application.extensions import extension_descriptor

    monkeypatch.setattr(
        extension_descriptor,
        "extract_llm_provider_contract",
        lambda _record: SimpleNamespace(
            required_connection_config=["upstream_base_url"],
            required_provider_secrets=["api_key"],
            optional_provider_secrets=["bearer_token"],
        ),
    )

    response = _client().post(
        "/api/llm-provider-extensions/install-1/credentials",
        json={"connection_config": {"api_key": "must-not-be-plaintext"}},
    )

    assert response.status_code == 422
    assert "no such setting" in response.json()["detail"]


def test_credentials_reject_manifest_whitelisted_nested_plaintext_secrets(monkeypatch) -> None:
    from core.application.extensions import extension_descriptor

    monkeypatch.setattr(
        extension_descriptor,
        "extract_llm_provider_contract",
        lambda _record: SimpleNamespace(
            required_connection_config=["apiKey", "headers"],
            required_provider_secrets=["runtime_bearer_token"],
            optional_provider_secrets=[],
        ),
    )

    response = _client().post(
        "/api/llm-provider-extensions/install-1/credentials",
        json={
            "connection_config": {
                "apiKey": "plaintext-top",
                "headers": {"Authorization": "Bearer plaintext-nested"},
            }
        },
    )

    assert response.status_code == 422
    assert "no such setting" in response.json()["detail"]


def test_credentials_reject_host_owned_allowlist_bypasses(monkeypatch) -> None:
    from core.application.extensions import extension_descriptor

    monkeypatch.setattr(
        extension_descriptor,
        "extract_llm_provider_contract",
        lambda _record: SimpleNamespace(
            required_connection_config=[
                "authHeader",
                "clientCertificate",
                "signingKey",
                "headers",
            ],
            required_provider_secrets=[],
            optional_provider_secrets=[],
        ),
    )

    for connection_config in (
        {"authHeader": "plaintext"},
        {"clientCertificate": "plaintext"},
        {"signingKey": "plaintext"},
        {"headers": {"X-Custom-Auth": "plaintext"}},
    ):
        response = _client().post(
            "/api/llm-provider-extensions/install-1/credentials",
            json={"connection_config": connection_config},
        )
        assert response.status_code == 422
        assert "no such setting" in response.json()["detail"]


def test_credentials_reject_noncanonical_upstream_url_aliases(monkeypatch) -> None:
    from core.application.extensions import extension_descriptor

    monkeypatch.setattr(
        extension_descriptor,
        "extract_llm_provider_contract",
        lambda _record: SimpleNamespace(
            required_connection_config=["upstream_base_url"],
            required_provider_secrets=[],
            optional_provider_secrets=[],
        ),
    )

    for alias in ("upstreamBaseUrl", "UPSTREAM_BASE_URL", "upstream-base-url"):
        response = _client().post(
            "/api/llm-provider-extensions/install-1/credentials",
            json={"connection_config": {alias: "https://llm-endpoint.example"}},
        )
        assert response.status_code == 422
        assert "canonical" in response.json()["detail"]


def test_credentials_reject_unsafe_remote_runtime_url(monkeypatch) -> None:
    from core.application.extensions import extension_descriptor

    monkeypatch.setattr(
        extension_descriptor,
        "extract_llm_provider_contract",
        lambda _record: SimpleNamespace(
            required_connection_config=[],
            required_provider_secrets=[],
            optional_provider_secrets=[],
        ),
    )

    response = _client().post(
        "/api/llm-provider-extensions/install-1/credentials",
        json={"remote_base_url": "http://127.0.0.1:8090"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "remote_base_url is not allowed"


def test_concurrent_credential_rotations_keep_source_ref_resolvable(monkeypatch) -> None:
    from core.application.auth import secrets as secret_crypto
    from core.application.extensions import extension_descriptor
    from core.application.settings import llm_provider_extension_repository as store
    from core.infrastructure.db import db_pool, model_provider_store

    monkeypatch.setattr(
        extension_descriptor,
        "extract_llm_provider_contract",
        lambda _record: SimpleNamespace(
            required_connection_config=[],
            required_provider_secrets=["api_key"],
            optional_provider_secrets=["runtime_bearer_token"],
        ),
    )
    monkeypatch.setattr(secret_crypto, "encrypt_secret", lambda value: value)
    monkeypatch.setattr(secret_crypto, "decrypt_secret", lambda value: value)

    mutex = threading.Lock()
    state = {
        "active": 0,
        "max_active": 0,
        "source_ref": "llmext_cred_install-1",
        "refs": {
            "llmext_cred_install-1": json.dumps(
                {"api_key": "old", "runtime_bearer_token": "runtime"}
            )
        },
    }

    @contextmanager
    def serialized_transaction(_conn):
        with mutex:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                yield object()
            finally:
                state["active"] -= 1

    monkeypatch.setattr(db_pool, "transaction_connection", serialized_transaction)
    monkeypatch.setattr(store, "lock_install_for_credential_rotation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_args, **_kwargs: [
            {
                "id": "source-1",
                "extension_install_id": "install-1",
                "credential_secret_ref": state["source_ref"],
                "connection_config_jsonb": {},
            }
        ],
    )
    monkeypatch.setattr(
        store,
        "read_secret_ref_ciphertext",
        lambda *_args, ref_key, **_kwargs: state["refs"].get(ref_key),
    )
    monkeypatch.setattr(
        model_provider_store,
        "upsert_secret_ref",
        lambda *_args, ref_key, encrypted_value, **_kwargs: state["refs"].__setitem__(
            ref_key, encrypted_value
        ),
    )
    monkeypatch.setattr(
        store,
        "set_source_credential_ref",
        lambda *_args, credential_secret_ref, **_kwargs: state.__setitem__(
            "source_ref", credential_secret_ref
        ),
    )

    def cleanup(*_args, base_ref_key, keep_ref_keys, **_kwargs):
        for ref_key in list(state["refs"]):
            if ref_key.startswith(base_ref_key) and ref_key not in keep_ref_keys:
                del state["refs"][ref_key]

    monkeypatch.setattr(store, "delete_install_credential_refs_except", cleanup)
    for name in (
        "mark_install_models_stale",
        "mark_install_runtime_unknown",
        "set_install_connection_config",
        "set_install_credential_ref",
        "set_install_provider_state_revision",
        "set_source_connection_config",
    ):
        monkeypatch.setattr(store, name, lambda *_args, **_kwargs: True)
    from core.application.settings import llm_provider_connection_status as connection_status

    for name in ("clear_last_check", "record_saved_secrets"):
        monkeypatch.setattr(connection_status, name, lambda *_args, **_kwargs: True)

    client = _client()

    def rotate(value: str):
        return client.post(
            "/api/llm-provider-extensions/install-1/credentials",
            json={"secrets": {"api_key": value}},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(rotate, ["rotated-a", "rotated-b"]))

    assert [response.status_code for response in responses] == [200, 200]
    assert state["max_active"] == 1
    assert state["source_ref"] in state["refs"]
    final_credentials = json.loads(state["refs"][state["source_ref"]])
    assert final_credentials["api_key"] in {"rotated-a", "rotated-b"}
    assert final_credentials["runtime_bearer_token"] == "runtime"


def test_credential_rotation_rejects_unreadable_existing_secret_without_mutation(
    monkeypatch,
) -> None:
    from core.application.auth import secrets as secret_crypto
    from core.application.extensions import extension_descriptor
    from core.application.settings import llm_provider_extension_repository as store
    from core.infrastructure.db import db_pool, model_provider_store

    monkeypatch.setattr(
        extension_descriptor,
        "extract_llm_provider_contract",
        lambda _record: SimpleNamespace(
            required_connection_config=[],
            required_provider_secrets=["runtime_bearer_token"],
            optional_provider_secrets=["bearer_token"],
        ),
    )
    monkeypatch.setattr(
        secret_crypto,
        "decrypt_secret",
        lambda _value: (_ for _ in ()).throw(ValueError("invalid ciphertext")),
    )
    monkeypatch.setattr(secret_crypto, "encrypt_secret", lambda value: f"encrypted:{value}")

    state = {
        "install_ref": "endpoint-credentials",
        "refs": {"endpoint-credentials": "ciphertext-before"},
    }
    mutations: list[str] = []

    @contextmanager
    def transaction(_conn):
        yield object()

    monkeypatch.setattr(db_pool, "transaction_connection", transaction)
    monkeypatch.setattr(
        store,
        "lock_install_for_credential_rotation",
        lambda *_args, **_kwargs: {
            store.INSTALL_CREDENTIAL_REF_KEY: state["install_ref"],
            "llm_provider_connection_config": {},
            "remote_base_url": "https://runtime.example.com",
        },
    )
    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_args, **_kwargs: [
            {
                "id": "source-1",
                "extension_install_id": "install-1",
                "credential_secret_ref": state["install_ref"],
                "connection_config_jsonb": {},
            }
        ],
    )
    monkeypatch.setattr(
        store,
        "read_secret_ref_ciphertext",
        lambda *_args, ref_key, **_kwargs: state["refs"].get(ref_key),
    )

    def upsert_secret(*_args, ref_key, encrypted_value, **_kwargs):
        mutations.append("upsert_secret_ref")
        state["refs"][ref_key] = encrypted_value

    monkeypatch.setattr(model_provider_store, "upsert_secret_ref", upsert_secret)
    for name in (
        "mark_install_models_stale",
        "mark_install_runtime_unknown",
        "set_install_connection_config",
        "set_install_credential_ref",
        "set_install_provider_state_revision",
        "set_source_connection_config",
        "set_source_credential_ref",
        "delete_install_credential_refs_except",
    ):
        monkeypatch.setattr(
            store,
            name,
            lambda *_args, _name=name, **_kwargs: mutations.append(_name) or True,
        )

    response = _client().post(
        "/api/llm-provider-extensions/install-1/credentials",
        json={"secrets": {"runtime_bearer_token": "replacement-runtime-token"}},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Stored extension credentials are unavailable"
    assert state == {
        "install_ref": "endpoint-credentials",
        "refs": {"endpoint-credentials": "ciphertext-before"},
    }
    assert mutations == []


def test_refresh_rejects_snapshot_after_provider_state_rotation(monkeypatch) -> None:
    from contextlib import contextmanager

    from core.application.extensions import extension_descriptor
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings import llm_provider_extension_service as svc
    from core.infrastructure.db import db_pool

    contract = SimpleNamespace(
        required_connection_config=[],
        required_provider_secrets=[],
        optional_provider_secrets=[],
    )
    monkeypatch.setattr(
        extension_descriptor, "extract_llm_provider_contract", lambda _record: contract
    )

    @contextmanager
    def transaction(_conn):
        yield object()

    monkeypatch.setattr(db_pool, "transaction_connection", transaction)
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: {
            "release_id": "release-1",
            "descriptor_sha256": "a" * 64,
            "descriptor_jsonb": {"extension_id": "fixture.provider"},
            "extension_id": "fixture.provider",
            "llm_provider_state_revision": "revision-before",
            "remote_base_url": "https://runtime.example.com",
            "credential_secret_ref": "",
            "encrypted_value": "",
            "connection_config_jsonb": {},
        },
    )
    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_args, **_kwargs: [
            {
                "id": "source-1",
                "extension_install_id": "install-1",
                "credential_secret_ref": "secret-before",
                "connection_config_jsonb": {},
            }
        ],
    )
    monkeypatch.setattr(
        store,
        "get_install_current_credential_ref",
        lambda *_args, **_kwargs: "secret-before",
    )
    monkeypatch.setattr(store, "read_secret_ref_ciphertext", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(svc, "fetch_models_via_remote_runtime", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(svc, "group_discovered_models", lambda *_args, **_kwargs: [])
    seen_cas = []
    monkeypatch.setattr(
        store,
        "lock_install_discovery_generation_current",
        lambda *_args, **kwargs: seen_cas.append(kwargs) or False,
    )
    monkeypatch.setattr(
        svc,
        "persist_provider_groups",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale refresh must not persist")
        ),
    )

    response = _client().post(
        "/api/llm-provider-extensions/install-1/refresh-models",
        json={"connection_config": {}},
    )

    assert response.status_code == 409, response.text
    assert seen_cas[0]["state_revision"] == "revision-before"


def test_enable_rejects_unverified_extension_model(monkeypatch) -> None:
    from core.application.orchestration import extension_chat_model
    from core.application.settings import llm_capability_verifier
    from core.application.settings import llm_provider_extension_repository as store

    monkeypatch.setattr(
        store,
        "get_model",
        lambda *_args, **_kwargs: {
            "id": "m1",
            "provider_source_id": "s1",
            "model_id": "owner/model",
            "supports_structured_output": True,
            "verification_status": "stale",
            "lifecycle": "active",
        },
    )
    monkeypatch.setattr(
        store,
        "get_source",
        lambda *_args, **_kwargs: {"provider_key": "llmext_owner", "extension_install_id": "i1"},
    )
    monkeypatch.setattr(extension_chat_model, "ExtensionChatModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        store,
        "claim_model_verification",
        lambda *_args, generation, **_kwargs: {"verification_generation": generation},
    )
    monkeypatch.setattr(
        llm_capability_verifier,
        "run_capability_suite",
        lambda _model: (_ for _ in ()).throw(ValueError("bad JSON")),
    )
    monkeypatch.setattr(store, "set_model_verification_if_current", lambda *_args, **_kwargs: True)
    called = []
    monkeypatch.setattr(store, "set_model_enabled", lambda *_args, **_kwargs: called.append(1))

    response = _client().post("/api/llm-provider-extensions/models/m1/enable")

    assert response.status_code == 422
    assert "capability verification" in response.json()["detail"].lower()
    assert called == []


def test_enable_runs_live_suite_then_persists_verified_model(monkeypatch) -> None:
    from core.application.orchestration import extension_chat_model
    from core.application.settings import llm_capability_verifier
    from core.application.settings import llm_provider_extension_repository as store

    monkeypatch.setattr(
        store,
        "get_model",
        lambda *_args, **_kwargs: {
            "id": "m1",
            "provider_source_id": "s1",
            "model_id": "owner/model",
            "supports_structured_output": True,
            "verification_status": "unverified",
            "lifecycle": "active",
            "updated_at": "model-revision-1",
        },
    )
    monkeypatch.setattr(
        store,
        "get_source",
        lambda *_args, **_kwargs: {
            "provider_key": "llmext_owner_install",
            "chat_action_id": "llm.chat",
            "structured_chat_action_id": "llm.chat_structured",
            "extension_install_id": "install-1",
            "extension_id": "fixture.provider",
            "credential_secret_ref": "secret-ref",
        },
    )
    monkeypatch.setattr(extension_chat_model, "ExtensionChatModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        store,
        "claim_model_verification",
        lambda *_args, generation, **_kwargs: {"verification_generation": generation},
    )
    seen: list[object] = []
    monkeypatch.setattr(
        llm_capability_verifier,
        "run_capability_suite",
        lambda model: seen.append(model) or {"chat", "tools", "structured_output"},
    )
    saved: list[dict] = []
    monkeypatch.setattr(
        store,
        "set_model_verification_if_current",
        lambda *_args, **kwargs: saved.append(kwargs) or True,
    )
    enabled_writes: list[bool] = []
    monkeypatch.setattr(
        store,
        "set_model_enabled",
        lambda *_args, **kwargs: enabled_writes.append(bool(kwargs["enabled"])) or True,
    )

    response = _client().post("/api/llm-provider-extensions/models/m1/enable")

    assert response.status_code == 200
    assert seen
    assert seen[0]["model_id"] == "owner/model"
    assert len(saved) == 1
    assert saved[0] == {
        "account_id": "00000000-0000-0000-0000-000000000002",
        "model_row_id": "m1",
        "status": "passed",
        "verified_capabilities": ["chat", "structured_output", "tools"],
        "safe_error": None,
        "enabled": True,
        "expected_generation": saved[0]["expected_generation"],
        "expected_source_updated_at": None,
        "expected_upstream_revision": None,
        "expected_upstream_digest": None,
    }
    assert saved[0]["expected_generation"]
    assert enabled_writes == []


def test_lost_verification_cas_releases_owned_pending_generation(monkeypatch) -> None:
    from core.application.orchestration import extension_chat_model
    from core.application.settings import llm_capability_verifier
    from core.application.settings import llm_provider_extension_repository as store

    monkeypatch.setattr(
        store,
        "get_model",
        lambda *_args, **_kwargs: {
            "id": "m1",
            "provider_source_id": "s1",
            "model_id": "owner/model",
            "supports_structured_output": True,
            "verification_status": "stale",
            "lifecycle": "active",
            "updated_at": "model-revision-1",
        },
    )
    monkeypatch.setattr(
        store,
        "get_source",
        lambda *_args, **_kwargs: {
            "provider_key": "llmext_owner",
            "extension_install_id": "install-1",
            "updated_at": "source-revision-1",
        },
    )
    monkeypatch.setattr(extension_chat_model, "ExtensionChatModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        store,
        "claim_model_verification",
        lambda *_args, generation, **_kwargs: {"verification_generation": generation},
    )
    monkeypatch.setattr(
        llm_capability_verifier,
        "run_capability_suite",
        lambda _model: {"chat", "tools", "structured_output"},
    )
    monkeypatch.setattr(
        store,
        "set_model_verification_if_current",
        lambda *_args, **_kwargs: False,
    )
    released: list[dict] = []
    monkeypatch.setattr(
        store,
        "cancel_model_verification_generation",
        lambda *_args, **kwargs: released.append(kwargs) or True,
        raising=False,
    )

    response = _client().post("/api/llm-provider-extensions/models/m1/enable")

    assert response.status_code == 409
    assert len(released) == 1
    assert released[0]["account_id"] == "00000000-0000-0000-0000-000000000002"
    assert released[0]["model_row_id"] == "m1"
    assert released[0]["expected_generation"]


def test_parallel_enable_claims_once_before_paid_capability_suite(monkeypatch) -> None:
    from core.application.orchestration import extension_chat_model
    from core.application.settings import llm_capability_verifier
    from core.application.settings import llm_provider_extension_repository as store

    model = {
        "id": "m1",
        "provider_source_id": "s1",
        "model_id": "owner/model",
        "supports_structured_output": True,
        "verification_status": "unverified",
        "lifecycle": "active",
        "updated_at": "model-revision-1",
    }
    source = {
        "provider_key": "llmext_owner_install",
        "extension_install_id": "install-1",
        "extension_id": "fixture.provider",
        "updated_at": "source-revision-1",
    }
    monkeypatch.setattr(store, "get_model", lambda *_args, **_kwargs: dict(model))
    monkeypatch.setattr(store, "get_source", lambda *_args, **_kwargs: dict(source))
    monkeypatch.setattr(extension_chat_model, "ExtensionChatModel", lambda **kwargs: kwargs)
    claim_lock = threading.Lock()
    claimed = False

    def claim(*_args, generation, **_kwargs):
        nonlocal claimed
        with claim_lock:
            if claimed:
                return None
            claimed = True
            return {"verification_generation": generation}

    monkeypatch.setattr(store, "claim_model_verification", claim, raising=False)
    suite_started = threading.Event()
    suite_release = threading.Event()
    suite_calls: list[object] = []

    def run_suite(adapter):
        suite_calls.append(adapter)
        suite_started.set()
        assert suite_release.wait(timeout=5)
        return {"chat", "tools", "structured_output"}

    monkeypatch.setattr(llm_capability_verifier, "run_capability_suite", run_suite)
    monkeypatch.setattr(store, "set_model_verification_if_current", lambda *_a, **_k: True)

    client = _client()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/api/llm-provider-extensions/models/m1/enable")
        assert suite_started.wait(timeout=5)
        second = pool.submit(client.post, "/api/llm-provider-extensions/models/m1/enable")
        second_response = second.result(timeout=5)
        suite_release.set()
        first_response = first.result(timeout=5)

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert len(suite_calls) == 1


def test_enable_unchanged_passed_model_does_not_repeat_paid_suite(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store

    monkeypatch.setattr(
        store,
        "get_model",
        lambda *_args, **_kwargs: {
            "id": "m1",
            "model_id": "owner/model",
            "supports_structured_output": True,
            "verification_status": "passed",
            "verified_at": datetime.now(UTC),
            "lifecycle": "active",
            "enabled": True,
        },
    )
    monkeypatch.setattr(
        store,
        "get_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("passed model must not be probed again")
        ),
    )

    response = _client().post("/api/llm-provider-extensions/models/m1/enable")

    assert response.status_code == 200


def test_enable_expired_passed_model_repeats_capability_suite(monkeypatch) -> None:
    from core.application.orchestration import extension_chat_model
    from core.application.settings import llm_capability_verifier
    from core.application.settings import llm_provider_extension_repository as store

    monkeypatch.setattr(
        store,
        "get_model",
        lambda *_args, **_kwargs: {
            "id": "m1",
            "provider_source_id": "s1",
            "model_id": "owner/model",
            "supports_structured_output": True,
            "verification_status": "passed",
            "verified_at": datetime.now(UTC) - timedelta(days=31),
            "lifecycle": "active",
            "enabled": True,
        },
    )
    monkeypatch.setattr(
        store,
        "get_source",
        lambda *_args, **_kwargs: {
            "provider_key": "llmext_owner",
            "extension_install_id": "install-1",
        },
    )
    monkeypatch.setattr(extension_chat_model, "ExtensionChatModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        store,
        "claim_model_verification",
        lambda *_args, generation, **_kwargs: {"verification_generation": generation},
    )
    suite_calls: list[object] = []
    monkeypatch.setattr(
        llm_capability_verifier,
        "run_capability_suite",
        lambda model: suite_calls.append(model) or {"chat", "tools", "structured_output"},
    )
    monkeypatch.setattr(store, "set_model_verification_if_current", lambda *_a, **_k: True)

    response = _client().post("/api/llm-provider-extensions/models/m1/enable")

    assert response.status_code == 200
    assert len(suite_calls) == 1
