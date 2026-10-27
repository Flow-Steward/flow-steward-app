"""Unit coverage for DB-driven LLM provider extensions.

Covers the descriptor `llm_provider` contract, model discovery/grouping
(exact model-id preservation + display labels), the ExtensionChatModel adapter
(chat + structured output, fail-closed), canonical catalog options
(`provider_options`), fail-closed runtime resolution in `get_chat_model`, and
structured-output gating — using the Remote LLM A / Remote LLM B reference
extensions as fixtures.
"""

from __future__ import annotations

import contextlib
from typing import ClassVar

import pytest
from pydantic import BaseModel

from core.application.extensions.extension_descriptor import (
    EXTENSION_DESCRIPTOR_SCHEMA_VERSION,
    extract_llm_provider_contract,
    validate_extension_descriptor,
)
from core.application.settings import llm_provider_connection_status as connection_status
from core.application.settings import llm_provider_extension_service as svc


@pytest.fixture(autouse=True)
def _no_connection_status_writes(monkeypatch) -> None:
    """These tests drive save/discovery against stub connections.

    The connection-status writes (saved secret names, last check) have their
    own tests in test_llm_provider_connection_status.py.
    """
    for name in ("clear_last_check", "record_saved_secrets", "record_last_check"):
        monkeypatch.setattr(connection_status, name, lambda *_a, **_k: True)


def _descriptor(supplier: str) -> dict:
    is_remote_llm_a = supplier == "remote_llm_a"
    extension_id = "fixture.remote-llm-a" if is_remote_llm_a else "fixture.remote-llm-b"
    supplier_key = "remote_llm_a" if is_remote_llm_a else "remote_llm_b_remote"
    supplier_label = "Remote LLM A" if is_remote_llm_a else "Remote LLM B"
    return {
        "schema_version": EXTENSION_DESCRIPTOR_SCHEMA_VERSION,
        "extension_id": extension_id,
        "release_version": "1.0.0",
        "extension_type": "service_extension",
        "identity": {
            "display_name": f"{supplier_label} LLM Provider",
            "description": f"Remote HTTPS {supplier_label} provider fixture.",
            "tags": ["llm", supplier_key],
        },
        "vendor": {"vendor_name": "Flow Steward", "support_url": ""},
        "distribution": {
            "channel": "private_byo",
            "execution_modes": ["remote_https"],
            "allows_self_hosted": True,
            "requires_connected_control_plane": False,
            "import_sources": ["file", "url"],
        },
        "runtime": {
            "contract_version": "extension_host_v2",
            "remote": {
                "base_url": f"https://{supplier_key}.example.com",
                "endpoints": {
                    "handshake": "/handshake",
                    "health": "/health",
                    "query": "/queries/{query_id}",
                    "action": "/actions/{action_id}",
                    "emit_event": "/events",
                },
            },
            "extension_contract_v2": {
                "llm_provider": {
                    "schema_version": "llm_provider_extension_v1",
                    "supplier_key": supplier_key,
                    "supplier_label": supplier_label,
                    "runtime_mode": "remote_https",
                    "credential_provider_key": supplier_key,
                    "required_provider_secrets": ["api_key"],
                    "operations": {
                        "list_models": {"query_id": "llm.list_models"},
                        "chat": {"action_id": "llm.chat"},
                        "structured_chat": {
                            "action_id": "llm.chat_structured",
                            "optional": True,
                        },
                    },
                    "model_grouping": {
                        "strategy": "upstream_owner" if is_remote_llm_a else "endpoint",
                        "owner_key_field": "upstream_owner_key",
                        "owner_label_field": "upstream_owner_label",
                    },
                    "required_capabilities_for_enablement": ["chat", "tools", "structured_output"],
                }
            },
        },
        "artifacts": {
            "tools": [
                {
                    "tool_id": f"{extension_id.split('.', 1)[1].replace('-', '_')}.llm_provider",
                    "snapshot": {"kind": "llm_provider"},
                }
            ]
        },
    }


def _remote_llm_a_descriptor() -> dict:
    return _descriptor("remote_llm_a")


def _claimed_discovery_snapshot(**overrides) -> dict:
    descriptor = overrides.pop("descriptor_jsonb", _remote_llm_a_descriptor())
    return {
        "release_id": "release-1",
        "descriptor_sha256": "a" * 64,
        "descriptor_jsonb": descriptor,
        "extension_id": str(descriptor.get("extension_id") or ""),
        "llm_provider_state_revision": "revision-1",
        "remote_base_url": "https://runtime.example.com",
        "credential_secret_ref": "",
        "encrypted_value": "",
        "connection_config_jsonb": {},
        **overrides,
    }


_REMOTE_LLM_A_MODELS = [
    {
        "model_id": "deepseek/deepseek-chat",
        "upstream_owner_key": "deepseek",
        "upstream_owner_label": "DeepSeek",
        "supports_structured_output": True,
    },
    {
        "model_id": "z-ai/glm-4.5",
        "upstream_owner_key": "z-ai",
        "upstream_owner_label": "Z.AI / GLM",
        "supports_structured_output": True,
    },
    {
        "model_id": "meta-llama/llama-3.3-70b-instruct",
        "upstream_owner_key": "meta-llama",
        "upstream_owner_label": "Meta Llama",
        "supports_structured_output": False,
    },
]
_REMOTE_LLM_B_MODELS = [
    {"model_id": "llama3.3:70b", "supports_structured_output": True},
    {"model_id": "qwen2.5-coder:32b", "supports_structured_output": True},
]
_CHAT_RESULT = {
    "message": {"role": "assistant", "content": "Hello via Remote LLM A."},
    "finish_reason": "stop",
}
_STRUCTURED_RESULT = {
    "structured_output": {"intent": "build_workflow", "confidence": 0.92},
    "finish_reason": "stop",
}


# --------------------------------------------------------------------------- #
# Descriptor contract                                                          #
# --------------------------------------------------------------------------- #


class TestLlmProviderDescriptor:
    def test_reference_descriptors_validate(self):
        for supplier in ("remote_llm_a", "remote_llm_b"):
            result = validate_extension_descriptor(_descriptor(supplier))
            assert result["valid"], result["errors"]

    def test_a_declared_provider_contract_parses_into_a_typed_contract(self):
        """The parser, on a descriptor built here.

        This used to loop over two shipped bundles and assert their supplier keys.
        What each bundle declares is that bundle's claim and is asserted in its own
        suite; what Core owns is that a well-formed declaration parses.
        """
        descriptor = _remote_llm_a_descriptor()
        descriptor["runtime"]["extension_contract_v2"]["llm_provider"]["supplier_key"] = (
            "acme_supplier"
        )

        contract = extract_llm_provider_contract(descriptor)

        assert contract is not None
        assert contract.supplier_key == "acme_supplier"
        assert contract.runtime_mode == "remote_https"

    def test_non_llm_descriptor_unchanged(self):
        descriptor = _remote_llm_a_descriptor()
        descriptor["runtime"].pop("extension_contract_v2", None)
        result = validate_extension_descriptor(descriptor)
        assert result["valid"], result["errors"]
        assert extract_llm_provider_contract(descriptor) is None

    @pytest.mark.parametrize(
        ("section", "field"),
        [("model_grouping", "stratgey"), ("operations.chat", "unknown_operation_key")],
    )
    def test_rejects_unknown_llm_v1_leaf_fields(self, section, field):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        target = block["operations"]["chat"] if section == "operations.chat" else block[section]
        target[field] = "endpoint"

        result = validate_extension_descriptor(descriptor)

        assert not result["valid"]
        assert any(
            field in error["message"] or field in error["path"] for error in result["errors"]
        )

    def test_reject_non_remote_https_runtime_mode(self):
        descriptor = _remote_llm_a_descriptor()
        descriptor["runtime"]["extension_contract_v2"]["llm_provider"]["runtime_mode"] = "local"
        result = validate_extension_descriptor(descriptor)
        assert not result["valid"]
        assert any(
            "runtime_mode" in e["message"] or "runtime_mode" in e["path"] for e in result["errors"]
        )

    def test_reject_missing_structured_capability(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_capabilities_for_enablement"] = ["chat"]
        result = validate_extension_descriptor(descriptor)
        assert not result["valid"]
        assert any("structured_output" in e["message"] for e in result["errors"])

    def test_reject_missing_tools_capability(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_capabilities_for_enablement"] = ["chat", "structured_output"]
        result = validate_extension_descriptor(descriptor)
        assert not result["valid"]
        assert any("tools" in e["message"] for e in result["errors"])

    def test_reject_unsupported_additional_required_capability(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_capabilities_for_enablement"] = [
            "chat",
            "tools",
            "structured_output",
            "vision",
        ]

        result = validate_extension_descriptor(descriptor)

        assert not result["valid"]
        assert any(
            "unsupported" in e["message"] and "vision" in e["message"] for e in result["errors"]
        )

    def test_discovery_generation_compare_and_swap_handles_dict_rows(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {
                "discovery_generation": "newest",
                "state_revision": "revision-1",
                "release_id": "release-1",
                "extension_id": "ext-1",
            },
            {"extension_id": "ext-1", "descriptor_sha256": "a" * 64},
            {
                "discovery_generation": "newest",
                "state_revision": "revision-1",
                "release_id": "release-1",
                "extension_id": "ext-1",
            },
        ]

        assert store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="newest",
            state_revision="revision-1",
            release_id="release-1",
            descriptor_sha256="a" * 64,
        )
        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="older",
            state_revision="revision-1",
            release_id="release-1",
            descriptor_sha256="a" * 64,
        )
        query = cursor.execute.call_args_list[0].args[0]
        assert "FOR UPDATE" in query
        assert "AS discovery_generation" in query

    def test_discovery_cas_includes_provider_state_revision(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "discovery_generation": "generation-1",
            "state_revision": "revision-2",
            "release_id": "release-1",
        }

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="generation-1",
            state_revision="revision-1",
            release_id="release-1",
            descriptor_sha256="a" * 64,
        )
        query = cursor.execute.call_args.args[0]
        assert "llm_provider_state_revision" in query

    def test_discovery_cas_includes_extension_release(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "discovery_generation": "generation-1",
            "state_revision": "revision-1",
            "release_id": "release-new",
        }

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="generation-1",
            state_revision="revision-1",
            release_id="release-old",
            descriptor_sha256="a" * 64,
        )
        query = cursor.execute.call_args.args[0]
        assert "release_id" in query

    def test_discovery_claim_rejects_blank_expected_release_before_lock(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()

        with pytest.raises(store.InstallDiscoveryReleaseMismatchError):
            store.begin_install_discovery(
                conn,
                account_id="acc-1",
                extension_install_id="install-1",
                generation="generation-1",
                expected_release_id="   ",
                expected_descriptor_sha256="a" * 64,
            )

        conn.cursor.assert_not_called()

    def test_discovery_claim_rejects_blank_expected_descriptor_before_lock(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()

        with pytest.raises(store.InstallDiscoveryReleaseMismatchError):
            store.begin_install_discovery(
                conn,
                account_id="acc-1",
                extension_install_id="install-1",
                generation="generation-1",
                expected_release_id="release-1",
                expected_descriptor_sha256="   ",
            )

        conn.cursor.assert_not_called()

    def test_discovery_claim_rejects_same_release_descriptor_change_before_generation(
        self,
    ) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "extension_id": "fixture.remote-llm-a",
            },
            {
                "release_id": "release-1",
                "extension_id": "fixture.remote-llm-a",
                "descriptor_sha256": "b" * 64,
                "descriptor_jsonb": _remote_llm_a_descriptor(),
            },
        ]

        with pytest.raises(store.InstallDiscoveryReleaseMismatchError):
            store.begin_install_discovery(
                conn,
                account_id="acc-1",
                extension_install_id="install-1",
                generation="generation-1",
                expected_release_id="release-1",
                expected_descriptor_sha256="a" * 64,
            )

        assert len(cursor.execute.call_args_list) == 2
        install_lock_query = cursor.execute.call_args_list[0].args[0]
        release_lock_query = cursor.execute.call_args_list[1].args[0]
        assert "extension_installs" in install_lock_query
        assert "FOR UPDATE" in install_lock_query
        assert "extension_releases" in release_lock_query
        assert "descriptor_sha256" in release_lock_query
        assert "FOR UPDATE" in release_lock_query

    def test_discovery_claim_rejects_blank_locked_release_before_generation_mutation(
        self,
    ) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"install_id": "install-1", "release_id": None}

        with pytest.raises(store.InstallDiscoveryReleaseMismatchError):
            store.begin_install_discovery(
                conn,
                account_id="acc-1",
                extension_install_id="install-1",
                generation="generation-1",
                expected_release_id="release-1",
                expected_descriptor_sha256="a" * 64,
            )

        assert len(cursor.execute.call_args_list) == 1
        assert "FOR UPDATE" in cursor.execute.call_args.args[0]

    def test_discovery_completion_rejects_blank_expected_release_before_lock(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="generation-1",
            state_revision="revision-1",
            release_id=" ",
            descriptor_sha256="a" * 64,
        )
        conn.cursor.assert_not_called()

    def test_discovery_completion_rejects_same_release_descriptor_change(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {
                "discovery_generation": "generation-1",
                "state_revision": "revision-1",
                "release_id": "release-1",
                "extension_id": "fixture.remote-llm-a",
            },
            {
                "extension_id": "fixture.remote-llm-a",
                "descriptor_sha256": "b" * 64,
            },
        ]

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="generation-1",
            state_revision="revision-1",
            release_id="release-1",
            descriptor_sha256="a" * 64,
        )
        assert "extension_installs" in cursor.execute.call_args_list[0].args[0]
        assert "extension_releases" in cursor.execute.call_args_list[1].args[0]

    def test_discovery_completion_rejects_blank_expected_descriptor_before_lock(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="generation-1",
            state_revision="revision-1",
            release_id="release-1",
            descriptor_sha256=" ",
        )
        conn.cursor.assert_not_called()

    def test_discovery_completion_rejects_blank_locked_release(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "discovery_generation": "generation-1",
            "state_revision": "revision-1",
            "release_id": None,
        }

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="generation-1",
            state_revision="revision-1",
            release_id="release-1",
            descriptor_sha256="a" * 64,
        )

    def test_provider_state_revision_update_is_account_scoped(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        assert store.set_install_provider_state_revision(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            revision="revision-2",
        )
        query, params = cursor.execute.call_args.args
        assert "llm_provider_state_revision" in query
        assert "account_id = %s" in query
        assert params == ("revision-2", "acc-1", "install-1")

    def test_release_drift_transition_is_cross_account_and_cancels_discovery(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 2

        assert (
            store.invalidate_extension_models_for_release_change(
                conn,
                account_id="acc-1",
                extension_install_id="install-1",
                changed_release_id="release-global",
            )
            == 2
        )
        query, params = cursor.execute.call_args.args
        assert "install.release_id = %s" in query
        assert "install.account_id = %s" in query
        assert "llm_provider_state_revision" in query
        assert "llm_provider_discovery_generation" in query
        assert "verification_status = 'stale'" in query
        assert "verification_generation = NULL" in query
        assert "enabled = false" in query
        assert params == (
            "release-global",
            "acc-1",
            "install-1",
            "extension_release_or_descriptor_changed",
        )

    def test_invalid_extension_transition_disables_all_accounts_and_models(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 3

        assert (
            store.deactivate_invalid_extension_routes(
                conn,
                extension_id="fixture.remote",
            )
            == 3
        )
        query, params = cursor.execute.call_args.args
        assert "install.extension_id = %s" in query
        assert "state = 'disabled'" in query
        assert "llm_provider_state_revision" in query
        assert "llm_provider_discovery_generation" in query
        assert "verification_status = 'stale'" in query
        assert "enabled = false" in query
        assert params == (
            "fixture.remote",
            "extension_invalid_or_missing",
        )

    def test_install_credential_ref_update_is_account_scoped(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        assert store.set_install_credential_ref(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            credential_secret_ref="llmext_cred_install-1",
        )
        query, params = cursor.execute.call_args.args
        assert "llm_provider_credential_secret_ref" in query
        assert "account_id = %s" in query
        assert params == ("llmext_cred_install-1", "acc-1", "install-1")

    def test_begin_discovery_returns_locked_provider_state_snapshot(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "extension_id": "ext-1",
            },
            {
                "release_id": "release-1",
                "extension_id": "ext-1",
                "descriptor_jsonb": _remote_llm_a_descriptor(),
                "descriptor_sha256": "a" * 64,
            },
            {
                "config_jsonb": {
                    "llm_provider_state_revision": "revision-1",
                    "llm_provider_connection_config": {"upstream_base_url": "https://one.example"},
                },
                "remote_base_url": "https://runtime-new.example",
            },
            {
                "credential_secret_ref": "credential-new",
                "encrypted_value": "ciphertext-new",
                "connection_config_jsonb": {"upstream_base_url": "https://upstream-new.example"},
            },
        ]

        snapshot = store.begin_install_discovery(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            generation="generation-1",
            expected_release_id="release-1",
            expected_descriptor_sha256="a" * 64,
        )

        lock_query, lock_params = cursor.execute.call_args_list[0].args
        release_query, release_params = cursor.execute.call_args_list[1].args
        claim_query, claim_params = cursor.execute.call_args_list[2].args
        snapshot_query, snapshot_params = cursor.execute.call_args_list[3].args
        assert "FOR UPDATE" in lock_query
        assert lock_params == ("acc-1", "install-1")
        assert "extension_releases" in release_query
        assert "FOR UPDATE" in release_query
        assert release_params == ("release-1",)
        assert "llm_provider_discovery_generation" in claim_query
        assert "RETURNING config_jsonb, remote_base_url" in claim_query
        assert claim_params == ("generation-1", "acc-1", "install-1")
        assert "llm_provider_sources" in snapshot_query
        assert "JOIN secret_refs" in snapshot_query
        assert "COALESCE(source.credential_secret_ref, %s)" in snapshot_query
        assert snapshot_params == ("", "acc-1", "install-1", "acc-1", "")
        assert snapshot["llm_provider_state_revision"] == "revision-1"
        assert snapshot["release_id"] == "release-1"
        assert snapshot["descriptor_sha256"] == "a" * 64
        assert snapshot["remote_base_url"] == "https://runtime-new.example"
        assert snapshot["credential_secret_ref"] == "credential-new"
        assert snapshot["encrypted_value"] == "ciphertext-new"
        assert snapshot["connection_config_jsonb"] == {
            "upstream_base_url": "https://upstream-new.example"
        }

    def test_runtime_snapshot_reads_endpoint_and_credentials_in_one_statement(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "install_id": "install-1",
            "release_id": "release-2",
            "runtime_mode": "remote_https",
            "remote_base_url": "https://runtime-new.example",
            "credential_secret_ref": "credential-new",
            "encrypted_value": "ciphertext-new",
        }

        snapshot = store.get_install_runtime_credential_snapshot(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
        )

        query, params = cursor.execute.call_args.args
        assert "extension_installs AS install" in query
        assert "LEFT JOIN LATERAL" in query
        assert "JOIN secret_refs" in query
        assert params == ("acc-1", "install-1")
        assert snapshot["remote_base_url"] == "https://runtime-new.example"
        assert snapshot["encrypted_value"] == "ciphertext-new"

    def test_model_runtime_snapshot_joins_current_route_and_verification_generation(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"install_id": "install-1", "model_id": "owner/model"}

        snapshot = store.get_model_runtime_snapshot(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
            provider_key="llmext_owner",
            model_id="owner/model",
            verification_generation="generation-2",
        )

        query, params = cursor.execute.call_args.args
        assert "JOIN extension_releases" in query
        assert "JOIN llm_provider_sources" in query
        assert "JOIN llm_provider_models" in query
        assert "LEFT JOIN secret_refs" in query
        assert "install.health_status <> 'failed'" in query
        assert "model.verification_generation = %s" in query
        assert "install.runtime_mode IS DISTINCT FROM 'remote_https'" in query
        assert "COALESCE(btrim(install.remote_base_url), '') <> ''" in query
        assert "install.config_jsonb ->> 'llm_provider_credential_secret_ref'" in query
        assert params == (
            "llmext_owner",
            "owner/model",
            "acc-1",
            "install-1",
            "generation-2",
            "generation-2",
            "generation-2",
        )
        assert snapshot["install_id"] == "install-1"

    def test_current_install_credential_ref_uses_latest_source_pointer(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"credential_secret_ref": "llmext_cred_install_new"}

        ref_key = store.get_install_current_credential_ref(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
        )

        query, params = cursor.execute.call_args.args
        assert "llm_provider_sources" in query
        assert "updated_at DESC" in query
        assert params == ("acc-1", "install-1")
        assert ref_key == "llmext_cred_install_new"

    def test_current_install_credential_snapshot_reads_pointer_and_ciphertext_atomically(
        self,
    ) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "credential_secret_ref": "llmext_cred_install_new",
            "encrypted_value": "ciphertext-new",
        }

        snapshot = store.get_install_current_credential_snapshot(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
        )

        query, params = cursor.execute.call_args.args
        assert "JOIN secret_refs" in query
        assert "credential_secret_ref" in query
        assert "encrypted_value" in query
        assert params == ("acc-1", "install-1")
        assert snapshot == ("llmext_cred_install_new", "ciphertext-new")

    def test_credential_ref_cleanup_keeps_only_current_and_previous_generation(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 2

        deleted = store.delete_install_credential_refs_except(
            conn,
            account_id="acc-1",
            base_ref_key="llmext_cred_install-1",
            keep_ref_keys=["llmext_cred_install-1_old", "llmext_cred_install-1_new"],
        )

        assert deleted == 2
        query, params = cursor.execute.call_args.args
        assert "DELETE FROM secret_refs" in query
        assert "account_id = %s" in query
        assert "ref_key = ANY" in query
        assert params == (
            "acc-1",
            "llmext_cred_install-1",
            "llmext_cred_install-1",
            "llmext_cred_install-1",
            ["llmext_cred_install-1_old", "llmext_cred_install-1_new"],
        )

    def test_rejects_secret_declared_as_plaintext_connection_config(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_connection_config"] = ["api_key"]
        result = validate_extension_descriptor(descriptor)
        assert not result["valid"]
        assert any("connection" in e["message"].lower() for e in result["errors"])

    def test_rejects_camel_case_secret_as_plaintext_connection_config(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_connection_config"] = ["apiKey"]
        result = validate_extension_descriptor(descriptor)
        assert not result["valid"]

    def test_rejects_manifest_whitelisted_secret_like_connection_field(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_provider_secrets"] = ["runtime_bearer_token"]
        block["required_connection_config"] = ["apiKey"]

        result = validate_extension_descriptor(descriptor)

        assert not result["valid"]
        assert any("secret" in error["message"].lower() for error in result["errors"])

    @pytest.mark.parametrize(
        "field",
        ["authHeader", "clientCertificate", "signingKey", "headers"],
    )
    def test_rejects_connection_fields_outside_host_owned_v1_allowlist(self, field):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_connection_config"] = [field]

        result = validate_extension_descriptor(descriptor)

        assert not result["valid"]
        assert any("upstream_base_url" in error["message"] for error in result["errors"])

    def test_rejects_noncanonical_manifest_connection_field_alias(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["required_connection_config"] = ["upstreamBaseUrl"]

        result = validate_extension_descriptor(descriptor)

        assert not result["valid"]
        assert any("canonical" in error["message"] for error in result["errors"])

    def test_credential_rotation_lock_is_account_scoped_and_for_update(self) -> None:
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"config_jsonb": {"existing": True}}

        config = store.lock_install_for_credential_rotation(
            conn,
            account_id="acc-1",
            extension_install_id="install-1",
        )

        query, params = cursor.execute.call_args.args
        assert "extension_installs" in query
        assert "account_id = %s" in query
        assert "install_id = %s" in query
        assert "FOR UPDATE" in query
        assert params == ("acc-1", "install-1")
        assert config == {"existing": True, "remote_base_url": ""}

    def test_reject_missing_list_models_operation(self):
        descriptor = _remote_llm_a_descriptor()
        block = descriptor["runtime"]["extension_contract_v2"]["llm_provider"]
        block["operations"]["list_models"]["query_id"] = ""
        result = validate_extension_descriptor(descriptor)
        assert not result["valid"]
        assert any("list_models" in e["path"] for e in result["errors"])

    def test_schema_version_constant(self):
        assert EXTENSION_DESCRIPTOR_SCHEMA_VERSION == "extension_descriptor_v1"


# --------------------------------------------------------------------------- #
# Discovery, grouping, naming                                                  #
# --------------------------------------------------------------------------- #


class TestModelGrouping:
    def test_discovery_uses_runtime_bearer_without_forwarding_it_upstream(self, monkeypatch):
        from core.infrastructure.db import remote_extension_runtime as runtime

        captured = {}

        def fake_query(extension, **_kwargs):
            captured.update(extension)
            return {
                "status": "succeeded",
                "response": {"result": {"models": [], "snapshot_complete": True}},
            }

        monkeypatch.setattr(runtime, "remote_query", fake_query)
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())
        assert (
            svc.fetch_models_via_remote_runtime(
                {
                    "runtime_mode": "remote_https",
                    "handshake_status": "failed",
                    "health_status": "failed",
                },
                contract,
                account_id="acc-1",
                credentials={
                    "runtime_bearer_token": "runtime-secret",
                    "api_key": "upstream-secret",
                },
            )
            == []
        )
        assert captured["remote_bearer_token"] == "runtime-secret"
        assert captured["connection_credentials"] == {"api_key": "upstream-secret"}
        assert captured["handshake_status"] == "unknown"

    def test_discovery_rejects_repeated_cursor_before_reconciliation(self, monkeypatch):
        from core.infrastructure.db import remote_extension_runtime as runtime

        calls = 0

        def query(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {
                "status": "succeeded",
                "response": {
                    "result": {
                        "models": [{"model_id": f"partial-{calls}"}],
                        "next_cursor": "again",
                        "snapshot_complete": True,
                    }
                },
            }

        monkeypatch.setattr(
            runtime,
            "remote_query",
            query,
        )
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())
        with pytest.raises(RuntimeError, match="cursor repeated"):
            svc.fetch_models_via_remote_runtime(
                {"runtime_mode": "remote_https"},
                contract,
                account_id="acc-1",
                credentials={},
            )

    def test_discovery_rejects_page_larger_than_requested_limit(self, monkeypatch):
        from core.infrastructure.db import remote_extension_runtime as runtime

        monkeypatch.setattr(
            runtime,
            "remote_query",
            lambda *_args, **_kwargs: {
                "status": "succeeded",
                "response": {
                    "result": {
                        "models": [
                            {"model_id": f"model-{index}"}
                            for index in range(svc._LIST_MODELS_LIMIT + 1)
                        ],
                        "next_cursor": "",
                        "snapshot_complete": True,
                    }
                },
            },
        )
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())

        with pytest.raises(RuntimeError, match="page limit"):
            svc.fetch_models_via_remote_runtime(
                {"runtime_mode": "remote_https"},
                contract,
                account_id="acc-1",
                credentials={},
            )

    def test_discovery_rejects_duplicate_model_ids_across_pages(self, monkeypatch):
        from core.infrastructure.db import remote_extension_runtime as runtime

        calls = 0

        def query(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {
                "status": "succeeded",
                "response": {
                    "result": {
                        "models": [{"model_id": "same/model"}],
                        "next_cursor": "page-2" if calls == 1 else "",
                        "snapshot_complete": True,
                    }
                },
            }

        monkeypatch.setattr(runtime, "remote_query", query)
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())

        with pytest.raises(RuntimeError, match="duplicate model_id"):
            svc.fetch_models_via_remote_runtime(
                {"runtime_mode": "remote_https"},
                contract,
                account_id="acc-1",
                credentials={},
            )

    def test_discovery_rejects_aggregate_snapshot_over_byte_budget(self, monkeypatch):
        from core.infrastructure.db import remote_extension_runtime as runtime

        monkeypatch.setattr(svc, "_MAX_DISCOVERY_BYTES", 100)
        monkeypatch.setattr(
            runtime,
            "remote_query",
            lambda *_args, **_kwargs: {
                "status": "succeeded",
                "response": {
                    "result": {
                        "models": [{"model_id": "owner/model", "metadata": {"value": "x" * 200}}],
                        "next_cursor": "",
                        "snapshot_complete": True,
                    }
                },
            },
        )
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())

        with pytest.raises(RuntimeError, match="byte limit"):
            svc.fetch_models_via_remote_runtime(
                {"runtime_mode": "remote_https"},
                contract,
                account_id="acc-1",
                credentials={},
            )

    def test_discovery_enforces_one_total_time_budget(self, monkeypatch):
        from core.infrastructure.db import remote_extension_runtime as runtime

        ticks = iter([0.0, 61.0])
        monkeypatch.setattr(svc.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(
            runtime,
            "remote_query",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("request must not start after the total deadline")
            ),
        )
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())

        with pytest.raises(RuntimeError, match="time budget"):
            svc.fetch_models_via_remote_runtime(
                {"runtime_mode": "remote_https"},
                contract,
                account_id="acc-1",
                credentials={},
            )

    def test_provider_key_matches_brief_example(self):
        key = svc.derive_provider_key(
            install_id="abc123", supplier_key="remote_llm_a", owner_key="deepseek"
        )
        assert key.startswith("llmext_remote_llm_a_deepseek_abc123_")

    def test_remote_llm_a_grouping_preserves_model_ids_and_labels(self):
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())
        models = _REMOTE_LLM_A_MODELS
        groups = svc.group_discovered_models(contract, models, install_id="abc123")

        by_label = {g["display_label"]: g for g in groups}
        assert "DeepSeek (Remote LLM A)" in by_label
        assert "Z.AI / GLM (Remote LLM A)" in by_label
        assert "Meta Llama (Remote LLM A)" in by_label

        deepseek = by_label["DeepSeek (Remote LLM A)"]
        assert deepseek["provider_key"].startswith("llmext_remote_llm_a_deepseek_abc123_")
        # model_id is never rewritten
        assert deepseek["models"][0]["model_id"] == "deepseek/deepseek-chat"
        assert deepseek["models"][0]["supports_structured_output"] is True

        # Llama has no structured output -> stored but not structured-routable
        llama = by_label["Meta Llama (Remote LLM A)"]
        assert llama["models"][0]["model_id"] == "meta-llama/llama-3.3-70b-instruct"
        assert llama["models"][0]["supports_structured_output"] is False

    def test_grouping_preserves_upstream_revision_for_stale_gate(self):
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())
        groups = svc.group_discovered_models(
            contract,
            [
                {
                    "model_id": "owner/model",
                    "upstream_owner_key": "owner",
                    "upstream_owner_label": "Owner",
                    "upstream_revision": "sha256:revision-a",
                    "upstream_digest": "sha256:revision-a",
                }
            ],
            install_id="abc123",
        )
        assert groups[0]["models"][0]["upstream_digest"] == "sha256:revision-a"

    def test_grouping_canonicalizes_capability_set_and_structured_declaration(self):
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())

        groups = svc.group_discovered_models(
            contract,
            [
                {
                    "model_id": "owner/model",
                    "upstream_owner_key": "owner",
                    "capabilities": ["tools", "chat", "tools"],
                    "supports_structured_output": True,
                }
            ],
            install_id="abc123",
        )

        model = groups[0]["models"][0]
        assert model["capabilities"] == ["chat", "structured_output", "tools"]
        assert model["supports_structured_output"] is True

    def test_persist_reconciles_removed_models_and_groups(self, monkeypatch):
        """Refresh must prune models/groups absent from the latest discovery."""
        from core.application.settings import llm_provider_extension_repository as store
        from core.application.settings import llm_provider_extension_service as svc

        calls: dict[str, list] = {"models": [], "sources": []}
        monkeypatch.setattr(store, "upsert_source", lambda conn, **kw: "src-1")
        monkeypatch.setattr(
            store, "upsert_discovered_models", lambda conn, **kw: len(kw.get("models") or [])
        )
        monkeypatch.setattr(
            store,
            "reconcile_models_for_source",
            lambda conn, *, account_id, provider_source_id, keep_model_ids: (
                calls["models"].append((provider_source_id, keep_model_ids)) or 0
            ),
        )
        monkeypatch.setattr(
            store,
            "reconcile_sources_for_install",
            lambda conn, *, account_id, extension_install_id, keep_provider_keys: (
                calls["sources"].append((extension_install_id, keep_provider_keys)) or 0
            ),
        )
        contract = extract_llm_provider_contract(_remote_llm_a_descriptor())
        groups = [
            {
                "provider_key": "llmext_remote_llm_a_deepseek_i1",
                "display_label": "DeepSeek (Remote LLM A)",
                "supplier_key": "remote_llm_a",
                "supplier_label": "Remote LLM A",
                "upstream_owner_key": "deepseek",
                "upstream_owner_label": "DeepSeek",
                "models": [{"model_id": "deepseek/deepseek-chat"}],
            }
        ]
        out = svc.persist_provider_groups(
            object(),
            account_id="acc-1",
            install_id="i1",
            extension_id="fixture.remote-llm-a",
            contract=contract,
            groups=groups,
            credential_ref=None,
        )
        # Per-source model reconciliation keeps only the latest model ids.
        assert calls["models"] == [("src-1", ["deepseek/deepseek-chat"])]
        # Install-level source reconciliation keeps only the latest provider keys.
        assert calls["sources"] == [("i1", ["llmext_remote_llm_a_deepseek_i1"])]
        assert out["removed_sources"] == 0

    def test_persist_uses_one_transaction_without_recursive_wrapper(self, monkeypatch):
        from contextlib import contextmanager
        from types import SimpleNamespace

        from core.application.settings import llm_provider_extension_repository as store
        from core.application.settings import llm_provider_extension_service as svc
        from core.infrastructure.db import db_pool

        proxy = SimpleNamespace(autocommit=True)
        concrete = SimpleNamespace(autocommit=False)
        opens: list[object] = []

        @contextmanager
        def transaction_connection(_conn):
            opens.append(_conn)
            yield concrete

        monkeypatch.setattr(db_pool, "transaction_connection", transaction_connection)
        monkeypatch.setattr(db_pool, "active_transaction_connection", lambda _conn: None)
        monkeypatch.setattr(store, "reconcile_sources_for_install", lambda *_args, **_kw: 0)
        contract = SimpleNamespace(
            operations=SimpleNamespace(
                list_models=SimpleNamespace(query_id="llm.list_models"),
                chat=SimpleNamespace(action_id="llm.chat"),
                structured_chat=None,
            )
        )

        result = svc.persist_provider_groups(
            proxy,
            account_id="account",
            install_id="install",
            extension_id="extension",
            contract=contract,
            groups=[],
            credential_ref=None,
        )

        assert opens == [proxy]
        assert result["group_count"] == 0

    def test_reconcile_models_for_source_sql(self):
        from core.application.settings import llm_provider_extension_repository as store

        captured: list = []

        class _Cur:
            rowcount = 1

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                captured.append((sql, params))

        class _Conn:
            def cursor(self, **_kwargs):
                return _Cur()

        store.reconcile_models_for_source(
            _Conn(), account_id="a", provider_source_id="s", keep_model_ids=["m1", "m2"]
        )
        sql, params = captured[0]
        assert "DELETE FROM llm_provider_models" in sql
        assert "NOT IN" in sql
        assert params == ("a", "s", "m1", "m2")

        captured.clear()
        store.reconcile_models_for_source(
            _Conn(), account_id="a", provider_source_id="s", keep_model_ids=[]
        )
        sql_all, _ = captured[0]
        assert "NOT IN" not in sql_all  # empty keep -> delete all models for source

    def test_routable_models_query_requires_passed_active_verification(self):
        from core.application.settings import llm_provider_extension_repository as store

        captured: list[str] = []

        class _Cur:
            description: ClassVar[list] = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                captured.append(sql)

            def fetchall(self):
                return []

        class _Conn:
            def cursor(self, **_kwargs):
                return _Cur()

        assert (
            store.list_enabled_structured_models(_Conn(), account_id="a", provider_source_id="s")
            == []
        )
        assert "verification_status = 'passed'" in captured[0]
        assert "lifecycle = 'active'" in captured[0]
        assert "verified_at >= now() - interval '30 days'" in captured[0]
        assert "verified_at <= now()" in captured[0]

        captured.clear()
        assert (
            store.list_enabled_structured_models_for_sources(
                _Conn(), account_id="a", provider_source_ids=["s"]
            )
            == []
        )
        assert "verified_at >= now() - interval '30 days'" in captured[0]
        assert "verified_at <= now()" in captured[0]

    def test_verification_claim_is_atomic_and_generation_owned(self):
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"verification_generation": "generation-1"}

        claimed = store.claim_model_verification(
            conn,
            account_id="account",
            model_row_id="model",
            generation="generation-1",
            expected_model_updated_at="model-revision",
            expected_source_updated_at="source-revision",
            expected_upstream_revision="upstream-revision",
            expected_upstream_digest="digest",
        )

        sql, params = cursor.execute.call_args.args
        assert "verification_status = 'pending'" in sql
        assert "verification_generation = %s" in sql
        assert "verification_status <> 'pending'" in sql
        assert params[0] == "generation-1"
        assert claimed == {"verification_generation": "generation-1"}

    def test_runtime_url_persistence_targets_existing_account_install(self):
        from unittest.mock import MagicMock

        from core.application.settings import llm_provider_extension_repository as store

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        assert store.set_install_remote_base_url(
            conn,
            account_id="account",
            extension_install_id="install",
            remote_base_url="https://runtime.example",
        )

        sql, params = cursor.execute.call_args.args
        assert "UPDATE extension_installs" in sql
        assert "remote_base_url = %s" in sql
        assert "account_id = %s AND install_id = %s" in sql
        assert params == ("https://runtime.example", "account", "install")

    def test_discovery_insert_binds_every_value(self):
        """A non-empty refresh must not fail from SQL placeholder drift."""
        from core.application.settings import llm_provider_extension_repository as store

        captured: list[tuple[str, tuple]] = []

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                captured.append((str(sql), tuple(params or ())))

        class _Conn:
            def cursor(self, **_kwargs):
                return _Cur()

        assert (
            store.upsert_discovered_models(
                _Conn(),
                account_id="account",
                provider_source_id="source",
                models=[{"model_id": "owner/model", "upstream_revision": "revision"}],
            )
            == 1
        )
        sql, params = captured[0]
        values_clause = sql.split("VALUES", 1)[1].split("ON CONFLICT", 1)[0]
        assert values_clause.count("%s") == len(params) == 12
        assert (
            "declared_capabilities_jsonb IS DISTINCT FROM EXCLUDED.declared_capabilities_jsonb"
            in sql
        )

    def test_remote_llm_b_endpoint_grouping(self):
        contract = extract_llm_provider_contract(_descriptor("remote_llm_b"))
        models = _REMOTE_LLM_B_MODELS
        groups = svc.group_discovered_models(
            contract, models, install_id="inst9", install_display_name="Production GPU"
        )
        assert len(groups) == 1
        group = groups[0]
        assert group["display_label"] == "Remote LLM B: Production GPU"
        ids = {m["model_id"] for m in group["models"]}
        assert ids == {"llama3.3:70b", "qwen2.5-coder:32b"}  # exact preservation


@pytest.mark.parametrize(
    "category",
    ["authentication_error", "billing_quota_exceeded", "rate_limited", "timeout"],
)
def test_discovery_preserves_safe_error_category(monkeypatch, category: str) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
                "remote_base_url": "https://runtime.example.com",
            },
            object(),
            "install-1",
            "fixture.remote-llm-a",
            "Ext",
        ),
    )
    monkeypatch.setattr(use_cases, "_connection_config", lambda _contract, value: value)
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: _claimed_discovery_snapshot(),
    )
    monkeypatch.setattr(store, "list_sources", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        store,
        "get_install_current_credential_ref",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(store, "read_secret_ref_ciphertext", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(category)),
    )
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        connection_status,
        "record_last_check",
        lambda *_a, extension_install_id, status, **_k: recorded.append(
            (extension_install_id, status)
        ),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert caught.value.detail == category
    # The panel reads the failed check back as its connection status.
    assert recorded == [("install-1", category)]


def test_discovery_rejects_release_change_before_claim(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-old",
                "descriptor_sha256": "a" * 64,
            },
            object(),
            "install-1",
            "fixture.remote-llm-a",
            "Ext",
        ),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    captured: dict[str, str] = {}

    def claim(*_args, **kwargs):
        captured["release_id"] = str(kwargs.get("expected_release_id") or "")

    monkeypatch.setattr(store, "begin_install_discovery", claim)
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: pytest.fail("stale contract must not reach runtime"),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert captured == {"release_id": "release-old"}
    assert caught.value.status_code == 409
    assert "superseded" in caught.value.detail.lower()


def test_discovery_rejects_same_release_descriptor_change_before_claim(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    descriptor = _remote_llm_a_descriptor()
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
            },
            extract_llm_provider_contract(descriptor),
            "install-1",
            "fixture.remote-llm-a",
            "Remote LLM A",
        ),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    captured: dict[str, str] = {}

    def claim(*_args, **kwargs):
        captured["release_id"] = str(kwargs.get("expected_release_id") or "")
        captured["descriptor_sha256"] = str(kwargs.get("expected_descriptor_sha256") or "")
        raise store.InstallDiscoveryReleaseMismatchError

    monkeypatch.setattr(store, "begin_install_discovery", claim)
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: pytest.fail("descriptor drift must not reach runtime"),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert captured == {
        "release_id": "release-1",
        "descriptor_sha256": "a" * 64,
    }
    assert caught.value.status_code == 409


@pytest.mark.parametrize("resolved_release_id", [None, "   "])
def test_discovery_rejects_blank_release_before_runtime(
    monkeypatch, resolved_release_id: str | None
) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {"install_id": "install-1", "release_id": resolved_release_id},
            object(),
            "install-1",
            "ext-1",
            "Ext",
        ),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )

    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: pytest.fail(
            "blank expected release must be rejected before generation claim"
        ),
    )
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: pytest.fail("blank release must not reach runtime"),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert caught.value.status_code == 409
    assert "superseded" in caught.value.detail.lower()


@pytest.mark.parametrize("resolved_descriptor_sha256", [None, "   "])
def test_discovery_rejects_blank_descriptor_before_claim(
    monkeypatch, resolved_descriptor_sha256: str | None
) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    descriptor = _remote_llm_a_descriptor()
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": resolved_descriptor_sha256,
            },
            extract_llm_provider_contract(descriptor),
            "install-1",
            "fixture.remote-llm-a",
            "Remote LLM A",
        ),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: pytest.fail(
            "blank expected descriptor digest must be rejected before claim"
        ),
    )
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: pytest.fail("blank digest must not reach runtime"),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert caught.value.status_code == 409
    assert "superseded" in caught.value.detail.lower()


@pytest.mark.parametrize("claimed_descriptor_sha256", [None, "   ", "b" * 64])
def test_discovery_rejects_blank_or_drifted_claimed_descriptor_before_runtime(
    monkeypatch, claimed_descriptor_sha256: str | None
) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    descriptor = _remote_llm_a_descriptor()
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
            },
            extract_llm_provider_contract(descriptor),
            "install-1",
            "fixture.remote-llm-a",
            "Remote LLM A",
        ),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: {
            "release_id": "release-1",
            "descriptor_sha256": claimed_descriptor_sha256,
            "descriptor_jsonb": descriptor,
            "llm_provider_state_revision": "revision-1",
            "remote_base_url": "https://runtime.example.com",
            "credential_secret_ref": "",
            "encrypted_value": "",
            "connection_config_jsonb": {},
        },
    )
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: pytest.fail(
            "blank or drifted claimed digest must not reach runtime"
        ),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert caught.value.status_code == 409


def test_discovery_rejects_release_change_before_persistence(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-old",
                "descriptor_sha256": "a" * 64,
            },
            object(),
            "install-1",
            "fixture.remote-llm-a",
            "Ext",
        ),
    )
    monkeypatch.setattr(use_cases, "_connection_config", lambda _contract, value: value)
    monkeypatch.setattr(use_cases, "_safe_upstream_config", lambda value: value)
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: _claimed_discovery_snapshot(release_id="release-old"),
    )
    monkeypatch.setattr(svc, "fetch_models_via_remote_runtime", lambda *_a, **_k: [])
    monkeypatch.setattr(svc, "group_discovered_models", lambda *_a, **_k: [])
    captured: dict[str, str] = {}

    def current(*_args, **kwargs):
        captured["release_id"] = str(kwargs.get("release_id") or "")
        return False

    monkeypatch.setattr(store, "lock_install_discovery_generation_current", current)
    monkeypatch.setattr(
        svc,
        "persist_provider_groups",
        lambda *_args, **_kwargs: pytest.fail("stale contract must not be persisted"),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert captured == {"release_id": "release-old"}
    assert caught.value.status_code == 409


def test_discovery_rejects_same_release_descriptor_change_after_runtime(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    descriptor = _remote_llm_a_descriptor()
    contract = extract_llm_provider_contract(descriptor)
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
            },
            contract,
            "install-1",
            "fixture.remote-llm-a",
            "Remote LLM A",
        ),
    )
    monkeypatch.setattr(use_cases, "_connection_config", lambda _contract, value: value)
    monkeypatch.setattr(use_cases, "_safe_upstream_config", lambda value: value)
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
            "extension_id": "fixture.remote-llm-a",
            "llm_provider_state_revision": "revision-1",
            "remote_base_url": "https://runtime.example.com",
            "credential_secret_ref": "",
            "encrypted_value": "",
            "connection_config_jsonb": {},
        },
    )
    runtime_calls: list[str] = []
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: runtime_calls.append("called") or [],
    )
    monkeypatch.setattr(svc, "group_discovered_models", lambda *_args, **_kwargs: [])
    captured: dict[str, str] = {}

    def current(*_args, **kwargs):
        captured["release_id"] = str(kwargs.get("release_id") or "")
        captured["descriptor_sha256"] = str(kwargs.get("descriptor_sha256") or "")
        return False

    monkeypatch.setattr(store, "lock_install_discovery_generation_current", current)
    last_good_snapshot = [{"model_id": "last-good"}]
    monkeypatch.setattr(
        svc,
        "persist_provider_groups",
        lambda *_args, **_kwargs: last_good_snapshot.clear(),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert runtime_calls == ["called"]
    assert captured == {
        "release_id": "release-1",
        "descriptor_sha256": "a" * 64,
    }
    assert last_good_snapshot == [{"model_id": "last-good"}]
    assert caught.value.status_code == 409


def test_discovery_uses_contract_from_claimed_descriptor_snapshot(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    resolved_descriptor = _remote_llm_a_descriptor()
    resolved_descriptor["runtime"]["extension_contract_v2"]["llm_provider"]["operations"][
        "list_models"
    ]["query_id"] = "llm.resolved"
    claimed_descriptor = _remote_llm_a_descriptor()
    claimed_descriptor["runtime"]["extension_contract_v2"]["llm_provider"]["operations"][
        "list_models"
    ]["query_id"] = "llm.claimed"
    resolved_contract = extract_llm_provider_contract(resolved_descriptor)
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
            },
            resolved_contract,
            "install-1",
            "fixture.remote-llm-a",
            "Remote LLM A",
        ),
    )
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
            "descriptor_jsonb": claimed_descriptor,
            "extension_id": "fixture.remote-llm-a",
            "llm_provider_state_revision": "revision-1",
            "remote_base_url": "https://runtime.example.com",
            "credential_secret_ref": "",
            "encrypted_value": "",
            "connection_config_jsonb": {},
        },
    )
    contract_queries: list[str] = []

    def capture_contract(_record, contract, **_kwargs):
        contract_queries.append(contract.operations.list_models.query_id)
        return []

    def capture_grouping(contract, _models, **_kwargs):
        contract_queries.append(contract.operations.list_models.query_id)
        return []

    def capture_persistence(_conn, *, contract, **_kwargs):
        contract_queries.append(contract.operations.list_models.query_id)
        return {"sources": []}

    monkeypatch.setattr(svc, "fetch_models_via_remote_runtime", capture_contract)
    monkeypatch.setattr(svc, "group_discovered_models", capture_grouping)
    monkeypatch.setattr(
        store,
        "lock_install_discovery_generation_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(svc, "persist_provider_groups", capture_persistence)

    use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert contract_queries == ["llm.claimed", "llm.claimed", "llm.claimed"]


def test_discovery_rejects_blank_locked_release_before_persistence(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
            },
            object(),
            "install-1",
            "fixture.remote-llm-a",
            "Ext",
        ),
    )
    monkeypatch.setattr(use_cases, "_connection_config", lambda _contract, value: value)
    monkeypatch.setattr(use_cases, "_safe_upstream_config", lambda value: value)
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: _claimed_discovery_snapshot(),
    )
    monkeypatch.setattr(svc, "fetch_models_via_remote_runtime", lambda *_a, **_k: [])
    monkeypatch.setattr(svc, "group_discovered_models", lambda *_a, **_k: [])
    monkeypatch.setattr(
        store,
        "lock_install_discovery_generation_current",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        svc,
        "persist_provider_groups",
        lambda *_args, **_kwargs: pytest.fail("blank locked release must not be persisted"),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert caught.value.status_code == 409


def test_discovery_requires_configured_runtime_endpoint(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
                "runtime_mode": "remote_https",
            },
            object(),
            "install-1",
            "fixture.remote-llm-a",
            "Ext",
        ),
    )
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: pytest.fail("runtime must not be invoked without an endpoint"),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: _claimed_discovery_snapshot(remote_base_url=""),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert caught.value.status_code == 422
    assert caught.value.detail == "runtime_endpoint_required"


def test_discovery_requires_effective_manifest_connection_config(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    descriptor = _remote_llm_a_descriptor()
    descriptor["runtime"]["extension_contract_v2"]["llm_provider"]["required_connection_config"] = [
        "upstream_base_url"
    ]
    contract = extract_llm_provider_contract(descriptor)
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
            },
            contract,
            "install-1",
            "fixture.remote-llm-a",
            "Ext",
        ),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: _claimed_discovery_snapshot(
            descriptor_jsonb=descriptor,
            llm_provider_connection_config={},
        ),
    )
    monkeypatch.setattr(
        svc,
        "fetch_models_via_remote_runtime",
        lambda *_args, **_kwargs: pytest.fail(
            "runtime must not be invoked without required connection config"
        ),
    )

    with pytest.raises(LlmProviderExtensionError) as caught:
        use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert caught.value.status_code == 422
    assert caught.value.detail == "Fill in the required setting: Base URL"


def test_required_connection_config_validates_the_effective_merged_snapshot() -> None:
    from types import SimpleNamespace

    contract = SimpleNamespace(required_connection_config=["upstream_base_url"])

    effective = svc.require_llm_provider_connection_config(
        contract,
        {"upstream_base_url": "https://llm.example.com"},
    )

    assert effective == {"upstream_base_url": "https://llm.example.com"}
    with pytest.raises(ValueError, match="Fill in the required setting: Base URL"):
        svc.require_llm_provider_connection_config(contract, {})


def test_discovery_uses_endpoint_and_credentials_from_claimed_snapshot(monkeypatch) -> None:
    from core.application.settings import llm_provider_extension_repository as store
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases,
        "_resolve_install",
        lambda *_args: (
            {
                "install_id": "install-1",
                "release_id": "release-1",
                "descriptor_sha256": "a" * 64,
                "remote_base_url": "https://runtime-old.example",
            },
            object(),
            "install-1",
            "fixture.remote-llm-a",
            "Ext",
        ),
    )
    monkeypatch.setattr(use_cases, "_connection_config", lambda _contract, value: value)
    monkeypatch.setattr(use_cases, "_safe_upstream_config", lambda value: value)
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_args, **_kwargs: _claimed_discovery_snapshot(
            llm_provider_state_revision="revision-new",
            remote_base_url="https://runtime-new.example",
            credential_secret_ref="credential-new",
            encrypted_value='{"api_key":"key-new"}',
        ),
    )
    monkeypatch.setattr(store, "list_sources", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(store, "get_install_current_credential_ref", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(store, "read_secret_ref_ciphertext", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.application.auth.secrets.decrypt_secret", lambda value: value)
    captured: dict = {}

    def fetch(record, _contract, **kwargs):
        captured["remote_base_url"] = record["remote_base_url"]
        captured["credentials"] = kwargs["credentials"]
        return []

    monkeypatch.setattr(svc, "fetch_models_via_remote_runtime", fetch)
    monkeypatch.setattr(svc, "group_discovered_models", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(store, "lock_install_discovery_generation_current", lambda *_a, **_k: True)
    monkeypatch.setattr(svc, "persist_provider_groups", lambda *_a, **_k: {"providers": 0})

    use_cases.refresh_models("install-1", "acc-1", connection_config={})

    assert captured == {
        "remote_base_url": "https://runtime-new.example",
        "credentials": {"api_key": "key-new"},
    }


class TestCapabilityVerifier:
    def test_probe_results_require_exact_host_side_json_schema(self):
        from core.application.settings.llm_capability_verifier import validate_probe_result

        assert (
            validate_probe_result(
                "structured",
                {"status": "ok", "items": [{"id": 7, "label": "verified"}]},
            )
            is None
        )
        assert (
            "additional properties"
            in str(
                validate_probe_result(
                    "structured",
                    {"status": "ok", "items": [{"id": 7, "label": "verified", "extra": True}]},
                )
            ).lower()
        )

    def test_probe_error_normalization_exposes_only_safe_categories(self):
        from core.application.settings.llm_capability_verifier import (
            CapabilityMismatchError,
            normalize_verification_error,
        )

        assert (
            normalize_verification_error(RuntimeError("401 invalid api key"))
            == "authentication_error"
        )
        assert normalize_verification_error(TimeoutError("socket timeout")) == "timeout"
        assert normalize_verification_error(ValueError("bad JSON")) == "invalid_response"
        assert (
            normalize_verification_error(CapabilityMismatchError("tool contract"))
            == "capability_mismatch"
        )
        for category in (
            "authentication_error",
            "rate_limited",
            "timeout",
            "model_unavailable",
            "capability_mismatch",
            "invalid_response",
            "billing_quota_exceeded",
            "upstream_error",
        ):
            assert normalize_verification_error(RuntimeError(category)) == category

    def test_capability_suite_requires_tool_result_then_validates_combined_schema(self):
        from langchain_core.messages import AIMessage

        from core.application.settings.llm_capability_verifier import (
            PROBE_SCHEMAS,
            run_capability_suite,
        )

        assert PROBE_SCHEMAS["structured"]["title"] == "flow_steward_structured_probe"
        assert PROBE_SCHEMAS["combined"]["title"] == "flow_steward_combined_probe"
        assert PROBE_SCHEMAS["structured"]["properties"]["status"] == {
            "type": "string",
            "const": "ok",
        }
        assert PROBE_SCHEMAS["structured"]["properties"]["items"]["items"]["properties"]["id"] == {
            "type": "integer",
            "const": 7,
        }
        assert PROBE_SCHEMAS["combined"]["properties"]["tool_value"] == {
            "type": "string",
            "const": "probe-ok",
        }

        class _Structured:
            def __init__(self, result):
                self._result = result

            def invoke(self, _messages):
                return self._result

        class _Model:
            def __init__(self):
                self.tool_choices = []

            def bind_tools(self, tools, **kwargs):
                assert tools[0]["function"]["name"] == "flow_steward_capability_probe"
                self.tool_choices.append(kwargs)
                return self

            def invoke(self, messages):
                if isinstance(messages, str):
                    return AIMessage(content="basic-ok")
                if len(messages) == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "probe-call",
                                "name": "flow_steward_capability_probe",
                                "args": {"key": "probe"},
                            }
                        ],
                    )
                return AIMessage(content="probe-ok")

            def with_structured_output(self, schema, **kwargs):
                if schema["properties"].get("tool_value"):
                    return _Structured({"status": "ok", "tool_value": "probe-ok"})
                return _Structured({"status": "ok", "items": [{"id": 7, "label": "verified"}]})

        model = _Model()
        assert run_capability_suite(model) == {"chat", "tools", "structured_output"}
        assert model.tool_choices == [
            {"tool_choice": "required", "parallel_tool_calls": False},
            {"tool_choice": "none", "parallel_tool_calls": False},
        ]


# --------------------------------------------------------------------------- #
# ExtensionChatModel adapter                                                   #
# --------------------------------------------------------------------------- #


class _IntentSpec(BaseModel):
    intent: str
    confidence: float


def _make_adapter():
    from core.application.orchestration.extension_chat_model import ExtensionChatModel

    return ExtensionChatModel(
        account_id="acc-1",
        provider_key="llmext_remote_llm_a_deepseek_abc123",
        model_id="deepseek/deepseek-chat",
        chat_action_id="llm.chat",
        structured_chat_action_id="llm.chat_structured",
        extension_install_id="inst1",
        extension_id="fixture.remote-llm-a",
    )


def _stub_runtime(monkeypatch, *, action_result, captured=None):
    """Stub the remote runtime + DB resolution used by the adapter."""
    from core.application.orchestration import extension_chat_model as mod

    monkeypatch.setattr(
        mod.ExtensionChatModel, "_runtime_extension", lambda self: {"runtime_mode": "remote_https"}
    )

    def _fake_remote_action(extension, *, action_id, input_payload, **kwargs):
        if captured is not None:
            captured.append(
                {
                    "action_id": action_id,
                    "payload": input_payload,
                    "timeout_seconds": kwargs.get("timeout_seconds"),
                }
            )
        return {"status": "succeeded", "response": {"ok": True, "result": action_result}}

    import core.infrastructure.db.remote_extension_runtime as rt

    monkeypatch.setattr(rt, "remote_action", _fake_remote_action)


class TestExtensionChatModel:
    def test_bound_timeout_reaches_remote_runtime(self, monkeypatch):
        captured = []
        _stub_runtime(monkeypatch, action_result=_CHAT_RESULT, captured=captured)

        _make_adapter().bind(timeout=12.5).invoke("probe")

        assert captured[0]["timeout_seconds"] == 12.5

    def test_transient_retry_reuses_one_idempotency_key(self, monkeypatch):
        from core.application.orchestration import extension_chat_model as mod

        monkeypatch.setattr(
            mod.ExtensionChatModel,
            "_runtime_extension",
            lambda self: {"runtime_mode": "remote_https"},
        )
        keys: list[str] = []

        def _remote_action(_extension, **kwargs):
            keys.append(kwargs["idempotency_key"])
            if len(keys) == 1:
                return {"status": "failed", "detail": "timeout"}
            return {"status": "succeeded", "response": {"result": _CHAT_RESULT}}

        monkeypatch.setattr(
            "core.infrastructure.db.remote_extension_runtime.remote_action", _remote_action
        )

        _make_adapter().model_copy(update={"max_retries": 2}).invoke("probe")

        assert len(keys) == 2
        assert keys[0]
        assert keys[0] == keys[1]

    def test_non_transient_failure_is_not_retried(self, monkeypatch):
        from core.application.orchestration import extension_chat_model as mod

        monkeypatch.setattr(
            mod.ExtensionChatModel,
            "_runtime_extension",
            lambda self: {"runtime_mode": "remote_https"},
        )
        calls = 0

        def _remote_action(_extension, **_kwargs):
            nonlocal calls
            calls += 1
            return {"status": "failed", "detail": "authentication_error"}

        monkeypatch.setattr(
            "core.infrastructure.db.remote_extension_runtime.remote_action", _remote_action
        )

        _raises_callable_2422_1 = _make_adapter().model_copy(update={"max_retries": 2}).invoke
        with pytest.raises(Exception, match="authentication_error"):
            _raises_callable_2422_1("probe")
        assert calls == 1

    def test_missing_result_is_invalid_response(self, monkeypatch):
        from core.application.orchestration import extension_chat_model as mod

        monkeypatch.setattr(
            mod.ExtensionChatModel,
            "_runtime_extension",
            lambda self: {"runtime_mode": "remote_https"},
        )
        monkeypatch.setattr(
            "core.infrastructure.db.remote_extension_runtime.remote_action",
            lambda *_args, **_kwargs: {"status": "succeeded", "response": {"ok": True}},
        )

        _raises_callable_2439_1 = _make_adapter().invoke
        with pytest.raises(Exception, match="invalid_response"):
            _raises_callable_2439_1("probe")

    def test_billing_error_survives_runtime_transport_and_verifier_normalization(self, monkeypatch):
        from core.application.orchestration.extension_chat_model import ExtensionLLMError
        from core.application.settings.llm_capability_verifier import (
            normalize_verification_error,
        )
        from core.infrastructure.db import remote_extension_runtime

        class Client:
            def handshake(self, payload=None):
                # The runtime confirms the host contract before the first call.
                return {
                    "ok": True,
                    "result": {
                        "contract_version": "extension_host_v2",
                        "execution_scope_fields": [
                            "group_id",
                            "for_each_id",
                            "step_id",
                            "step_execution_id",
                        ],
                    },
                }

            def invoke_action(self, operation_id, envelope):
                return {
                    "ok": False,
                    "error": {
                        "code": "billing_quota_exceeded",
                        "message": "upstream billing details must remain private",
                    },
                }

        monkeypatch.setattr(
            remote_extension_runtime,
            "_build_remote_client",
            lambda *_args, **_kwargs: Client(),
        )
        monkeypatch.setattr(
            _make_adapter().__class__,
            "_runtime_extension",
            lambda self: {
                "runtime_mode": "remote_https",
                "runtime": {"contract_version": "extension_host_v2"},
                "remote_base_url": "https://runtime.example.com",
            },
        )

        _raises_callable_2473_1 = _make_adapter().invoke
        with pytest.raises(ExtensionLLMError) as exc_info:
            _raises_callable_2473_1("probe")

        assert "billing_quota_exceeded" in str(exc_info.value)
        assert normalize_verification_error(exc_info.value) == "billing_quota_exceeded"

    def test_runtime_snapshot_rejects_current_failed_health_without_cached_generation(
        self, monkeypatch
    ):
        from core.application.orchestration import extension_chat_model as mod
        from core.application.settings import llm_provider_extension_repository as store
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        logger = type("Logger", (), {"conn": object()})()
        monkeypatch.setattr(PlatformIdentityStore, "get_shared_instance", lambda: logger)
        monkeypatch.setattr(
            store,
            "get_model_runtime_snapshot",
            lambda *_args, **_kwargs: {
                "state": "active",
                "runtime_mode": "remote_https",
                "remote_base_url": "https://runtime-generation-b.example",
                "handshake_status": "failed",
                "health_status": "failed",
                "runtime_status": "compatible",
                "encrypted_value": '{"runtime_bearer_token":"runtime-b","api_key":"key-b"}',
                "connection_config_jsonb": {"upstream_base_url": "https://upstream-b.example"},
                "descriptor_jsonb": {"extension_id": "fixture.remote-llm-a"},
            },
            raising=False,
        )
        monkeypatch.setattr(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            lambda *_args, **_kwargs: (
                {
                    "runtime_mode": "remote_https",
                    "remote_base_url": "https://runtime-generation-b.example",
                    "handshake_status": "failed",
                    "health_status": "failed",
                },
                logger,
            ),
        )

        _raises_callable_2517_1 = _make_adapter()._runtime_extension
        with pytest.raises(mod.ExtensionLLMError, match="model_unavailable"):
            _raises_callable_2517_1()

    def test_runtime_snapshot_uses_one_current_endpoint_credential_and_config_generation(
        self, monkeypatch
    ):
        from core.application.auth import secrets as secret_crypto
        from core.application.settings import llm_provider_extension_repository as store
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        logger = type("Logger", (), {"conn": object()})()
        monkeypatch.setattr(PlatformIdentityStore, "get_shared_instance", lambda: logger)
        monkeypatch.setattr(secret_crypto, "decrypt_secret", lambda value: value)
        monkeypatch.setattr(
            store,
            "get_model_runtime_snapshot",
            lambda *_args, **_kwargs: {
                "install_id": "inst1",
                "extension_id": "fixture.remote-llm-a",
                "state": "active",
                "runtime_mode": "remote_https",
                "remote_base_url": "https://runtime-generation-b.example",
                "handshake_status": "ok",
                "health_status": "ok",
                "runtime_status": "compatible",
                "credential_secret_ref": "credential-b",
                "encrypted_value": '{"runtime_bearer_token":"runtime-b","api_key":"key-b"}',
                "connection_config_jsonb": {"upstream_base_url": "https://upstream-b.example"},
                "descriptor_jsonb": {
                    "extension_id": "fixture.remote-llm-a",
                    "runtime": {"remote": {"endpoints": {"action": "/actions/{operation_id}"}}},
                },
            },
            raising=False,
        )

        extension = _make_adapter()._runtime_extension()

        assert extension["remote_base_url"] == "https://runtime-generation-b.example"
        # One decrypted generation; the invocation layer splits the runtime bearer.
        assert extension["llm_credentials"] == {
            "runtime_bearer_token": "runtime-b",
            "api_key": "key-b",
        }
        assert extension["llm_connection_config"] == {
            "upstream_base_url": "https://upstream-b.example"
        }
        assert extension["handshake_status"] == "ok"
        assert extension["health_status"] == "ok"

    def test_usage_metadata_preserves_cache_accounting(self):
        from core.application.orchestration.extension_chat_model import _usage_metadata

        assert _usage_metadata(
            {
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_creation_input_tokens": 4,
                "cache_read_input_tokens": 2,
            }
        ) == {
            "input_tokens": 10,
            "output_tokens": 3,
            "total_tokens": 13,
            "cache_creation_input_tokens": 4,
            "cache_read_input_tokens": 2,
            "input_token_details": {"cache_creation": 4, "cache_read": 2},
        }

    def test_wire_messages_preserve_structured_tool_round_trip_fields(self):
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        from core.application.orchestration.extension_chat_model import _messages_to_wire

        messages = [
            HumanMessage(content=[{"type": "text", "text": "use the probe"}], name="operator"),
            AIMessage(
                content=[
                    {
                        "type": "thinking",
                        "thinking": "I need the tool",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tool_calls=[
                    {
                        "id": "call_probe",
                        "name": "flow_steward_capability_probe",
                        "args": {"key": "probe"},
                    }
                ],
                response_metadata={"continuation": {"provider_token": "opaque"}},
            ),
            ToolMessage(
                content=[{"type": "json", "json": {"value": "probe-ok"}}],
                tool_call_id="call_probe",
                name="flow_steward_capability_probe",
            ),
        ]

        assert _messages_to_wire(messages) == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "use the probe"}],
                "name": "operator",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "I need the tool",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "tool_calls": [
                    {
                        "id": "call_probe",
                        "name": "flow_steward_capability_probe",
                        "arguments": {"key": "probe"},
                    }
                ],
                "continuation": {"provider_token": "opaque"},
            },
            {
                "role": "tool",
                "content": [{"type": "json", "json": {"value": "probe-ok"}}],
                "name": "flow_steward_capability_probe",
                "tool_call_id": "call_probe",
            },
        ]

    def test_wire_messages_bound_opaque_continuation_metadata(self):
        from langchain_core.messages import AIMessage

        from core.application.orchestration.extension_chat_model import _messages_to_wire

        message = AIMessage(
            content="ok",
            additional_kwargs={"cache_control": {"type": "ephemeral"}},
            response_metadata={"continuation": {"opaque": "x" * (16 * 1024)}},
        )
        assert _messages_to_wire([message]) == [
            {
                "role": "assistant",
                "content": "ok",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_chat_returns_ai_message_and_preserves_model_id(self, monkeypatch):
        captured: list = []
        result = _CHAT_RESULT
        _stub_runtime(monkeypatch, action_result=result, captured=captured)
        from langchain_core.messages import HumanMessage

        adapter = _make_adapter()
        message = adapter.invoke([HumanMessage(content="hi")])
        assert "Remote LLM A" in message.content
        # exact model_id sent on the wire
        assert captured[0]["payload"]["model_id"] == "deepseek/deepseek-chat"

    def test_bind_tools_sends_canonical_tool_schema(self, monkeypatch):
        captured: list = []
        _stub_runtime(monkeypatch, action_result=_CHAT_RESULT, captured=captured)
        from langchain_core.messages import HumanMessage

        adapter = _make_adapter().bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "flow_steward_capability_probe",
                        "description": "Return probe status.",
                        "parameters": {
                            "type": "object",
                            "properties": {"key": {"type": "string"}},
                            "required": ["key"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tool_choice="required",
            parallel_tool_calls=False,
        )
        adapter.invoke([HumanMessage(content="call the probe")])

        assert captured[0]["payload"]["tools"][0]["function"]["name"] == (
            "flow_steward_capability_probe"
        )
        assert captured[0]["payload"]["tool_choice"] == "required"
        assert captured[0]["payload"]["parallel_tool_calls"] is False

    def test_chat_parses_stable_tool_calls_from_runtime_response(self, monkeypatch):
        _stub_runtime(
            monkeypatch,
            action_result={
                "message": {
                    "role": "assistant",
                    "content": [],
                    "tool_calls": [
                        {
                            "id": "call_probe",
                            "name": "flow_steward_capability_probe",
                            "arguments": {"key": "probe"},
                        }
                    ],
                    "continuation": {"provider_token": "opaque"},
                },
                "finish_reason": "tool_calls",
            },
        )
        from langchain_core.messages import HumanMessage

        message = _make_adapter().invoke([HumanMessage(content="call the probe")])
        assert message.tool_calls == [
            {
                "id": "call_probe",
                "name": "flow_steward_capability_probe",
                "args": {"key": "probe"},
                "type": "tool_call",
            }
        ]
        assert message.response_metadata["continuation"] == {"provider_token": "opaque"}

    def test_structured_output_parses_into_schema(self, monkeypatch):
        result = _STRUCTURED_RESULT
        _stub_runtime(monkeypatch, action_result=result)
        from langchain_core.messages import HumanMessage

        adapter = _make_adapter()
        structured = adapter.with_structured_output(_IntentSpec)
        parsed = structured.invoke([HumanMessage(content="classify")])
        assert isinstance(parsed, _IntentSpec)
        assert parsed.intent == "build_workflow"
        assert parsed.confidence == pytest.approx(0.92)

    def test_structured_output_emits_llm_end_with_usage_metadata(self, monkeypatch):
        from langchain_core.callbacks import BaseCallbackHandler
        from langchain_core.messages import HumanMessage

        class _UsageHandler(BaseCallbackHandler):
            def __init__(self):
                self.responses = []

            def on_llm_end(self, response, **_kwargs):
                self.responses.append(response)

        _stub_runtime(
            monkeypatch,
            action_result={
                **_STRUCTURED_RESULT,
                "usage": {
                    "input_tokens": 13,
                    "output_tokens": 5,
                    "total_tokens": 18,
                    "cache_read_input_tokens": 3,
                    "cost_usd": 0.0042,
                },
            },
        )
        handler = _UsageHandler()

        parsed = (
            _make_adapter()
            .with_structured_output(_IntentSpec)
            .invoke(
                [HumanMessage(content="classify")],
                config={"callbacks": [handler]},
            )
        )

        assert isinstance(parsed, _IntentSpec)
        assert len(handler.responses) == 1
        assert handler.responses[0].llm_output == {
            "model_name": "deepseek/deepseek-chat",
            "provider_key": "llmext_remote_llm_a_deepseek_abc123",
            "cost_usd": 0.0042,
        }
        message = handler.responses[0].generations[0][0].message
        assert message.usage_metadata == {
            "input_tokens": 13,
            "output_tokens": 5,
            "total_tokens": 18,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 3,
            "input_token_details": {"cache_creation": 0, "cache_read": 3},
        }

    def test_structured_output_include_raw_returns_accounted_ai_message(self, monkeypatch):
        from langchain_core.messages import AIMessage, HumanMessage

        _stub_runtime(
            monkeypatch,
            action_result={
                **_STRUCTURED_RESULT,
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
        )

        result = (
            _make_adapter()
            .with_structured_output(
                _IntentSpec,
                include_raw=True,
            )
            .invoke([HumanMessage(content="classify")])
        )

        assert isinstance(result["raw"], AIMessage)
        assert result["raw"].usage_metadata == {
            "input_tokens": 2,
            "output_tokens": 1,
            "total_tokens": 3,
        }
        assert isinstance(result["parsed"], _IntentSpec)
        assert result["parsing_error"] is None

    def test_structured_output_uses_canonical_runtime_message(self, monkeypatch):
        from langchain_core.messages import HumanMessage

        _stub_runtime(
            monkeypatch,
            action_result={
                **_STRUCTURED_RESULT,
                "message": {"role": "assistant", "content": '{"intent":"build_workflow"}'},
            },
        )

        result = (
            _make_adapter()
            .with_structured_output(_IntentSpec, include_raw=True)
            .invoke([HumanMessage(content="classify")])
        )

        assert result["raw"].content == '{"intent":"build_workflow"}'

    def test_structured_output_include_raw_captures_parse_error(self, monkeypatch):
        from langchain_core.messages import AIMessage, HumanMessage

        _stub_runtime(
            monkeypatch,
            action_result={
                "structured_output": {"intent": "unsupported", "confidence": "not-a-number"},
                "message": {"role": "assistant", "content": "invalid"},
                "finish_reason": "stop",
            },
        )

        result = (
            _make_adapter()
            .with_structured_output(_IntentSpec, include_raw=True)
            .invoke([HumanMessage(content="classify")])
        )

        assert isinstance(result["raw"], AIMessage)
        assert result["parsed"] is None
        assert isinstance(result["parsing_error"], Exception)

    def test_structured_output_missing_raises(self, monkeypatch):
        from core.application.orchestration.extension_chat_model import ExtensionLLMError

        _stub_runtime(monkeypatch, action_result={"finish_reason": "stop"})  # no structured_output
        from langchain_core.messages import HumanMessage

        adapter = _make_adapter()
        structured = adapter.with_structured_output(_IntentSpec)
        _raises_input_2880_1 = [HumanMessage(content="x")]
        with pytest.raises(ExtensionLLMError):
            structured.invoke(_raises_input_2880_1)

    def test_plain_json_schema_is_validated_host_side_without_repair(self, monkeypatch):
        from langchain_core.messages import HumanMessage

        from core.application.orchestration.extension_chat_model import ExtensionLLMError

        _stub_runtime(
            monkeypatch,
            action_result={"structured_output": {"status": "ok", "unexpected": "nope"}},
        )
        schema = {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        }
        _raises_callable_2898_1 = _make_adapter().with_structured_output(schema).invoke
        _raises_input_2898_2 = [HumanMessage(content="x")]
        with pytest.raises(ExtensionLLMError, match="canonical JSON Schema"):
            _raises_callable_2898_1(_raises_input_2898_2)

    def test_typed_dict_schema_is_not_normalized_to_empty_schema(self, monkeypatch):
        from langchain_core.messages import HumanMessage
        from typing_extensions import TypedDict

        from core.application.orchestration.extension_chat_model import ExtensionLLMError

        class CountResult(TypedDict):
            count: int

        _stub_runtime(
            monkeypatch,
            action_result={"structured_output": {"count": "not-an-integer"}},
        )

        _raises_callable_2915_1 = _make_adapter().with_structured_output(CountResult).invoke
        _raises_input_2915_2 = [HumanMessage(content="x")]
        with pytest.raises(ExtensionLLMError, match="canonical JSON Schema"):
            _raises_callable_2915_1(_raises_input_2915_2)

    def test_structured_chat_action_used(self, monkeypatch):
        captured: list = []
        result = _STRUCTURED_RESULT
        _stub_runtime(monkeypatch, action_result=result, captured=captured)
        from langchain_core.messages import HumanMessage

        adapter = _make_adapter()
        adapter.with_structured_output(_IntentSpec).invoke([HumanMessage(content="x")])
        assert captured[0]["action_id"] == "llm.chat_structured"
        assert captured[0]["payload"]["model_id"] == "deepseek/deepseek-chat"

    def test_structured_output_preserves_bound_request_metadata(self, monkeypatch):
        captured: list = []
        _stub_runtime(
            monkeypatch,
            action_result=_STRUCTURED_RESULT,
            captured=captured,
        )

        _make_adapter().with_structured_output(
            _IntentSpec,
            metadata={"trace_id": "trace-1"},
        ).invoke("classify")

        assert captured[0]["payload"]["metadata"] == {"trace_id": "trace-1"}

    def test_generic_adapter_preserves_sampling_parameter_for_opaque_model_ids(self, monkeypatch):
        captured: list = []
        _stub_runtime(monkeypatch, action_result=_STRUCTURED_RESULT, captured=captured)
        from langchain_core.messages import HumanMessage

        adapter = _make_adapter()
        adapter.model_id = "claude-sonnet-5"
        adapter.temperature = 0.4
        adapter.with_structured_output(_IntentSpec).invoke([HumanMessage(content="x")])

        assert captured[0]["payload"]["temperature"] == 0.4

    def test_runtime_phase_result_schema_is_preserved(self, monkeypatch):
        """Real production schemas retain refs, enums, unions and strict object semantics."""
        from langchain_core.messages import HumanMessage

        from core.application.orchestration.models import PhaseResultV2

        cases = [
            (
                PhaseResultV2,
                PhaseResultV2(status="pass", summary="verified").model_dump(),
            ),
        ]
        for schema, payload in cases:
            captured: list = []
            _stub_runtime(
                monkeypatch, action_result={"structured_output": payload}, captured=captured
            )
            parsed = (
                _make_adapter()
                .with_structured_output(schema)
                .invoke([HumanMessage(content="verify")])
            )
            assert isinstance(parsed, schema)
            forwarded = captured[0]["payload"]["schema"]
            assert forwarded == schema.model_json_schema()


# --------------------------------------------------------------------------- #
# Canonical catalog options                                                    #
# --------------------------------------------------------------------------- #


class TestCatalogOptions:
    def _stub_groups(self, monkeypatch):
        from core.application.settings import model_catalog as mc

        groups = [
            {
                "provider_key": "llmext_remote_llm_a_deepseek_abc123",
                "display_label": "DeepSeek (Remote LLM A)",
                "supplier_key": "remote_llm_a",
                "supplier_label": "Remote LLM A",
                "upstream_owner_key": "deepseek",
                "upstream_owner_label": "DeepSeek",
                "models": [
                    {
                        "model_id": "deepseek/deepseek-chat",
                        "label": "DeepSeek Chat",
                        "capabilities": ["chat", "structured_output"],
                    }
                ],
            }
        ]
        monkeypatch.setattr(mc, "_extension_provider_groups", lambda account_id, conn: groups)
        monkeypatch.setattr(mc, "_custom_provider_groups", lambda account_id, conn: [])
        monkeypatch.setattr(mc, "_load_visibility_policy", lambda conn, account_id: {})

    def test_provider_options_includes_extension_group(self, monkeypatch):
        from core.application.settings import model_catalog as mc

        self._stub_groups(monkeypatch)
        out = mc.get_effective_options_structured(account_id="acc-1", conn=object())
        kinds = {o["provider_id"]: o for o in out["provider_options"]}
        assert "llmext_remote_llm_a_deepseek_abc123" in kinds
        ext = kinds["llmext_remote_llm_a_deepseek_abc123"]
        assert ext["source_kind"] == "extension_llm_provider"
        assert ext["label"] == "DeepSeek (Remote LLM A)"
        assert ext["supplier_label"] == "Remote LLM A"
        # provider_models keyed by stable provider_key
        assert out["provider_models"]["llmext_remote_llm_a_deepseek_abc123"][0]["model_id"] == (
            "deepseek/deepseek-chat"
        )
        # all_models entry carries provider_id
        ext_all = [m for m in out["all_models"] if m.get("provider_id")]
        assert ext_all
        assert ext_all[0]["model_id"] == "deepseek/deepseek-chat"

    def test_builtin_options_require_account_catalog_connection(self, monkeypatch):
        from core.application.settings import model_catalog as mc

        with pytest.raises(mc.ModelCatalogUnavailableError):
            mc.get_effective_options_structured(account_id="acc-1", conn=None)

    def test_validate_extension_pair_uses_store(self, monkeypatch):
        from core.application.settings import llm_provider_extension_repository as store
        from core.application.settings import model_catalog as mc

        monkeypatch.setattr(
            store,
            "get_enabled_source_by_provider_key",
            lambda conn, *, account_id, provider_key: {"id": "s1", "enabled": True},
        )
        monkeypatch.setattr(
            store,
            "list_enabled_structured_models",
            lambda conn, *, account_id, provider_source_id: [
                {"model_id": "deepseek/deepseek-chat"}
            ],
        )
        ok, err = mc.validate_provider_model_pair(
            "llmext_remote_llm_a_deepseek_abc123", "deepseek/deepseek-chat", conn=object()
        )
        assert ok, err
        ok2, err2 = mc.validate_provider_model_pair(
            "llmext_remote_llm_a_deepseek_abc123", "unknown/model", conn=object()
        )
        assert not ok2
        assert "not enabled" in err2

    def test_validate_rejects_stale_or_unhealthy_install(self, monkeypatch):
        """Save-time validation must apply the same active/healthy-install gate as
        the catalog/runtime: a source found by provider_key but excluded by the
        enabled/healthy join is rejected (not saveable)."""
        from core.application.settings import llm_provider_extension_repository as store
        from core.application.settings import model_catalog as mc

        # Stale row exists by provider_key...
        monkeypatch.setattr(
            store,
            "get_source_by_provider_key",
            lambda conn, *, account_id, provider_key: {"id": "s1", "enabled": True},
        )
        # ...but the active/healthy-install join excludes it.
        monkeypatch.setattr(store, "list_enabled_sources", lambda conn, *, account_id: [])
        ok, err = mc.validate_provider_model_pair(
            "llmext_remote_llm_a_deepseek_abc123", "deepseek/deepseek-chat", conn=object()
        )
        assert not ok
        assert "not available" in err

    def test_relabel_does_not_break_routing(self, monkeypatch):
        """Display-label change must not affect validation (routing keys off provider_key)."""
        from core.application.settings import llm_provider_extension_repository as store
        from core.application.settings import model_catalog as mc

        # Source label irrelevant — only provider_key + model matter.
        monkeypatch.setattr(
            store,
            "get_enabled_source_by_provider_key",
            lambda conn, *, account_id, provider_key: {
                "id": "s1",
                "enabled": True,
                "display_label": "A Totally Different Label",
            },
        )
        monkeypatch.setattr(
            store,
            "list_enabled_structured_models",
            lambda conn, *, account_id, provider_source_id: [
                {"model_id": "deepseek/deepseek-chat"}
            ],
        )
        ok, _ = mc.validate_provider_model_pair(
            "llmext_remote_llm_a_deepseek_abc123", "deepseek/deepseek-chat", conn=object()
        )
        assert ok


# --------------------------------------------------------------------------- #
# Runtime resolution + structured gating                                       #
# --------------------------------------------------------------------------- #


class _FakeBL:
    conn = object()


class TestRuntimeResolution:
    def _stub_logger(self, monkeypatch):
        from core.infrastructure.db.platform_stores import (
            PlatformConnectionStore,
            PlatformIdentityStore,
        )

        for store in (PlatformConnectionStore, PlatformIdentityStore):
            monkeypatch.setattr(store, "get_shared_instance", staticmethod(lambda: _FakeBL()))

    def test_get_chat_model_returns_extension_adapter(self, monkeypatch):
        from core.application.orchestration import model_provider as mp
        from core.application.orchestration.extension_chat_model import ExtensionChatModel
        from core.application.settings import llm_provider_extension_repository as store

        self._stub_logger(monkeypatch)
        monkeypatch.setattr(
            store,
            "list_enabled_sources",
            lambda conn, *, account_id: [
                {
                    "id": "s1",
                    "provider_key": "llmext_remote_llm_a_deepseek_abc123",
                    "chat_action_id": "llm.chat",
                    "structured_chat_action_id": "llm.chat_structured",
                    "extension_install_id": "inst1",
                    "extension_id": "fixture.remote-llm-a",
                    "credential_secret_ref": "llmext_cred_inst1",
                }
            ],
        )
        monkeypatch.setattr(
            store,
            "list_enabled_structured_models",
            lambda conn, *, account_id, provider_source_id: [
                {"model_id": "deepseek/deepseek-chat"}
            ],
        )
        config = {
            "account_id": "acc-1",
            "agents_config": {
                "default_provider": "llmext_remote_llm_a_deepseek_abc123",
                "default_model": "deepseek/deepseek-chat",
            },
        }
        model = mp.get_chat_model(config)
        assert isinstance(model, ExtensionChatModel)
        assert model.model_id == "deepseek/deepseek-chat"
        assert model.provider_key == "llmext_remote_llm_a_deepseek_abc123"

    def test_get_chat_model_fail_closed_when_unavailable(self, monkeypatch):
        from core.application.orchestration import model_provider as mp
        from core.application.settings import llm_provider_extension_repository as store

        self._stub_logger(monkeypatch)
        # No enabled sources -> unavailable, must raise (no fallback).
        monkeypatch.setattr(store, "list_enabled_sources", lambda conn, *, account_id: [])
        config = {
            "account_id": "acc-1",
            "agents_config": {
                "default_provider": "llmext_missing_default_x",
                "default_model": "foo/bar",
            },
        }
        with pytest.raises(mp.ModelProviderResolutionError):
            mp.get_chat_model(config)

    def test_structured_output_gating_recognizes_extension(self):
        from core.application.orchestration.agents.common_runtime_helpers import (
            _should_use_native_structured_output,
        )

        state = {
            "project_config": {
                "agents_config": {
                    "default_provider": "llmext_remote_llm_a_deepseek_abc123",
                    "default_model": "deepseek/deepseek-chat",
                }
            }
        }
        assert _should_use_native_structured_output(state, "runtime_agent", _IntentSpec) is True

    def test_list_enabled_sources_joins_on_install_id(self):
        """Stale/reinstalled sources must not ride a sibling install's health."""
        from core.application.settings import llm_provider_extension_repository as store

        captured: list = []

        class _Cur:
            description: ClassVar[list] = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                captured.append(sql)

            def fetchall(self):
                return []

        class _Conn:
            def cursor(self, **_kwargs):
                return _Cur()

        store.list_enabled_sources(_Conn(), account_id="acc-1")
        assert any("ei.install_id = s.extension_install_id" in sql for sql in captured)

    def test_delete_sources_for_extension_cleanup(self):
        from core.application.settings import llm_provider_extension_repository as store

        captured: list = []

        class _Cur:
            rowcount = 2

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                captured.append((sql, params))

        class _Conn:
            def cursor(self, **_kwargs):
                return _Cur()

        deleted = store.delete_sources_for_extension(_Conn(), extension_id="fixture.remote-llm-a")
        assert deleted == 2
        assert any("DELETE FROM llm_provider_sources" in sql for sql, _ in captured)
        assert captured[0][1] == ("fixture.remote-llm-a",)

    def test_connection_credentials_pass_through(self):
        from core.infrastructure.db.remote_extension_runtime import _connection_from_extension

        connection = _connection_from_extension(
            {"runtime_mode": "remote_https", "connection_credentials": {"api_key": "secret"}}
        )
        assert connection["credentials"] == {"api_key": "secret"}
        # absent by default
        assert "credentials" not in _connection_from_extension({"runtime_mode": "remote_https"})
