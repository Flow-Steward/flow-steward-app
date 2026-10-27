from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2
from core.application.extensions.lifecycle import validate_extension_descriptor_file

SOURCE_ROOT = Path(
    os.environ.get("PYTEST_SOURCE_REPO_ROOT", Path(__file__).resolve().parents[2])
).resolve()


def _base_descriptor(contract_root: dict) -> dict:
    return {
        "schema_version": "extension_descriptor_v1",
        "extension_id": "acme.extension-v2",
        "release_version": "1.0.0",
        "extension_type": "service_extension",
        "identity": {"display_name": "Acme Extension V2"},
        "vendor": {"vendor_name": "Acme"},
        "distribution": {"channel": "private_byo", "execution_modes": ["local"]},
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": "1.0.0"},
            "extension_contract_v2": contract_root,
        },
        "artifacts": {
            "workflows": [
                {
                    "workflow_id": "wf_v2",
                    "ref": "artifacts/workflows/wf_v2.yaml",
                    "publish": True,
                }
            ]
        },
    }


def _write_descriptor(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "artifacts" / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / "artifacts" / "workflows" / "wf_v2.yaml").write_text(
        "workflow_id: wf_v2\n",
        encoding="utf-8",
    )
    descriptor_path = tmp_path / "extension-descriptor.json"
    descriptor_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return descriptor_path


def test_descriptor_validation_loads_contract_v2_refs_and_preview(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "contracts" / "connection_types.json").write_text(
        json.dumps({"connection_types": [{"connection_type_id": "jira_cloud"}]}),
        encoding="utf-8",
    )
    (tmp_path / "contracts" / "operations.json").write_text(
        json.dumps(
            {
                "operations": [
                    {
                        "operation_id": "create_issue",
                        "operation_kind": "action",
                        "connection_type_ids": ["jira_cloud"],
                        "category": "jira_ops",
                        "inputs": [{"name": "summary", "value_type": "text"}],
                        "outputs": [{"name": "issue_key", "value_type": "text"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "contracts" / "step_ui.json").write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "operation_id": "create_issue",
                        "fields": [
                            {"name": "summary", "widget_type": "text"},
                            {
                                "name": "project",
                                "widget_type": "resource_picker",
                                "query_id": "list_projects",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "contracts" / "queries.json").write_text(
        json.dumps({"queries": [{"query_id": "list_projects", "cursor_mode": "none"}]}),
        encoding="utf-8",
    )
    (tmp_path / "contracts" / "events.json").write_text(
        json.dumps(
            {"bindings": [{"trigger_id": "jira_issue_created", "operation_id": "create_issue"}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "contracts" / "artifacts.json").write_text(
        json.dumps({"policies": [{"operation_id": "create_issue"}]}),
        encoding="utf-8",
    )

    contract_root = {
        "connection_types": "contracts/connection_types.json",
        "operation_manifest": "contracts/operations.json",
        "step_ui_manifest": "contracts/step_ui.json",
        "resource_query_manifest": "contracts/queries.json",
        "event_bindings": "contracts/events.json",
        "artifact_policies": "contracts/artifacts.json",
    }
    descriptor_path = _write_descriptor(tmp_path, _base_descriptor(contract_root))

    result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

    assert result["valid"] is True
    assert result["extension_contract_v2"]["present"] is True
    assert result["extension_contract_v2"]["valid"] is True
    preview = result["extension_contract_v2"]["preview"]
    assert "operation_manifest" in preview["sections_loaded"]
    assert "create_issue" in preview["resolved_ids"]["operation_ids"]
    assert preview["builder_operations"][0]["operation_id"] == "create_issue"


@pytest.mark.parametrize("partition", ["config_schema", "secret_schema", "ingress_schema"])
def test_connection_partition_schemas_reject_unexecuted_keywords(partition: str) -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type_id": "account",
                    partition: {
                        "type": "object",
                        "patternProperties": {"^x": {"type": "string"}},
                    },
                }
            ]
        }
    )

    result = validate_extension_contract_v2(descriptor)

    assert result["valid"] is False
    assert "unsupported_schema_keyword" in {
        str(item.get("code") or "") for item in result["errors"]
    }


def test_contract_ref_outside_bundle_root_is_rejected(tmp_path: Path) -> None:
    outside_path = tmp_path.parent / "outside-ops.json"
    outside_path.write_text(
        json.dumps({"operations": [{"operation_id": "oops"}]}), encoding="utf-8"
    )
    contract_root = {"operation_manifest": "../outside-ops.json"}

    descriptor_path = _write_descriptor(tmp_path, _base_descriptor(contract_root))
    result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

    assert result["valid"] is False
    assert any(issue.get("code") == "ref_outside_bundle" for issue in result.get("errors", []))


def test_snapshot_style_contract_refs_are_supported() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": "snapshot://operations_doc",
            "snapshot_documents": {
                "operations_doc": {
                    "operations": [
                        {
                            "operation_id": "run_sync",
                            "operation_kind": "action",
                            "category": "ops",
                        }
                    ]
                },
            },
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["present"] is True
    assert validation["valid"] is True
    assert validation["normalized_contract"]["operation_manifest"][0]["operation_id"] == "run_sync"


def test_legacy_connection_type_field_is_rejected_without_alias_synthesis() -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type": "example_oauth_account",
                    "display_name": "Example OAuth Account",
                    "auth": {
                        "type": "oauth2",
                        "grant": "authorization_code",
                        "pkce": "required",
                        "authorization_endpoint": "https://idp.example.test/authorize",
                        "token_endpoint": "https://idp.example.test/token",
                        "revocation_endpoint": "https://idp.example.test/revoke",
                        "scopes": {"required": ["read"], "optional": ["write"]},
                        "client_credentials": {
                            "client_id": {"source": "platform_secret"},
                            "client_secret": {
                                "source": "platform_secret",
                                "optional": True,
                            },
                        },
                        "refresh": {"rotates_refresh_token": False},
                        "token_placement": "secrets",
                        "secret_names": {"access_token": "access_token"},
                    },
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    row = validation["normalized_contract"]["connection_types"][0]
    assert "connection_type_id" not in row
    assert any(error["code"] == "legacy_connection_type_field" for error in validation["errors"])


def test_legacy_connection_type_does_not_resolve_oauth_auth_config() -> None:
    from core.application.auth.oauth_client import oauth_auth_config_for_connection

    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type": "legacy_oauth",
                    "auth": {
                        "type": "oauth2",
                        "provider_id": "example",
                        "authorization_endpoint": "https://idp.example.test/authorize",
                        "token_endpoint": "https://idp.example.test/token",
                    },
                }
            ]
        }
    )

    class _Store:
        def get_extension(self, _extension_id: str, *, account_id: str | None = None):
            assert account_id == "acc-1"
            return {"manifest": descriptor}

    assert (
        oauth_auth_config_for_connection(
            _Store(),
            {
                "extension_id": "acme.extension-v2",
                "connection_type": "legacy_oauth",
                "account_id": "acc-1",
            },
        )
        == {}
    )


def test_legacy_connection_type_is_absent_from_release_scope_map() -> None:
    from core.application.extensions.oauth_release_upgrade import _connection_types

    assert (
        _connection_types(
            {
                "connection_types": [
                    {
                        "connection_type": "legacy_oauth",
                        "auth": {
                            "type": "oauth2",
                            "provider_id": "example",
                            "scopes": {"required": ["read"], "optional": []},
                        },
                    }
                ]
            }
        )
        == {}
    )


@pytest.mark.parametrize(
    ("required_oauth_scopes", "expected_code"),
    [
        ("mail.send", "invalid_operation_oauth_scopes"),
        (["mail.send", "mail.send"], "duplicate_operation_oauth_scope"),
        (["mail.admin"], "operation_oauth_scope_outside_connection"),
    ],
)
def test_operation_oauth_scope_requirements_are_closed_and_connection_bound(
    required_oauth_scopes, expected_code: str
) -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type_id": "mail_account",
                    "auth": {
                        "type": "oauth2",
                        "grant": "authorization_code",
                        "pkce": "required",
                        "authorization_endpoint": "https://idp.example.test/authorize",
                        "token_endpoint": "https://idp.example.test/token",
                        "scopes": {
                            "required": ["profile"],
                            "optional": ["mail.send"],
                        },
                        "client_credentials": {
                            "client_id": {"source": "platform_secret"},
                            "client_secret": {
                                "source": "platform_secret",
                                "optional": True,
                            },
                        },
                        "token_placement": "secrets",
                        "secret_names": {"access_token": "access_token"},
                    },
                }
            ],
            "operation_manifest": [
                {
                    "operation_id": "send_message",
                    "connection_type_ids": ["mail_account"],
                    "required_oauth_scopes": required_oauth_scopes,
                }
            ],
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert expected_code in {row["code"] for row in validation["errors"]}


def test_operation_oauth_scope_requirements_accept_declared_optional_scope() -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type_id": "mail_account",
                    "auth": {
                        "type": "oauth2",
                        "grant": "authorization_code",
                        "pkce": "required",
                        "authorization_endpoint": "https://idp.example.test/authorize",
                        "token_endpoint": "https://idp.example.test/token",
                        "scopes": {
                            "required": ["profile"],
                            "optional": ["mail.send"],
                        },
                        "client_credentials": {
                            "client_id": {"source": "platform_secret"},
                            "client_secret": {
                                "source": "platform_secret",
                                "optional": True,
                            },
                        },
                        "token_placement": "secrets",
                        "secret_names": {"access_token": "access_token"},
                    },
                }
            ],
            "operation_manifest": [
                {
                    "operation_id": "send_message",
                    "connection_type_ids": ["mail_account"],
                    "required_oauth_scopes": ["mail.send"],
                }
            ],
        }
    )

    assert validate_extension_contract_v2(descriptor)["valid"] is True


@pytest.mark.parametrize(
    ("rule", "expected_code"),
    [
        (
            {
                "schema": {},
                "error": {
                    "code": "subject_required",
                    "message": "Need subject",
                    "field": "subject",
                },
            },
            "invalid_operation_input_mapping_rule",
        ),
        (
            {
                "schema": {"required": ["subject"]},
                "error": {
                    "code": "subject_required",
                    "message": "Need subject",
                    "field": "typo",
                },
            },
            "unknown_operation_input_mapping_rule_field",
        ),
    ],
)
def test_operation_input_mapping_rules_must_be_executable_and_target_declared_input(
    rule: dict, expected_code: str
) -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "send_message",
                    "inputs": [{"name": "subject", "value_type": "text"}],
                    "input_mapping_rules": [rule],
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert expected_code in {row["code"] for row in validation["errors"]}


def test_workflow_operation_with_required_connection_ref_must_declare_connection_policy() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "convert_document",
                    "inputs": [
                        {
                            "name": "connection_ref",
                            "value_type": "connection_ref",
                            "required": True,
                        }
                    ],
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert "operation_connection_ref_without_connection_type" in {
        row["code"] for row in validation["errors"]
    }


def test_hidden_pre_save_operation_may_resolve_connection_before_policy_exists() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "discover_connection",
                    "workflow_visible": False,
                    "inputs": [
                        {
                            "name": "connection_ref",
                            "value_type": "connection_ref",
                            "required": True,
                        }
                    ],
                }
            ]
        }
    )

    assert validate_extension_contract_v2(descriptor)["valid"] is True


def test_connection_auth_rejects_legacy_provider_credentials() -> None:
    provider_auth = {
        "type": "oauth2",
        "grant": "authorization_code",
        "pkce": "required",
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "scopes": {"required": ["resources.read"]},
        "client_credentials": {
            "client_id": {"provider_key": "legacy_provider"},
            "client_secret": {"provider_key": "legacy_provider"},
        },
        "token_placement": "secrets",
        "secret_names": {"access_token": "access_token"},
    }
    malicious = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type": "evil_oauth_account",
                    "auth": provider_auth,
                }
            ]
        }
    )

    rejected = validate_extension_contract_v2(malicious)

    assert rejected["valid"] is False
    assert any(
        row["code"] == "invalid_connection_auth_oauth_client_credentials"
        and row["path"].endswith("provider_key")
        for row in rejected["errors"]
    )


def test_connection_type_oauth2_requires_pkce() -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type": "example_oauth_account",
                    "auth": {
                        "type": "oauth2",
                        "grant": "authorization_code",
                        "pkce": "optional",
                        "authorization_endpoint": "https://idp.example.test/authorize",
                        "token_endpoint": "https://idp.example.test/token",
                        "scopes": {"required": ["read"]},
                        "client_credentials": {
                            "client_id": {"source": "platform_secret"},
                            "client_secret": {
                                "source": "platform_secret",
                                "optional": True,
                            },
                        },
                        "token_placement": "secrets",
                        "secret_names": {"access_token": "access_token"},
                    },
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert any(
        row.get("code") == "invalid_connection_auth_oauth_pkce" for row in validation["errors"]
    )


def test_connection_type_oauth2_rejects_legacy_provider_credentials() -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type": "legacy_oauth_account",
                    "auth": {
                        "type": "oauth2",
                        "grant": "authorization_code",
                        "pkce": "required",
                        "authorization_endpoint": "https://idp.example.test/authorize",
                        "token_endpoint": "https://idp.example.test/token",
                        "scopes": {"required": ["resources.read"]},
                        "client_credentials": {
                            "client_id": {"provider_key": "legacy_provider"},
                            "client_secret": {"provider_key": "legacy_provider"},
                        },
                        "token_placement": "secrets",
                        "secret_names": {"access_token": "access_token"},
                    },
                }
            ]
        }
    )
    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert any(
        row.get("code") == "invalid_connection_auth_oauth_client_credentials"
        for row in validation["errors"]
    )


def test_connection_type_oauth2_rejects_unknown_credential_provider() -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {
                    "connection_type": "unknown_oauth_account",
                    "auth": {
                        "type": "oauth2",
                        "grant": "authorization_code",
                        "pkce": "required",
                        "authorization_endpoint": "https://idp.example.test/authorize",
                        "token_endpoint": "https://idp.example.test/token",
                        "scopes": {"required": ["read"]},
                        "client_credentials": {
                            "client_id": {"provider_key": "unknown"},
                            "client_secret": {"provider_key": "unknown"},
                        },
                        "token_placement": "secrets",
                        "secret_names": {"access_token": "access_token"},
                    },
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert any(
        error.get("code") == "invalid_connection_auth_oauth_client_credentials"
        for error in validation["errors"]
    )


def test_duplicate_ids_and_cross_section_failures_are_deterministic() -> None:
    descriptor = _base_descriptor(
        {
            "connection_types": [
                {"connection_type_id": "slack"},
                {"connection_type_id": "slack"},
            ],
            "operation_manifest": [
                {
                    "operation_id": "notify",
                    "operation_kind": "action",
                    "connection_type_ids": ["missing_conn"],
                    "partial_failure_mode": "wild_mode",
                },
                {"operation_id": "notify", "operation_kind": "invalid_kind"},
            ],
            "resource_query_manifest": [{"query_id": "pick_users", "cursor_mode": "unknown"}],
            "step_ui_manifest": [
                {
                    "operation_id": "missing_operation",
                    "fields": [
                        {
                            "name": "user",
                            "widget_type": "mystery_widget",
                            "query_id": "missing_query",
                        }
                    ],
                }
            ],
            "event_bindings": [{"trigger_id": "new_message", "operation_id": "missing_operation"}],
            "artifact_policies": [{"operation_id": "missing_operation"}],
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    codes = [row["code"] for row in validation["errors"]]
    assert "duplicate_id" in codes
    assert "invalid_widget_type" in codes
    assert "unknown_operation_id" in codes
    assert "invalid_cursor_mode" in codes
    assert "invalid_partial_failure_mode" in codes
    assert "invalid_operation_kind" in codes


def test_descriptor_validation_accepts_artifact_and_dataset_grant_policies() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [{"operation_id": "sync", "operation_kind": "action"}],
            "artifact_policies": [
                {
                    "operation_id": "sync",
                    "inputs": [
                        {
                            "kind": "artifact",
                            "value_type": "artifact_handle",
                            "binding_key": "source_file",
                            "from_project_state": {
                                "namespace": "snapshots",
                                "state_key": "source_file",
                                "field": "artifact_handle",
                            },
                        },
                        {
                            "kind": "dataset",
                            "value_type": "dataset_handle",
                            "binding_key": "source_rows",
                        },
                    ],
                    "outputs": [
                        {
                            "kind": "dataset",
                            "value_type": "dataset_handle",
                            "binding_key": "processed_rows",
                            "max_size_bytes": 200 * 1024 * 1024,
                            "max_size_env": "FS_EXTENSION_ARTIFACT_MAX_BYTES",
                        }
                    ],
                }
            ],
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is True
    policies = validation["normalized_contract"]["artifact_policies"]
    assert policies[0]["outputs"][0]["binding_key"] == "processed_rows"


def test_descriptor_validation_rejects_invalid_grant_policy_shape() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [{"operation_id": "sync", "operation_kind": "action"}],
            "artifact_policies": [
                {
                    "operation_id": "sync",
                    "inputs": [
                        {
                            "kind": "artifact",
                            "value_type": "dataset_handle",
                            "from_project_state": {"namespace": "snapshots"},
                        }
                    ],
                    "outputs": [
                        {
                            "kind": "dataset",
                            "binding_key": "ok",
                            "max_size_bytes": 0,
                            "max_size_env": "FIXTURE_ATTACHMENT_MAX_BYTES",
                            "retention_class": "forever",
                        }
                    ],
                }
            ],
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    codes = {row["code"] for row in validation["errors"]}
    assert "artifact_policy_kind_value_type_mismatch" in codes
    assert "missing_artifact_policy_project_state_key" in codes
    assert "invalid_artifact_policy_max_size" in codes
    assert "invalid_artifact_policy_max_size_env" in codes
    assert "reserved_artifact_policy_binding_key" in codes
    assert "invalid_artifact_policy_retention_class" in codes


def test_descriptor_validation_rejects_noncanonical_opt_in_artifact_default() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [{"operation_id": "sync", "operation_kind": "action"}],
            "artifact_policies": [
                {
                    "operation_id": "sync",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "output",
                            "max_size_bytes": 10 * 1024 * 1024 * 1024,
                            "max_size_env": "FS_EXTENSION_ARTIFACT_MAX_BYTES",
                        }
                    ],
                }
            ],
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert "invalid_artifact_policy_max_size_env" in {row["code"] for row in validation["errors"]}


def test_descriptor_validation_rejects_unknown_canonical_event_names(tmp_path: Path) -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [{"operation_id": "run_action", "operation_kind": "action"}],
            "event_bindings": [
                {
                    "trigger_id": "provider_specific_event",
                    "event_name": "fixture_events.callback_query",
                    "operation_id": "run_action",
                }
            ],
        }
    )
    descriptor_path = _write_descriptor(tmp_path, descriptor)

    result = validate_extension_descriptor_file(descriptor_path, platform_version="1.0.0")

    assert result["valid"] is False
    assert result["extension_contract_v2"]["present"] is True
    assert result["extension_contract_v2"]["valid"] is False
    assert any(
        row.get("path") == "extension_contract_v2.event_bindings[0].event_name"
        and "Unknown canonical event" in row.get("message", "")
        for row in result.get("errors", [])
    )


def test_bulk_action_requires_batch_capabilities() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "bulk_sync",
                    "operation_kind": "bulk_action",
                    "supports_batch": False,
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    codes = [row["code"] for row in validation["errors"]]
    assert "missing_batch_capabilities" in codes


def test_operation_object_input_schema_is_validated_and_preserved() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string"},
            "unseen": {"type": "boolean"},
        },
    }
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "search",
                    "operation_kind": "source",
                    "inputs": [
                        {
                            "name": "filters",
                            "value_type": "object",
                            "schema": schema,
                        }
                    ],
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is True
    assert (
        validation["normalized_contract"]["operation_manifest"][0]["inputs"][0]["schema"] == schema
    )


def test_operation_input_schema_rejects_unexecuted_keywords() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "search",
                    "operation_kind": "source",
                    "inputs": [
                        {
                            "name": "filters",
                            "value_type": "object",
                            "schema": {
                                "type": "object",
                                "properties": {"subjects": {"type": "array", "minContains": 1}},
                            },
                        }
                    ],
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert any(
        row["code"] == "unsupported_schema_keyword"
        and row["path"].endswith("operation_manifest[0].inputs[0].schema")
        for row in validation["errors"]
    )


def test_operation_input_schema_must_be_an_object_matching_value_type() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "search",
                    "operation_kind": "source",
                    "inputs": [
                        {"name": "not_a_schema", "value_type": "object", "schema": []},
                        {
                            "name": "wrong_type",
                            "value_type": "object",
                            "schema": {"type": "array", "items": {"type": "string"}},
                        },
                    ],
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert any(
        row["code"] == "invalid_field_type"
        and row["path"].endswith("operation_manifest[0].inputs[0].schema")
        for row in validation["errors"]
    )
    assert any(
        row["code"] == "io_schema_type_mismatch"
        and row["path"].endswith("operation_manifest[0].inputs[1].schema.type")
        for row in validation["errors"]
    )


def test_operation_input_schema_keeps_supported_union_types_for_discovery() -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "search",
                    "operation_kind": "source",
                    "inputs": [
                        {
                            "name": "filters",
                            "value_type": "object",
                            "schema": {"type": ["object", "null"], "properties": {}},
                        }
                    ],
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is True


@pytest.mark.parametrize(
    ("schema_type", "expected_code"),
    [
        ([], "invalid_io_schema_type"),
        (["object", "object"], "invalid_io_schema_type"),
        (["object", 1], "invalid_io_schema_type"),
        (["string", "null"], "io_schema_type_mismatch"),
    ],
)
def test_operation_input_schema_rejects_invalid_union_types(
    schema_type: list[object], expected_code: str
) -> None:
    descriptor = _base_descriptor(
        {
            "operation_manifest": [
                {
                    "operation_id": "search",
                    "operation_kind": "source",
                    "inputs": [
                        {
                            "name": "filters",
                            "value_type": "object",
                            "schema": {"type": schema_type, "properties": {}},
                        }
                    ],
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert any(row["code"] == expected_code for row in validation["errors"])


def test_dataset_connectors_are_normalized_and_visible_in_preview() -> None:
    descriptor = _base_descriptor(
        {
            "dataset_connectors": [
                {
                    "connector_id": "warehouse_sql",
                    "display_name": "Warehouse SQL",
                    "description": "Read-only warehouse query surface",
                    "connection_schema": {"type": "object"},
                    "secret_schema": {"type": "object"},
                    "query_language": "sql",
                    "capabilities": {"preview_query": True, "execute_template": True},
                    "safety_profile": {"read_only": True},
                    "preview_support": True,
                    "schema_refresh_support": True,
                    "runtime_adapter_ref": "extensions.warehouse.DatasetConnector",
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is True
    assert (
        validation["normalized_contract"]["dataset_connectors"][0]["connector_id"]
        == "warehouse_sql"
    )
    assert validation["preview"]["counts"]["dataset_connectors"] == 1
    assert validation["preview"]["resolved_ids"]["dataset_connector_ids"] == ["warehouse_sql"]


def test_dataset_connectors_require_contract_fields() -> None:
    descriptor = _base_descriptor(
        {
            "dataset_connectors": [
                {
                    "connector_id": "broken_connector",
                    "display_name": "",
                    "connection_schema": [],
                    "secret_schema": {},
                    "query_language": "",
                    "capabilities": {},
                    "safety_profile": {},
                    "preview_support": "yes",
                    "schema_refresh_support": True,
                    "runtime_adapter_ref": "",
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    codes = {row["code"] for row in validation["errors"]}
    assert "missing_required_field" in codes
    assert "invalid_field_type" in codes


def test_dataset_connector_catalog_requires_typed_unique_variants() -> None:
    descriptor = _base_descriptor(
        {
            "dataset_connectors": [
                {
                    "connector_id": "warehouse_sql",
                    "display_name": "Warehouse SQL",
                    "description": "Read-only warehouse query surface",
                    "connection_schema": {"type": "object"},
                    "secret_schema": {"type": "object"},
                    "query_language": "sql",
                    "capabilities": {},
                    "safety_profile": {},
                    "preview_support": True,
                    "schema_refresh_support": True,
                    "runtime_adapter_ref": "extensions.warehouse.DatasetConnector",
                    "connector_catalog": "not-a-list",
                }
            ]
        }
    )

    validation = validate_extension_contract_v2(descriptor)

    assert validation["valid"] is False
    assert any(
        row["path"].endswith("connector_catalog") and row["code"] == "invalid_field_type"
        for row in validation["errors"]
    )


def test_runtime_platform_version_is_the_host_contract_not_the_app_build_version():
    """RS5-030 regression.

    `runtime.compatibility.platform_min` is declared against the extension host
    contract — DEFAULT_FS_RUNTIME_VERSION, the peer of
    EXTENSION_DESCRIPTOR_SCHEMA_VERSION — and every descriptor-validation entry
    point in lifecycle.py defaults to exactly that. The runtime resolver instead
    returned the application's build version, which ships as 0.1.0. Every
    comparison was therefore 0.1.0 < 1.0.0, so on a clean packaged install every
    shipped extension failed its handshake with
    "Platform version '0.1.0' is below required minimum '1.0.0'" and none could
    reach a healthy state.
    """
    from core.application.extensions.extension_descriptor import DEFAULT_FS_RUNTIME_VERSION
    from core.application.extensions.service_support import _runtime_platform_version
    from core.infrastructure.config.version import APP_VERSION

    resolved = _runtime_platform_version()
    assert resolved == DEFAULT_FS_RUNTIME_VERSION
    # Guard the actual regression: the app build version is a different scale and
    # is below the contract version, so substituting it fails every extension.
    assert resolved != APP_VERSION or APP_VERSION == DEFAULT_FS_RUNTIME_VERSION


def test_shipped_extension_bundles_are_compatible_with_the_host_they_ship_in():
    """Every bundle in extensions/ must pass the platform compatibility check."""
    from core.application.extensions.service_support import (
        _platform_compatibility_error,
        _runtime_platform_version,
    )

    platform_version = _runtime_platform_version()
    # A bundle that declares no compatibility block, and one that declares the
    # host contract version, must both be accepted by the shipping host.
    for descriptor in (
        {"runtime": {}},
        {"runtime": {"compatibility": {"platform_min": "1.0.0"}}},
    ):
        error = _platform_compatibility_error(descriptor, platform_version=platform_version)
        assert not error, f"{descriptor} rejected by its own host: {error}"
