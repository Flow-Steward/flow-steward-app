"""Generic JSON-object provider secret contract and validation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.application.extensions.extension_descriptor import (
    extract_llm_provider_contract,
    validate_extension_descriptor,
)
from core.application.settings.llm_provider_extension_use_cases import (
    LlmProviderExtensionError,
    LlmProviderExtensionUseCases,
)
from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

_FIELD_NAME = "credential_json"
_OBJECT_FORMAT = "json_object"
_UNSUPPORTED_FORMAT = "yaml"


def _descriptor(**overrides):
    if "provider_secret_formats" in overrides:
        overrides.setdefault("required_provider_secrets", [_FIELD_NAME])
        overrides.setdefault("optional_provider_secrets", [])
    return _subprocess_descriptor(**overrides)


def test_declared_json_secret_format_is_projected_and_requires_host_14():
    from core.application.extensions.browser_manifest_projection import project_browser_manifest

    descriptor = _descriptor(provider_secret_formats={_FIELD_NAME: _OBJECT_FORMAT})
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
    contract = extract_llm_provider_contract(descriptor)
    assert contract.provider_secret_formats == {_FIELD_NAME: _OBJECT_FORMAT}
    assert contract.model_dump()["provider_secret_formats"] == {_FIELD_NAME: _OBJECT_FORMAT}
    browser = project_browser_manifest(descriptor)
    assert browser["runtime"]["extension_contract_v2"]["llm_provider"][
        "provider_secret_formats"
    ] == {_FIELD_NAME: _OBJECT_FORMAT}
    assert validate_extension_descriptor(descriptor, platform_version="1.4.0")["valid"] is True
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.3.0"}
    result = validate_extension_descriptor(descriptor, platform_version="1.4.0")
    assert result["valid"] is False
    assert any("1.4.0" in item["message"] for item in result["errors"])


@pytest.mark.parametrize("formats", [{"other": _OBJECT_FORMAT}, {_FIELD_NAME: _UNSUPPORTED_FORMAT}])
def test_format_requires_declared_secret_and_supported_value(formats):
    with pytest.raises(ValueError):
        extract_llm_provider_contract(_descriptor(provider_secret_formats=formats))


@pytest.mark.parametrize(
    "raw", ['{"secret":', "null", "[]", "1", '{"x": NaN}', '{"x": Infinity}', b"{}"]
)
def test_invalid_new_json_secret_is_rejected_before_transaction_without_echo(monkeypatch, raw):
    contract = SimpleNamespace(provider_secret_formats={_FIELD_NAME: _OBJECT_FORMAT})
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(use_cases, "_resolve_install", lambda *_: ({}, contract, "i", "e", "E"))
    monkeypatch.setattr(use_cases, "_connection_config", lambda _contract, config: config)
    monkeypatch.setattr(use_cases, "_safe_upstream_config", lambda config: config)
    monkeypatch.setattr(use_cases, "_safe_runtime_url", lambda url: url)
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda *_: pytest.fail("invalid secret reached persistence"),
    )
    with pytest.raises(LlmProviderExtensionError) as exc:
        use_cases.save_credentials("i", "a", secrets={"credential_json": raw}, connection_config={})
    assert exc.value.status_code == 422
    assert str(raw) not in str(exc.value)
    assert "credential_json" in str(exc.value)


def test_json_secret_saves_exact_raw_text_and_blank_replacement_preserves_saved(monkeypatch):
    from core.application.settings import llm_provider_extension_repository as store
    from tests.unit.test_llm_provider_catalog_ux import _saving, _with_contract

    use_cases, _stored = _saving(monkeypatch, existing_config={})
    _with_contract(
        monkeypatch,
        use_cases,
        required_provider_secrets=["credential_json"],
        optional_provider_secrets=[],
        provider_secret_formats={_FIELD_NAME: _OBJECT_FORMAT},
    )
    ciphertexts = []
    monkeypatch.setattr("core.application.auth.secrets.encrypt_secret", lambda value: value)
    monkeypatch.setattr("core.application.auth.secrets.decrypt_secret", lambda value: value)
    monkeypatch.setattr(
        "core.infrastructure.db.model_provider_store.upsert_secret_ref",
        lambda *_a, encrypted_value, **_k: ciphertexts.append(encrypted_value),
    )
    raw = '{\n  "type": "service_account"\n}'
    use_cases.save_credentials(
        "pi_acme", "acc-1", secrets={"credential_json": raw}, connection_config={}
    )
    assert json.loads(ciphertexts[-1])["credential_json"] == raw
    saved = ciphertexts[-1]
    monkeypatch.setattr(store, "read_secret_ref_ciphertext", lambda *_a, **_k: saved)
    use_cases.save_credentials(
        "pi_acme",
        "acc-1",
        secrets={"credential_json": "  "},
        connection_config={"upstream_base_url": ""},
    )
    assert ciphertexts == [saved]


def test_discovery_rejects_invalid_stored_json_before_provider(monkeypatch):
    import contextlib

    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings import llm_provider_extension_service as service

    descriptor = _descriptor(provider_secret_formats={_FIELD_NAME: _OBJECT_FORMAT})
    record = {
        "runtime_mode": "subprocess",
        "release_id": "r1",
        "descriptor_sha256": "a" * 64,
    }
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(use_cases, "_resolve_install", lambda *_: (record, None, "i", "e", "E"))
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_a, **_k: {
            "release_id": "r1",
            "descriptor_sha256": "a" * 64,
            "descriptor_jsonb": descriptor,
            "extension_id": descriptor["extension_id"],
            "llm_provider_state_revision": "s1",
            "encrypted_value": "encrypted",
        },
    )
    monkeypatch.setattr(
        "core.application.auth.secrets.decrypt_secret",
        lambda _cipher: json.dumps({"credential_json": "not json private secret"}),
    )
    monkeypatch.setattr(use_cases, "_record_failed_check", lambda *_: None)
    monkeypatch.setattr(
        service,
        "fetch_models_via_remote_runtime",
        lambda *_a, **_k: pytest.fail("invalid credential reached provider"),
    )
    with pytest.raises(LlmProviderExtensionError) as exc:
        use_cases.refresh_models("i", "a", connection_config={})
    assert exc.value.status_code == 422
    assert "not json private secret" not in str(exc.value)


def test_legacy_descriptor_round_trip_does_not_emit_new_secret_format():
    from core.application.extensions.extension_descriptor import ExtensionDescriptor

    descriptor = _descriptor()
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.3.0"}
    serialized = ExtensionDescriptor.model_validate(descriptor).model_dump(mode="json")
    contract = serialized["runtime"]["extension_contract_v2"]["llm_provider"]
    assert "provider_secret_formats" not in contract


@pytest.mark.parametrize("required", [False, True])
def test_discovery_omits_blank_json_and_blocks_missing_required_before_provider(
    monkeypatch, required
):
    import contextlib

    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings import llm_provider_extension_service as service

    descriptor = _descriptor(
        required_provider_secrets=["credential_json"] if required else [],
        optional_provider_secrets=[] if required else ["credential_json"],
        provider_secret_formats={_FIELD_NAME: _OBJECT_FORMAT},
    )
    record = {"runtime_mode": "subprocess", "release_id": "r1", "descriptor_sha256": "a" * 64}
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(use_cases, "_resolve_install", lambda *_: (record, None, "i", "e", "E"))
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_a, **_k: {
            "release_id": "r1",
            "descriptor_sha256": "a" * 64,
            "descriptor_jsonb": descriptor,
            "extension_id": descriptor["extension_id"],
            "llm_provider_state_revision": "s1",
            "encrypted_value": "encrypted",
        },
    )
    monkeypatch.setattr(
        "core.application.auth.secrets.decrypt_secret",
        lambda _cipher: json.dumps({"credential_json": "  "}),
    )
    monkeypatch.setattr(use_cases, "_record_failed_check", lambda *_: None)
    captured = []

    def stop_before_network(*_args, credentials, **_kwargs):
        captured.append(credentials)
        raise RuntimeError("synthetic stop")

    monkeypatch.setattr(service, "fetch_models_via_remote_runtime", stop_before_network)
    with pytest.raises(LlmProviderExtensionError) as exc:
        use_cases.refresh_models("i", "a", connection_config={})
    if required:
        assert exc.value.status_code == 422
        assert captured == []
    else:
        assert exc.value.status_code == 502
        assert captured == [{}]


def test_empty_format_map_also_requires_host_14_when_declared():
    descriptor = _descriptor(provider_secret_formats={})
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.3.0"}
    result = validate_extension_descriptor(descriptor, platform_version="1.4.0")
    assert result["valid"] is False
    assert any(
        "secret formats require host contract 1.4.0" in item["message"] for item in result["errors"]
    )
