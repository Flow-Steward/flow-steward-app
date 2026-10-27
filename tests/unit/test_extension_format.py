"""
Verification tests for Epic 14 — Extension Format v1 + Scoped Install Model.

Covers:
- E14-S1: Extension schema validation for workflow-only and connector extensions
- E14-S2: Extension lifecycle (validate, build, import, export) + CLI entry point
- E14-S3: Scoped install records, visibility filtering, import creates install records
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.extensions.extension_descriptor import (
    EXTENSION_DESCRIPTOR_SCHEMA_VERSION,
    validate_extension_descriptor,
)
from core.application.extensions.install import (
    ExtensionInstallRecord,
    ExtensionInstallStore,
    InstallState,
    InstallVisibility,
    _row_to_record,
)
from core.application.extensions.lifecycle import (
    validate_extension_descriptor_builtin,
    validate_extension_descriptor_file,
    validate_extension_descriptor_url,
)
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.infrastructure.db.schema_state import read_canonical_schema_sql
from tests.unit._sql_debug import composable_to_debug_sql

# ---------------------------------------------------------------------------
# E14-S1: Extension Schema
# ---------------------------------------------------------------------------


class TestExtensionDescriptorSchema:
    """Validation coverage for extension_descriptor_v1."""

    @staticmethod
    def _descriptor_data() -> dict:
        return {
            "schema_version": EXTENSION_DESCRIPTOR_SCHEMA_VERSION,
            "extension_id": "acme.stock-extension",
            "release_version": "1.2.0",
            "extension_type": "service_extension",
            "identity": {
                "display_name": "Acme Stock Sync",
                "description": "Imports inventory events and publishes workflows.",
                "tags": ["commerce", "inventory"],
            },
            "vendor": {
                "vendor_name": "Acme Automation",
                "support_url": "https://acme.example.com/support",
            },
            "distribution": {
                "channel": "private_byo",
                "execution_modes": ["remote_https"],
                "allows_self_hosted": True,
                "requires_connected_control_plane": False,
                "import_sources": ["file", "url"],
            },
            "runtime": {
                "contract_version": "extension_host_v2",
                "compatibility": {"platform_min": "1.0.0"},
                "remote": {
                    "base_url": "https://runtime.acme.example.com/flow-steward",
                    "endpoints": {
                        "handshake": "/handshake",
                        "health": "/health",
                        "query": "/queries/{query_id}",
                        "action": "/actions/{action_id}",
                        "emit_event": "/events",
                    },
                },
            },
            "artifacts": {
                "workflows": [
                    {
                        "workflow_id": "acme_stock_import",
                        "ref": "artifacts/workflows/acme_stock_import.yaml",
                        "publish": True,
                    }
                ],
                "agents": [
                    {
                        "agent_id": "acme_stock_reviewer",
                        "ref": "artifacts/agents/acme_stock_reviewer.yaml",
                        "publish": True,
                    }
                ],
            },
            "provenance": {"origin": "imported"},
            "commerce": {"plan_id": "starter"},
        }

    def test_descriptor_accepts_ordered_data_migration_chain(self):
        descriptor = self._descriptor_data()
        descriptor["lifecycle"] = {
            "data_schema_version": 3,
            "data_migrations": [
                {"source_version": 1, "target_version": 2, "action_id": "migrate.v1_to_v2"},
                {"source_version": 2, "target_version": 3, "action_id": "migrate.v2_to_v3"},
            ],
        }

        result = validate_extension_descriptor(
            descriptor,
            artifact_resolver=lambda *_args: True,
        )

        assert result["valid"] is True, result["errors"]

    def test_descriptor_rejects_branching_data_migration_chain(self):
        descriptor = self._descriptor_data()
        descriptor["lifecycle"] = {
            "data_schema_version": 3,
            "data_migrations": [
                {"source_version": 1, "target_version": 2, "action_id": "migrate.one"},
                {"source_version": 1, "target_version": 3, "action_id": "migrate.branch"},
            ],
        }

        result = validate_extension_descriptor(
            descriptor,
            artifact_resolver=lambda *_args: True,
        )

        assert result["valid"] is False
        assert any("branch" in error["message"].lower() for error in result["errors"])

    def test_host_plans_each_skipped_data_schema_step(self):
        from core.application.extensions.data_migration_plan import plan_data_migration_steps

        steps = plan_data_migration_steps(
            {
                "data_schema_version": 3,
                "data_migrations": [
                    {"source_version": 1, "target_version": 2, "action_id": "migrate.one"},
                    {"source_version": 2, "target_version": 3, "action_id": "migrate.two"},
                ],
            },
            source_version=1,
            target_version=3,
        )

        assert [step["action_id"] for step in steps] == ["migrate.one", "migrate.two"]

    def test_host_migration_envelope_excludes_connection_secrets(self):
        from core.application.extensions.data_migrations import build_migration_envelope

        envelope = build_migration_envelope(
            target_type="project_connection_config",
            record={
                "connection_id": "conn-1",
                "connection_config": {"region": "eu"},
                "secret_refs": {"refs": {"api_key": "project_connection:conn-1:api_key"}},
                "credential_revision": 8,
            },
            source_version=1,
            target_version=2,
        )

        assert envelope["record_id"] == "conn-1"
        assert envelope["value"] == {"region": "eu"}
        assert "secret" not in json.dumps(envelope).lower()

    def test_candidate_migrations_run_sequentially_without_runtime_context(self):
        from core.application.extensions.data_migrations import run_candidate_data_migrations

        calls: list[dict] = []

        def invoke(_extension, **kwargs):
            calls.append(kwargs)
            value = dict(kwargs["input_payload"]["value"])
            value[f"v{kwargs['input_payload']['target_version']}"] = True
            return {"status": "succeeded", "response": {"value": value}}

        migrated = run_candidate_data_migrations(
            candidate_extension={
                "extension_id": "fixture.migrating-extension",
                "action_definitions": [
                    {"action_id": "migrate.one"},
                    {"action_id": "migrate.two"},
                ],
            },
            lifecycle={
                "data_migrations": [
                    {"source_version": 1, "target_version": 2, "action_id": "migrate.one"},
                    {"source_version": 2, "target_version": 3, "action_id": "migrate.two"},
                ]
            },
            records=[
                {
                    "target_type": "install_config",
                    "record": {"install_id": "pi-1", "config": {"enabled": True}},
                }
            ],
            source_version=1,
            target_version=3,
            invoke_action=invoke,
        )

        assert migrated[0]["value"] == {"enabled": True, "v2": True, "v3": True}
        assert [call["action_id"] for call in calls] == ["migrate.one", "migrate.two"]
        assert all(call.get("runtime_context") in (None, {}) for call in calls)

    def test_candidate_migration_failure_keeps_results_unpublished(self):
        from core.application.extensions.data_migrations import run_candidate_data_migrations

        with pytest.raises(ValueError, match="failed"):
            run_candidate_data_migrations(
                candidate_extension={
                    "extension_id": "fixture.migrating-extension",
                    "action_definitions": [{"action_id": "migrate.one"}],
                },
                lifecycle={
                    "data_migrations": [
                        {"source_version": 1, "target_version": 2, "action_id": "migrate.one"}
                    ]
                },
                records=[
                    {
                        "target_type": "install_config",
                        "record": {"install_id": "pi-1", "config": {}},
                    }
                ],
                source_version=1,
                target_version=2,
                invoke_action=lambda *_args, **_kwargs: {
                    "status": "failed",
                    "detail": "timeout",
                    "response": {},
                },
            )

    def test_candidate_project_state_migration_requires_inline_namespace_schema(self):
        from core.application.extensions.data_migrations import (
            validate_candidate_migration_results,
        )

        migrated = [
            {
                "target_type": "project_state",
                "record_id": "state-1",
                "namespace": "sync",
                "value": {"cursor": 2},
            }
        ]
        with pytest.raises(ValueError, match="no bounded candidate schema"):
            validate_candidate_migration_results(
                candidate_manifest={"lifecycle": {"project_state_schemas": {}}},
                migrated=migrated,
            )

        validate_candidate_migration_results(
            candidate_manifest={
                "lifecycle": {
                    "project_state_schemas": {
                        "sync": {
                            "type": "object",
                            "properties": {"cursor": {"type": "integer"}},
                            "required": ["cursor"],
                            "additionalProperties": False,
                        }
                    }
                }
            },
            migrated=migrated,
        )

    def test_candidate_connection_migration_uses_contract_v2_schema(self):
        from core.application.extensions.data_migrations import (
            validate_candidate_migration_results,
        )

        with pytest.raises(ValueError, match=r"project_connection_config\.port"):
            validate_candidate_migration_results(
                candidate_manifest={
                    "runtime": {
                        "extension_contract_v2": {
                            "dataset_connectors": [
                                {
                                    "connector_id": "warehouse",
                                    "connector_catalog": [
                                        {"connector_type_id": "warehouse_postgres"}
                                    ],
                                    "connection_schema": {
                                        "type": "object",
                                        "properties": {"port": {"type": "integer"}},
                                        "required": ["port"],
                                    },
                                }
                            ]
                        }
                    }
                },
                migrated=[
                    {
                        "target_type": "project_connection_config",
                        "record_id": "conn-1",
                        "value": {"port": "not-an-integer"},
                    }
                ],
                snapshot={
                    "by_id": {
                        ("project_connection_config", "conn-1"): {
                            "value": {"connector_type_id": "warehouse_postgres"},
                            "connection_type": "warehouse_postgres",
                            "secret_refs": {"refs": {}},
                        }
                    }
                },
            )

    def test_candidate_connection_migration_rejects_mutable_identity_conflict(self):
        from core.application.extensions.data_migrations import (
            validate_candidate_migration_results,
        )

        with pytest.raises(ValueError, match=r"project_connection_config\.port"):
            validate_candidate_migration_results(
                candidate_manifest={
                    "runtime": {
                        "extension_contract_v2": {
                            "dataset_connectors": [
                                {
                                    "connector_id": "warehouse_postgres",
                                    "connection_schema": {
                                        "type": "object",
                                        "properties": {"port": {"type": "integer"}},
                                        "required": ["port"],
                                    },
                                },
                                {
                                    "connector_id": "warehouse_mysql",
                                    "connection_schema": {
                                        "type": "object",
                                        "properties": {"port": {"type": "string"}},
                                        "required": ["port"],
                                    },
                                },
                            ]
                        }
                    }
                },
                migrated=[
                    {
                        "target_type": "project_connection_config",
                        "record_id": "conn-1",
                        "value": {"connector_type_id": "warehouse_postgres", "port": 3306},
                    }
                ],
                snapshot={
                    "by_id": {
                        ("project_connection_config", "conn-1"): {
                            "value": {"connector_type_id": "warehouse_postgres"},
                            "connection_type": "warehouse_mysql",
                            "secret_refs": {"refs": {}},
                        }
                    }
                },
            )

    def test_candidate_connection_migration_requires_exact_multi_connector_contract(self):
        from core.application.extensions.data_migrations import (
            validate_candidate_migration_results,
        )

        with pytest.raises(ValueError, match=r"connector identity"):
            validate_candidate_migration_results(
                candidate_manifest={
                    "runtime": {
                        "extension_contract_v2": {
                            "dataset_connectors": [
                                {
                                    "connector_id": "warehouse",
                                    "connection_schema": {"type": "object"},
                                },
                                {
                                    "connector_id": "analytics",
                                    "connection_schema": {"type": "object"},
                                },
                            ]
                        }
                    }
                },
                migrated=[
                    {
                        "target_type": "project_connection_config",
                        "record_id": "conn-1",
                        "value": {"port": "not-an-integer"},
                    }
                ],
                snapshot={
                    "by_id": {
                        ("project_connection_config", "conn-1"): {
                            "value": {},
                            "secret_refs": {"refs": {}},
                        }
                    }
                },
            )

    @staticmethod
    def _write_descriptor_fixture(tmp_path, descriptor: dict) -> Path:
        (tmp_path / "artifacts" / "workflows").mkdir(parents=True, exist_ok=True)
        (tmp_path / "artifacts" / "agents").mkdir(parents=True, exist_ok=True)
        (tmp_path / "artifacts" / "workflows" / "acme_stock_import.yaml").write_text(
            "workflow_id: acme_stock_import\n",
            encoding="utf-8",
        )
        (tmp_path / "artifacts" / "agents" / "acme_stock_reviewer.yaml").write_text(
            "agent_id: acme_stock_reviewer\n",
            encoding="utf-8",
        )
        descriptor_path = tmp_path / "extension-descriptor.json"
        descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
        return descriptor_path

    @staticmethod
    def _embed_artifact_snapshots(descriptor: dict) -> None:
        workflow = descriptor["artifacts"]["workflows"][0]
        workflow.pop("ref", None)
        workflow["snapshot"] = {"workflow_id": workflow["workflow_id"]}
        agent = descriptor["artifacts"]["agents"][0]
        agent.pop("ref", None)
        agent["snapshot"] = {"agent_id": agent["agent_id"]}

    def test_descriptor_file_validation_success(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is True
        assert result["descriptor"] is not None
        assert result["normalized"]["schema_version"] == EXTENSION_DESCRIPTOR_SCHEMA_VERSION
        assert result["normalized"]["distribution"]["channel"] == "private_byo"
        assert result["normalized"]["artifacts"]["workflows"][0]["snapshot"] == {
            "workflow_id": "acme_stock_import"
        }

    def test_descriptor_accepts_python_requirements(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor["python_requirements"] = [
            {
                "name": "attrs",
                "version": "25.3.0",
                "hashes": ["sha256:" + "0" * 64],
            }
        ]
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is True
        assert result["normalized"]["python_requirements"][0]["name"] == "attrs"

    def test_descriptor_accepts_guardrail_artifacts(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor["artifacts"]["guardrails"] = [
            {
                "guardrail_id": "output_safety_gate",
                "publish": True,
                "snapshot": {
                    "id": "output_safety_gate",
                    "display_name": "Output safety gate",
                    "mode": "deterministic",
                    "rule_type": "output_safety",
                    "severity": "error",
                    "config": {},
                },
            }
        ]
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is True
        normalized = result["normalized"]["artifacts"]
        assert normalized["guardrails"][0]["guardrail_id"] == "output_safety_gate"

    def test_descriptor_non_https_remote_rejected(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor["runtime"]["remote"]["base_url"] = "http://runtime.acme.example.com/flow-steward"
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is False
        assert any(
            e["path"] == "runtime.remote.base_url" and "HTTPS" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_missing_remote_endpoint_rejected(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor["runtime"]["remote"]["endpoints"].pop("health")
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is False
        assert any(e["path"] == "runtime.remote.endpoints.health" for e in result["errors"])

    def test_descriptor_absolute_remote_endpoint_rejected(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor["runtime"]["remote"]["endpoints"]["handshake"] = "https://evil.example/steal"
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is False
        assert any(
            e["path"] == "runtime.remote.endpoints.handshake" and "absolute URL" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_missing_artifact_ref_rejected(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor["artifacts"]["workflows"][0]["ref"] = "artifacts/workflows/missing.yaml"
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is False
        assert any(
            e["path"] == "artifacts.workflows[0].ref" and "missing or escapes" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_artifact_ref_escape_rejected(self, tmp_path):
        secret = tmp_path / "secret.yaml"
        secret.write_text("workflow_id: stolen\n", encoding="utf-8")
        descriptor = self._descriptor_data()
        descriptor["artifacts"]["workflows"][0]["ref"] = "../secret.yaml"
        descriptor_path = self._write_descriptor_fixture(tmp_path / "bundle", descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is False
        assert any(
            e["path"] == "artifacts.workflows[0].ref" and "missing or escapes" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_platform_compatibility_rejected(self, tmp_path):
        descriptor = self._descriptor_data()
        descriptor["runtime"]["compatibility"]["platform_min"] = "9.0.0"
        descriptor_path = self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

        assert result["valid"] is False
        assert any(e["path"] == "runtime.compatibility.platform_min" for e in result["errors"])

    def test_descriptor_url_validation_success(self, monkeypatch):
        descriptor = self._descriptor_data()
        self._embed_artifact_snapshots(descriptor)
        descriptor_raw = json.dumps(descriptor)

        def _mock_fetch(url: str, *, timeout_seconds: float = 20.0):
            assert url == "https://example.com/extension/descriptor.json"
            return descriptor_raw

        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        monkeypatch.setattr("core.application.extensions.lifecycle._fetch_url_text", _mock_fetch)

        result = validate_extension_descriptor_url(
            "https://example.com/extension/descriptor.json",
            platform_version="1.0.0",
        )

        assert result["valid"] is True
        assert result["normalized"]["extension_id"] == "acme.stock-extension"

    def test_descriptor_url_contract_v2_ref_uses_safe_fetcher(self, monkeypatch):
        descriptor = self._descriptor_data()
        self._embed_artifact_snapshots(descriptor)
        descriptor["runtime"]["extension_contract_v2"] = {
            "connection_types": {"ref": "contracts/connection_types.json"}
        }
        fetched_urls: list[str] = []

        def _mock_fetch(url: str, *, timeout_seconds: float = 20.0):
            fetched_urls.append(url)
            if url.endswith("/descriptor.json"):
                return json.dumps(descriptor)
            if url.endswith("/contracts/connection_types.json"):
                return json.dumps([{"connection_type_id": "warehouse"}])
            raise AssertionError(f"unexpected fetch: {url}")

        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        monkeypatch.setattr("core.application.extensions.lifecycle._fetch_url_text", _mock_fetch)

        result = validate_extension_descriptor_url(
            "https://example.com/extension/descriptor.json",
            platform_version="1.0.0",
        )

        assert result["valid"] is True
        assert "https://example.com/extension/contracts/connection_types.json" in fetched_urls

    def test_descriptor_url_validation_blocks_private_descriptor_target(self):
        result = validate_extension_descriptor_url(
            "http://127.0.0.1/extension/descriptor.json",
            platform_version="1.0.0",
        )
        assert result["valid"] is False
        assert any(
            e["path"] == "descriptor_url" and "not allowed" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_url_validation_blocks_private_artifact_url(self, monkeypatch):
        descriptor = self._descriptor_data()
        descriptor["artifacts"]["workflows"][0]["ref"] = "http://169.254.169.254/latest/meta-data"
        descriptor_raw = json.dumps(descriptor)

        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle._fetch_url_text",
            lambda _url, *, timeout_seconds=20.0: descriptor_raw,
        )

        result = validate_extension_descriptor_url(
            "https://example.com/extension/descriptor.json",
            platform_version="1.0.0",
        )
        assert result["valid"] is False
        assert any(
            e["path"] == "artifacts.workflows[0].ref" and "bundle-local" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_url_validation_rejects_descriptor_redirect(self, monkeypatch):
        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )

        def _redirect(_request, *, timeout_seconds: float):
            raise HTTPError(
                "https://example.com/extension/descriptor.json",
                302,
                "Found",
                {"Location": "http://127.0.0.1/admin"},
                None,
            )

        monkeypatch.setattr(
            "core.application.extensions.lifecycle._open_url_no_redirect", _redirect
        )

        result = validate_extension_descriptor_url(
            "https://example.com/extension/descriptor.json",
            platform_version="1.0.0",
        )

        assert result["valid"] is False
        assert any(
            e["path"] == "descriptor_url" and "HTTP Error 302" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_remote_artifact_ref_is_rejected_without_network_probe(self, monkeypatch):
        descriptor = self._descriptor_data()
        descriptor["artifacts"]["workflows"][0]["ref"] = "https://cdn.example.com/artifacts/wf.yaml"
        descriptor_raw = json.dumps(descriptor)

        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle._fetch_url_text",
            lambda _url, *, timeout_seconds=20.0: descriptor_raw,
        )

        opened_methods: list[str] = []

        def _redirect(request, *, timeout_seconds: float):
            opened_methods.append(request.get_method())
            raise HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "http://127.0.0.1/admin"},
                None,
            )

        monkeypatch.setattr(
            "core.application.extensions.lifecycle._open_url_no_redirect", _redirect
        )

        result = validate_extension_descriptor_url(
            "https://example.com/extension/descriptor.json",
            platform_version="1.0.0",
        )

        assert result["valid"] is False
        assert opened_methods == []
        assert any(
            e["path"] == "artifacts.workflows[0].ref" and "bundle-local" in e["message"]
            for e in result["errors"]
        )

    def test_descriptor_builtin_validation_success(self, tmp_path):
        descriptor = self._descriptor_data()
        self._write_descriptor_fixture(tmp_path, descriptor)

        result = validate_extension_descriptor_builtin(
            deepcopy(descriptor),
            source_root=tmp_path,
            platform_version="1.0.0",
        )

        assert result["valid"] is True
        assert result["normalized"]["schema_version"] == EXTENSION_DESCRIPTOR_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# E14-S3: Scoped Install and Visibility
# ---------------------------------------------------------------------------


class TestScopedInstall:
    def test_install_creates_record(self):
        store = ExtensionInstallStore()
        record = store.install(
            extension_id="my_extension", version="1.0.0", account_id="acct_1", installed_by="admin"
        )
        assert record.extension_id == "my_extension"
        assert record.account_id == "acct_1"
        assert record.state == InstallState.active

    def test_same_extension_independent_installs_per_account(self):
        store = ExtensionInstallStore()
        store.install(extension_id="shared", version="1.0.0", account_id="acct_A")
        store.install(extension_id="shared", version="2.0.0", account_id="acct_B")
        assert store.get(extension_id="shared", account_id="acct_A").version == "1.0.0"
        assert store.get(extension_id="shared", account_id="acct_B").version == "2.0.0"

    def test_account_visibility_filters_correctly(self):
        store = ExtensionInstallStore()
        store.install(
            extension_id="private_a", version="1.0.0", account_id="acct_A", visibility="account"
        )
        store.install(
            extension_id="private_b", version="1.0.0", account_id="acct_B", visibility="account"
        )
        assert [r.extension_id for r in store.list_for_account("acct_A")] == ["private_a"]
        assert [r.extension_id for r in store.list_for_account("acct_B")] == ["private_b"]

    def test_global_visibility_visible_to_all_accounts(self):
        store = ExtensionInstallStore()
        store.install(
            extension_id="platform_extension",
            version="1.0.0",
            account_id="acct_system",
            visibility="global",
        )
        store.install(
            extension_id="my_extension", version="1.0.0", account_id="acct_A", visibility="account"
        )

        visible_a = {r.extension_id for r in store.list_for_account("acct_A")}
        assert "platform_extension" in visible_a
        assert "my_extension" in visible_a

        visible_b = {r.extension_id for r in store.list_for_account("acct_B")}
        assert "platform_extension" in visible_b
        assert "my_extension" not in visible_b

    def test_uninstall_one_account_does_not_affect_another(self):
        store = ExtensionInstallStore()
        store.install(extension_id="shared", version="1.0.0", account_id="acct_A")
        store.install(extension_id="shared", version="1.0.0", account_id="acct_B")
        store.uninstall(extension_id="shared", account_id="acct_A")
        assert store.get(extension_id="shared", account_id="acct_A") is None
        assert store.get(extension_id="shared", account_id="acct_B") is not None

    def test_disable_hides_from_listing(self):
        store = ExtensionInstallStore()
        store.install(extension_id="temp", version="1.0.0", account_id="acct_1")
        store.disable(extension_id="temp", account_id="acct_1")
        assert len(store.list_for_account("acct_1")) == 0
        assert store.get(extension_id="temp", account_id="acct_1").state == InstallState.disabled

    def test_list_for_account_can_include_inactive_owned_rows(self):
        store = ExtensionInstallStore()
        store.install(extension_id="temp", version="1.0.0", account_id="acct_1")
        store.disable(extension_id="temp", account_id="acct_1")
        rows = store.list_for_account("acct_1", include_inactive_owned=True)
        assert len(rows) == 1
        assert rows[0].extension_id == "temp"
        assert rows[0].state == InstallState.disabled

    def test_install_record_has_no_extension_content(self):
        record = ExtensionInstallRecord(extension_id="clean", version="1.0.0", account_id="acct_1")
        d = record.model_dump()
        assert "artifacts" not in d
        assert "snapshot" not in d
        assert d["account_id"] == "acct_1"
        assert d["applied_data_schema_version"] == 1


# ---------------------------------------------------------------------------
# E14-S3 continued: DB-backed install store
# ---------------------------------------------------------------------------


class TestDBExtensionInstallStore:
    """Verify DBExtensionInstallStore provides the same interface with persistence."""

    def test_db_store_has_same_interface_as_memory_store(self):
        """DBExtensionInstallStore exposes the same public methods as ExtensionInstallStore."""
        from core.application.extensions.install import DBExtensionInstallStore

        memory_methods = {
            m
            for m in dir(ExtensionInstallStore)
            if not m.startswith("_") and callable(getattr(ExtensionInstallStore, m))
        }
        db_methods = {
            m
            for m in dir(DBExtensionInstallStore)
            if not m.startswith("_") and callable(getattr(DBExtensionInstallStore, m))
        }
        # DB store must have all memory store methods
        assert memory_methods <= db_methods, f"Missing: {memory_methods - db_methods}"

    def test_db_store_install_sql_valid(self):
        """DBExtensionInstallStore.install() generates valid SQL (mocked cursor)."""
        from core.application.extensions.install import DBExtensionInstallStore

        mock_conn = MagicMock()
        mock_conn.autocommit = True
        mock_cursor = MagicMock()
        mock_cursor.fetchone = MagicMock(return_value=None)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        store = DBExtensionInstallStore(mock_conn)

        record = store.install(
            extension_id="test_extension",
            version="1.0.0",
            account_id="acct_42",
            visibility="global",
            installed_by="admin",
        )

        assert record.extension_id == "test_extension"
        assert record.account_id == "acct_42"
        assert record.visibility == InstallVisibility.global_

        # Verify SQL was executed
        assert mock_cursor.execute.call_count >= 1
        sql = composable_to_debug_sql(mock_cursor.execute.call_args_list[-1][0][0])
        assert "INSERT INTO extension_installs" in sql
        assert "ON CONFLICT" in sql
        params = mock_cursor.execute.call_args_list[-1][0][1]
        assert params[2] == "test_extension"
        assert params[4] == "acct_42"
        assert params[17] == "global"

    def test_db_store_install_locks_release_membership_before_upsert(self):
        """Late joins must serialize with scans before taking an install row lock."""
        from core.application.extensions.install import DBExtensionInstallStore

        mock_conn = MagicMock()
        mock_conn.autocommit = False
        mock_conn.conn = None
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        DBExtensionInstallStore(mock_conn).install(
            extension_id="test_extension",
            version="1.0.0",
            account_id="acct_42",
            install_id="pi_membership_lock",
            release_id="release-membership-lock",
        )

        calls = [
            (composable_to_debug_sql(call.args[0]), call.args[1])
            for call in mock_cursor.execute.call_args_list
        ]
        statement, params = next(
            (statement, params)
            for statement, params in calls
            if "pg_advisory_xact_lock" in statement
        )
        assert params[:2] == ("fs.extension_release_membership", "release-membership-lock")
        assert statement.index("pg_advisory_xact_lock") < statement.index("INSERT INTO")

    def test_db_store_list_for_account_includes_global(self):
        """list_for_account SQL filters by account_id OR global visibility."""
        from core.application.extensions.install import DBExtensionInstallStore

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {
                "extension_id": "local",
                "version": "1.0.0",
                "account_id": "acct_A",
                "visibility": "account",
                "state": "active",
                "installed_by": "",
                "installed_at": 0,
                "config_jsonb": "{}",
            },
            {
                "extension_id": "shared",
                "version": "1.0.0",
                "account_id": "sys",
                "visibility": "global",
                "state": "active",
                "installed_by": "",
                "installed_at": 0,
                "config_jsonb": "{}",
            },
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        store = DBExtensionInstallStore(mock_conn)

        results = store.list_for_account("acct_A", include_global=True)
        assert len(results) == 2
        sql = composable_to_debug_sql(mock_cursor.execute.call_args[0][0])
        assert "visibility = 'global'" in sql

    def test_db_row_parser_ignores_noncanonical_config_alias(self):
        """Clean-slate DB rows use config_jsonb only; config is not a compatibility alias."""
        record = _row_to_record(
            {
                "extension_id": "canonical",
                "version": "1.0.0",
                "account_id": "acct_A",
                "install_id": "pi_123",
                "visibility": "account",
                "state": "active",
                "installed_by": "",
                "installed_at": 0,
                "config": {"retired": True},
            }
        )

        assert record.config == {}

    def test_db_row_parser_reads_applied_data_schema_version(self):
        record = _row_to_record(
            {
                "extension_id": "fixture.migrating-extension",
                "version": "3.0.0",
                "account_id": "acct_A",
                "install_id": "pi_migrating",
                "visibility": "account",
                "state": "active",
                "applied_data_schema_version": 3,
            }
        )

        assert record.applied_data_schema_version == 3

    def test_canonical_schema_contains_extension_installs(self):
        """extension_installs schema is present in the canonical schema snapshot."""
        # Individual migration files were consolidated into db/schema/*.sql.
        # The migrations/ directory no longer exists after the clean-bootstrap cutover.
        content = read_canonical_schema_sql()
        assert "extension_installs" in content
        assert "extension_id" in content
        assert "account_id" in content
        assert "visibility" in content
        assert "PRIMARY KEY" in content


class TestExtensionRuntimeStore:
    def test_release_bundle_root_can_be_rebound_to_immutable_storage(self, tmp_path):
        store = ExtensionRuntimeStore()
        release = store.create_release(
            {
                "release_id": "pr_immutable",
                "extension_id": "acme.runtime-extension",
                "release_version": "1.0.0",
                "descriptor": {
                    "extension_id": "acme.runtime-extension",
                    "bundle_root": "/extensions/acme_runtime_extension",
                },
                "source_kind": "extension_scan",
                "source_ref": "acme.runtime-extension",
                "account_id": "acc_default",
            }
        )
        previous_sha = release.descriptor_sha256
        immutable_root = tmp_path / ".releases" / "acme_runtime_extension" / "1.0.0"

        updated = store.update_release_bundle_root(
            release.release_id,
            bundle_root=str(immutable_root),
        )

        assert updated is release
        assert updated.descriptor["bundle_root"] == str(immutable_root)
        assert updated.descriptor_sha256
        assert updated.descriptor_sha256 != previous_sha

    def test_release_snapshot_entitlement_projection_lifecycle(self):
        store = ExtensionRuntimeStore()
        release = store.create_release(
            {
                "extension_id": "acme.runtime-extension",
                "release_version": "1.0.0",
                "descriptor": {
                    "schema_version": "extension_descriptor_v1",
                    "extension_id": "acme.runtime-extension",
                    "release_version": "1.0.0",
                    "distribution": {"execution_modes": ["local"]},
                    "runtime": {"contract_version": "extension_host_v2"},
                },
                "execution_modes": ["local"],
                "contract_version": "extension_host_v2",
                "source_kind": "file",
                "source_ref": "/tmp/descriptor.json",
                "account_id": "acc_default",
                "created_by": "test",
            }
        )
        assert release.release_id.startswith("pr_")

        snapshots = store.replace_artifact_snapshots(
            release.release_id,
            [
                {
                    "release_id": release.release_id,
                    "artifact_kind": "workflow",
                    "artifact_id": "wf_stock_import",
                    "artifact_ref": "artifacts/workflows/wf_stock_import.yaml",
                    "publish": True,
                    "snapshot": {"workflow_id": "wf_stock_import"},
                }
            ],
        )
        assert len(snapshots) == 1
        assert store.list_artifact_snapshots(release.release_id)[0].artifact_id == "wf_stock_import"

        entitlement = store.upsert_entitlement(
            install_id="pi_install_1",
            account_id="acc_default",
            status="trial_active",
            updated_by="checker",
        )
        assert entitlement.status.value == "trial_active"

        projection = store.upsert_projection(
            {
                "account_id": "acc_default",
                "install_id": "pi_install_1",
                "release_id": release.release_id,
                "extension_id": "acme.runtime-extension",
                "artifact_kind": "workflow",
                "artifact_id": "wf_stock_import",
                "artifact_ref": "artifacts/workflows/wf_stock_import.yaml",
                "projection": {"workflow_id": "wf_stock_import"},
                "is_active": True,
            }
        )
        assert projection.is_active is True
        assert len(store.list_projections(account_id="acc_default")) == 1

        store.set_projection_active_for_install("pi_install_1", is_active=False)
        assert len(store.list_projections(account_id="acc_default")) == 0
        assert len(store.list_projections(account_id="acc_default", only_active=False)) == 1

        generated = store.create_generated_extension_job(
            {
                "account_id": "acc_default",
                "requester_user": "tester",
                "request_payload": {"prompt": "Build private extension"},
            }
        )
        assert generated.generated_job_id.startswith("gpj_")
        assert store.get_generated_extension_job(generated.generated_job_id) is not None

        store.delete_projections_for_install("pi_install_1")
        assert store.list_projections(account_id="acc_default", only_active=False) == []

    def test_canonical_schema_contains_extension_runtime_tables(self):
        # Individual migration files (090-095) were consolidated into the canonical
        # clean-bootstrap schema after the cutover.  The migrations/ directory no
        # longer exists.  Assert that every required table/column is present in
        # db/schema/*.sql instead.
        content = read_canonical_schema_sql()

        required_tokens = (
            # 090 — extension_releases_and_artifacts
            "extension_releases",
            "descriptor_sha256",
            "extension_artifact_snapshots",
            # 091 — extension_entitlements
            "extension_entitlements",
            "trial_active",
            "suspended_nonpayment",
            # 092 — extension_usage_events_and_generated_jobs
            "extension_usage_events",
            "generated_extension_jobs",
            # 093 — project_connections_extension_runtime
            "extension_install_id",
            "extension_runtime",
            # 094 — extension_registry_projections
            "extension_registry_projections",
            "artifact_kind",
            # 095 — descriptor_hash_immutability
            "source_snapshot",
        )
        for token in required_tokens:
            assert token in content, f"Bootstrap schema missing expected token: {token!r}"
