"""An llm_provider extension runs like any other extension: as a subprocess.

A remote HTTPS runtime stays supported where an install is configured for it,
but nothing about the provider contract requires one. An in-process provider
receives only its own declared secrets and connection config, never a runtime
bearer token or a runtime endpoint.
"""

from __future__ import annotations

import contextlib
import copy

import pytest

from core.application.extensions.extension_descriptor import (
    EXTENSION_DESCRIPTOR_SCHEMA_VERSION,
    extract_llm_provider_contract,
    validate_extension_descriptor,
)
from core.application.settings import llm_provider_extension_service as svc

_EXTENSION_ID = "acme.llm"
_INSTALL_ID = "pi_acme_llm"
_PROVIDER_KEY = "provider-key"  # pragma: allowlist secret - test sentinel, not a credential
_ENTRYPOINTS = "core.infrastructure.extension_runtime.extension_runtime_entrypoints"
_HELPERS = "core.infrastructure.extension_runtime.extension_runtime_helpers"


def _subprocess_descriptor(**contract_overrides) -> dict:
    contract = {
        "schema_version": "llm_provider_extension_v1",
        "supplier_key": "acme_llm",
        "supplier_label": "Acme LLM",
        "runtime_mode": "subprocess",
        "credential_provider_key": "acme_llm",
        "required_provider_secrets": [],
        "optional_provider_secrets": ["api_key"],
        "required_connection_config": [],
        "optional_connection_config": ["upstream_base_url"],
        "operations": {
            "list_models": {"query_id": "llm.list_models"},
            "chat": {"action_id": "llm.chat"},
            "structured_chat": {"action_id": "llm.chat_structured", "optional": True},
        },
        "model_grouping": {"strategy": "endpoint"},
        "required_capabilities_for_enablement": ["chat", "tools", "structured_output"],
        **contract_overrides,
    }
    return {
        "schema_version": EXTENSION_DESCRIPTOR_SCHEMA_VERSION,
        "extension_id": _EXTENSION_ID,
        "release_version": "1.1.0",
        "extension_type": "service_extension",
        "identity": {"display_name": "Acme LLM", "description": "Synthetic provider."},
        "vendor": {"vendor_name": "Acme Test", "support_url": ""},
        "distribution": {
            "channel": "private_byo",
            "execution_modes": ["local"],
            "allows_self_hosted": True,
            "requires_connected_control_plane": False,
            "import_sources": ["file"],
        },
        "runtime": {
            "contract_version": "extension_host_v2",
            "extension_contract_v2": {"llm_provider": contract},
        },
        "artifacts": {
            "tools": [{"tool_id": "acme_llm.llm_provider", "snapshot": {"kind": "llm_provider"}}]
        },
    }


class TestSubprocessContract:
    def test_subprocess_provider_with_local_execution_is_valid(self):
        descriptor = _subprocess_descriptor()

        result = validate_extension_descriptor(descriptor)
        contract = extract_llm_provider_contract(descriptor)

        assert result["valid"], result["errors"]
        assert contract is not None
        assert contract.runtime_mode == "subprocess"
        assert contract.optional_connection_config == ["upstream_base_url"]

    def test_subprocess_provider_needs_the_local_execution_mode(self):
        descriptor = _subprocess_descriptor()
        descriptor["distribution"]["execution_modes"] = ["remote_https"]

        result = validate_extension_descriptor(descriptor)

        assert not result["valid"]
        assert any("requires the local execution mode" in e["message"] for e in result["errors"])

    def test_remote_provider_still_needs_the_remote_execution_mode(self):
        descriptor = _subprocess_descriptor(runtime_mode="remote_https")

        result = validate_extension_descriptor(descriptor)

        assert not result["valid"]
        assert any(
            "requires the remote_https execution mode" in e["message"] for e in result["errors"]
        )

    @pytest.mark.parametrize("field", ["api_key", "access_token"])
    def test_optional_connection_config_cannot_carry_a_secret(self, field):
        descriptor = _subprocess_descriptor(optional_connection_config=[field])

        with pytest.raises(ValueError, match="must not declare provider secret fields"):
            extract_llm_provider_contract(descriptor)

    def test_generic_optional_connection_config_requires_compatible_host(self):
        descriptor = _subprocess_descriptor(optional_connection_config=["region"])
        descriptor["runtime"]["compatibility"] = {"platform_min": "1.3.0"}
        assert not validate_extension_descriptor(descriptor)["valid"]
        descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
        assert validate_extension_descriptor(descriptor)["valid"]

    def test_optional_fields_count_as_declared_connection_config(self):
        contract = extract_llm_provider_contract(_subprocess_descriptor())

        assert svc.declared_connection_config_fields(contract) == {"upstream_base_url"}
        # Optional means optional: nothing is required before discovery.
        assert svc.require_llm_provider_connection_config(contract, {}) == {}


def _stub_in_process(monkeypatch, *, invocation, record=None, allow_private=False):
    captured: dict = {}

    def resolve(install_id, **kwargs):
        captured["resolved"] = (install_id, kwargs.get("account_id"))
        resolved = {"install_id": install_id, "extension_id": _EXTENSION_ID}
        return (resolved if record is None else record), None

    def run_query(extension, **kwargs):
        captured.update(kind="query", extension=extension, **kwargs)
        return copy.deepcopy(invocation)

    def run_action(extension, **kwargs):
        captured.update(kind="action", extension=extension, **kwargs)
        return copy.deepcopy(invocation)

    monkeypatch.setattr(f"{_HELPERS}.resolve_extension_runtime_record_by_install_id", resolve)
    monkeypatch.setattr(
        "core.application.extensions.url_safety.allow_private_provider_addresses",
        lambda: allow_private,
    )
    monkeypatch.setattr(f"{_ENTRYPOINTS}.run_extension_query", run_query)
    monkeypatch.setattr(f"{_ENTRYPOINTS}.run_extension_action", run_action)
    monkeypatch.setattr(
        "core.infrastructure.db.remote_extension_runtime.remote_query",
        lambda *_a, **_k: pytest.fail("an in-process provider must not use the remote runtime"),
    )
    return captured


class TestInProcessInvocation:
    def test_query_reaches_the_subprocess_with_only_provider_settings(self, monkeypatch):
        captured = _stub_in_process(
            monkeypatch,
            invocation={"status": "succeeded", "response": {"ok": True, "result": {"x": 1}}},
        )

        result = svc.invoke_llm_provider_operation(
            {"install_id": _INSTALL_ID, "runtime_mode": "subprocess"},
            op_kind="query",
            op_id="llm.list_models",
            payload={"cursor": "", "limit": 200},
            account_id="acc-1",
            credentials={"api_key": _PROVIDER_KEY, "runtime_bearer_token": "stale-bearer"},
            connection_config={"upstream_base_url": "https://llm.example.com"},
            timeout_seconds=12.2,
        )

        assert result == {"status": "succeeded", "detail": "", "response": captured_response()}
        assert captured["kind"] == "query"
        assert captured["resolved"] == (_INSTALL_ID, "acc-1")
        assert captured["query_id"] == "llm.list_models"
        assert captured["params"] == {"cursor": "", "limit": 200}
        assert captured["timeout_seconds"] == 13
        assert captured["runtime_context"] == {
            "account_id": "acc-1",
            "extension_install_id": _INSTALL_ID,
            "resources": {
                "provider_secrets": {"api_key": _PROVIDER_KEY},
                "connection_config": {"upstream_base_url": "https://llm.example.com"},
            },
            "network_policy": {"allow_private_addresses": False},
        }

    def test_action_carries_the_operation_input_without_secrets(self, monkeypatch):
        captured = _stub_in_process(
            monkeypatch,
            invocation={"status": "succeeded", "response": {"ok": True, "result": {}}},
        )

        svc.invoke_llm_provider_operation(
            {"extension_install_id": _INSTALL_ID, "runtime_mode": "subprocess"},
            op_kind="action",
            op_id="llm.chat",
            payload={"model_id": "m", "messages": []},
            account_id="acc-1",
            credentials={"api_key": _PROVIDER_KEY},
        )

        assert captured["kind"] == "action"
        assert captured["action_id"] == "llm.chat"
        assert captured["input_payload"] == {"model_id": "m", "messages": []}
        assert _PROVIDER_KEY not in repr(captured["input_payload"])

    @pytest.mark.parametrize(
        ("invocation", "category"),
        [
            (
                {
                    "status": "failed",
                    "detail": "Ollama request failed: rate_limited",
                    "response": {
                        "ok": False,
                        "errors": [{"code": "rate_limited", "message": "upstream said no"}],
                    },
                },
                "rate_limited",
            ),
            (
                {"status": "failed", "detail": "exit_code=1", "response": {"raw_stdout": "x"}},
                "upstream_error",
            ),
            (
                {"status": "failed", "detail": "timed out after 30s", "response": {}},
                "timeout",
            ),
        ],
    )
    def test_failures_leave_as_one_safe_category(self, monkeypatch, invocation, category):
        _stub_in_process(monkeypatch, invocation=invocation)

        result = svc.invoke_llm_provider_operation(
            {"install_id": _INSTALL_ID, "runtime_mode": "subprocess"},
            op_kind="action",
            op_id="llm.chat",
            payload={},
            account_id="acc-1",
        )

        assert result == {"status": "failed", "detail": category, "response": {}}

    def test_unresolvable_install_is_model_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            f"{_HELPERS}.resolve_extension_runtime_record_by_install_id",
            lambda *_a, **_k: (None, None),
        )
        monkeypatch.setattr(
            f"{_ENTRYPOINTS}.run_extension_query",
            lambda *_a, **_k: pytest.fail("nothing runs without a resolved install"),
        )

        result = svc.invoke_llm_provider_operation(
            {"install_id": _INSTALL_ID, "runtime_mode": "subprocess"},
            op_kind="query",
            op_id="llm.list_models",
            payload={},
            account_id="acc-1",
        )

        assert result == {"status": "failed", "detail": "model_unavailable", "response": {}}

    def test_remote_install_keeps_its_runtime_bearer_apart_from_provider_secrets(self, monkeypatch):
        captured: dict = {}

        def remote_action(extension, **kwargs):
            captured.update(extension=extension, **kwargs)
            return {"status": "succeeded", "response": {"result": {}}}

        monkeypatch.setattr(
            "core.infrastructure.db.remote_extension_runtime.remote_action", remote_action
        )
        monkeypatch.setattr(
            f"{_ENTRYPOINTS}.run_extension_action",
            lambda *_a, **_k: pytest.fail("a remote install must not run a subprocess"),
        )

        svc.invoke_llm_provider_operation(
            {"install_id": _INSTALL_ID, "runtime_mode": "remote_https"},
            op_kind="action",
            op_id="llm.chat",
            payload={"model_id": "m"},
            account_id="acc-1",
            credentials={"api_key": _PROVIDER_KEY, "runtime_bearer_token": "runtime-bearer"},
            connection_config={"upstream_base_url": "https://llm.example.com"},
            idempotency_key="llm-1",
        )

        extension = captured["extension"]
        assert extension["remote_bearer_token"] == "runtime-bearer"
        assert extension["connection_credentials"] == {"api_key": _PROVIDER_KEY}
        assert extension["connection_config"] == {"upstream_base_url": "https://llm.example.com"}
        assert captured["idempotency_key"] == "llm-1"


def captured_response() -> dict:
    return {"ok": True, "result": {"x": 1}}


def _use_cases(monkeypatch, *, runtime_mode: str):
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    descriptor = _subprocess_descriptor()
    contract = extract_llm_provider_contract(descriptor)
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    record = {
        "install_id": _INSTALL_ID,
        "release_id": "release-1",
        "descriptor_sha256": "a" * 64,
        "runtime_mode": runtime_mode,
    }
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (record, contract, _INSTALL_ID, _EXTENSION_ID, "Acme LLM"),
    )
    from core.application.settings import llm_provider_connection_status as connection_status

    # The connection-status writes have their own tests.
    for name in ("clear_last_check", "record_saved_secrets", "record_last_check"):
        monkeypatch.setattr(connection_status, name, lambda *_a, **_k: True)
    return use_cases, descriptor


class TestInProcessUseCases:
    def test_discovery_of_a_subprocess_install_needs_no_runtime_endpoint(self, monkeypatch):
        from core.application.settings import llm_provider_extension_repository as store

        use_cases, descriptor = _use_cases(monkeypatch, runtime_mode="subprocess")
        monkeypatch.setattr(
            "core.infrastructure.db.db_pool.transaction_connection",
            lambda conn: contextlib.nullcontext(conn),
        )
        monkeypatch.setattr(
            store,
            "begin_install_discovery",
            lambda *_args, **_kwargs: {
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
                "descriptor_jsonb": descriptor,
                "extension_id": _EXTENSION_ID,
                "llm_provider_state_revision": "revision-1",
                "remote_base_url": "",
                "credential_secret_ref": "",
                "encrypted_value": "",
                "connection_config_jsonb": {},
            },
        )
        captured: dict = {}

        def fetch(record, _contract, **kwargs):
            captured.update(
                install_id=record["install_id"],
                runtime_mode=record["runtime_mode"],
                connection_config=kwargs["connection_config"],
                has_logger=callable(kwargs.get("get_logger_only")),
            )
            return []

        monkeypatch.setattr(svc, "fetch_models_via_remote_runtime", fetch)
        monkeypatch.setattr(
            store, "lock_install_discovery_generation_current", lambda *_a, **_k: True
        )
        monkeypatch.setattr(svc, "persist_provider_groups", lambda *_a, **_k: {"sources": []})
        from core.application.settings import llm_provider_connection_status as connection_status

        checks: list[str] = []
        monkeypatch.setattr(
            connection_status,
            "record_last_check",
            lambda *_a, status, **_k: checks.append(status),
        )

        result = use_cases.refresh_models(_INSTALL_ID, "acc-1", connection_config={})

        assert result["status"] == "ok"
        # A successful discovery is the panel's "Connected".
        assert checks == ["ok"]
        assert captured == {
            "install_id": _INSTALL_ID,
            "runtime_mode": "subprocess",
            "connection_config": {},
            "has_logger": True,
        }

    def test_a_subprocess_install_accepts_an_optional_connection_field(self):
        from core.application.settings.llm_provider_extension_use_cases import (
            LlmProviderExtensionUseCases,
        )

        contract = extract_llm_provider_contract(_subprocess_descriptor())

        assert LlmProviderExtensionUseCases._connection_config(
            contract, {"upstream_base_url": "https://llm.example.com"}
        ) == {"upstream_base_url": "https://llm.example.com"}

    def test_a_subprocess_install_refuses_a_runtime_endpoint(self, monkeypatch):
        from core.application.settings.llm_provider_extension_use_cases import (
            LlmProviderExtensionError,
        )

        use_cases, _ = _use_cases(monkeypatch, runtime_mode="subprocess")
        monkeypatch.setattr(use_cases, "_safe_runtime_url", lambda value: value)

        with pytest.raises(LlmProviderExtensionError) as caught:
            use_cases.save_credentials(
                _INSTALL_ID,
                "acc-1",
                secrets={"api_key": _PROVIDER_KEY},
                connection_config={},
                remote_base_url="https://runtime.example.com",
            )

        assert caught.value.status_code == 422
        assert caught.value.detail == "remote_base_url applies only to a remote_https install"


@pytest.mark.parametrize(
    ("secrets", "connection_config", "reason"),
    [
        ({"api_key": _PROVIDER_KEY}, {}, "credentials_changed"),
        ({}, {"upstream_base_url": "https://llm.example.com"}, "configuration_changed"),
        (
            {"api_key": _PROVIDER_KEY},
            {"upstream_base_url": "https://llm.example.com"},
            "configuration_changed",
        ),
    ],
)
def test_saving_settings_names_why_verified_models_went_stale(
    monkeypatch, secrets, connection_config, reason
):
    from core.application.auth import secrets as secret_crypto
    from core.application.settings import llm_provider_extension_repository as store
    from core.infrastructure.db import model_provider_store

    use_cases, _ = _use_cases(monkeypatch, runtime_mode="subprocess")
    monkeypatch.setattr(use_cases, "_safe_upstream_config", lambda value: value)
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(secret_crypto, "encrypt_secret", lambda value: value)
    monkeypatch.setattr(store, "lock_install_for_credential_rotation", lambda *_a, **_k: {})
    monkeypatch.setattr(store, "list_sources", lambda *_a, **_k: [])
    monkeypatch.setattr(store, "read_secret_ref_ciphertext", lambda *_a, **_k: "")
    monkeypatch.setattr(model_provider_store, "upsert_secret_ref", lambda *_a, **_k: None)
    for name in (
        "mark_install_runtime_unknown",
        "set_install_connection_config",
        "set_install_credential_ref",
        "set_install_provider_state_revision",
        "delete_install_credential_refs_except",
    ):
        monkeypatch.setattr(store, name, lambda *_a, **_k: True)
    reasons: list[str] = []
    monkeypatch.setattr(
        store,
        "mark_install_models_stale",
        lambda *_a, reason, **_k: reasons.append(reason) or 1,
    )

    use_cases.save_credentials(
        _INSTALL_ID, "acc-1", secrets=secrets, connection_config=connection_config
    )

    assert reasons == [reason]


class TestChatModelRouting:
    def test_chat_model_calls_the_install_runtime_with_its_decrypted_settings(self, monkeypatch):
        from core.application.orchestration import extension_chat_model as mod

        monkeypatch.setattr(
            mod.ExtensionChatModel,
            "_runtime_extension",
            lambda self: {
                "install_id": _INSTALL_ID,
                "runtime_mode": "subprocess",
                "llm_credentials": {"api_key": _PROVIDER_KEY},
                "llm_connection_config": {"upstream_base_url": "https://llm.example.com"},
            },
        )
        captured: dict = {}

        def invoke(extension, **kwargs):
            captured.update(extension=extension, **kwargs)
            return {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "result": {
                        "message": {"role": "assistant", "content": "basic-ok"},
                        "finish_reason": "stop",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
            }

        monkeypatch.setattr(svc, "invoke_llm_provider_operation", invoke)
        model = mod.ExtensionChatModel(
            account_id="acc-1",
            provider_key="llmext_acme",
            model_id="acme-1",
            extension_install_id=_INSTALL_ID,
            extension_id=_EXTENSION_ID,
        )

        reply = model.invoke("Return exactly basic-ok.")

        assert reply.content == "basic-ok"
        assert captured["op_kind"] == "action"
        assert captured["op_id"] == "llm.chat"
        assert captured["credentials"] == {"api_key": _PROVIDER_KEY}
        assert captured["connection_config"] == {"upstream_base_url": "https://llm.example.com"}
        assert "llm_credentials" not in captured["extension"]
        assert captured["idempotency_key"].startswith("llm-")


def test_vertex_project_and_location_are_declared_nonsecret_settings():
    descriptor = _subprocess_descriptor(required_connection_config=["project_id", "location"])
    contract = extract_llm_provider_contract(descriptor)
    assert svc.declared_connection_config_fields(contract) == {
        "project_id",
        "location",
        "upstream_base_url",
    }


def test_cloud_settings_must_be_strings():
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    contract = extract_llm_provider_contract(
        _subprocess_descriptor(required_connection_config=["project_id", "location"])
    )
    with pytest.raises(LlmProviderExtensionError, match="Project id"):
        LlmProviderExtensionUseCases._connection_config(contract, {"project_id": {"unsafe": 1}})


@pytest.mark.parametrize(
    "declaration", ["required_connection_config", "optional_connection_config"]
)
def test_cloud_scope_requires_contract_version_1_4(declaration):
    descriptor = _subprocess_descriptor(**{declaration: ["project_id", "location"]})
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.3.0"}
    result = validate_extension_descriptor(descriptor)
    assert not result["valid"]
    assert any("1.4.0" in error["message"] for error in result["errors"])


def test_cloud_scope_accepts_compatible_version_and_optional_fields():
    descriptor = _subprocess_descriptor(optional_connection_config=["project_id", "location"])
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
    assert validate_extension_descriptor(descriptor)["valid"]


def test_generic_provider_schema_accepts_provider_owned_values_and_preserves_changes():
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
        effective_connection_config,
    )

    for permitted in (["UPPER/project", "other"], ["short", "third"]):
        descriptor = _subprocess_descriptor(
            optional_connection_config=["project_id", "tenant_path"],
            connection_config_schema={
                "project_id": {"type": "string", "enum": permitted},
                "tenant_path": {"type": "string", "minLength": 1, "maxLength": 12},
            },
        )
        descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
        assert validate_extension_descriptor(descriptor)["valid"]
        contract = extract_llm_provider_contract(descriptor)
        values = {"project_id": permitted[0], "tenant_path": "tenant/"}
        assert LlmProviderExtensionUseCases._connection_config(contract, values) == values
        assert effective_connection_config(contract, values) != effective_connection_config(
            contract, {**values, "tenant_path": "tenant"}
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "pattern": ".*"},
        {"type": "string", "minLength": 3, "maxLength": 2},
        {"type": "string", "enum": ["same", "same"]},
        {"type": "string", "enum": ["allowed"], "maxLength": 3},
        {"type": "number"},
    ],
)
def test_generic_provider_schema_rejects_invalid_metadata(schema):
    descriptor = _subprocess_descriptor(
        optional_connection_config=["tenant_path"], connection_config_schema={"tenant_path": schema}
    )
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
    assert not validate_extension_descriptor(descriptor)["valid"]


@pytest.mark.parametrize(
    "name", ["constructor", "prototype", "__proto__", "api_key", "bad-name", "x" * 65]
)
def test_generic_provider_rejects_unsafe_field_names(name):
    descriptor = _subprocess_descriptor(optional_connection_config=[name])
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
    assert not validate_extension_descriptor(descriptor)["valid"]


def test_generic_provider_rejects_unknown_schema_and_invalid_default():
    for overrides in (
        {"connection_config_schema": {"other": {"type": "string"}}},
        {
            "connection_config_schema": {"tenant": {"type": "string", "enum": ["one"]}},
            "connection_config_defaults": {"tenant": "two"},
        },
    ):
        descriptor = _subprocess_descriptor(optional_connection_config=["tenant"], **overrides)
        descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
        assert not validate_extension_descriptor(descriptor)["valid"]


def test_generic_provider_validates_unicode_lengths_and_literal_values():
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    descriptor = _subprocess_descriptor(
        optional_connection_config=["tenant"],
        connection_config_schema={"tenant": {"type": "string", "minLength": 2, "maxLength": 2}},
    )
    contract = extract_llm_provider_contract(descriptor)
    assert LlmProviderExtensionUseCases._connection_config(contract, {"tenant": "😀x"}) == {
        "tenant": "😀x"
    }
    for value in ("😀", "😀xy", 1):
        with pytest.raises(LlmProviderExtensionError):
            LlmProviderExtensionUseCases._connection_config(contract, {"tenant": value})
    with pytest.raises(LlmProviderExtensionError):
        LlmProviderExtensionUseCases._connection_config(contract, {"unknown": "x"})


def test_schema_round_trip_and_whitespace_enum_rejection():
    descriptor = _subprocess_descriptor(
        optional_connection_config=["tenant"],
        connection_config_schema={"tenant": {"type": "string", "title": "Tenant"}},
    )
    contract = extract_llm_provider_contract(descriptor)
    from core.application.extensions.extension_descriptor_llm_provider import LlmProviderContractV1

    assert LlmProviderContractV1.model_validate(contract.model_dump()) == contract
    descriptor["runtime"]["extension_contract_v2"]["llm_provider"]["connection_config_schema"][
        "tenant"
    ]["enum"] = [" "]
    with pytest.raises(ValueError):
        extract_llm_provider_contract(descriptor)


def test_required_setting_error_uses_declared_title():
    contract = extract_llm_provider_contract(
        _subprocess_descriptor(
            required_connection_config=["tenant"],
            connection_config_schema={"tenant": {"type": "string", "title": "Workspace"}},
        )
    )
    with pytest.raises(ValueError, match="Workspace"):
        svc.require_llm_provider_connection_config(contract, {})


@pytest.mark.parametrize(
    "name",
    [
        "signing_key",
        "auth_header",
        "client_certificate",
        "provider_signing_key",
        "provider_auth_header",
        "provider_client_certificate",
    ],
)
def test_generic_provider_rejects_sensitive_canonical_names(name):
    descriptor = _subprocess_descriptor(optional_connection_config=[name])
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
    assert not validate_extension_descriptor(descriptor)["valid"]
