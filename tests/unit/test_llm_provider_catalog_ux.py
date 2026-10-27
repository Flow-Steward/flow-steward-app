"""What the user sees about LLM provider models and settings.

- One list of required routing capabilities, and models the provider says
  lack one are flagged before verification.
- Every provider a saved route may name has a display label, even when none of
  its models is selectable right now.
- An emptied optional setting clears back to the provider's default.
- Messages use the field labels the user sees, never internal names.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest import mock

import pytest

from core.application.settings import llm_provider_extension_repository as store
from core.application.settings import llm_verification_policy as policy


def test_the_required_capabilities_are_one_list_read_everywhere(monkeypatch) -> None:
    from core.application.extensions import extension_descriptor
    from core.application.settings import llm_capability_verifier

    # Actual first, the order the rest of this file uses; a lowercase name keeps
    # ruff from reading the module constant as the expected side.
    required = policy.REQUIRED_ROUTING_CAPABILITIES
    assert required == {"chat", "tools", "structured_output"}
    # The contract check and the suite's result come from the same constant.
    assert "REQUIRED_ROUTING_CAPABILITIES" in Path(extension_descriptor.__file__).read_text()
    assert "REQUIRED_ROUTING_CAPABILITIES" in Path(llm_capability_verifier.__file__).read_text()


@pytest.mark.parametrize(
    ("declared", "missing"),
    [
        (["chat", "tools", "structured_output"], []),
        (["chat"], ["structured_output", "tools"]),
        (["chat", "tools", "prompt_caching"], ["structured_output"]),
        ({"chat": True, "tools": True}, ["structured_output"]),
        # Nothing reported is not evidence: the model stays a candidate.
        ([], []),
        (None, []),
    ],
)
def test_missing_capabilities_come_from_what_the_provider_reports(declared, missing) -> None:
    assert policy.missing_routing_capabilities(declared) == missing


def test_listing_marks_models_the_provider_says_cannot_qualify(monkeypatch) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_a, **_k: [{"id": "s1", "provider_key": "llmext_acme", "display_label": "Acme"}],
    )
    monkeypatch.setattr(
        store,
        "list_models",
        lambda *_a, **_k: [
            {
                "provider_source_id": "s1",
                "model_id": "full",
                "declared_capabilities_jsonb": ["chat", "tools", "structured_output"],
            },
            {
                "provider_source_id": "s1",
                "model_id": "text",
                "declared_capabilities_jsonb": ["chat"],
            },
        ],
    )

    listing = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None).list_sources(
        "acc-1"
    )

    models = {m["model_id"]: m for m in listing["sources"][0]["models"]}
    assert models["full"]["missing_capabilities"] == []
    assert models["text"]["missing_capabilities"] == ["structured_output", "tools"]
    assert listing["required_capabilities"] == ["chat", "structured_output", "tools"]


def test_a_provider_without_selectable_models_still_has_a_label(monkeypatch) -> None:
    from core.application.settings import model_catalog

    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_a, **_k: [
            {"provider_key": "llmext_acme_default_abc", "display_label": "Acme LLM"},
        ],
    )
    with (
        mock.patch.object(model_catalog, "_load_visibility_policy", return_value={}),
        mock.patch.object(model_catalog, "_extension_provider_groups", return_value=[]),
        mock.patch.object(
            model_catalog.model_provider_catalog_repository,
            "list_routable_model_providers",
            return_value=[],
        ),
    ):
        result = model_catalog.get_effective_options_structured(conn=object())

    assert "llmext_acme_default_abc" not in result["provider_models"]
    assert all(o["provider_id"] != "llmext_acme_default_abc" for o in result["provider_options"])
    assert result["provider_labels"]["llmext_acme_default_abc"] == "Acme LLM"
    # Built-in families are named too, with or without credentials.
    assert all(result["provider_labels"].get(fid) for fid in model_catalog.PROVIDER_CATALOG)


def test_labels_survive_an_unreadable_provider_store(monkeypatch) -> None:
    from core.application.settings import model_catalog

    def fail(*_a, **_k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(store, "list_sources", fail)

    labels = model_catalog._known_provider_labels(
        [{"provider_id": "openai", "label": "OpenAI"}], "acc-1", object()
    )

    assert labels["openai"] == "OpenAI"


def _saving(monkeypatch, *, existing_config):
    """A save_credentials run with an in-memory install row; returns what was stored."""
    from core.application.extensions.extension_descriptor import extract_llm_provider_contract
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    contract = extract_llm_provider_contract(_subprocess_descriptor())
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_a: (
            {"install_id": "pi_acme", "runtime_mode": "subprocess"},
            contract,
            "pi_acme",
            "acme.llm",
            "Acme",
        ),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "lock_install_for_credential_rotation",
        lambda *_a, **_k: {"llm_provider_connection_config": dict(existing_config)},
    )
    monkeypatch.setattr(
        store,
        "list_sources",
        lambda *_a, **_k: [
            {
                "id": "s1",
                "extension_install_id": "pi_acme",
                "connection_config_jsonb": dict(existing_config),
            }
        ],
    )
    monkeypatch.setattr(store, "read_secret_ref_ciphertext", lambda *_a, **_k: "")
    stored: dict = {"stale": []}
    monkeypatch.setattr(
        store,
        "set_install_connection_config",
        lambda *_a, connection_config, **_k: stored.__setitem__("install", connection_config),
    )
    monkeypatch.setattr(
        store,
        "set_source_connection_config",
        lambda *_a, connection_config, **_k: stored.__setitem__("source", connection_config),
    )
    monkeypatch.setattr(
        store, "mark_install_models_stale", lambda *_a, reason, **_k: stored["stale"].append(reason)
    )
    for name in (
        "mark_install_runtime_unknown",
        "set_install_provider_state_revision",
        "set_source_credential_ref",
        "set_install_credential_ref",
        "delete_install_credential_refs_except",
    ):
        monkeypatch.setattr(store, name, lambda *_a, **_k: True)
    from core.application.settings import llm_provider_connection_status as connection_status

    for name in ("clear_last_check", "record_saved_secrets"):
        monkeypatch.setattr(connection_status, name, lambda *_a, **_k: True)
    return use_cases, stored


def test_an_emptied_base_url_clears_back_to_the_provider_default(monkeypatch) -> None:
    use_cases, stored = _saving(
        monkeypatch, existing_config={"upstream_base_url": "https://ollama.example.com"}
    )

    use_cases.save_credentials(
        "pi_acme", "acc-1", secrets={}, connection_config={"upstream_base_url": ""}
    )

    # No key in the request: the stored key is untouched; the URL is gone.
    assert stored["install"] == {}
    assert stored["source"] == {}
    assert stored["stale"] == ["configuration_changed"]


def _with_contract(monkeypatch, use_cases, **contract_overrides) -> None:
    from core.application.extensions.extension_descriptor import extract_llm_provider_contract
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    contract = extract_llm_provider_contract(_subprocess_descriptor(**contract_overrides))
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_a: (
            {"install_id": "pi_acme", "runtime_mode": "subprocess"},
            contract,
            "pi_acme",
            "acme.llm",
            "Acme",
        ),
    )


@pytest.fixture
def _no_dns(monkeypatch) -> None:
    # The Base URL guard resolves the host; these tests are about comparing values.
    monkeypatch.setattr(
        "core.application.extensions.url_safety.assert_safe_provider_url",
        lambda url, **_k: str(url).strip(),
    )


@pytest.mark.usefixtures("_no_dns")
@pytest.mark.parametrize("saved", ["https://llm.acme.example", "https://llm.acme.example/"])
def test_saving_the_same_base_url_again_keeps_verified_models(monkeypatch, saved) -> None:
    use_cases, stored = _saving(
        monkeypatch, existing_config={"upstream_base_url": "https://llm.acme.example"}
    )

    result = use_cases.save_credentials(
        "pi_acme", "acc-1", secrets={}, connection_config={"upstream_base_url": saved}
    )

    assert stored["stale"] == []
    assert result["verification_invalidated"] is False


@pytest.mark.usefixtures("_no_dns")
def test_clearing_a_base_url_that_held_the_declared_default_keeps_verified_models(
    monkeypatch,
) -> None:
    use_cases, stored = _saving(
        monkeypatch, existing_config={"upstream_base_url": "https://llm.acme.example"}
    )
    _with_contract(
        monkeypatch,
        use_cases,
        connection_config_defaults={"upstream_base_url": "https://llm.acme.example"},
    )

    result = use_cases.save_credentials(
        "pi_acme", "acc-1", secrets={}, connection_config={"upstream_base_url": ""}
    )

    assert stored["install"] == {}
    assert stored["stale"] == []
    assert result["verification_invalidated"] is False


@pytest.mark.usefixtures("_no_dns")
def test_typing_the_declared_default_into_an_empty_field_keeps_verified_models(
    monkeypatch,
) -> None:
    use_cases, stored = _saving(monkeypatch, existing_config={})
    _with_contract(
        monkeypatch,
        use_cases,
        connection_config_defaults={"upstream_base_url": "https://llm.acme.example"},
    )

    use_cases.save_credentials(
        "pi_acme",
        "acc-1",
        secrets={},
        connection_config={"upstream_base_url": "https://llm.acme.example/"},
    )

    assert stored["stale"] == []


@pytest.mark.usefixtures("_no_dns")
def test_a_real_base_url_change_still_makes_verification_stale(monkeypatch) -> None:
    use_cases, stored = _saving(
        monkeypatch, existing_config={"upstream_base_url": "https://llm.acme.example"}
    )
    _with_contract(
        monkeypatch,
        use_cases,
        connection_config_defaults={"upstream_base_url": "https://llm.acme.example"},
    )

    result = use_cases.save_credentials(
        "pi_acme",
        "acc-1",
        secrets={},
        connection_config={"upstream_base_url": "https://other.acme.example"},
    )

    assert stored["stale"] == ["configuration_changed"]
    assert result["verification_invalidated"] is True


@pytest.mark.parametrize(
    ("defaults", "match"),
    [
        ({"region_name": "eu"}, "undeclared settings: region_name"),
        ({"upstream_base_url": " "}, "must not be empty: upstream_base_url"),
    ],
)
def test_declared_defaults_must_name_a_declared_setting_and_a_value(defaults, match) -> None:
    from core.application.extensions.extension_descriptor import extract_llm_provider_contract
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    descriptor = _subprocess_descriptor(connection_config_defaults=defaults)
    with pytest.raises(Exception, match=match):
        extract_llm_provider_contract(descriptor)


def test_host_contract_1_2_0_accepts_a_bundle_that_declares_setting_defaults() -> None:
    """A bundle using connection_config_defaults declares platform_min 1.2.0.

    This host accepts it; a 1.1.0 host, whose contract forbids the field, says
    the bundle needs a newer platform instead of failing on an unknown field.
    """
    from core.application.extensions.extension_descriptor import (
        DEFAULT_FS_RUNTIME_VERSION,
        extract_llm_provider_contract,
    )
    from core.application.extensions.semver import compare_semver
    from core.application.extensions.service_support import (
        _platform_compatibility_error,
        _runtime_platform_version,
    )
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    descriptor = _subprocess_descriptor(
        connection_config_defaults={"upstream_base_url": "https://llm.acme.example"}
    )
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.2.0"}

    # A later host contract still accepts a 1.2.0 bundle.
    assert compare_semver(DEFAULT_FS_RUNTIME_VERSION, "1.2.0", field_name="host") >= 0
    contract = extract_llm_provider_contract(descriptor)
    assert contract.connection_config_defaults == {"upstream_base_url": "https://llm.acme.example"}
    assert (
        _platform_compatibility_error(descriptor, platform_version=_runtime_platform_version())
        == ""
    )
    assert "below required minimum '1.2.0'" in _platform_compatibility_error(
        descriptor, platform_version="1.1.0"
    )


_BASE_URL_HELP = "Your Ollama server's address, without /api or /v1, e.g. https://ollama.com."
_KEY_HELP = (
    "Copy the whole key from the provider's settings page; it has two parts joined by a dot."
)


def test_host_contract_1_3_0_accepts_field_help_for_declared_fields() -> None:
    """connection_config_help / provider_secret_help need host contract 1.3.0.

    A 1.2.0 host's contract forbids the fields, so a bundle that declares them
    sets platform_min 1.3.0 and that host refuses it as below its minimum.
    """
    from core.application.extensions.extension_descriptor import (
        DEFAULT_FS_RUNTIME_VERSION,
        extract_llm_provider_contract,
        validate_extension_descriptor,
    )
    from core.application.extensions.service_support import (
        _platform_compatibility_error,
        _runtime_platform_version,
    )
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    descriptor = _subprocess_descriptor(
        connection_config_help={"upstream_base_url": _BASE_URL_HELP},
        provider_secret_help={"api_key": _KEY_HELP},
    )
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.3.0"}

    assert DEFAULT_FS_RUNTIME_VERSION == "1.3.0"
    contract = extract_llm_provider_contract(descriptor)
    assert contract.connection_config_help == {"upstream_base_url": _BASE_URL_HELP}
    assert contract.provider_secret_help == {"api_key": _KEY_HELP}
    assert validate_extension_descriptor(descriptor)["valid"] is True
    assert (
        _platform_compatibility_error(descriptor, platform_version=_runtime_platform_version())
        == ""
    )
    assert "below required minimum '1.3.0'" in _platform_compatibility_error(
        descriptor, platform_version="1.2.0"
    )
    older_host = validate_extension_descriptor(descriptor, platform_version="1.2.0")
    assert older_host["valid"] is False
    assert any(
        "below required minimum '1.3.0'" in error["message"] for error in older_host["errors"]
    )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        (
            {"connection_config_help": {"region_name": "EU or US"}},
            "connection_config_help names undeclared settings: region_name",
        ),
        (
            {"provider_secret_help": {"org_token": "From the console"}},
            "provider_secret_help names undeclared provider secrets: org_token",
        ),
        (
            {"connection_config_help": {"upstream_base_url": "  "}},
            "connection_config_help must not be empty: upstream_base_url",
        ),
        (
            {"provider_secret_help": {"api_key": "x" * 301}},
            "provider_secret_help must be at most 300 characters: api_key",
        ),
        ({"connection_config_help": {"upstream_base_url": 42}}, "string"),
        ({"provider_secret_help": ["api_key"]}, "dict"),
    ],
)
def test_field_help_must_name_a_declared_field_with_short_text(overrides, match) -> None:
    from core.application.extensions.extension_descriptor import extract_llm_provider_contract
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    descriptor = _subprocess_descriptor(**overrides)
    with pytest.raises(Exception, match=match):
        extract_llm_provider_contract(descriptor)


def test_field_help_reaches_the_browser_manifest() -> None:
    from core.application.extensions.browser_manifest_projection import (
        project_browser_manifest,
    )
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    descriptor = _subprocess_descriptor(
        connection_config_defaults={"upstream_base_url": "https://llm.acme.example"},
        connection_config_help={"upstream_base_url": _BASE_URL_HELP},
        provider_secret_help={"api_key": _KEY_HELP},
    )

    projected = project_browser_manifest(descriptor)
    contract = projected["runtime"]["extension_contract_v2"]["llm_provider"]
    assert contract["connection_config_defaults"] == {
        "upstream_base_url": "https://llm.acme.example"
    }
    assert contract["connection_config_help"] == {"upstream_base_url": _BASE_URL_HELP}
    assert contract["provider_secret_help"] == {"api_key": _KEY_HELP}


def test_saving_nothing_asks_for_a_setting_in_plain_words(monkeypatch) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
    )

    use_cases, _ = _saving(monkeypatch, existing_config={})

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.save_credentials("pi_acme", "acc-1", secrets={}, connection_config={})

    assert caught.value.detail == "Nothing to save: enter a setting or a key first."


def test_a_missing_required_key_is_named_by_its_label(monkeypatch) -> None:
    from core.application.extensions.extension_descriptor import extract_llm_provider_contract
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
    )
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    use_cases, _ = _saving(monkeypatch, existing_config={})
    contract = extract_llm_provider_contract(
        _subprocess_descriptor(required_provider_secrets=["api_key"], optional_provider_secrets=[])
    )
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_a: (
            {"install_id": "pi_acme", "runtime_mode": "subprocess"},
            contract,
            "pi_acme",
            "acme.llm",
            "Acme",
        ),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.save_credentials(
            "pi_acme", "acc-1", secrets={}, connection_config={"upstream_base_url": ""}
        )

    assert caught.value.detail == "Enter the required key: API key"


@pytest.mark.parametrize(
    ("name", "label"),
    [("upstream_base_url", "Base URL"), ("api_key", "API key"), ("region_name", "Region name")],
)
def test_field_labels(name, label) -> None:
    from core.application.settings.llm_provider_extension_service import llm_field_label

    assert llm_field_label(name) == label
