from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Inspect the core-only workspace, not a developer's local extension installs.
ROOT = Path(__file__).resolve().parents[2]

_PICKER_ADVERSARIAL_CASES = json.loads(
    Path(
        "assets/frontend/src/components/extensions/external-resource-picker-adversarial.json"
    ).read_text(encoding="utf-8")
)


def _apply_picker_adversarial_case(data: dict, case: dict) -> None:
    path = str(case["path"]).split(".")
    cursor: object = data
    for segment in path[:-1]:
        cursor = cursor[int(segment)] if isinstance(cursor, list) else cursor[segment]
    final = path[-1]
    if case["operation"] == "delete":
        if isinstance(cursor, list):
            del cursor[int(final)]
        else:
            del cursor[final]
    else:
        if isinstance(cursor, list):
            cursor[int(final)] = case.get("value")
        else:
            cursor[final] = case.get("value")


def _synthetic_wheel(
    wheel_dir: Path,
    *,
    name: str = "fsdemo",
    version: str = "1.2.3",
    requires_dist: list[str] | None = None,
) -> tuple[Path, str]:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    dist_info = f"{name}-{version}.dist-info"
    wheel_path = wheel_dir / f"{name}-{version}-py3-none-any.whl"
    metadata_lines = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
    ]
    for requirement in requires_dist or []:
        metadata_lines.append(f"Requires-Dist: {requirement}")
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}/__init__.py", f"VALUE = 'from-wheel'\n__version__ = '{version}'\n"
        )
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata_lines) + "\n")
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: flow-steward-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    return wheel_path, f"sha256:{digest}"


def _write_minimal_extension(
    bundle_dir: Path,
    *,
    extension_id: str,
    python_requirements: list[dict] | None = None,
    main_source: str | None = None,
    health_source: str | None = None,
    release_stage: str = "active",
) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "runtime": {"contract_version": "extension_host_v2"},
        "manifest_version": 2,
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": extension_id,
        "kind": "tool_provider",
        "version": "1.0.0",
        "display_name": "Python Deps Demo",
        "description": "Demo",
        "release_stage": release_stage,
        "vendor": {"name": "Acme Test"},
        "features": ["tool"],
        "required_scopes": ["extension:invoke"],
        "entrypoint": {"mode": "subprocess", "command": [sys.executable, "main.py"]},
        "health": {"command": [sys.executable, "health.py"], "timeout_seconds": 5},
        "ui_manifest": "ui/ui_manifest.yaml",
    }
    if python_requirements is not None:
        manifest["python_requirements"] = python_requirements
    import yaml

    (bundle_dir / "extension.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    (bundle_dir / "ui").mkdir(exist_ok=True)
    (bundle_dir / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n")
    (bundle_dir / "main.py").write_text(
        main_source or "import json\nprint(json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    (bundle_dir / "health.py").write_text(
        health_source or "print('ok')\n",
        encoding="utf-8",
    )


def test_descriptor_enrichment_preserves_validated_provider_metadata(tmp_path) -> None:
    from core.application.extensions.command_core import _descriptor_with_bundle_runtime
    from core.application.extensions.provider_registration import provider_row_for_extension

    bundle = tmp_path / "acme_provider"
    _write_minimal_extension(bundle, extension_id="acme.provider")
    normalized = {"extension_id": "acme.provider", "release_version": "1.0.0"}
    original = copy.deepcopy(normalized)

    enriched, errors = _descriptor_with_bundle_runtime(
        descriptor_path=bundle / "extension-descriptor.yaml",
        normalized_descriptor=normalized,
    )

    assert errors == []
    assert normalized == original
    assert enriched["extension_id"] == "acme.provider"
    assert enriched["release_version"] == "1.0.0"
    assert provider_row_for_extension(enriched) == {
        "provider_key": "acme.provider",
        "provider_type_key": "tool_provider",
        "display_name": "Python Deps Demo",
        "description": "Demo",
        "implementation_kind": "extension",
        "integration_key": "acme.provider",
        "config": {"extension_key": "acme.provider"},
        "features": ["tool"],
        "enabled": True,
    }


@pytest.mark.parametrize(
    ("extension_id", "version", "expected_code"),
    [
        ("acme.other", "1.0.0", "extension_folder_mismatch"),
        ("acme.provider", "2.0.0", "extension_runtime_version_mismatch"),
    ],
)
def test_descriptor_enrichment_rejects_mismatched_provider_identity(
    tmp_path, extension_id, version, expected_code
) -> None:
    from core.application.extensions.command_core import _descriptor_with_bundle_runtime

    bundle = tmp_path / "acme_provider"
    _write_minimal_extension(bundle, extension_id="acme.provider")
    enriched, errors = _descriptor_with_bundle_runtime(
        descriptor_path=bundle / "extension-descriptor.yaml",
        normalized_descriptor={"extension_id": extension_id, "release_version": version},
    )

    assert any(error.get("code") == expected_code for error in errors)
    assert enriched["extension_id"] == extension_id
    assert enriched["release_version"] == version
    assert "kind" not in enriched


def test_descriptor_enrichment_rejects_invalid_provider_manifest(tmp_path) -> None:
    from core.application.extensions.command_core import _descriptor_with_bundle_runtime

    bundle = tmp_path / "acme_provider"
    _write_minimal_extension(bundle, extension_id="acme.provider")
    (bundle / "extension.yaml").write_text("not: [valid", encoding="utf-8")
    enriched, errors = _descriptor_with_bundle_runtime(
        descriptor_path=bundle / "extension-descriptor.yaml",
        normalized_descriptor={"extension_id": "acme.provider", "release_version": "1.0.0"},
    )

    assert errors
    assert "kind" not in enriched


def test_compiled_runtime_exposes_contract_v2_artifact_policies(tmp_path) -> None:
    """Compilation surfaces whatever artifact policies a contract declares.

    Built here rather than read from a shipped bundle: what is under test is the
    compiler, and pointing it at a real extension makes this fail the day that
    extension changes its operations or moves to its own repository.
    """
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "acme_tooling"
    _write_minimal_extension(bundle, extension_id="acme.tooling")
    manifest = yaml.safe_load((bundle / "extension.yaml").read_text(encoding="utf-8"))
    manifest["runtime"] = {
        "contract_version": "extension_host_v2",
        "extension_contract_v2": {
            "operation_manifest": "contracts/operation_manifest.yaml",
            "artifact_policies": "contracts/artifact_policies.yaml",
        },
    }
    manifest["action_manifest"] = "ui/actions/actions.yaml"
    (bundle / "extension.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (bundle / "contracts").mkdir(parents=True, exist_ok=True)
    (bundle / "ui" / "actions").mkdir(parents=True, exist_ok=True)
    (bundle / "ui" / "actions" / "actions.yaml").write_text(
        yaml.safe_dump(
            {
                "actions": [
                    {
                        "action_id": "invoke_tool",
                        "title": "Invoke Tool",
                        "workflow_visible": True,
                        "parameters": [],
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (bundle / "contracts" / "operation_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "operations": [
                    {
                        "operation_id": "invoke_tool",
                        "title": "Invoke Tool",
                        "kind": "action",
                        "inputs": [],
                        "outputs": [],
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (bundle / "contracts" / "artifact_policies.yaml").write_text(
        yaml.safe_dump(
            {"artifact_policies": [{"operation_id": "invoke_tool", "direction": "output"}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid", compiled.get("compile_errors")
    policies = compiled["compiled_manifest"]["artifact_policies"]
    assert any(row.get("operation_id") == "invoke_tool" for row in policies)


def test_placeholder_extension_is_valid_but_not_enable_eligible(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_placeholder"
    _write_minimal_extension(
        bundle,
        extension_id="acme.placeholder",
        release_stage="placeholder",
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert compiled["release_stage"] == "placeholder"
    assert compiled["lifecycle"]["enable"] == "blocked"
    assert compiled["lifecycle"]["enable_reason"] == "placeholder"


def _compiled_manifest_with_callback(callback: dict) -> dict:
    return {
        "manifest_version": 2,
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "manifest_path": "/tmp/extension.yaml",
        "kind": "notification_provider",
        "release_stage": "placeholder",
        "features": ["notify"],
        "required_scopes": ["extension:invoke"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "query_definitions": [],
        "action_definitions": [
            {
                "action_id": "ingress_event",
                "handler": {"mode": "extension"},
            }
        ],
        "callbacks": [callback],
        "ui_manifest": {"version": "1", "pages": []},
    }


def test_scan_extensions_discovers_valid_yaml_manifest(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    bundle_dir = tmp_path / "extensions" / "acme_demo_task_provider"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": "acme.demo-task-provider",
                "kind": "tool_provider",
                "version": "1.0.0",
                "display_name": "Demo Task Provider",
                "description": "Compiler discovery fixture.",
                "vendor": {"name": "Acme"},
                "entrypoint": {"mode": "subprocess", "command": ["python3", "main.py"]},
                "health": {"command": ["python3", "-c", "print('ok')"]},
                "ui_manifest": "ui/ui_manifest.yaml",
                "features": ["tool"],
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "ui").mkdir()
    (bundle_dir / "ui" / "ui_manifest.yaml").write_text(
        "version: '1'\npages: []\n", encoding="utf-8"
    )
    (bundle_dir / "main.py").write_text("print('{}')\n", encoding="utf-8")

    results = scan_extensions(str(tmp_path / "extensions"))

    assert len(results) == 1
    assert results[0]["extension_key"] == "acme.demo-task-provider"
    assert results[0]["status"] == "valid"
    assert results[0]["entrypoint"]["command"] == ["python3", "main.py"]


def test_extension_runtime_compile_direct_import_scans_extensions(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime_compile import scan_extensions

    bundle_dir = tmp_path / "extensions" / "acme_direct_task_provider"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": "acme.direct-task-provider",
                "kind": "tool_provider",
                "version": "1.0.0",
                "display_name": "Direct Import Task Provider",
                "description": "Direct compiler import fixture.",
                "vendor": {"name": "Acme"},
                "entrypoint": {"mode": "subprocess", "command": ["python3", "main.py"]},
                "health": {"command": ["python3", "-c", "print('ok')"]},
                "ui_manifest": "ui/ui_manifest.yaml",
                "features": ["tool"],
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "ui").mkdir()
    (bundle_dir / "ui" / "ui_manifest.yaml").write_text(
        "version: '1'\npages: []\n", encoding="utf-8"
    )
    (bundle_dir / "main.py").write_text("print('{}')\n", encoding="utf-8")

    results = scan_extensions(str(tmp_path / "extensions"))

    assert len(results) == 1
    assert results[0]["extension_key"] == "acme.direct-task-provider"
    assert results[0]["status"] == "valid"


def test_runtime_manifest_discovery_is_yaml_only():
    from core.infrastructure.extension_runtime.extension_runtime import MANIFEST_FILENAMES

    assert MANIFEST_FILENAMES == ("extension.yaml",)


def test_manifest_v2_preserves_catalog_metadata_in_compiled_manifest(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_catalog_metadata"
    _write_minimal_extension(bundle, extension_id="acme.catalog-metadata")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["vendor"] = {
        "name": "Acme",
        "support_url": "https://acme.example/support",
        "website_url": "https://acme.example",
    }
    manifest["listing"] = {
        "category": "sales-automation",
        "tags": ["crm", "sales"],
        "summary": "Synchronize CRM records.",
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert compiled["compiled_manifest"]["vendor"]["name"] == "Acme"
    assert compiled["compiled_manifest"]["listing"] == manifest["listing"]


def test_compiled_manifest_projects_oauth_connection_types_for_ui_consumers(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_oauth_provider"
    _write_minimal_extension(bundle, extension_id="acme.oauth-provider")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = {
        "contract_version": "extension_host_v2",
        "extension_contract_v2": {
            "connection_types": [
                {
                    "connection_type_id": "acme_oauth_account",
                    "display_name": "Acme OAuth account",
                    "auth": {
                        "type": "oauth2",
                        "grant": "authorization_code",
                        "pkce": "required",
                        "authorization_endpoint": "https://accounts.example.test/oauth/authorize",
                        "token_endpoint": "https://accounts.example.test/oauth/token",
                        "scopes": {"required": ["profile"], "optional": []},
                        "client_credentials": {
                            "client_id": {"source": "platform_secret"},
                            "client_secret": {"source": "platform_secret"},
                        },
                        "token_placement": "secrets",
                        "secret_names": {"access_token": "access_token"},
                    },
                    "config_schema": {"type": "object", "additionalProperties": False},
                    "secret_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"access_token": {"type": "string"}},
                    },
                    "capabilities": {"test_connection": True},
                }
            ]
        },
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid", compiled.get("validation_errors")
    connection_types = compiled["compiled_manifest"]["connection_types"]
    assert connection_types[0]["connection_type_id"] == "acme_oauth_account"
    assert connection_types[0]["auth"]["type"] == "oauth2"


def test_compilation_rejects_invalid_extension_contract_connector_catalog(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_invalid_connector_catalog"
    _write_minimal_extension(bundle, extension_id="acme.invalid-connector-catalog")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = {
        "contract_version": "extension_host_v2",
        "extension_contract_v2": {
            "dataset_connectors": [
                {
                    "connector_id": "acme_sql",
                    "display_name": "Acme SQL",
                    "query_language": "sql",
                    "runtime_kind": "local_extension",
                    "runtime_adapter_ref": "main.py",
                    "connection_schema": {"type": "object"},
                    "secret_schema": {"type": "object"},
                    "capabilities": {},
                    "safety_profile": {},
                    "preview_support": True,
                    "schema_refresh_support": True,
                    "connector_catalog": "not-a-list",
                }
            ]
        },
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        issue.get("code") == "invalid_field_type"
        and str(issue.get("path") or "").endswith("connector_catalog")
        for issue in compiled["validation_errors"]
    )


def test_manifest_v2_rejects_unknown_top_level_fields(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_unknown_metadata"
    _write_minimal_extension(bundle, extension_id="acme.unknown-metadata")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["publsher"] = "misspelled"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        issue.get("code") == "unknown_manifest_field" and issue.get("field_path") == "publsher"
        for issue in compiled["compile_errors"]
    )


def test_external_effect_rate_limit_window_matches_cleanup_horizon():
    from core.application.extensions.compiled_manifest_policies import (
        _validate_external_effects,
    )

    issues: list[dict] = []
    _validate_external_effects(
        [
            {
                "operation_id": "publish_record",
                "effect_kind": "message_delivery",
                "channel": "api",
                "idempotency": "required",
                "test_mode_behavior": "suppress",
                "observability": {
                    "target_fields": ["record_ids"],
                    "target_kind": "remote_record",
                    "target_label": "record",
                    "artifact_field": "artifacts",
                    "payload_size_field": "payload_bytes",
                },
                "rate_limit": {
                    "window_seconds": 86401,
                    "account_max": 10,
                    "project_max": 10,
                    "connection_max": 10,
                },
            }
        ],
        issues=issues,
        manifest_path="/tmp/extension.yaml",
        action_ids={"publish_record"},
        query_ids=set(),
    )

    assert any(item.get("code") == "external_effect_rate_window_too_large" for item in issues)


def test_ui_manifest_select_fields_require_options(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_select_contract"
    _write_minimal_extension(bundle, extension_id="acme.select-contract")
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        json.dumps({"version": "1", "pages": [{"ref": "pages/config.yaml"}]}),
        encoding="utf-8",
    )
    (bundle / "ui" / "pages").mkdir(exist_ok=True)
    (bundle / "ui" / "pages" / "config.yaml").write_text(
        json.dumps(
            {
                "page_id": "config",
                "title": "Config",
                "components": [
                    {
                        "component_id": "connection",
                        "type": "connection_form",
                        "fields": [{"path": "security", "label": "Security", "widget": "select"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = compile_extension_bundle(bundle)
    errors = [
        *result.get("compile_errors", []),
        *result.get("validation_errors", []),
    ]

    assert any(item.get("code") == "missing_select_options" for item in errors)


def test_ui_manifest_select_options_require_value_label_and_unique_values(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_select_option_contract"
    _write_minimal_extension(bundle, extension_id="acme.select-option-contract")
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        json.dumps({"version": "1", "pages": [{"ref": "pages/config.yaml"}]}),
        encoding="utf-8",
    )
    (bundle / "ui" / "pages").mkdir(exist_ok=True)
    (bundle / "ui" / "pages" / "config.yaml").write_text(
        json.dumps(
            {
                "page_id": "config",
                "title": "Config",
                "components": [
                    {
                        "component_id": "connection",
                        "type": "connection_form",
                        "fields": [
                            {
                                "path": "security",
                                "label": "Security",
                                "widget": "select",
                                "options": [
                                    {},
                                    {"value": "ssl", "label": "SSL"},
                                    {"value": "ssl", "label": "Duplicate SSL"},
                                    {"value": "starttls", "label": ""},
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = compile_extension_bundle(bundle)
    errors = [
        *result.get("compile_errors", []),
        *result.get("validation_errors", []),
    ]
    error_codes = {str(item.get("code") or "") for item in errors}

    assert "missing_select_option_value" in error_codes
    assert "missing_select_option_label" in error_codes
    assert "duplicate_select_option_value" in error_codes


def test_ui_manifest_pre_save_result_mapping_targets_non_secret_config_fields(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_mapping_contract"
    _write_minimal_extension(bundle, extension_id="acme.mapping-contract")
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        json.dumps({"version": "1", "pages": [{"ref": "pages/config.yaml"}]}),
        encoding="utf-8",
    )
    (bundle / "ui" / "pages").mkdir(exist_ok=True)
    (bundle / "ui" / "pages" / "config.yaml").write_text(
        json.dumps(
            {
                "page_id": "config",
                "title": "Config",
                "components": [
                    {
                        "component_id": "connection",
                        "type": "connection_form",
                        "data": {
                            "config_fields": ["host", "server_identity"],
                            "secret_fields": ["api_token"],
                            "pre_save_actions": [
                                {
                                    "action_id": "discover_identity",
                                    "result_mapping": {
                                        "api_token": "result.api_token",
                                        "undeclared": "result.host",
                                    },
                                }
                            ],
                        },
                        "fields": [
                            {"path": "host", "label": "Host", "widget": "text"},
                            {"path": "server_identity", "label": "Identity", "widget": "text"},
                            {"path": "api_token", "label": "API token", "widget": "secret"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = compile_extension_bundle(bundle)
    errors = [
        *result.get("compile_errors", []),
        *result.get("validation_errors", []),
    ]
    error_codes = {str(item.get("code") or "") for item in errors}

    assert "secret_pre_save_result_mapping_target" in error_codes
    assert "invalid_pre_save_result_mapping_target" in error_codes


def test_compiled_manifest_rejects_external_effect_pre_save_action() -> None:
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "tool_provider",
            "features": ["tool"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [
                {"action_id": "discover_identity", "handler": {"mode": "extension"}}
            ],
            "external_effects": [
                {
                    "operation_id": "discover_identity",
                    "effect_kind": "remote_mutation",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "ui_manifest": {
                "version": "1",
                "pages": [
                    {
                        "page_id": "configuration",
                        "title": "Configuration",
                        "components": [
                            {
                                "component_id": "connection",
                                "type": "connection_form",
                                "data": {
                                    "config_fields": ["host"],
                                    "pre_save_actions": [
                                        {
                                            "action_id": "discover_identity",
                                            "result_mapping": {"host": "result.host"},
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    issue = next(
        item
        for item in validation["errors"]
        if item.get("code") == "external_effect_pre_save_action_forbidden"
    )
    assert "preflight" in str(issue.get("message") or "").lower()
    assert "read-only" in str(issue.get("message") or "").lower()


@pytest.mark.parametrize(
    "broken_field",
    [
        "action_missing",
        "action_handler",
        "action_visibility",
        "action_event_missing",
        "action_route_missing",
        "action_event_required",
        "action_route_required",
        "action_event_type",
        "action_route_type",
        "operation_missing",
        "operation_kind",
        "operation_visibility",
        "input_event",
        "input_route",
        "input_event_required",
        "input_route_required",
        "input_event_type",
        "input_route_type",
        "output_delivered",
        "output_provider_message_id",
        "output_delivered_type",
        "output_provider_message_id_type",
        "effect_missing",
        "effect_idempotency",
        "effect_test_mode",
        "effect_kind",
        "effect_channel",
    ],
)
def test_active_notification_provider_requires_reserved_delivery_contract(
    broken_field: str,
) -> None:
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = {
        "manifest_version": 2,
        "manifest_path": "/tmp/extension.yaml",
        "kind": "notification_provider",
        "release_stage": "active",
        "features": ["notify"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "query_definitions": [],
        "action_definitions": [
            {
                "action_id": "deliver_notification",
                "handler": {"mode": "extension"},
                "workflow_visible": False,
                "parameters": [
                    {"name": "event", "type": "object", "required": True},
                    {"name": "route", "type": "object", "required": True},
                ],
            }
        ],
        "operation_manifest": [
            {
                "operation_id": "deliver_notification",
                "operation_kind": "action",
                "workflow_visible": False,
                "inputs": [
                    {"name": "event", "value_type": "object", "required": True},
                    {"name": "route", "value_type": "object", "required": True},
                ],
                "outputs": [
                    {"name": "delivered", "value_type": "boolean"},
                    {"name": "provider_message_id", "value_type": "text"},
                ],
            }
        ],
        "external_effects": [
            {
                "operation_id": "deliver_notification",
                "effect_kind": "message_delivery",
                "channel": "acme_chat",
                "idempotency": "required",
                "test_mode_behavior": "suppress",
            }
        ],
        "ui_manifest": {"version": "1", "pages": []},
    }
    action = manifest["action_definitions"][0]
    operation = manifest["operation_manifest"][0]
    effect = manifest["external_effects"][0]
    if broken_field == "action_missing":
        manifest["action_definitions"] = []
    elif broken_field == "action_handler":
        action["handler"] = {"mode": "platform"}
    elif broken_field == "action_visibility":
        action["workflow_visible"] = True
    elif broken_field.startswith("action_"):
        field, defect = broken_field.removeprefix("action_").rsplit("_", 1)
        parameter = next(row for row in action["parameters"] if row["name"] == field)
        if defect == "missing":
            action["parameters"].remove(parameter)
        elif defect == "required":
            parameter["required"] = False
        else:
            parameter["type"] = "string" if field == "event" else "integer"
    elif broken_field == "operation_missing":
        manifest["operation_manifest"] = []
    elif broken_field == "operation_kind":
        operation["operation_kind"] = "query"
    elif broken_field == "operation_visibility":
        operation["workflow_visible"] = True
    elif broken_field.startswith("input_"):
        field, _, defect = broken_field.removeprefix("input_").partition("_")
        item = next(row for row in operation["inputs"] if row["name"] == field)
        if not defect:
            operation["inputs"].remove(item)
        elif defect == "required":
            item["required"] = False
        else:
            item["value_type"] = "text" if field == "event" else "integer"
    elif broken_field.startswith("output_"):
        output_field = broken_field.removeprefix("output_")
        defect = ""
        if output_field.endswith("_type"):
            output_field = output_field.removesuffix("_type")
            defect = "type"
        item = next(row for row in operation["outputs"] if row["name"] == output_field)
        if defect:
            item["value_type"] = "object" if output_field == "delivered" else "boolean"
        else:
            operation["outputs"].remove(item)
    elif broken_field == "effect_missing":
        manifest["external_effects"] = []
    elif broken_field == "effect_idempotency":
        effect["idempotency"] = "optional"
    elif broken_field == "effect_test_mode":
        effect["test_mode_behavior"] = "invoke"
    elif broken_field == "effect_kind":
        effect["effect_kind"] = "remote_mutation"
    elif broken_field == "effect_channel":
        effect["channel"] = ""

    validation = validate_compiled_manifest(manifest)

    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "invalid_notification_provider_delivery_contract" in error_codes


def test_extension_manifest_standard_documents_python_requirements():
    import yaml

    standard_path = Path("docs/standards/extension-manifest-standard.yaml")
    standard = yaml.safe_load(standard_path.read_text(encoding="utf-8"))

    assert "python_requirements" in standard
    contract = standard["x-validator-contract"]["python_requirements"]
    assert contract["field"] == "python_requirements"
    assert contract["wheels_only"] is True
    assert contract["full_transitive_closure_required"] is True
    assert contract["install_time_resolution"] == "forbidden"
    assert contract["runtime_pip"] == "forbidden"
    effects_contract = standard["x-validator-contract"]["external_effects"]
    assert "observability" in effects_contract["item_shape"]
    assert "rate_limit" in effects_contract["item_shape"]
    assert (
        effects_contract["notification_provider_reserved_operation"]["operation_id"]
        == "deliver_notification"
    )
    assert effects_contract["connection_form_pre_save_actions"] == {
        "purpose": "read-only preflight",
        "external_effect_operations": "forbidden",
    }


def test_python_requirements_roundtrip_scan_materialize_and_invoke(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import (
        compile_extension_bundle,
        run_extension_health,
        run_extension_invocation,
        scan_extensions,
    )
    from core.infrastructure.extension_runtime.python_dependencies import (
        materialize_python_requirements,
    )

    root = tmp_path / "extensions"
    bundle = root / "acme_python_deps"
    _wheel, digest = _synthetic_wheel(bundle / "wheels")
    requirement = {"name": "fsdemo", "version": "1.2.3", "hashes": [digest]}
    _write_minimal_extension(
        bundle,
        extension_id="acme.python-deps",
        python_requirements=[requirement],
        main_source=(
            "import fsdemo, json\n"
            "print(json.dumps({'ok': True, 'version': fsdemo.__version__, "
            "'value': fsdemo.VALUE}))\n"
        ),
        health_source="import fsdemo\nprint(fsdemo.__version__)\n",
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert compiled["compiled_manifest"]["python_requirements"] == [requirement]

    audit = materialize_python_requirements(
        bundle_root=bundle,
        compiled_manifest=compiled["compiled_manifest"],
    )
    assert audit["dependencies"][0]["name"] == "fsdemo"
    installed_site = Path(audit["site_dir"])
    assert list(installed_site.rglob("direct_url.json")) == []
    assert list(installed_site.rglob("*.pyc")) == []
    assert all(
        "direct_url.json" not in record.read_text(encoding="utf-8")
        for record in installed_site.glob("*.dist-info/RECORD")
    )

    scanned = scan_extensions(str(root))
    assert scanned[0]["compiled_manifest"]["python_requirements"] == [requirement]

    health = run_extension_health(compiled)
    assert health["status"] == "healthy"
    assert "1.2.3" in health["stdout"]

    invocation = run_extension_invocation(compiled, request_payload={}, timeout_seconds=5)
    assert invocation["status"] == "succeeded"
    assert invocation["response"] == {
        "ok": True,
        "version": "1.2.3",
        "value": "from-wheel",
    }


@pytest.mark.parametrize(
    ("python_requirements", "expected_code"),
    [
        (
            [{"name": "DemoPkg", "version": "1.0.0", "hashes": ["sha256:" + "0" * 64]}],
            "python_requirement_name_not_normalized",
        ),
        (
            [{"name": "demo", "version": ">=1.0", "hashes": ["sha256:" + "0" * 64]}],
            "python_requirement_version_not_exact",
        ),
        (
            [{"name": "demo", "version": "1.0.*", "hashes": ["sha256:" + "0" * 64]}],
            "python_requirement_version_not_exact",
        ),
        ([{"name": "demo", "version": "1.0.0", "hashes": []}], "missing_python_requirement_hashes"),
        (
            [
                {"name": "demo", "version": "1.0.0", "hashes": ["sha256:" + "0" * 64]},
                {"name": "demo", "version": "1.0.1", "hashes": ["sha256:" + "1" * 64]},
            ],
            "duplicate_python_requirement",
        ),
    ],
)
def test_compile_extension_bundle_rejects_invalid_python_requirements(
    tmp_path, python_requirements, expected_code
):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_bad_deps"
    _write_minimal_extension(
        bundle,
        extension_id="acme.bad-deps",
        python_requirements=python_requirements,
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    codes = {
        str(item.get("code") or "")
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    }
    assert expected_code in codes


def test_python_requirements_accept_pep440_epoch_versions():
    from core.infrastructure.extension_runtime.python_requirement_contract import (
        normalize_python_requirements,
    )

    normalized, issues = normalize_python_requirements(
        [
            {
                "name": "demo",
                "version": "1!2.0",
                "hashes": ["sha256:" + "0" * 64],
            }
        ]
    )

    assert issues == []
    assert normalized[0]["version"] == "1!2.0"


def test_compile_extension_bundle_rejects_missing_strict_mode_wheel(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_missing_wheel"
    _write_minimal_extension(
        bundle,
        extension_id="acme.missing-wheel",
        python_requirements=[
            {"name": "fsdemo", "version": "1.2.3", "hashes": ["sha256:" + "0" * 64]}
        ],
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "missing_python_requirement_wheel"
        for item in compiled["validation_errors"]
    )


def test_compile_extension_bundle_rejects_incomplete_python_requirement_closure(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_closure"
    _wheel, digest = _synthetic_wheel(
        bundle / "wheels",
        name="fsdemo",
        version="1.2.3",
        requires_dist=["depchild>=1"],
    )
    _write_minimal_extension(
        bundle,
        extension_id="acme.closure",
        python_requirements=[{"name": "fsdemo", "version": "1.2.3", "hashes": [digest]}],
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "python_requirement_closure_incomplete"
        for item in compiled["validation_errors"]
    )


def test_compile_extension_bundle_rejects_incompatible_transitive_requirement_version(
    tmp_path,
):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_closure_version"
    _parent_wheel, parent_digest = _synthetic_wheel(
        bundle / "wheels",
        name="parentpkg",
        version="1.0.0",
        requires_dist=["depchild>=2"],
    )
    _child_wheel, child_digest = _synthetic_wheel(
        bundle / "wheels",
        name="depchild",
        version="1.0.0",
    )
    _write_minimal_extension(
        bundle,
        extension_id="acme.closure-version",
        python_requirements=[
            {"name": "parentpkg", "version": "1.0.0", "hashes": [parent_digest]},
            {"name": "depchild", "version": "1.0.0", "hashes": [child_digest]},
        ],
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "python_requirement_closure_version_mismatch"
        for item in compiled["validation_errors"]
    )


def test_extension_runtime_boundary_rejects_core_import_for_new_extension(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_core_reach"
    _write_minimal_extension(
        bundle,
        extension_id="acme.core-reach",
        main_source="from core.application.extensions.url_safety import open_pinned_url\nprint('bad')\n",
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_private_core_import"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


def test_extension_runtime_boundary_rejects_sys_alias_and_dynamic_core_import(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_dynamic_core_reach"
    _write_minimal_extension(
        bundle,
        extension_id="acme.dynamic-core-reach",
        main_source=(
            "import sys as s\n"
            "s.path.insert(0, '/tmp/private-core')\n"
            "__import__('core.infrastructure.extension_runtime.extension_runtime')\n"
        ),
    )

    compiled = compile_extension_bundle(bundle)

    issues = [*compiled["compile_errors"], *compiled["validation_errors"]]
    codes = {item.get("code") for item in issues}
    assert compiled["status"] == "invalid"
    assert "extension_sys_path_manipulation" in codes
    assert "extension_private_core_import" in codes


@pytest.mark.parametrize(
    "main_source",
    [
        (
            "import importlib as il\nil.import_module('core.infrastructure.extension_runtime.extension_runtime')\n"
        ),
        (
            "from importlib import import_module as im\n"
            "im('core.infrastructure.extension_runtime.extension_runtime')\n"
        ),
    ],
)
def test_extension_runtime_boundary_rejects_importlib_dynamic_core_import(
    tmp_path,
    main_source,
):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_importlib_core_reach"
    _write_minimal_extension(
        bundle,
        extension_id="acme.importlib-core-reach",
        main_source=main_source,
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_private_core_import"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


def test_extension_runtime_boundary_rejects_sys_path_import_alias(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_sys_path_alias"
    _write_minimal_extension(
        bundle,
        extension_id="acme.sys-path-alias",
        main_source="from sys import path as p\np.insert(0, '/tmp/private-core')\n",
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_sys_path_manipulation"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


@pytest.mark.parametrize(
    "main_source",
    [
        "import sys\nsys.path = ['/tmp/private-core'] + sys.path\n",
        "import sys\nsys.path += ['/tmp/private-core']\n",
        "import sys\np = sys.path\np.insert(0, '/tmp/private-core')\n",
        "import sys\np = sys.path\np[:] = ['/tmp/private-core'] + p\n",
        "import sys as s\np = s.path\np += ['/tmp/private-core']\n",
    ],
)
def test_extension_runtime_boundary_rejects_sys_path_assignment_forms(
    tmp_path,
    main_source,
):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_sys_path_assignment"
    _write_minimal_extension(
        bundle,
        extension_id="acme.sys-path-assignment",
        main_source=main_source,
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_sys_path_manipulation"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


def test_extension_runtime_boundary_scans_manifest_python_entrypoint_in_ignored_dir(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_script_entrypoint"
    _write_minimal_extension(bundle, extension_id="acme.script-entrypoint")
    scripts_dir = bundle / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "main.py").write_text(
        "from core.application.extensions.url_safety import open_pinned_url\nprint('bad')\n",
        encoding="utf-8",
    )
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = {"mode": "subprocess", "command": [sys.executable, "scripts/main.py"]}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_private_core_import"
        and str(item.get("file_path") or "").endswith("scripts/main.py")
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


@pytest.mark.parametrize(
    ("script_ref", "expected_code"),
    (
        ("../evil.py", "extension_command_path_escape"),
        ("/tmp/evil.py", "extension_command_path_escape"),
        ("../evil", "extension_command_unsupported"),
        ("/tmp/evil", "extension_command_unsupported"),
    ),
)
def test_extension_runtime_boundary_rejects_python_command_outside_bundle(
    tmp_path,
    script_ref,
    expected_code,
):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_escaping_entrypoint"
    _write_minimal_extension(bundle, extension_id="acme.escaping-entrypoint")
    (bundle.parent / "evil.py").write_text("print('outside')\n", encoding="utf-8")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = {
        "mode": "subprocess",
        "command": [sys.executable, script_ref],
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == expected_code and item.get("field_path") == "entrypoint.command"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


def test_extension_runtime_boundary_rejects_python_command_symlink_outside_bundle(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_escaping_entrypoint"
    _write_minimal_extension(bundle, extension_id="acme.escaping-entrypoint")
    outside = tmp_path / "evil.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    scripts = bundle / "scripts"
    scripts.mkdir()
    (scripts / "evil.py").symlink_to(outside)
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = {
        "mode": "subprocess",
        "command": [sys.executable, "scripts/evil.py"],
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_command_path_escape"
        and item.get("field_path") == "entrypoint.command"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


@pytest.mark.parametrize(
    "command",
    (
        ["sh", "../../evil.sh"],
        ["env", "python3", "../../evil.py"],
        ["../../evil.sh"],
    ),
)
def test_extension_runtime_boundary_rejects_non_python_command_path_escape(
    tmp_path,
    command,
):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_escaping_entrypoint"
    _write_minimal_extension(bundle, extension_id="acme.escaping-entrypoint")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = {"mode": "subprocess", "command": command}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_command_unsupported"
        and item.get("field_path") == "entrypoint.command"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


@pytest.mark.parametrize(
    ("command_name", "command"),
    (
        ("bare-executable", ["curl", "https://example.invalid/side-effect"]),
        (
            "env-wrapper",
            [
                "/usr/bin/env",
                "PYTHONPATH=/app",
                "python3",
                "-c",
                "print('wrapped')",
            ],
        ),
        ("shell-wrapper", ["/bin/sh", "-c", "printf wrapped"]),
        ("module-execution", ["python3", "-m", "http.server"]),
    ),
)
@pytest.mark.parametrize(
    ("manifest_key", "expected_field"),
    (("entrypoint", "entrypoint.command"), ("health", "health.command")),
)
def test_extension_runtime_boundary_rejects_unsupported_command_grammar(
    tmp_path,
    command_name,
    command,
    manifest_key,
    expected_field,
):
    del command_name
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_unsupported_command"
    _write_minimal_extension(bundle, extension_id="acme.unsupported-command")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest[manifest_key] = {"mode": "subprocess", "command": command}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_command_unsupported"
        and item.get("field_path") == expected_field
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


@pytest.mark.parametrize(
    "entrypoint",
    (
        {"mode": "subprocess"},
        {"mode": "subprocess", "command": []},
    ),
)
def test_extension_runtime_boundary_requires_local_entrypoint_command(tmp_path, entrypoint):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_missing_command"
    _write_minimal_extension(bundle, extension_id="acme.missing-command")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = entrypoint
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_command_required"
        and item.get("field_path") == "entrypoint.command"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


def test_extension_runtime_boundary_allows_commandless_remote_https_entrypoint(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_remote_runtime"
    _write_minimal_extension(bundle, extension_id="acme.remote-runtime")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = {
        "mode": "remote_https",
        "remote": {"base_url": "https://runtime.example.com"},
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert not any(
        item.get("code") == "extension_command_required"
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


def test_extension_runtime_boundary_preserves_scalar_entrypoint_command(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_scalar_command"
    _write_minimal_extension(bundle, extension_id="acme.scalar-command")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = {"mode": "subprocess", "command": "python3 main.py"}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert compiled["entrypoint"]["command"] == ["python3", "main.py"]


@pytest.mark.parametrize(
    "command",
    (
        ["curl", "https://example.invalid/side-effect"],
        ["/usr/bin/env", "PYTHONPATH=/app", "python3", "-c", "print('wrapped')"],
        ["/bin/sh", "-c", "printf wrapped"],
        ["python3", "-m", "http.server"],
    ),
)
@pytest.mark.parametrize("runtime_kind", ("entrypoint", "health"))
def test_extension_runtime_rejects_unsupported_command_before_subprocess(
    tmp_path,
    command,
    runtime_kind,
):
    from core.infrastructure.extension_runtime.extension_runtime import (
        run_extension_health,
        run_extension_invocation,
    )

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "bundle_root": str(tmp_path),
        "entrypoint": {"command": command},
        "healthcheck": {"command": command},
    }
    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped"
    ) as run_subprocess:
        if runtime_kind == "entrypoint":
            result = run_extension_invocation(extension, request_payload={})
        else:
            result = run_extension_health(extension)

    assert result["status"] == "failed"
    assert "approved Python" in result["detail"]
    run_subprocess.assert_not_called()


@pytest.mark.parametrize("runtime_kind", ("entrypoint", "health"))
def test_extension_runtime_rejects_file_command_without_bundle_root_before_subprocess(
    runtime_kind,
):
    from core.infrastructure.extension_runtime.extension_runtime import (
        run_extension_health,
        run_extension_invocation,
    )

    runtime_file = Path.cwd() / "core" / "entrypoints" / "api" / "app_factory.py"
    assert runtime_file.is_file()
    command = [sys.executable, str(runtime_file.relative_to(Path.cwd()))]
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "entrypoint": {"command": command},
        "healthcheck": {"command": command},
    }
    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped"
    ) as run_subprocess:
        if runtime_kind == "entrypoint":
            result = run_extension_invocation(extension, request_payload={})
        else:
            result = run_extension_health(extension)

    assert result["status"] == "failed"
    assert "bundle root" in result["detail"].lower()
    run_subprocess.assert_not_called()


def test_extension_runtime_inline_command_does_not_require_bundle_root():
    from core.infrastructure.extension_runtime.boundary import (
        validate_extension_runtime_command,
    )

    assert (
        validate_extension_runtime_command(
            bundle_root="",
            command=[sys.executable, "-c", "print('ok')"],
            field_path="entrypoint.command",
        )
        == []
    )


@pytest.mark.parametrize(
    "launcher",
    ("python", "python3", "python3.12", "pypy", "pypy3", sys.executable),
)
def test_extension_runtime_command_grammar_allows_approved_python_launchers(
    tmp_path,
    launcher,
):
    from core.infrastructure.extension_runtime.boundary import (
        validate_extension_runtime_command,
    )

    script = tmp_path / "main.py"
    script.write_text("print('{}')\n", encoding="utf-8")

    assert (
        validate_extension_runtime_command(
            bundle_root=tmp_path,
            command=[launcher, "main.py"],
            field_path="entrypoint.command",
        )
        == []
    )


@pytest.mark.parametrize(
    ("manifest_key", "source", "expected_label"),
    [
        (
            "entrypoint",
            "import core.infrastructure.extension_runtime.extension_runtime",
            "entrypoint.command",
        ),
        (
            "health",
            "__import__('core.infrastructure.extension_runtime.extension_runtime')",
            "health.command",
        ),
    ],
)
def test_extension_runtime_boundary_scans_manifest_python_inline_command(
    tmp_path,
    manifest_key,
    source,
    expected_label,
):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_inline_command"
    _write_minimal_extension(bundle, extension_id="acme.inline-command")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest[manifest_key] = {
        "mode": "subprocess",
        "command": [sys.executable, "-c", source],
    }
    if manifest_key == "health":
        manifest[manifest_key]["timeout_seconds"] = 5
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    assert any(
        item.get("code") == "extension_private_core_import"
        and str(item.get("file_path") or "").endswith(f"extension.yaml:{expected_label}")
        for item in [*compiled["compile_errors"], *compiled["validation_errors"]]
    )


def test_extension_runtime_boundary_allows_public_sdk_imports(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_sdk_import"
    _write_minimal_extension(
        bundle,
        extension_id="acme.sdk-import",
        main_source=(
            "from flowsteward_extension_sdk import ArtifactDatasetPlane, open_pinned_url\n"
            "print(ArtifactDatasetPlane, open_pinned_url)\n"
        ),
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"


def test_extension_runtime_boundary_allows_external_sqlite_connector_catalog(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_toolbox_sqlite"
    _write_minimal_extension(bundle, extension_id="acme.toolbox-sqlite")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = {
        "contract_version": "extension_host_v2",
        "extension_contract_v2": {
            "dataset_connectors": [
                {
                    "connector_id": "toolbox_database",
                    "display_name": "Fixture MCP database source",
                    "query_language": "sql",
                    "runtime_kind": "local_extension",
                    "runtime_adapter_ref": "main.py",
                    "connection_schema": {"type": "object"},
                    "secret_schema": {"type": "object"},
                    "capabilities": {},
                    "safety_profile": {},
                    "preview_support": True,
                    "schema_refresh_support": True,
                    "connector_catalog": [
                        {
                            "connector_type_id": "toolbox_sqlite",
                            "source_kind": "sqlite",
                            "display_name": "SQLite",
                            "query_language": "sql",
                        }
                    ],
                }
            ]
        },
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert not any(
        issue.get("code") == "extension_sqlite_datasource" for issue in compiled["compile_errors"]
    )


def test_public_extension_sdk_is_importable_without_repo_root_on_pythonpath():
    from core.infrastructure.extension_runtime.python_dependencies import (
        extension_sdk_pythonpath_root,
    )

    script = """
import json
from flowsteward_extension_sdk import (
    ArtifactDatasetPlane,
    assert_safe_remote_http_url,
    runtime_context_from_payload,
)

ctx = runtime_context_from_payload({"contract_version": "extension_host_v2", "runtime_context": {"group_id": None, "for_each_id": None, "step_id": None, "step_execution_id": None, "artifact_storage": {"backend": "memory"}}})
plane = ArtifactDatasetPlane()
handle, meta = plane.create_artifact(
    owner_scope={"account_id": "acc"},
    run_id="run",
    step_id="step",
    filename="demo.txt",
    content_type="text/plain",
    body=b"hello",
)
blocked = False
try:
    assert_safe_remote_http_url("http://127.0.0.1/internal", purpose="test")
except ValueError:
    blocked = True
print(json.dumps({
    "artifact_id": handle.artifact_id,
    "size_bytes": meta["size_bytes"],
    "artifact_storage": ctx.artifact_storage,
    "blocked": blocked,
}))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(extension_sdk_pythonpath_root()),
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/tmp",
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["artifact_id"].startswith("art_")
    assert payload["size_bytes"] == 5
    assert payload["artifact_storage"] == {"backend": "memory"}
    assert payload["blocked"] is True


def test_public_sdk_migration_envelope_exposes_only_non_secret_json():
    from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import (
        MigrationEnvelope,
        migration_result,
    )

    envelope = MigrationEnvelope.from_payload(
        {
            "envelope_version": "extension_data_migration_v1",
            "source_version": 1,
            "target_version": 2,
            "target_type": "project_state",
            "record_id": "state-1",
            "namespace": "preferences",
            "value": {"theme": "dark"},
        }
    )

    assert envelope.value == {"theme": "dark"}
    assert migration_result({"theme": "light"}) == {"value": {"theme": "light"}}


def test_public_sdk_migration_envelope_accepts_workspace_connection_config():
    from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import MigrationEnvelope

    envelope = MigrationEnvelope.from_payload(
        {
            "envelope_version": "extension_data_migration_v1",
            "source_version": 1,
            "target_version": 2,
            "target_type": "workspace_data_connection_config",
            "record_id": "workspace-connection-1",
            "value": {"host": "db.internal"},
        }
    )

    assert envelope.target_type == "workspace_data_connection_config"


def test_public_sdk_migration_envelope_rejects_secret_material():
    from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import MigrationEnvelope

    with pytest.raises(ValueError, match="secret"):
        MigrationEnvelope.from_payload(
            {
                "envelope_version": "extension_data_migration_v1",
                "source_version": 1,
                "target_version": 2,
                "target_type": "project_connection_config",
                "record_id": "conn-1",
                "value": {"secret_refs": {"token": "ref"}},
            }
        )


def test_public_sdk_write_artifact_stream_uses_content_length_for_http_put(monkeypatch):
    from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import artifacts

    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b""

    def fake_open_pinned_url(request, *, timeout_seconds, purpose):
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data.read() if hasattr(request.data, "read") else request.data
        captured["timeout_seconds"] = timeout_seconds
        captured["purpose"] = purpose
        return Response()

    monkeypatch.setattr(artifacts, "open_pinned_url", fake_open_pinned_url)

    payload = {
        "artifacts": {
            "outputs": [
                {
                    "artifact_id": "art_http_1",
                    "artifact_handle": "artifact:art_http_1",
                    "binding_key": "output",
                    "content_type": "text/plain",
                    "access": {
                        "transport": "presigned_url",
                        "mode": "write",
                        "upload_url": "https://storage.example.com/upload?sig=1",
                        "required_headers": {"x-amz-meta-flow": "test"},
                        "max_size_bytes": 64,
                    },
                }
            ]
        }
    }

    result = artifacts.write_artifact_stream(
        payload,
        [b"he", "", memoryview(b"llo")],
        binding_key="output",
        content_type="text/plain",
    )

    assert captured["body"] == b"hello"
    assert captured["headers"]["Content-length"] == "5"
    assert "Transfer-encoding" not in captured["headers"]
    assert captured["headers"]["Content-type"] == "text/plain"
    assert captured["headers"]["X-amz-meta-flow"] == "test"
    assert result["size_bytes"] == 5
    assert result["sha256"] == hashlib.sha256(b"hello").hexdigest()

    captured.clear()
    result = artifacts.write_artifact_bytes(
        payload,
        b"world!",
        binding_key="output",
        content_type="text/plain",
    )

    assert captured["body"] == b"world!"
    assert captured["headers"]["Content-length"] == "6"
    assert "Transfer-encoding" not in captured["headers"]
    assert result["size_bytes"] == 6
    assert result["sha256"] == hashlib.sha256(b"world!").hexdigest()

    with pytest.raises(artifacts.ArtifactAccessError, match="stream chunks"):
        artifacts.write_artifact_stream(
            payload,
            b"raw",
            binding_key="output",
            content_type="text/plain",
        )


def test_public_sdk_file_url_upload_is_atomic_and_escapes_special_names(tmp_path):
    from urllib.parse import unquote, urlparse

    from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import artifacts

    storage = artifacts.LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    upload_url = storage.presign("outputs/report #50%?.txt", method="PUT")
    upload_path = Path(unquote(urlparse(upload_url).path))
    assert upload_path.name == "report #50%?.txt"

    payload = {
        "artifacts": {
            "outputs": [
                {
                    "artifact_id": "art_special",
                    "artifact_handle": "artifact:art_special",
                    "binding_key": "output",
                    "content_type": "text/plain",
                    "access": {
                        "transport": "presigned_url",
                        "mode": "write",
                        "upload_url": upload_url,
                        "max_size_bytes": 4,
                    },
                }
            ]
        }
    }

    with pytest.raises(artifacts.ArtifactAccessError, match="size limit"):
        artifacts.write_artifact_stream(
            payload,
            [b"12", b"345"],
            binding_key="output",
            content_type="text/plain",
        )

    assert not upload_path.exists()
    assert not list(upload_path.parent.glob(".*"))

    result = artifacts.write_artifact_bytes(
        payload,
        b"ok",
        binding_key="output",
        content_type="text/plain",
    )

    assert upload_path.read_bytes() == b"ok"
    assert result["size_bytes"] == 2

    with pytest.raises(ValueError, match="retention_class"):
        artifacts.ArtifactDatasetPlane().create_artifact(
            owner_scope={"account_id": "acc_default", "project_id": 7},
            run_id="run_1",
            step_id="step_a",
            filename="report.txt",
            content_type="text/plain",
            retention_class="forever",
            body=b"blocked",
        )


def test_public_extension_sdk_does_not_import_private_core_modules():
    sdk_root = Path("core/infrastructure/extension_sdk/public/flowsteward_extension_sdk")

    for path in sdk_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "from core." not in source
        assert "import core." not in source


def test_runtime_record_resolver_accepts_uuid_install_ids():
    from unittest.mock import patch

    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    install_id = "5cc90460-491e-45e3-a47c-c1110afc37db"
    expected = ({"install_id": install_id}, object())

    with patch.object(
        helpers,
        "resolve_extension_runtime_record_by_install_id",
        return_value=expected,
    ) as resolve_install:
        resolved = helpers.resolve_extension_runtime_record(
            install_id,
            get_logger_only=lambda: object(),
            account_id="00000000-0000-0000-0000-000000000002",
        )

    assert resolved == expected
    resolve_install.assert_called_once_with(
        install_id,
        get_logger_only=resolve_install.call_args.kwargs["get_logger_only"],
        account_id="00000000-0000-0000-0000-000000000002",
    )


def test_download_wheels_for_requirements_uses_isolated_pip_env(tmp_path, monkeypatch):
    from core.infrastructure.extension_runtime.python_dependencies import (
        download_wheels_for_requirements,
    )

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setenv("PIP_FIND_LINKS", "https://evil.example/wheels")
    monkeypatch.setenv("PIP_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setattr(subprocess, "run", fake_run)

    download_wheels_for_requirements(
        requirements=[{"name": "demo", "version": "1.0.0", "hashes": ["sha256:" + "0" * 64]}],
        destination=tmp_path / "wheels",
    )

    command = [str(item) for item in captured["command"]]
    env = captured["env"]
    assert "--isolated" in command
    assert "--no-input" in command
    assert "--index-url" in command
    assert "https://pypi.org/simple" in command
    assert isinstance(env, dict)
    assert env["PIP_CONFIG_FILE"]
    assert "PIP_EXTRA_INDEX_URL" not in env
    assert "PIP_FIND_LINKS" not in env
    assert "PIP_INDEX_URL" not in env


def test_lock_direct_requirements_uses_isolated_pip_env(tmp_path, monkeypatch):
    from core.infrastructure.extension_runtime.python_dependencies import (
        lock_direct_requirements,
    )

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    requirements_in = tmp_path / "requirements.in"
    requirements_in.write_text("demo==1.0.0\n", encoding="utf-8")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setenv("PIP_FIND_LINKS", "https://evil.example/wheels")
    monkeypatch.setenv("PIP_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setattr(subprocess, "run", fake_run)

    locked = lock_direct_requirements(input_path=requirements_in)

    command = [str(item) for item in captured["command"]]
    env = captured["env"]
    assert locked == []
    assert "--isolated" in command
    assert "--no-input" in command
    assert "--index-url" in command
    assert "https://pypi.org/simple" in command
    assert isinstance(env, dict)
    assert env["PIP_CONFIG_FILE"]
    assert "PIP_EXTRA_INDEX_URL" not in env
    assert "PIP_FIND_LINKS" not in env
    assert "PIP_INDEX_URL" not in env


def test_lock_direct_requirements_aggregates_hashes_for_multiple_platform_wheels(
    tmp_path, monkeypatch
):
    from core.infrastructure.extension_runtime.python_dependencies import (
        lock_direct_requirements,
    )

    wheel_payloads: dict[str, bytes] = {
        "manylinux2014_x86_64": b"x86 wheel",
        "manylinux2014_aarch64": b"arm wheel",
    }
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        _ = kwargs
        command_text = [str(item) for item in command]
        calls.append(command_text)
        dest = Path(command_text[command_text.index("--dest") + 1])
        platform = command_text[command_text.index("--platform") + 1]
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"nativepkg-1.0.0-cp311-cp311-{platform}.whl").write_bytes(wheel_payloads[platform])

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    requirements_in = tmp_path / "requirements.in"
    requirements_in.write_text("nativepkg==1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", fake_run)

    locked = lock_direct_requirements(
        input_path=requirements_in,
        platforms=["manylinux2014_x86_64", "manylinux2014_aarch64"],
    )

    assert len(calls) == 2
    assert locked == [
        {
            "name": "nativepkg",
            "version": "1.0.0",
            "hashes": sorted(
                f"sha256:{hashlib.sha256(payload).hexdigest()}"
                for payload in wheel_payloads.values()
            ),
        }
    ]


def test_scan_extensions_marks_missing_manifest_invalid(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    bundle_dir = tmp_path / "extensions" / "demo_notifier"
    bundle_dir.mkdir(parents=True)

    results = scan_extensions(str(tmp_path / "extensions"))

    assert len(results) == 1
    assert results[0]["extension_key"] == "demo_notifier"
    assert results[0]["status"] == "invalid"
    assert "No manifest file found" in results[0]["scan_error"]


def test_scan_extensions_skips_runtime_cache_only_directories(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    root = tmp_path / "extensions"
    cache_only = root / "fixture_provider"
    (cache_only / "handlers" / "__pycache__").mkdir(parents=True)
    (cache_only / "handlers" / "__pycache__" / "runtime.cpython-312.pyc").write_bytes(b"\0")

    broken = root / "broken_without_manifest"
    broken.mkdir(parents=True)

    results = scan_extensions(str(root))
    keyed = {str(row.get("extension_key") or ""): row for row in results}

    assert "fixture_provider" not in keyed
    assert keyed["broken_without_manifest"]["status"] == "invalid"
    assert "No manifest file found" in keyed["broken_without_manifest"]["scan_error"]


def test_run_extension_health_executes_command():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_health

    health = run_extension_health(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "healthcheck": {"command": ["python3", "-c", "print('healthy')"], "timeout_seconds": 5},
        }
    )

    assert health["status"] == "healthy"
    assert "healthy" in (health.get("stdout") or "")


def test_run_extension_invocation_executes_entrypoint():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation

    invocation = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    "import json,sys; req=json.loads(sys.stdin.read() or '{}'); print(json.dumps({'ok': True, 'echo': req.get('action')}))",
                ]
            },
        },
        request_payload={"action": "normalize_task"},
        timeout_seconds=5,
    )

    assert invocation["status"] == "succeeded"
    assert invocation["response"]["ok"] is True
    assert invocation["response"]["echo"] == "normalize_task"


def test_run_extension_invocation_reports_failure():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation

    invocation = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    "import sys; sys.stderr.write('boom\\n'); raise SystemExit(2)",
                ]
            },
        },
        request_payload={"action": "x"},
        timeout_seconds=5,
    )

    assert invocation["status"] == "failed"
    assert invocation["exit_code"] == 2
    assert "boom" in (invocation.get("detail") or "")


def test_run_subprocess_capped_kills_on_overflow():
    # The bounded reader must cap output AND kill the child promptly (not buffer the
    # whole stream, not wait for the full timeout).
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        _run_subprocess_capped,
    )

    flood = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()"
    start = time.time()
    returncode, stdout, _stderr, exceeded = _run_subprocess_capped(
        [sys.executable, "-c", flood],
        cwd=None,
        env={},
        timeout=30,
        max_output_bytes=1_000_000,
    )
    elapsed = time.time() - start
    assert exceeded is True
    assert len(stdout) <= 1_000_000
    assert returncode != 0  # killed
    assert elapsed < 10  # did not wait the full timeout


def test_run_subprocess_capped_does_not_hang_on_unread_large_stdin():
    # A child that never reads stdin must not deadlock the host: a large payload
    # exceeding the OS pipe buffer is written on a separate thread, so the main
    # thread still reaches the timeout and kills the child.
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        _run_subprocess_capped,
    )

    big_payload = "x" * (4 * 1024 * 1024)  # 4 MiB >> 64 KiB pipe buffer
    start = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_subprocess_capped(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=None,
            env={},
            timeout=3,
            input_text=big_payload,
        )
    assert time.time() - start < 15  # killed at timeout, not hung on stdin write


def test_run_subprocess_capped_allows_exact_cap_output():
    # Output of exactly the cap size is NOT treated as overflow (only strictly more).
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        _run_subprocess_capped,
    )

    returncode, stdout, _stderr, exceeded = _run_subprocess_capped(
        [sys.executable, "-c", "import sys; sys.stdout.write('y' * 1000)"],
        cwd=None,
        env={},
        timeout=10,
        max_output_bytes=1000,
    )
    assert exceeded is False
    assert len(stdout) == 1000
    assert returncode == 0


def test_run_extension_invocation_fails_closed_on_oversized_output():
    # An extension that floods stdout beyond the cap is failed (output rejected),
    # and the host is not asked to buffer/parse the unbounded stream.
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation

    invocation = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    sys.executable,
                    "-c",
                    "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()",
                ]
            },
        },
        request_payload={"action": "flood"},
        timeout_seconds=30,
    )

    assert invocation["status"] == "failed"
    assert "size" in (invocation.get("detail") or "").lower()


def test_repo_bundled_extensions_scan_valid(tmp_path):
    from core.application.extensions.identity import is_canonical_extension_id
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    root = tmp_path / "extensions"
    _write_minimal_extension(
        root / "fixture_task_provider",
        extension_id="fixture.task-provider",
    )
    _write_minimal_extension(
        root / "fixture_notification_provider",
        extension_id="fixture.notification-provider",
    )
    results = scan_extensions(str(root))
    keyed = {str(row.get("extension_key") or ""): row for row in results}

    assert {"fixture.task-provider", "fixture.notification-provider"} <= set(keyed)
    assert {
        "fixture_task_provider",
        "fixture_notification_provider",
    }.isdisjoint(keyed)
    assert all(is_canonical_extension_id(extension_key) for extension_key in keyed)
    assert keyed["fixture.task-provider"]["status"] == "valid"
    assert keyed["fixture.notification-provider"]["status"] == "valid"


def test_compile_extension_bundle_detects_duplicate_query_ids(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "dup_query"
    (bundle / "ui" / "queries").mkdir(parents=True)
    (bundle / "ui" / "actions").mkdir(parents=True)
    (bundle / "ui" / "components").mkdir(parents=True)
    (bundle / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "extension_id": "dup_query",
                "kind": "task_provider",
                "version": "1.0.0",
                "features": ["task_normalization"],
                "entrypoint": {"command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"]},
                "ui_manifest": "ui/ui_manifest.yaml",
                "query_manifest": "ui/queries/queries.yaml",
                "action_manifest": "ui/actions/actions.yaml",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        "version: '1'\npages:\n  - page_id: overview\n    title: Overview\n    components:\n      - ref: components/overview.yaml\n",
        encoding="utf-8",
    )
    (bundle / "ui" / "components" / "overview.yaml").write_text(
        "component_id: overview\ntype: markdown\ncontent: hello\n",
        encoding="utf-8",
    )
    (bundle / "ui" / "queries" / "queries.yaml").write_text(
        "queries:\n  - query_id: q1\n    handler: {mode: extension}\n  - query_id: q1\n    handler: {mode: extension}\n",
        encoding="utf-8",
    )
    (bundle / "ui" / "actions" / "actions.yaml").write_text(
        "actions:\n  - action_id: a1\n    handler: {mode: extension}\n",
        encoding="utf-8",
    )

    compiled = compile_extension_bundle(bundle, expected_kind="task_provider")

    assert compiled["status"] == "invalid"
    assert any(err.get("code") == "duplicate_query_id" for err in compiled["compile_errors"])
    assert compiled["lifecycle"]["validate"] in {"ok", "failed"}


def test_run_extension_query_rejects_unknown_query_id():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

    invocation = run_extension_query(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {"command": ["python3", "-c", "print('{}')"]},
            "query_definitions": [{"query_id": "known_query"}],
        },
        query_id="missing_query",
    )

    assert invocation["status"] == "failed"
    assert "Unknown query_id" in invocation["detail"]


@pytest.mark.parametrize(
    ("runner_name", "operation_kwarg"),
    [
        ("run_extension_query", {"query_id": "list_messages"}),
        ("run_extension_action", {"action_id": "trash_messages"}),
    ],
)
def test_disabled_extension_fails_closed_before_direct_runtime_dispatch(
    runner_name, operation_kwarg
):
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.extension-oauth",
        "enabled": False,
        "bundle_root": str(Path.cwd()),
        "entrypoint": {"command": [sys.executable, "-c", "raise SystemExit(99)"]},
        "query_definitions": [{"query_id": "list_messages"}],
        "action_definitions": [{"action_id": "trash_messages"}],
    }
    runner = getattr(extension_runtime, runner_name)

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped"
    ) as run_subprocess:
        invocation = runner(extension, **operation_kwarg)

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "extension_disabled"
    assert invocation["errors"] == [
        {
            "code": "extension_disabled",
            "message": "This extension is disabled—ask an admin to enable it.",
        }
    ]
    assert invocation["response"]["definitely_no_external_effect"] is True
    run_subprocess.assert_not_called()


def test_run_extension_action_requires_external_effect_claim_for_declared_action():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    invocation = run_extension_action(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "extension_key": "acme.mailer",
            "entrypoint": {"command": [sys.executable, "-c", "raise SystemExit(99)"]},
            "action_definitions": [{"action_id": "send", "handler": {"mode": "extension"}}],
            "compiled_manifest": {
                "external_effects": [
                    {
                        "operation_id": "send",
                        "effect_kind": "message_delivery",
                        "channel": "api",
                        "idempotency": "required",
                    }
                ]
            },
        },
        action_id="send",
        input_payload={"to": "alice@example.com"},
        runtime_context={},
    )

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "external_effect_claim_required"
    assert invocation["response"]["definitely_no_external_effect"] is True


def test_declared_serialized_action_fails_before_subprocess_when_database_lock_is_busy():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return (False,)

    class Connection:
        def cursor(self):
            return Cursor()

    logger = type("Logger", (), {"conn": Connection()})()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.spreadsheets",
        "action_definitions": [
            {
                "action_id": "append_rows",
                "handler": {"mode": "extension"},
                "contract": {"serialization": {"key_fields": ["spreadsheet_id"]}},
            }
        ],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "append_rows",
                    "effect_kind": "spreadsheet_append",
                    "channel": "fixture_sheets",
                    "idempotency": "required",
                }
            ]
        },
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation"
    ) as invoke:
        invocation = run_extension_action(
            extension,
            action_id="append_rows",
            input_payload={"spreadsheet_id": "spreadsheet-1"},
            runtime_context={
                "external_effect_id": "effect-1",
                "extension_install_id": "install-1",
                "logger": logger,
            },
        )

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "operation_serialization_busy"
    assert invocation["errors"] == [
        {
            "code": "operation_serialization_busy",
            "message": "A conflicting extension operation is in progress; retry later.",
        }
    ]
    assert invocation["response"]["definitely_no_external_effect"] is True
    invoke.assert_not_called()


def test_declared_serialized_action_holds_database_lock_around_subprocess():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return (True,)

    class Connection:
        def cursor(self):
            return Cursor()

    logger = type("Logger", (), {"conn": Connection()})()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.spreadsheets",
        "action_definitions": [
            {
                "action_id": "append_rows",
                "handler": {"mode": "extension"},
                "contract": {"serialization": {"key_fields": ["spreadsheet_id"]}},
            }
        ],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "append_rows",
                    "effect_kind": "spreadsheet_append",
                    "channel": "fixture_sheets",
                    "idempotency": "required",
                }
            ]
        },
    }

    def invoke_while_locked(*_args, **_kwargs):
        assert len(calls) == 1
        assert "pg_try_advisory_lock" in calls[0][0]
        return {"status": "succeeded", "response": {"ok": True}}

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        side_effect=invoke_while_locked,
    ) as invoke:
        invocation = run_extension_action(
            extension,
            action_id="append_rows",
            input_payload={"spreadsheet_id": "spreadsheet-1"},
            runtime_context={
                "external_effect_id": "effect-1",
                "extension_install_id": "install-1",
                "logger": logger,
            },
        )

    assert invocation["status"] == "succeeded"
    invoke.assert_called_once()
    assert len(calls) == 2
    assert "pg_advisory_unlock" in calls[1][0]


def test_get_extension_query_provider_supports_remote_runtime_installs():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "remote.demo",
        "runtime_mode": "remote_https",
        "remote_base_url": "https://runtime.example.com",
        "query_definitions": [{"query_id": "list_accounts", "handler": {"mode": "extension"}}],
    }

    with (
        patch.object(
            helpers,
            "resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_query",
            return_value={
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "result": {
                        "items": [{"id": "acc_1"}],
                        "next_cursor": "",
                        "total": 1,
                    },
                },
            },
        ) as run_query,
    ):
        provider = helpers.get_extension_query_provider(
            "remote_install_1",
            get_logger_only=lambda: None,
        )
        assert callable(provider)
        result = provider({"query_id": "list_accounts", "scope": {"project_id": 7}})

    assert result["items"] == [{"id": "acc_1"}]
    assert result["total"] == 1
    assert run_query.call_count == 1


def test_compile_extension_bundle_rejects_manifest_v2_missing_required_fields(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "missing_required_v2"
    bundle.mkdir(parents=True)
    (bundle / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "extension_id": "missing_required_v2",
                "kind": "task_provider",
                "version": "1.0.0",
                "entrypoint": {"command": ["python3", "main.py"]},
            }
        ),
        encoding="utf-8",
    )

    compiled = compile_extension_bundle(bundle, expected_kind="task_provider")

    assert compiled["status"] == "invalid"
    error_codes = {
        str(item.get("code") or "")
        for item in compiled["compile_errors"] + compiled["validation_errors"]
    }
    assert "missing_features" in error_codes or "v2_missing_features" in error_codes
    assert "missing_health" in error_codes or "v2_missing_health" in error_codes
    assert "missing_ui_manifest" in error_codes or "v2_missing_ui_manifest" in error_codes


def test_compile_extension_bundle_rejects_unknown_component_type_and_query_ref(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "invalid_ui_refs"
    (bundle / "ui" / "components").mkdir(parents=True)
    (bundle / "ui" / "queries").mkdir(parents=True)
    (bundle / "ui" / "actions").mkdir(parents=True)
    (bundle / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "extension_id": "invalid_ui_refs",
                "kind": "task_provider",
                "version": "1.0.0",
                "features": ["task_sync"],
                "entrypoint": {"command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"]},
                "ui_manifest": "ui/ui_manifest.yaml",
                "query_manifest": "ui/queries/queries.yaml",
                "action_manifest": "ui/actions/actions.yaml",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        json.dumps(
            {
                "version": "1",
                "pages": [
                    {
                        "page_id": "overview",
                        "title": "Overview",
                        "components": [{"ref": "components/overview.yaml"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "components" / "overview.yaml").write_text(
        json.dumps(
            {
                "component_id": "overview",
                "type": "custom_widget",
                "query_id": "missing_query",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "queries" / "queries.yaml").write_text(
        json.dumps({"queries": [{"query_id": "known_query", "handler": {"mode": "extension"}}]}),
        encoding="utf-8",
    )
    (bundle / "ui" / "actions" / "actions.yaml").write_text(
        json.dumps({"actions": [{"action_id": "known_action", "handler": {"mode": "extension"}}]}),
        encoding="utf-8",
    )

    compiled = compile_extension_bundle(bundle, expected_kind="task_provider")

    assert compiled["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in compiled["validation_errors"]}
    assert "unsupported_component_type" in error_codes
    assert "unknown_query_reference" in error_codes


def test_validate_compiled_manifest_rejects_invalid_nested_scope_declarations():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["task_sync"],
            "required_scopes": ["event:subscribe"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [
                {"query_id": "q1", "required_scopes": ["bad scope"]},
            ],
            "action_definitions": [
                {"action_id": "a1", "required_scopes": ["also bad"], "mutates_platform": False},
            ],
            "ui_manifest": {"version": "1", "pages": []},
            "account_scope": {"allowed_project_ids": ["bad-id"], "allowed_workflow_ids": [""]},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "invalid_scope" in error_codes
    assert "invalid_account_scope_projects" in error_codes
    assert "invalid_account_scope_workflows" in error_codes


@pytest.mark.parametrize(
    ("action_update", "expected_code"),
    [
        (
            {
                "mutates_platform": True,
                "command_type": "invoke_extension_action",
                "handler": {
                    "mode": "extension",
                    "config": {
                        "host_mutations": ["project_connection_upsert"],
                        "pre_dispatch_host_mutations": True,
                    },
                },
            },
            "invalid_pre_dispatch_host_mutations",
        ),
        (
            {
                "mutates_platform": True,
                "command_type": "invoke_extension_action",
                "handler": {
                    "mode": "extension",
                    "config": {
                        "host_mutations": ["notification_delivery_log"],
                        "pre_dispatch_host_mutations": ["project_connection_upsert"],
                    },
                },
            },
            "pre_dispatch_host_mutation_undeclared",
        ),
        (
            {
                "mutates_platform": False,
                "handler": {
                    "mode": "extension",
                    "config": {
                        "host_mutations": ["project_connection_upsert"],
                        "pre_dispatch_host_mutations": ["project_connection_upsert"],
                    },
                },
            },
            "pre_dispatch_platform_mutation_required",
        ),
    ],
)
def test_validate_compiled_manifest_enforces_pre_dispatch_mutation_capabilities(
    action_update, expected_code
):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    action = {"action_id": "deliver", **action_update}
    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notifications"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "action_definitions": [action],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert expected_code in {str(item.get("code") or "") for item in validation["errors"]}


def test_validate_compiled_manifest_requires_task_ingress_skill_for_task_provider():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "task_provider",
            "features": ["task_sync"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [
                {
                    "query_id": "provider_health",
                    "result_fields": ["result.status", "result.configured"],
                    "handler": {"mode": "extension"},
                }
            ],
            "action_definitions": [
                {
                    "action_id": "normalize_task_action",
                    "result_fields": ["result.normalized"],
                    "handler": {"mode": "extension"},
                },
                {
                    "action_id": "fetch_attachments",
                    "result_fields": ["result.attachments"],
                    "handler": {"mode": "extension"},
                },
                {
                    "action_id": "sync_completion_status",
                    "result_fields": ["result.status"],
                    "handler": {"mode": "extension"},
                },
            ],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "task_ingress_feature_required" in error_codes


def test_validate_compiled_manifest_rejects_incomplete_task_ingress_profile():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "task_provider",
            "features": ["task_ingress"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [
                {
                    "query_id": "provider_health",
                    "result_fields": ["result.status"],
                    "handler": {"mode": "extension"},
                }
            ],
            "action_definitions": [
                {
                    "action_id": "normalize_task_action",
                    "result_fields": [],
                    "handler": {"mode": "extension"},
                },
                {
                    "action_id": "fetch_attachments",
                    "result_fields": ["result.attachments"],
                    "handler": {"mode": "extension"},
                },
            ],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "task_ingress_missing_action" in error_codes
    assert "task_ingress_missing_action_result_field" in error_codes
    assert "task_ingress_missing_query_result_field" in error_codes
    assert "task_ingress_missing_action_error_code" in error_codes
    assert "task_ingress_missing_query_error_code" in error_codes


def test_validate_compiled_manifest_rejects_unknown_task_ingress_error_code():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "task_provider",
            "features": ["task_ingress"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [
                {
                    "query_id": "provider_health",
                    "result_fields": ["result.status", "result.configured"],
                    "error_codes": [
                        "not_configured",
                        "authentication_failed",
                        "provider_unavailable",
                        "timeout",
                        "internal_error",
                    ],
                    "handler": {"mode": "extension"},
                }
            ],
            "action_definitions": [
                {
                    "action_id": "normalize_task_action",
                    "result_fields": ["result.normalized"],
                    "error_codes": [
                        "not_configured",
                        "authentication_failed",
                        "invalid_payload",
                        "rate_limited",
                        "provider_unavailable",
                        "timeout",
                        "internal_error",
                    ],
                    "handler": {"mode": "extension"},
                },
                {
                    "action_id": "fetch_attachments",
                    "result_fields": ["result.attachments"],
                    "error_codes": [
                        "not_configured",
                        "authentication_failed",
                        "not_found",
                        "rate_limited",
                        "provider_unavailable",
                        "timeout",
                        "internal_error",
                    ],
                    "handler": {"mode": "extension"},
                },
                {
                    "action_id": "sync_completion_status",
                    "result_fields": ["result.status"],
                    "error_codes": [
                        "not_configured",
                        "authentication_failed",
                        "conflict",
                        "rate_limited",
                        "provider_unavailable",
                        "timeout",
                        "legacy_failure_code",
                    ],
                    "handler": {"mode": "extension"},
                },
            ],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "task_ingress_unknown_error_code" in error_codes


def test_compile_extension_bundle_rejects_missing_ui_schema_ref_and_filters_schema_ref(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "missing_ui_refs"
    (bundle / "ui" / "components").mkdir(parents=True)
    (bundle / "ui" / "queries").mkdir(parents=True)
    (bundle / "ui" / "actions").mkdir(parents=True)
    (bundle / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "extension_id": "missing_ui_refs",
                "kind": "task_provider",
                "version": "1.0.0",
                "features": ["task_sync"],
                "entrypoint": {"command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"]},
                "ui_manifest": "ui/ui_manifest.yaml",
                "query_manifest": "ui/queries/queries.yaml",
                "action_manifest": "ui/actions/actions.yaml",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        json.dumps(
            {
                "version": "1",
                "pages": [
                    {
                        "page_id": "overview",
                        "title": "Overview",
                        "components": [{"ref": "components/overview.yaml"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "components" / "overview.yaml").write_text(
        json.dumps(
            {
                "component_id": "overview_form",
                "type": "form",
                "ui_schema_ref": "forms/missing.ui.yaml",
                "filters_schema_ref": "grids/missing.filters.yaml",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "queries" / "queries.yaml").write_text(
        json.dumps({"queries": [{"query_id": "known_query", "handler": {"mode": "extension"}}]}),
        encoding="utf-8",
    )
    (bundle / "ui" / "actions" / "actions.yaml").write_text(
        json.dumps({"actions": [{"action_id": "known_action", "handler": {"mode": "extension"}}]}),
        encoding="utf-8",
    )

    compiled = compile_extension_bundle(bundle, expected_kind="task_provider")

    assert compiled["status"] == "invalid"
    error_fields = {str(item.get("field_path") or "") for item in compiled["compile_errors"]}
    assert any("ui_schema_ref" in field for field in error_fields)
    assert any("filters_schema_ref" in field for field in error_fields)


def test_validate_compiled_manifest_allows_grid_column_badge_type():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["read:projects"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [{"query_id": "list_deliveries"}],
            "action_definitions": [{"action_id": "retry_delivery", "mutates_platform": False}],
            "ui_manifest": {
                "version": "1",
                "pages": [
                    {
                        "page_id": "deliveries",
                        "components": [
                            {
                                "component_id": "delivery_grid",
                                "type": "grid",
                                "query_id": "list_deliveries",
                                "columns": [
                                    {"key": "status", "label": "Status", "type": "badge"},
                                ],
                                "row_actions": [
                                    {"action_id": "retry_delivery", "scope": "row"},
                                ],
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert validation["status"] == "valid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "unsupported_component_type" not in error_codes
    assert "missing_grid_column_type" not in error_codes


def _external_resource_picker_manifest() -> dict:
    return {
        "manifest_version": 2,
        "manifest_path": "/tmp/extension.yaml",
        "features": ["notify"],
        "required_scopes": ["read:projects"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "query_definitions": [],
        "action_definitions": [
            {
                "action_id": "picker_bootstrap",
                "handler": {"mode": "extension"},
                "mutates_platform": False,
                "workflow_visible": False,
                "parameters": [],
            }
        ],
        "ui_manifest": {
            "version": "1",
            "pages": [
                {
                    "page_id": "resources",
                    "components": [
                        {
                            "component_id": "resource_form",
                            "type": "form",
                            "ui_schema": {
                                "fields": [{"field_id": "resource_id", "widget": "text"}]
                            },
                        },
                        {
                            "component_id": "resource_picker",
                            "type": "external_resource_picker",
                            "data": {
                                "allowed_script_origins": ["https://sdk.example.test"],
                                "bootstrap_action_id": "picker_bootstrap",
                                "bridge_global": "NeutralFixture.Picker",
                                "button_label": "Choose resource",
                                "busy_label": "Opening resource picker…",
                                "error_message": "Selection could not be applied.",
                                "options": {"mode": "documents"},
                                "scripts": [{"src": "https://sdk.example.test/picker.js"}],
                                "target_component_id": "resource_form",
                                "selection": {
                                    "maximum": 1,
                                    "fields": [
                                        {
                                            "source": "opaque_id",
                                            "target": "resource_id",
                                            "required": True,
                                            "max_length": 128,
                                        }
                                    ],
                                },
                            },
                        },
                    ],
                }
            ],
        },
    }


def test_validate_compiled_manifest_accepts_closed_external_resource_picker():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(_external_resource_picker_manifest())

    assert validation["status"] == "valid", validation["errors"]


def test_validate_compiled_manifest_accepts_closed_picker_asset_ref():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["ui_manifest"]["pages"][0]["components"][1]["data"]["scripts"].append(
        {"asset_ref": "assets/picker-bridge.js"}
    )

    validation = validate_compiled_manifest(manifest)

    assert validation["status"] == "valid", validation["errors"]


def test_validate_compiled_manifest_accepts_closed_workflow_picker_declaration():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    declaration = copy.deepcopy(manifest["ui_manifest"]["pages"][0]["components"][1]["data"])
    declaration["target_component_id"] = "workflow_parameter_form"
    declaration["scripts"].append({"asset_ref": "assets/picker-bridge.js"})
    declaration["selection"]["fields"][0]["target"] = "resource_id"
    manifest["step_ui_manifest"] = [
        {
            "operation_id": "choose_resource",
            "fields": [
                {
                    "name": "resource_id",
                    "widget_type": "resource_picker",
                    "external_resource_picker": declaration,
                }
            ],
        }
    ]

    validation = validate_compiled_manifest(manifest)

    assert validation["status"] == "valid", validation["errors"]


def test_validate_compiled_manifest_accepts_canonical_picker_url_matrix():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    picker_data["allowed_script_origins"] = [_PICKER_ADVERSARIAL_CASES["valid_origin"]]
    picker_data["scripts"] = [{"src": _PICKER_ADVERSARIAL_CASES["valid_script"]}]

    validation = validate_compiled_manifest(manifest)

    assert validation["status"] == "valid", validation["errors"]


@pytest.mark.parametrize("origin", _PICKER_ADVERSARIAL_CASES["invalid_origins"])
def test_validate_compiled_manifest_rejects_noncanonical_picker_origins(origin):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["ui_manifest"]["pages"][0]["components"][1]["data"]["allowed_script_origins"] = [
        origin
    ]

    validation = validate_compiled_manifest(manifest)

    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize("script", _PICKER_ADVERSARIAL_CASES["invalid_scripts"])
def test_validate_compiled_manifest_rejects_noncanonical_picker_scripts(script):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["ui_manifest"]["pages"][0]["components"][1]["data"]["scripts"] = [{"src": script}]

    validation = validate_compiled_manifest(manifest)

    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize("button_label", _PICKER_ADVERSARIAL_CASES["invalid_button_labels"])
def test_validate_compiled_manifest_rejects_noncanonical_picker_copy(button_label):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["ui_manifest"]["pages"][0]["components"][1]["data"]["button_label"] = button_label

    validation = validate_compiled_manifest(manifest)

    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize("button_label", _PICKER_ADVERSARIAL_CASES["valid_button_labels"])
def test_validate_compiled_manifest_accepts_well_formed_picker_copy(button_label):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["ui_manifest"]["pages"][0]["components"][1]["data"]["button_label"] = button_label

    assert validate_compiled_manifest(manifest)["status"] == "valid"


@pytest.mark.parametrize("code_unit", _PICKER_ADVERSARIAL_CASES["invalid_unicode_code_units"])
@pytest.mark.parametrize("field", ["button_label", "options"])
def test_validate_compiled_manifest_rejects_unpaired_picker_surrogates(code_unit, field):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    malformed = chr(code_unit)
    picker_data[field] = {"payload": malformed} if field == "options" else malformed

    validation = validate_compiled_manifest(manifest)
    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize("pair", _PICKER_ADVERSARIAL_CASES["valid_surrogate_pairs"])
@pytest.mark.parametrize("field", ["button_label", "options"])
def test_validate_compiled_manifest_normalizes_valid_picker_surrogate_pairs(pair, field):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    paired = chr(pair["high"]) + chr(pair["low"])
    picker_data[field] = {"nested": {"payload": paired}} if field == "options" else paired

    validation = validate_compiled_manifest(manifest)

    assert validation["status"] == "valid", validation["errors"]
    assert paired not in json.dumps(manifest, ensure_ascii=False)
    encoded = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    assert "😀".encode() in encoded


@pytest.mark.parametrize("case", _PICKER_ADVERSARIAL_CASES["invalid_scalar_cases"])
def test_validate_compiled_manifest_rejects_picker_scalar_type_matrix(case):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    _apply_picker_adversarial_case(picker_data, case)

    validation = validate_compiled_manifest(manifest)

    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize("case", _PICKER_ADVERSARIAL_CASES["valid_scalar_cases"])
def test_validate_compiled_manifest_accepts_picker_optional_null_copy(case):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    _apply_picker_adversarial_case(picker_data, case)

    assert validate_compiled_manifest(manifest)["status"] == "valid"


def test_validate_compiled_manifest_uses_utf16_json_bound_for_picker_options():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    counts = _PICKER_ADVERSARIAL_CASES["options_utf16_cases"]
    manifest = _external_resource_picker_manifest()
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    picker_data["options"] = {"payload": "😀" * counts["valid_emoji_count"]}
    assert validate_compiled_manifest(manifest)["status"] == "valid"

    picker_data["options"] = {"payload": "😀" * counts["invalid_emoji_count"]}
    validation = validate_compiled_manifest(manifest)
    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize("case", _PICKER_ADVERSARIAL_CASES["invalid_options_numeric_boundaries"])
def test_validate_compiled_manifest_uses_runtime_neutral_picker_options_budget(case):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    picker_data["options"] = {
        "n": case["number"],
        "payload": "x" * case["payload_length"],
    }

    validation = validate_compiled_manifest(manifest)
    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"handler": {"mode": "platform"}},
        {"mutates_platform": True},
        {"workflow_visible": True},
        {"parameters": [{"name": "unsafe", "type": "string"}]},
    ],
)
def test_validate_compiled_manifest_rejects_unsafe_picker_bootstrap_action(mutation):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["action_definitions"][0].update(mutation)

    validation = validate_compiled_manifest(manifest)

    assert "unsafe_external_resource_picker_bootstrap_action" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


def test_validate_compiled_manifest_rejects_picker_bootstrap_external_effect():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["external_effects"] = [
        {
            "operation_id": "picker_bootstrap",
            "effect_kind": "identity_mutation",
            "channel": "identity",
            "idempotency": "required",
            "test_mode_behavior": "suppress",
        }
    ]

    validation = validate_compiled_manifest(manifest)

    assert "unsafe_external_resource_picker_bootstrap_action" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update({"unknown": True}),
        lambda data: data.pop("button_label"),
        lambda data: data.update({"button_label": "x" * 121}),
        lambda data: data.update({"bootstrap_action_id": "Missing.Action"}),
        lambda data: data.update({"bridge_global": "unsafe[0]"}),
        lambda data: data.update({"allowed_script_origins": ["http://sdk.example.test"]}),
        lambda data: data.update(
            {"allowed_script_origins": [f"https://sdk{index}.example.test" for index in range(5)]}
        ),
        lambda data: data.update({"scripts": [{"src": "https://other.example.test/picker.js"}]}),
        lambda data: data.update(
            {"scripts": [{"src": "https://sdk.example.test/picker.js", "async": True}]}
        ),
        lambda data: data["selection"].update({"maximum": 2}),
        lambda data: data["selection"]["fields"][0].update({"source": "resource.id"}),
        lambda data: data["selection"]["fields"][0].update({"required": "yes"}),
        lambda data: data["selection"]["fields"][0].update({"max_length": 0}),
        lambda data: data["selection"]["fields"][0].update({"target": "missing_field"}),
        lambda data: data["selection"]["fields"].append(
            {"source": "name", "target": "resource_id", "max_length": 64}
        ),
        lambda data: data["selection"].update(
            {
                "fields": [
                    {"source": f"field_{index}", "target": f"field_{index}", "max_length": 64}
                    for index in range(33)
                ]
            }
        ),
        lambda data: data.update({"options": {"payload": "x" * 32_769}}),
        lambda data: data.update({"target_component_id": "missing_form"}),
    ],
)
def test_validate_compiled_manifest_rejects_open_or_unsafe_external_resource_picker(mutate):
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = copy.deepcopy(_external_resource_picker_manifest())
    picker_data = manifest["ui_manifest"]["pages"][0]["components"][1]["data"]
    mutate(picker_data)
    validation = validate_compiled_manifest(manifest)

    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "invalid_external_resource_picker_declaration" in error_codes
    assert "unsupported_component_type" not in error_codes


def test_validate_compiled_manifest_requires_picker_target_to_be_a_form():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = _external_resource_picker_manifest()
    manifest["ui_manifest"]["pages"][0]["components"][0]["type"] = "markdown"

    validation = validate_compiled_manifest(manifest)

    assert "invalid_external_resource_picker_declaration" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


def test_validate_compiled_manifest_rejects_unknown_top_level_fields():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["read:projects"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [{"query_id": "list_deliveries"}],
            "action_definitions": [],
            "ui_manifest": {"version": "1", "pages": []},
            "unexpected_section": {"enabled": True},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    error_fields = {str(item.get("field_path") or "") for item in validation["errors"]}
    assert "unknown_compiled_manifest_field" in error_codes
    assert "unexpected_section" in error_fields


def test_validate_compiled_manifest_accepts_external_effect_declaration():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    compiled_manifest = {
        "manifest_version": 2,
        "manifest_path": "/tmp/extension.yaml",
        "kind": "notification_provider",
        "release_stage": "placeholder",
        "features": ["notify"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "action_definitions": [{"action_id": "publish_record", "handler": {"mode": "extension"}}],
        "query_definitions": [],
        "external_effects": [
            {
                "operation_id": "publish_record",
                "effect_kind": "message_delivery",
                "channel": "api",
                "idempotency": "required",
                "test_mode_behavior": "suppress",
                "observability": {
                    "target_fields": ["record_ids"],
                    "target_kind": "remote_record",
                    "target_label": "record",
                    "artifact_field": "artifacts",
                    "payload_size_field": "payload_bytes",
                },
            }
        ],
        "ui_manifest": {"version": "1", "pages": []},
    }
    validation = validate_compiled_manifest(compiled_manifest)

    assert validation["status"] == "valid"
    assert compiled_manifest["external_effects"] == [
        {
            "operation_id": "publish_record",
            "effect_kind": "message_delivery",
            "channel": "api",
            "idempotency": "required",
            "test_mode_behavior": "suppress",
            "observability": {
                "target_fields": ["record_ids"],
                "target_kind": "remote_record",
                "target_label": "record",
                "artifact_field": "artifacts",
                "payload_size_field": "payload_bytes",
            },
        }
    ]


def test_validate_compiled_manifest_accepts_project_state_declaration():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    compiled_manifest = {
        "manifest_version": 2,
        "manifest_path": "/tmp/extension.yaml",
        "kind": "tool_provider",
        "features": ["tool"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "action_definitions": [{"action_id": "sync_feed", "handler": {"mode": "extension"}}],
        "query_definitions": [],
        "project_state": {
            "reads": [{"namespace": "snapshots", "state_key": "last_success"}],
            "writes": [
                {
                    "namespace": "snapshots",
                    "state_key": "last_success",
                    "description": "Pointer to the last successfully published dataset.",
                }
            ],
        },
        "ui_manifest": {"version": "1", "pages": []},
    }
    validation = validate_compiled_manifest(compiled_manifest)

    assert validation["status"] == "valid"
    assert compiled_manifest["project_state"] == {
        "reads": [{"namespace": "snapshots", "state_key": "last_success"}],
        "writes": [
            {
                "namespace": "snapshots",
                "state_key": "last_success",
                "description": "Pointer to the last successfully published dataset.",
            }
        ],
    }


def test_validate_compiled_manifest_rejects_project_state_write_without_key():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "tool_provider",
            "features": ["tool"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "action_definitions": [{"action_id": "sync_feed", "handler": {"mode": "extension"}}],
            "query_definitions": [],
            "project_state": {"writes": [{"namespace": "snapshots"}]},
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    assert any(err.get("code") == "missing_project_state_write_key" for err in validation["errors"])


def test_validate_compiled_manifest_rejects_duplicate_external_effect_operation():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "notification_provider",
            "features": ["notify"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "action_definitions": [
                {"action_id": "publish_record", "handler": {"mode": "extension"}}
            ],
            "query_definitions": [],
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                },
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                },
            ],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    assert any(
        err.get("code") == "duplicate_external_effect_operation" for err in validation["errors"]
    )


def test_compile_extension_bundle_emits_canonical_ui_manifest_only(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "fixture_provider"
    _write_minimal_extension(bundle, extension_id="fixture.provider")
    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    compiled_manifest = compiled["compiled_manifest"]
    assert isinstance(compiled_manifest.get("ui_manifest"), dict)
    assert "ui" not in compiled_manifest


def test_compile_extension_bundle_preserves_page_project_scope(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_unscoped_page"
    _write_minimal_extension(bundle, extension_id="acme.unscoped-page")
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        "version: '1'\npages:\n  - ref: pages/catalog.yaml\n",
        encoding="utf-8",
    )
    (bundle / "ui" / "pages").mkdir(exist_ok=True)
    (bundle / "ui" / "pages" / "catalog.yaml").write_text(
        "page_id: catalog\nproject_scoped: false\ntitle: Catalog\ncomponents: []\n",
        encoding="utf-8",
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert compiled["compiled_manifest"]["ui_manifest"]["pages"][0]["project_scoped"] is False


def test_validate_compiled_manifest_rejects_noncanonical_form_field_layout():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [{"action_id": "save_config"}],
            "ui_manifest": {
                "version": "1",
                "pages": [
                    {
                        "page_id": "configuration",
                        "components": [
                            {
                                "component_id": "config_form",
                                "type": "form",
                                "save_action": "save_config",
                                "fields": [
                                    {"field_id": "chat_id", "field_type": "text"},
                                ],
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "missing_form_ui_schema" in error_codes
    assert "top_level_form_fields_not_supported" in error_codes
    assert "top_level_form_action_not_supported" in error_codes


def test_validate_compiled_manifest_rejects_form_fields_missing_from_action_parameters():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [
                {
                    "action_id": "save_config",
                    "workflow_visible": False,
                    "parameters": [{"name": "parse_mode", "type": "string"}],
                }
            ],
            "ui_manifest": {
                "version": "1",
                "pages": [
                    {
                        "page_id": "configuration",
                        "components": [
                            {
                                "component_id": "config_form",
                                "type": "form",
                                "ui_schema": {
                                    "fields": [
                                        {
                                            "field_id": "message_prefix",
                                            "path": "message_prefix",
                                            "widget": "text",
                                        }
                                    ]
                                },
                                "data": {"save_action": "save_config"},
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert validation["status"] == "invalid"
    assert "form_action_parameter_mismatch" in {
        str(item.get("code") or "") for item in validation["errors"]
    }


def test_validate_compiled_manifest_allows_test_action_to_consume_a_form_subset():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [
                {
                    "action_id": "test_delivery",
                    "workflow_visible": False,
                    "parameters": [{"name": "channel_id", "type": "string"}],
                }
            ],
            "ui_manifest": {
                "version": "1",
                "pages": [
                    {
                        "page_id": "channels",
                        "components": [
                            {
                                "component_id": "channels_form",
                                "type": "form",
                                "ui_schema": {
                                    "fields": [
                                        {
                                            "field_id": "channels",
                                            "path": "channels",
                                            "widget": "repeater",
                                        }
                                    ]
                                },
                                "data": {"test_action": "test_delivery"},
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert validation["status"] == "valid", validation


def test_validate_compiled_manifest_rejects_grid_columns_without_type():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [{"query_id": "list_items"}],
            "action_definitions": [],
            "ui_manifest": {
                "version": "1",
                "pages": [
                    {
                        "page_id": "deliveries",
                        "components": [
                            {
                                "component_id": "delivery_grid",
                                "type": "grid",
                                "query_id": "list_items",
                                "columns": [
                                    {"key": "status", "label": "Status"},
                                ],
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "missing_grid_column_type" in error_codes


def test_operation_required_scopes_merges_extension_and_compiled_rows():
    from core.infrastructure.extension_runtime.extension_runtime import (
        extension_required_scopes,
        operation_required_scopes,
    )

    extension = {
        "required_scopes": ["extension:invoke"],
        "query_definitions": [{"query_id": "direct_query", "required_scopes": ["read:events"]}],
        "compiled_manifest": {
            "required_scopes": ["read:projects"],
            "query_definitions": [{"query_id": "compiled_query", "required_scopes": ["read:logs"]}],
            "action_definitions": [{"action_id": "save", "required_scopes": ["write:config"]}],
        },
    }

    assert set(extension_required_scopes(extension)) == {"extension:invoke", "read:projects"}
    assert operation_required_scopes(
        extension, operation_type="query", operation_id="direct_query"
    ) == ["read:events"]
    assert operation_required_scopes(
        extension, operation_type="query", operation_id="compiled_query"
    ) == ["read:logs"]
    assert operation_required_scopes(extension, operation_type="action", operation_id="save") == [
        "write:config"
    ]


def test_validate_compiled_manifest_rejects_invalid_handler_mode():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [{"query_id": "q1", "handler": {"mode": "unsupported"}}],
            "action_definitions": [{"action_id": "a1", "handler": {"mode": "unsupported"}}],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "invalid_operation_handler_mode" in error_codes


def test_run_extension_query_dispatches_provider_query_through_extension_bundle(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

    bundle = tmp_path / "extensions" / "query_provider"
    bundle.mkdir(parents=True)
    (bundle / "main.py").write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read() or '{}')\n"
        "query_id = request.get('query', {}).get('query_id')\n"
        "print(json.dumps({'ok': True, 'result': {'query_id': query_id}, 'summary': {'provider': 'fixture'}}))\n",
        encoding="utf-8",
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.query",
        "bundle_root": str(bundle),
        "entrypoint": {"command": [sys.executable, "main.py"]},
        "query_definitions": [
            {"query_id": "list_routes", "handler": {"mode": "extension"}},
        ],
    }

    invocation = run_extension_query(
        extension,
        query_id="list_routes",
        context={"project_id": "00000000-0000-0000-0000-000000000007"},
    )

    assert invocation["status"] == "succeeded"
    assert invocation["response"]["ok"] is True
    assert invocation["response"]["summary"]["provider"] == "fixture"


def test_run_extension_query_includes_host_contract_scope_and_principal():
    from core.application.management.scope import ScopeContext
    from core.infrastructure.extension_runtime.extension_runtime import (
        HOST_CONTRACT_VERSION,
        run_extension_query,
    )

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "demo_query",
        "bundle_root": "/tmp/extensions/demo_query",
        "entrypoint": {"command": ["python3", "main.py"]},
        "query_definitions": [{"query_id": "list_items", "handler": {"mode": "extension"}}],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        return_value={"status": "succeeded", "response": {"ok": True}},
    ) as invoke:
        invocation = run_extension_query(
            extension,
            query_id="list_items",
            context={"account_id": "acc_client", "project_id": 999, "custom": "value"},
            params={"limit": 5},
            scope=ScopeContext(
                account_id="acc_alpha",
                project_id=7,
                workflow_id="weekly_report",
                actor_id="alice",
                principal_type="user",
            ),
        )

    request_payload = invoke.call_args.kwargs["request_payload"]
    assert invocation["status"] == "succeeded"
    assert request_payload["contract_version"] == HOST_CONTRACT_VERSION
    assert request_payload["host"]["operation"] == "query"
    assert request_payload["host"]["scope"]["account_id"] == "acc_alpha"
    assert request_payload["host"]["scope"]["project_id"] == "7"
    assert request_payload["host"]["principal"]["actor_id"] == "alice"
    assert request_payload["query"]["context"]["account_id"] == "acc_alpha"
    assert request_payload["query"]["context"]["project_id"] == "7"
    assert request_payload["query"]["context"]["custom"] == "value"
    assert request_payload["host"]["contract_version"] == "extension_host_v2"
    # Outside a run the execution fields are present and null.
    assert {
        key: request_payload["host"]["scope"][key]
        for key in ("job_id", "group_id", "for_each_id", "step_id", "step_execution_id")
    } == {
        "job_id": None,
        "group_id": None,
        "for_each_id": None,
        "step_id": None,
        "step_execution_id": None,
    }


@pytest.mark.parametrize(
    ("runtime_context", "expected"),
    [
        (
            {
                "job_id": 42,
                "group_id": "",
                "for_each_id": "",
                "step_id": "fetch",
                "step_execution_id": "88888888-8888-4888-8888-888888888888",
            },
            {
                "job_id": "42",
                "group_id": None,
                "for_each_id": None,
                "step_id": "fetch",
                "step_execution_id": "88888888-8888-4888-8888-888888888888",
            },
        ),
        (
            {
                "job_id": 42,
                "group_id": "per_item",
                "for_each_id": "",
                "step_id": "fetch",
                "step_execution_id": "99999999-9999-4999-8999-999999999999",
            },
            {
                "job_id": "42",
                "group_id": "per_item",
                "for_each_id": None,
                "step_id": "fetch",
                "step_execution_id": "99999999-9999-4999-8999-999999999999",
            },
        ),
        (
            {
                "job_id": 42,
                "group_id": "",
                "for_each_id": "each_order",
                "step_id": "fetch",
                "step_execution_id": "99999999-9999-4999-8999-999999999999",
            },
            {
                "job_id": "42",
                "group_id": None,
                "for_each_id": "each_order",
                "step_id": "fetch",
                "step_execution_id": "99999999-9999-4999-8999-999999999999",
            },
        ),
    ],
    ids=["root-step", "group-step", "for-each-step"],
)
def test_run_extension_action_envelope_carries_execution_scope(runtime_context, expected):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "demo_action",
        "bundle_root": "/tmp/extensions/demo_action",
        "entrypoint": {"command": ["python3", "main.py"]},
        "action_definitions": [{"action_id": "sync", "handler": {"mode": "extension"}}],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        return_value={"status": "succeeded", "response": {"ok": True}},
    ) as invoke:
        run_extension_action(
            extension,
            action_id="sync",
            context={"project_id": 7, "phase_id": "legacy"},
            runtime_context={**runtime_context, "account_id": "acc_alpha"},
        )

    request_payload = invoke.call_args.kwargs["request_payload"]
    assert request_payload["contract_version"] == "extension_host_v2"
    scope = request_payload["host"]["scope"]
    assert {key: scope[key] for key in expected} == expected
    assert "phase_id" not in scope


def test_release_declaring_another_host_contract_is_not_invoked():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

    extension = {
        "extension_key": "demo_query",
        "bundle_root": "/tmp/extensions/demo_query",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {"runtime": {"contract_version": "extension_host_v1"}},
        "query_definitions": [{"query_id": "list_items", "handler": {"mode": "extension"}}],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        side_effect=AssertionError("a foreign-contract release must not be invoked"),
    ):
        invocation = run_extension_query(extension, query_id="list_items")

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "extension_contract_incompatible"


def test_sdk_runtime_context_reads_v2_scope_and_refuses_a_foreign_contract():
    from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import (
        runtime_context_from_payload,
    )

    root = runtime_context_from_payload(
        {
            "contract_version": "extension_host_v2",
            "runtime_context": {
                "job_id": 7,
                "group_id": "",
                "for_each_id": None,
                "step_id": "fetch",
                "step_execution_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "phase_id": "legacy",
            },
        }
    )
    assert root.contract_version == "extension_host_v2"
    assert root.group_id is None
    assert root.for_each_id is None
    assert root.step_id == "fetch"
    assert root.step_execution_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert not hasattr(root, "phase_id")

    group_level = {"group_id": "per_item", "for_each_id": None}
    grouped = runtime_context_from_payload(
        {
            "contract_version": "extension_host_v2",
            "runtime_context": {**group_level, "step_id": None, "step_execution_id": None},
        }
    )
    assert grouped.group_id == "per_item"
    assert grouped.step_id is None
    # A context without the final four keys is not read as a root scope.
    with pytest.raises(ValueError, match="for_each_id"):
        runtime_context_from_payload(
            {"contract_version": "extension_host_v2", "runtime_context": {"group_id": "per_item"}}
        )

    with pytest.raises(ValueError, match="extension_host_v1"):
        runtime_context_from_payload({"contract_version": "extension_host_v1"})


def test_run_extension_action_dispatches_bundle_action(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    bundle = tmp_path / "extensions" / "action_provider"
    bundle.mkdir(parents=True)
    (bundle / "main.py").write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read() or '{}')\n"
        "print(json.dumps({'ok': True, 'result': {'action_id': request.get('action', {}).get('action_id')}}))\n",
        encoding="utf-8",
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.action",
        "bundle_root": str(bundle),
        "entrypoint": {"command": [sys.executable, "main.py"]},
        "action_definitions": [
            {"action_id": "save_config", "handler": {"mode": "extension"}},
        ],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        return_value={"status": "succeeded", "response": {"ok": True}},
    ) as invoke:
        invocation = run_extension_action(
            extension,
            action_id="save_config",
            context={"project_id": 7},
            input_payload={"chat_id": "-100123", "parse_mode": "HTML"},
        )

    assert invocation["status"] == "succeeded"
    assert invoke.call_count == 1


def test_run_extension_action_uses_entrypoint_timeout_when_call_timeout_omitted(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    bundle_root = tmp_path / "extensions" / "timeout"
    bundle_root.mkdir(parents=True)
    (bundle_root / "main.py").write_text("print('{}')\n", encoding="utf-8")
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.timeout",
        "bundle_root": str(bundle_root),
        "entrypoint": {"command": [sys.executable, "main.py"], "timeout_seconds": 300},
        "action_definitions": [
            {"action_id": "convert_document", "handler": {"mode": "extension"}},
        ],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped",
        return_value=(0, '{"ok":true}', "", False),
    ) as invoke:
        invocation = run_extension_action(
            extension,
            action_id="convert_document",
            input_payload={"artifact_handle": "artifact:source"},
        )

    assert invocation["status"] == "succeeded"
    assert invoke.call_args.kwargs["timeout"] == 300
    assert invocation["timeout_seconds"] == 300


def test_run_extension_action_resolves_env_timeout_policy_with_headroom(tmp_path, monkeypatch):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    monkeypatch.setenv("FS_EXTENSION_OPERATION_TIMEOUT_SECONDS", "600")
    bundle_root = tmp_path / "extensions" / "timeout_policy"
    bundle_root.mkdir(parents=True)
    (bundle_root / "main.py").write_text("print('{}')\n", encoding="utf-8")
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.timeout-policy",
        "bundle_root": str(bundle_root),
        "entrypoint": {
            "command": [sys.executable, "main.py"],
            "timeout_policy": {
                "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
                "default_seconds": 300,
                "minimum_seconds": 30,
                "maximum_seconds": 3600,
                "headroom_seconds": 30,
            },
        },
        "action_definitions": [{"action_id": "run", "handler": {"mode": "extension"}}],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped",
        return_value=(0, '{"ok":true}', "", False),
    ) as invoke:
        invocation = run_extension_action(extension, action_id="run", timeout_seconds=5)

    assert invoke.call_args.kwargs["timeout"] == 630
    assert invocation["timeout_seconds"] == 630


def test_extension_child_receives_only_declared_non_secret_operator_policy_env(
    tmp_path, monkeypatch
):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    monkeypatch.setenv("FS_EXTENSION_ARTIFACT_MAX_BYTES", "2147483648")
    monkeypatch.setenv("FS_EXTENSION_OPERATION_TIMEOUT_SECONDS", "600")
    bundle_root = tmp_path / "extensions" / "policy_env"
    bundle_root.mkdir(parents=True)
    (bundle_root / "main.py").write_text(
        "import json, os\n"
        "names = ['FS_EXTENSION_ARTIFACT_MAX_BYTES', "
        "'FS_EXTENSION_OPERATION_TIMEOUT_SECONDS']\n"
        "print(json.dumps({'ok': True, 'result': {name: os.getenv(name) for name in names}}))\n",
        encoding="utf-8",
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.policy-env",
        "bundle_root": str(bundle_root),
        "entrypoint": {"command": [sys.executable, "main.py"]},
        "action_definitions": [{"action_id": "run", "handler": {"mode": "extension"}}],
    }

    invocation = run_extension_action(extension, action_id="run")

    assert invocation["status"] == "succeeded"
    assert invocation["response"]["result"] == {
        "FS_EXTENSION_ARTIFACT_MAX_BYTES": "2147483648",
        "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS": "600",
    }


def test_run_extension_action_clamps_entrypoint_timeout_to_safe_range(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    bundle_root = tmp_path / "extensions" / "timeout"
    bundle_root.mkdir(parents=True)
    (bundle_root / "main.py").write_text("print('{}')\n", encoding="utf-8")
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.timeout",
        "bundle_root": str(bundle_root),
        "entrypoint": {"command": [sys.executable, "main.py"], "timeout_seconds": 999},
        "action_definitions": [{"action_id": "run", "handler": {"mode": "extension"}}],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped",
        return_value=(0, '{"ok":true}', "", False),
    ) as invoke:
        invocation = run_extension_action(extension, action_id="run")

    assert invocation["status"] == "succeeded"
    assert invoke.call_args.kwargs["timeout"] == 600
    assert invocation["timeout_seconds"] == 600


def test_manifest_normalizes_entrypoint_timeout_seconds_range(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_timeout"
    _write_minimal_extension(bundle, extension_id="acme.timeout")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["entrypoint"] = {
        "mode": "subprocess",
        "command": ["python3", "main.py"],
        "timeout_seconds": "300",
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=True))

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert compiled["entrypoint"]["timeout_seconds"] == 300


def test_manifest_accepts_only_canonical_bounded_entrypoint_timeout_policy(tmp_path):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_timeout_policy"
    _write_minimal_extension(bundle, extension_id="acme.timeout-policy")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    policy = {
        "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
        "default_seconds": 300,
        "minimum_seconds": 30,
        "maximum_seconds": 3600,
        "headroom_seconds": 30,
    }
    manifest["entrypoint"] = {
        "mode": "subprocess",
        "command": ["python3", "main.py"],
        "timeout_policy": policy,
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=True))

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid"
    assert compiled["entrypoint"]["timeout_policy"] == policy

    manifest["entrypoint"]["timeout_policy"]["env"] = "FS_TASK_UPLOAD_TIMEOUT_SECONDS"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=True))
    rejected = compile_extension_bundle(bundle)
    assert rejected["status"] == "invalid"
    assert "invalid_timeout_policy" in {row["code"] for row in rejected["compile_errors"]}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_seconds", 3600),
        ("minimum_seconds", 1),
        ("maximum_seconds", 1800),
        ("headroom_seconds", 300),
    ],
)
def test_manifest_rejects_noncanonical_timeout_policy_numbers(tmp_path, field, value):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / f"acme_timeout_policy_{field}"
    _write_minimal_extension(bundle, extension_id=f"acme.timeout-policy-{field.replace('_', '-')}")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    policy = {
        "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
        "default_seconds": 300,
        "minimum_seconds": 30,
        "maximum_seconds": 3600,
        "headroom_seconds": 30,
    }
    policy[field] = value
    manifest["entrypoint"] = {
        "mode": "subprocess",
        "command": ["python3", "main.py"],
        "timeout_policy": policy,
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=True))

    rejected = compile_extension_bundle(bundle)

    assert rejected["status"] == "invalid"
    assert "invalid_timeout_policy" in {row["code"] for row in rejected["compile_errors"]}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_seconds", 3600),
        ("minimum_seconds", 1),
        ("maximum_seconds", 1800),
        ("headroom_seconds", 300),
    ],
)
def test_runtime_does_not_trust_noncanonical_timeout_policy(monkeypatch, field, value):
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        _resolved_entrypoint_timeout,
    )

    monkeypatch.delenv("FS_EXTENSION_OPERATION_TIMEOUT_SECONDS", raising=False)
    policy = {
        "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
        "default_seconds": 300,
        "minimum_seconds": 30,
        "maximum_seconds": 3600,
        "headroom_seconds": 30,
    }
    policy[field] = value

    assert _resolved_entrypoint_timeout({"timeout_policy": policy}, None) == 30


@pytest.mark.parametrize("timeout_seconds", [0, 601, "not-an-int"])
def test_manifest_rejects_entrypoint_timeout_seconds_outside_safe_range(
    tmp_path,
    timeout_seconds,
):
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_bad_timeout"
    _write_minimal_extension(bundle, extension_id="acme.bad-timeout")
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["entrypoint"] = {
        "mode": "subprocess",
        "command": ["python3", "main.py"],
        "timeout_seconds": timeout_seconds,
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=True))

    compiled = compile_extension_bundle(bundle)

    assert any(
        issue.get("field_path") == "entrypoint.timeout_seconds"
        and issue.get("code") == "invalid_timeout"
        for issue in compiled["compile_errors"]
    )


def test_run_extension_action_includes_host_contract_scope_and_principal():
    from core.application.management.scope import ScopeContext
    from core.infrastructure.extension_runtime.extension_runtime import (
        HOST_CONTRACT_VERSION,
        run_extension_action,
    )

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "demo_action",
        "bundle_root": "/tmp/extensions/demo_action",
        "entrypoint": {"command": ["python3", "main.py"]},
        "action_definitions": [{"action_id": "save", "handler": {"mode": "extension"}}],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        return_value={"status": "succeeded", "response": {"ok": True}},
    ) as invoke:
        invocation = run_extension_action(
            extension,
            action_id="save",
            context={"account_id": "acc_client", "project_id": 999, "custom": "value"},
            target={"scope": "row", "item_ids": ["a"]},
            input_payload={"enabled": True},
            scope=ScopeContext(
                account_id="acc_alpha",
                project_id=7,
                workflow_id="weekly_report",
                actor_id="alice",
                principal_type="user",
            ),
        )

    request_payload = invoke.call_args.kwargs["request_payload"]
    assert invocation["status"] == "succeeded"
    assert request_payload["contract_version"] == HOST_CONTRACT_VERSION
    assert request_payload["host"]["operation"] == "command"
    assert request_payload["host"]["scope"]["workflow_id"] == "weekly_report"
    assert request_payload["host"]["principal"]["principal_type"] == "user"
    assert request_payload["action"]["context"]["account_id"] == "acc_alpha"
    assert request_payload["action"]["context"]["project_id"] == "7"
    assert request_payload["action"]["target"] == {"scope": "row", "item_ids": ["a"]}
    assert request_payload["action"]["input"]["enabled"] is True
    assert "target" not in request_payload


def test_run_extension_action_hydrates_only_declared_runtime_resources():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    logger = MagicMock()
    logger.get_project_connection.return_value = {
        "project_id": "7",
        "extension_id": "acme.notifier",
        "connection_config": {"channels": []},
    }
    logger.get_provider_secret.return_value = "secret-value"
    logger.list_notification_delivery_logs.return_value = [
        {"delivery_id": "delivery-1", "provider_key": "acme.notifier"},
        {"delivery_id": "delivery-foreign", "provider_key": "foreign"},
    ]
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.notifier",
        "bundle_root": "/tmp/extensions/acme_notifier",
        "install_metadata": {
            "runtime_grants": {
                "resources": {
                    "project_connection": {"scope": "owning_extension"},
                    "provider_secrets": {
                        "provider_key": "acme.notifier",
                        "secret_keys": ["api_key"],
                    },
                    "notification_deliveries": {"provider_key": "acme.notifier"},
                }
            }
        },
        "entrypoint": {"command": ["python3", "main.py"]},
        "action_definitions": [
            {
                "action_id": "retry",
                "handler": {
                    "mode": "extension",
                    "config": {
                        "runtime_resources": {
                            "project_connection": True,
                            "provider_key": "acme.notifier",
                            "provider_secrets": ["api_key"],
                            "notification_deliveries": True,
                        }
                    },
                },
            }
        ],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        return_value={"status": "succeeded", "response": {"ok": True}},
    ) as invoke:
        invocation = run_extension_action(
            extension,
            action_id="retry",
            context={"project_id": "7"},
            runtime_context={"logger": logger, "account_id": "acc_a", "project_id": "7"},
        )

    resources = invoke.call_args.kwargs["runtime_context"]["resources"]
    assert invocation["operation_id"] == "retry"
    assert resources["project_connection"]["extension_id"] == "acme.notifier"
    assert resources["provider_secrets"] == {"api_key": "secret-value"}
    assert resources["notification_deliveries"] == [
        {"delivery_id": "delivery-1", "provider_key": "acme.notifier"}
    ]
    assert logger.get_provider_secret.call_args.kwargs["include_global"] is False


def test_run_extension_action_hydrates_the_exact_install_bound_connection():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    class Store:
        def __init__(self) -> None:
            self.filters: dict[str, object] = {}

        def get_project_connection(self, **filters):
            self.filters = dict(filters)
            if (
                filters.get("connection_id") == "connection-active"
                and filters.get("extension_install_id") == "install-active"
            ):
                return {
                    "connection_id": "connection-active",
                    "project_id": "7",
                    "extension_id": "acme.notifier",
                    "extension_install_id": "install-active",
                    "connection_config": {"source": "install-bound"},
                }
            return {
                "connection_id": "connection-legacy",
                "project_id": "7",
                "extension_id": "acme.notifier",
                "extension_install_id": "",
                "connection_config": {"source": "legacy"},
            }

    store = Store()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.notifier",
        "bundle_root": "/tmp/extensions/acme_notifier",
        "install_metadata": {
            "runtime_grants": {"resources": {"project_connection": {"scope": "owning_extension"}}}
        },
        "entrypoint": {"command": ["python3", "main.py"]},
        "action_definitions": [
            {
                "action_id": "poll_now",
                "handler": {
                    "mode": "extension",
                    "config": {"runtime_resources": {"project_connection": True}},
                },
            }
        ],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        return_value={"status": "succeeded", "response": {"ok": True}},
    ) as invoke:
        run_extension_action(
            extension,
            action_id="poll_now",
            context={"project_id": "7"},
            target={"connection_id": "connection-active"},
            runtime_context={
                "logger": store,
                "account_id": "account-1",
                "project_id": "7",
                "connection_id": "connection-active",
                "extension_install_id": "install-active",
            },
        )

    resources = invoke.call_args.kwargs["runtime_context"]["resources"]
    assert resources["project_connection"]["connection_id"] == "connection-active"
    assert resources["project_connection"]["extension_install_id"] == "install-active"
    assert store.filters == {
        "project_id": "7",
        "extension_id": "acme.notifier",
        "connection_id": "connection-active",
        "extension_install_id": "install-active",
        "enabled_only": False,
    }


def test_scan_ignores_bundle_controlled_install_runtime_grants(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    bundle = tmp_path / "acme_notifier"
    _write_minimal_extension(bundle, extension_id="acme.notifier")
    (bundle / ".fs-extension-install.yaml").write_text(
        """origin: system
runtime_grants:
  resources:
    provider_secrets:
      provider_key: notifier
      secret_keys: [api_key]
""",
        encoding="utf-8",
    )

    scanned = scan_extensions(str(tmp_path))

    assert "origin" not in scanned[0]
    assert scanned[0]["install_metadata"] == {"runtime_grants": {}}


def test_an_operation_cannot_reach_provider_secrets_without_a_declared_grant():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    logger = MagicMock()
    logger.get_provider_secret.return_value = "GLOBAL_SECRET"
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "evil.extension",
        "bundle_root": "/tmp/extensions/evil_extension",
        "entrypoint": {"command": ["python3", "main.py"]},
        "action_definitions": [
            {
                "action_id": "steal",
                "handler": {
                    "mode": "extension",
                    "config": {
                        "runtime_resources": {
                            "provider_key": "openai",
                            "provider_secrets": ["api_key"],
                        }
                    },
                },
            }
        ],
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        return_value={"status": "succeeded", "response": {"ok": True}},
    ) as invoke:
        run_extension_action(
            extension,
            action_id="steal",
            context={"project_id": "7"},
            runtime_context={"logger": logger, "project_id": "7"},
        )

    runtime = invoke.call_args.kwargs["runtime_context"]
    assert "resources" not in runtime
    logger.get_provider_secret.assert_not_called()


def test_platform_config_persists_but_retired_unbound_secret_operation_is_rejected():
    from core.application.extensions.platform_connection_operations import (
        install_platform_connection_operation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    class _Store:
        def __init__(self) -> None:
            self.connection = {
                "account_id": "acc_a",
                "project_id": "project_1",
                "extension_id": "acme.notifier",
                "connection_kind": "extension",
                "enabled": True,
                "connection_config": {"parse_mode": "HTML"},
                "secret_refs": {"refs": {"api_key": "project_connection:conn_1:api_key"}},
                "connection_routes": [],
                "ingress_config": {},
                "workflow_mappings": {},
            }
            self.secrets: dict[str, str] = {}

        def get_project_connection(self, **kwargs):
            _ = kwargs
            return dict(self.connection)

        def upsert_project_connection(self, **payload):
            self.connection = dict(payload)

        def save_provider_secret(self, **payload):
            self.secrets[str(payload["secret_key"])] = str(payload["secret_value"])
            return True

    store = _Store()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.notifier",
        "bundle_root": "/tmp/extensions/acme_notifier",
        "action_definitions": [
            {
                "action_id": "save_config",
                "handler": {
                    "mode": "platform",
                    "operation": "project_connection_config_patch",
                    "config": {"fields": ["parse_mode", "message_prefix"]},
                },
            },
            {
                "action_id": "save_secrets",
                "handler": {
                    "mode": "platform",
                    "operation": "provider_secrets_save",
                    "config": {
                        "provider_key": "notifier",
                        "secret_fields": [{"input": "api_key", "secret_key": "api_key"}],
                    },
                },
            },
        ],
    }

    config_runtime_context = {"logger": store}
    install_platform_connection_operation(config_runtime_context)
    config_result = run_extension_action(
        extension,
        action_id="save_config",
        context={"project_id": "project_1"},
        input_payload={"parse_mode": "MarkdownV2", "message_prefix": "[ops]"},
        runtime_context=config_runtime_context,
    )
    secret_result = run_extension_action(
        extension,
        action_id="save_secrets",
        context={"project_id": "project_1"},
        input_payload={"api_key": "secret-value"},
        runtime_context={"logger": store},
    )

    assert config_result["status"] == "succeeded"
    assert store.connection["connection_config"] == {
        "parse_mode": "MarkdownV2",
        "message_prefix": "[ops]",
    }
    assert store.connection["secret_refs"] == {
        "refs": {"api_key": "project_connection:conn_1:api_key"}
    }
    assert secret_result["status"] == "failed"
    assert secret_result["detail"] == "Unsupported platform action handler"
    assert store.secrets == {}


def test_retired_platform_provider_secret_save_is_not_dispatchable():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    store = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.notifier",
        "bundle_root": "/tmp/extensions/acme_notifier",
        "action_definitions": [
            {
                "action_id": "save_secrets",
                "handler": {
                    "mode": "platform",
                    "operation": "provider_secrets_save",
                    "config": {
                        "provider_key": "notifier",
                        "secret_fields": [{"input": "api_key", "secret_key": "api_key"}],
                    },
                },
            }
        ],
    }

    result = run_extension_action(
        extension,
        action_id="save_secrets",
        context={"project_id": "project_1"},
        input_payload={},
        runtime_context={"logger": store},
    )

    assert result["status"] == "failed"
    assert result["detail"] == "Unsupported platform action handler"
    store.save_provider_secret.assert_not_called()


def test_platform_config_save_creates_a_disabled_install_bound_typed_connection():
    from core.application.extensions.platform_connection_operations import (
        install_platform_connection_operation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    class Store:
        def __init__(self):
            self.saved = None
            self.get_kwargs = None

        def get_project_connection(self, **kwargs):
            self.get_kwargs = kwargs
            return

        def upsert_project_connection(self, **payload):
            self.saved = payload
            return {**payload, "connection_id": "conn-new"}

    store = Store()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.notifier",
        "action_definitions": [
            {
                "action_id": "save_config",
                "handler": {
                    "mode": "platform",
                    "operation": "project_connection_config_patch",
                    "config": {
                        "connection_type_id": "bot",
                        "fields": ["message_prefix"],
                    },
                },
            }
        ],
    }

    runtime_context = {
        "logger": store,
        "extension_install_id": "install-1",
        "account_id": "account-1",
    }
    install_platform_connection_operation(runtime_context)
    result = run_extension_action(
        extension,
        action_id="save_config",
        context={"project_id": "project-1"},
        input_payload={"message_prefix": "[ops]"},
        runtime_context=runtime_context,
    )

    assert result["status"] == "succeeded", result
    assert store.saved["extension_install_id"] == "install-1"
    assert store.saved["connection_type"] == "bot"
    assert store.saved["connection_kind"] == "extension"
    assert store.saved["account_id"] == "account-1"
    assert store.saved["enabled"] is False
    assert store.get_kwargs["extension_install_id"] == "install-1"


def test_platform_config_patch_validates_final_connection_schema_before_writing():
    from core.application.extensions.platform_connection_operations import (
        install_platform_connection_operation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    store = MagicMock()
    store.get_project_connection.return_value = {
        "connection_id": "conn-1",
        "connection_type": "bot",
        "connection_config": {"message_prefix": "short"},
    }
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.notifier",
        "connection_types": [
            {
                "connection_type_id": "bot",
                "config_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"message_prefix": {"type": "string", "maxLength": 8}},
                },
            }
        ],
        "action_definitions": [
            {
                "action_id": "save_config",
                "handler": {
                    "mode": "platform",
                    "operation": "project_connection_config_patch",
                    "config": {
                        "connection_type_id": "bot",
                        "fields": ["message_prefix"],
                    },
                },
            }
        ],
    }
    runtime_context = {"logger": store}
    install_platform_connection_operation(runtime_context)

    result = run_extension_action(
        extension,
        action_id="save_config",
        context={"project_id": "project-1"},
        input_payload={"message_prefix": "way too long"},
        runtime_context=runtime_context,
    )

    assert result["status"] == "failed"
    assert result["detail"] == "invalid_payload"
    assert result["response"]["errors"][0]["code"] == "invalid_payload"
    store.upsert_project_connection.assert_not_called()


def test_platform_config_patch_rejects_an_unknown_type_in_a_multi_type_contract():
    from core.application.extensions.platform_connection_operations import (
        install_platform_connection_operation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    store = MagicMock()
    store.get_project_connection.return_value = {"connection_config": {}}
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.multi",
        "connection_types": [
            {"connection_type_id": name, "config_schema": {"type": "object"}}
            for name in ("alpha", "beta")
        ],
        "action_definitions": [
            {
                "action_id": "save_config",
                "handler": {
                    "mode": "platform",
                    "operation": "project_connection_config_patch",
                    "config": {"connection_type_id": "missing", "fields": ["label"]},
                },
            }
        ],
    }
    runtime_context = {"logger": store}
    install_platform_connection_operation(runtime_context)

    result = run_extension_action(
        extension,
        action_id="save_config",
        context={"project_id": "project-1"},
        input_payload={"label": "value"},
        runtime_context=runtime_context,
    )

    assert result["status"] == "failed"
    assert result["detail"] == "invalid_payload"
    store.upsert_project_connection.assert_not_called()


def test_platform_config_patch_fails_closed_without_application_contract_port():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    store = MagicMock()
    store.get_project_connection.return_value = {"connection_config": {}}
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.notifier",
        "action_definitions": [
            {
                "action_id": "save_config",
                "handler": {
                    "mode": "platform",
                    "operation": "project_connection_config_patch",
                    "config": {"fields": ["label"]},
                },
            }
        ],
    }

    result = run_extension_action(
        extension,
        action_id="save_config",
        context={"project_id": "project-1"},
        input_payload={"label": "value"},
        runtime_context={"logger": store},
    )

    assert result["status"] == "failed"
    assert result["detail"] == "invalid_payload"
    store.upsert_project_connection.assert_not_called()


def test_emit_extension_event_records_and_enqueues_validated_event():
    from core.application.management.scope import ScopeContext
    from core.infrastructure.extension_runtime.extension_runtime import emit_extension_event

    mock_logger = MagicMock()
    mock_logger.record_domain_event.return_value = {
        "event_id": "evt_saved",
        "event_type": "job.failed",
        "account_id": "acc_alpha",
        "correlation_id": "corr-7",
    }
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "demo_notifier",
        "supported_events": ["job.*"],
    }

    result = emit_extension_event(
        extension,
        event_type="job.failed",
        correlation_id="corr-7",
        summary="Job failed",
        payload={"reason": "boom"},
        scope=ScopeContext(
            account_id="acc_alpha",
            project_id=7,
            actor_id="fixture_provider",
            principal_type="service_account",
        ),
        runtime_context={"logger": mock_logger},
    )

    assert result["status"] == "accepted"
    assert result["event"]["correlation_id"] == "corr-7"
    record_call = mock_logger.record_domain_event.call_args.kwargs
    assert record_call["account_id"] == "acc_alpha"
    assert record_call["correlation_id"] == "corr-7"
    enqueue_call = mock_logger.enqueue_domain_event.call_args.kwargs
    assert enqueue_call["account_id"] == "acc_alpha"
    assert enqueue_call["correlation_id"] == "corr-7"
    assert enqueue_call["envelope"]["schema_version"] == "event_envelope_v1"


def test_validate_compiled_manifest_rejects_invalid_event_subscription_selectors():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "features": ["notify"],
            "required_scopes": ["event:subscribe"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [],
            "event_subscriptions": [
                {"event_type": "job.*", "severity": "urgent", "conditions": "bad"},
                {"event_type": "", "severity": "info"},
            ],
            "account_scope": {
                "subscriptions": [{"event_type": "workflow.*", "project_id": "bad"}],
            },
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "invalid_event_subscription_severity" in error_codes
    assert "invalid_event_subscription_conditions" in error_codes
    assert "missing_event_subscription_event_type" in error_codes
    assert "invalid_event_subscription_project" in error_codes


def test_validate_compiled_manifest_accepts_valid_callback_declaration():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        _compiled_manifest_with_callback(
            {
                "callback_id": "updates",
                "purpose": "inbound_events",
                "methods": ["POST"],
                "action_id": "ingress_event",
                "connection_scoped": True,
                "auth": {
                    "type": "shared_secret_header",
                    "header": "X-Fixture-Callback-Secret",
                },
            }
        )
    )

    assert validation["status"] == "valid"


def test_validate_compiled_manifest_rejects_callback_with_unknown_action():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        _compiled_manifest_with_callback(
            {
                "callback_id": "updates",
                "purpose": "inbound_events",
                "methods": ["POST"],
                "action_id": "missing_action",
                "auth": {"type": "none"},
            }
        )
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "unknown_callback_action" in error_codes


def test_validate_compiled_manifest_rejects_unsupported_callback_auth_type():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    for auth_type in ("signed_state", "oauth"):
        validation = validate_compiled_manifest(
            _compiled_manifest_with_callback(
                {
                    "callback_id": "updates",
                    "purpose": "inbound_events",
                    "methods": ["POST"],
                    "action_id": "ingress_event",
                    "auth": {"type": auth_type},
                }
            )
        )

        assert validation["status"] == "invalid"
        error_codes = {str(item.get("code") or "") for item in validation["errors"]}
        assert "unsupported_callback_auth_type" in error_codes


def test_validate_compiled_manifest_rejects_shared_secret_callback_without_header():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        _compiled_manifest_with_callback(
            {
                "callback_id": "updates",
                "purpose": "inbound_events",
                "methods": ["POST"],
                "action_id": "ingress_event",
                "auth": {"type": "shared_secret_header"},
            }
        )
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "missing_callback_auth_header" in error_codes


def test_validate_compiled_manifest_rejects_ingress_event_profile_missing_action_contract():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "notification_provider",
            "features": ["notify", "ingress_event"],
            "supported_events": ["external.comment.received"],
            "required_scopes": ["event:subscribe"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "ingress_event_missing_action" in error_codes


def test_validate_compiled_manifest_accepts_ingress_event_profile_with_contract_fields():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "notification_provider",
            "release_stage": "placeholder",
            "features": ["notify", "ingress_event"],
            "supported_events": [
                "external.comment.received",
                "notification.interaction_received",
            ],
            "required_scopes": ["event:subscribe"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [
                {
                    "action_id": "ingress_event",
                    "required_scopes": ["extension:invoke"],
                    "result_fields": ["result.ingress_type", "result.job_id"],
                    "handler": {"mode": "extension"},
                }
            ],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "valid"


def test_validate_compiled_manifest_rejects_tool_provider_without_tool_skill():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "/tmp/extension.yaml",
            "kind": "tool_provider",
            "features": ["action"],
            "required_scopes": ["extension:invoke"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "query_definitions": [],
            "action_definitions": [{"action_id": "invoke_tool", "mutates_platform": False}],
            "ui_manifest": {"version": "1", "pages": []},
        }
    )

    assert validation["status"] == "invalid"
    error_codes = {str(item.get("code") or "") for item in validation["errors"]}
    assert "tool_provider_missing_feature" in error_codes


def test_compile_extension_bundle_derives_runtime_event_index_from_subscriptions(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    bundle = tmp_path / "extensions" / "acme_subscription_materialize"
    (bundle / "ui").mkdir(parents=True)
    (bundle / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": "acme.subscription-materialize",
                "kind": "notification_provider",
                "release_stage": "placeholder",
                "version": "1.0.0",
                "display_name": "Subscription Materialize",
                "description": "Materializes explicit event subscriptions.",
                "vendor": {"name": "Acme Test"},
                "features": ["notify"],
                "event_subscriptions": [
                    {"event_type": "job.failed", "severity": "any", "conditions": {}},
                    {"event_type": "job.completed", "severity": "any", "conditions": {}},
                ],
                "entrypoint": {"command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"]},
                "ui_manifest": "ui/ui_manifest.yaml",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text(
        json.dumps({"version": "1", "pages": []}),
        encoding="utf-8",
    )
    (bundle / "main.py").write_text("print('{}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")

    compiled = compile_extension_bundle(bundle, expected_kind="notification_provider")

    assert compiled["status"] == "valid"
    selectors = compiled["compiled_manifest"].get("event_subscriptions") or []
    assert [str(item.get("event_type") or "") for item in selectors] == [
        "job.failed",
        "job.completed",
    ]
    assert compiled["compiled_manifest"]["supported_events"] == ["job.completed", "job.failed"]


def test_all_shipped_active_notification_providers_satisfy_reserved_delivery_contract():
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    failures: dict[str, dict] = {}
    for manifest_path in (ROOT / "extensions").glob("*/extension.yaml"):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if str(manifest.get("release_stage") or "active") != "active":
            continue
        compiled = compile_extension_bundle(manifest_path.parent)
        if compiled["status"] != "valid":
            failures[str(manifest_path.parent)] = {
                "status": compiled.get("status"),
                "error": compiled.get("error"),
                "errors": compiled.get("errors"),
                "validation": compiled.get("validation"),
                "issues": compiled.get("issues"),
                "compile_errors": compiled.get("compile_errors"),
                "validation_errors": compiled.get("validation_errors"),
            }
        if manifest.get("kind") == "notification_provider":
            issue_codes = {str(issue.get("code") or "") for issue in compiled.get("errors") or []}
            assert "invalid_notification_provider_delivery_contract" not in issue_codes

    assert failures == {}


# ---------------------------------------------------------------------------
# E12-S1 — Isolation: crashing extension does not crash host
# ---------------------------------------------------------------------------


def test_crashing_extension_does_not_crash_host():
    """A segfault / non-zero exit in the extension must be reported as failure,
    not propagated to the caller."""
    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {"command": ["python3", "-c", "import sys; sys.exit(139)"]},
        },
        request_payload={"action": "crash_test"},
        timeout_seconds=5,
    )

    assert result["status"] == "failed"
    assert result["exit_code"] == 139
    # Host is still alive — this assertion running proves isolation.


def test_extension_timeout_does_not_hang_host():
    """An extension exceeding its timeout is killed, not awaited forever."""
    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {"command": ["python3", "-c", "import time; time.sleep(60)"]},
        },
        request_payload={},
        timeout_seconds=1,
    )

    assert result["status"] == "failed"
    assert "timed out" in result["detail"].lower() or "timeout" in result["detail"].lower()


def test_extension_invocation_timeout_returns_safe_error_and_structured_log(monkeypatch, caplog):
    import logging

    from core.infrastructure.extension_runtime import extension_runtime_invocation
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation

    command = ["python3", "-c", "print('PRIVATE_COMMAND_MARKER')"]

    def timeout(command, *, timeout, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    monkeypatch.setattr(extension_runtime_invocation, "_run_subprocess_capped", timeout)
    with caplog.at_level(logging.WARNING):
        result = run_extension_invocation(
            {
                "runtime": {"contract_version": "extension_host_v2"},
                "extension_key": "fixture.timeout",
                "bundle_root": str(Path.cwd()),
                "entrypoint": {"command": command},
            },
            request_payload={"action": {"action_id": "preview_query"}},
            runtime_context={"source": "registry_saved_queries"},
            timeout_seconds=1,
        )

    assert result["status"] == "failed"
    assert result["detail"] == "Saved query timed out after 1 second."
    assert result["errors"] == [
        {
            "code": "extension_timeout",
            "message": "Saved query timed out after 1 second.",
        }
    ]
    assert result["response"] == {}
    assert "PRIVATE_COMMAND_MARKER" not in result["detail"]
    timeout_records = [
        record for record in caplog.records if getattr(record, "extension_timeout", False)
    ]
    assert len(timeout_records) == 1
    assert timeout_records[0].extension_key == "fixture.timeout"
    assert timeout_records[0].extension_operation_id == "preview_query"
    assert timeout_records[0].extension_timeout_seconds == 1
    assert timeout_records[0].extension_exception_type == "TimeoutExpired"
    assert "PRIVATE_COMMAND_MARKER" not in caplog.text


def test_extension_env_is_filtered():
    """Extensions must not receive arbitrary host env vars (e.g. secrets)."""
    import os

    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()

    sentinel = "__TEST_SECRET_SHOULD_NOT_LEAK__"
    os.environ[sentinel] = "leaked"
    try:
        result = run_extension_invocation(
            {
                "runtime": {"contract_version": "extension_host_v2"},
                "bundle_root": str(Path.cwd()),
                "entrypoint": {
                    "command": [
                        "python3",
                        "-c",
                        f"import os,json; print(json.dumps({{'ok': True, "
                        f"'has_secret': '{sentinel}' in os.environ}}))",
                    ]
                },
            },
            request_payload={},
            timeout_seconds=5,
        )
    finally:
        os.environ.pop(sentinel, None)

    assert result["status"] == "succeeded"
    assert result["response"]["has_secret"] is False


def test_extension_env_allows_fs_db_dsn(monkeypatch):
    """FS-DEEP-CAND-010: extension subprocesses must NOT inherit the host DB DSN.

    Previously the DSN was allow-listed into the child env; it is now stripped so
    a malicious/compromised extension cannot exfiltrate platform DB credentials.
    """

    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()

    monkeypatch.setenv("FS_DB_DSN", "postgresql://demo")
    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    "import os,json; print(json.dumps({'ok': True, "
                    "'dsn': os.environ.get('FS_DB_DSN', '')}))",
                ]
            },
        },
        request_payload={},
        timeout_seconds=5,
    )

    assert result["status"] == "succeeded"
    # The DSN must not be visible inside the extension subprocess.
    assert result["response"]["dsn"] == ""


def test_invocation_serializes_runtime_context_for_subprocess():
    """Serializable runtime_context fields are forwarded on stdin for subprocess consumers."""

    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    class _Logger:
        dsn = "postgresql://runtime-logger"

    ExtensionCircuitBreaker.reset_all()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    "import json,sys; payload=json.loads(sys.stdin.read()); "
                    "print(json.dumps({'ok': True, **payload.get('runtime_context', {})}))",
                ]
            },
        },
        request_payload={"mode": "action"},
        timeout_seconds=5,
        runtime_context={
            "project_id": "proj_123",
            "job_id": 42,
            "logger": _Logger(),
            "logger_dsn": "postgresql://explicit-logger",
            "observability_dsn": "postgresql://explicit-observability",
        },
    )

    assert result["status"] == "succeeded"
    forwarded = result["response"]
    assert forwarded["project_id"] == "proj_123"
    assert forwarded["job_id"] == 42
    assert "logger_dsn" not in forwarded
    assert "observability_dsn" not in forwarded


@pytest.mark.parametrize(
    "request_payload",
    [
        {"action": "deliver_notification", "notification": {"title": "t"}},
        {"action": "invoke_tool", "contract_version": "extension_host_v1"},
    ],
)
def test_invocation_stamps_the_host_contract_on_every_payload(request_payload):
    """Notification delivery, tool calls and direct invoke reach the runtime with the contract."""

    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    "import json,sys; payload=json.loads(sys.stdin.read()); "
                    "print(json.dumps({'ok': True, 'seen': payload.get('contract_version'), "
                    "'action': payload.get('action')}))",
                ]
            },
        },
        request_payload=request_payload,
        timeout_seconds=5,
    )

    assert result["status"] == "succeeded"
    assert result["response"]["seen"] == "extension_host_v2"
    assert result["response"]["action"] == request_payload["action"]


def test_invocation_serializes_uuid_payloads_for_subprocess():
    """UUID values in request payloads are normalized before subprocess stdin JSON serialization."""
    import uuid

    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()
    sample_id = uuid.uuid4()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    "import json,sys; payload=json.loads(sys.stdin.read()); "
                    "print(json.dumps({'ok': True, **payload}))",
                ]
            },
        },
        request_payload={
            "event": {"event_id": sample_id},
            "route": {"route_id": sample_id},
        },
        timeout_seconds=5,
    )

    assert result["status"] == "succeeded"
    echoed = result["response"]
    assert echoed["event"]["event_id"] == str(sample_id)
    assert echoed["route"]["route_id"] == str(sample_id)


# ---------------------------------------------------------------------------
# E12-S2 — Circuit breaker and quarantine
# ---------------------------------------------------------------------------


def test_circuit_breaker_quarantines_after_threshold():
    """After N consecutive failures the breaker quarantines the extension."""
    from core.infrastructure.extension_runtime.extension_runtime import ExtensionCircuitBreaker

    ExtensionCircuitBreaker.reset_all()
    breaker = ExtensionCircuitBreaker(
        "test_ext",
        failure_threshold=3,
        quarantine_seconds=10,
    )

    # Simulate 3 failures
    for _ in range(3):
        assert breaker.pre_invoke() is None
        breaker.post_invoke(succeeded=False)

    # 4th call should be rejected
    rejection = breaker.pre_invoke()
    assert rejection is not None
    assert "quarantined" in rejection

    status = breaker.status()
    assert status["quarantined"] is True
    assert status["consecutive_failures"] == 3


def test_circuit_breaker_resets_on_success():
    """A single success resets the consecutive failure counter."""
    from core.infrastructure.extension_runtime.extension_runtime import ExtensionCircuitBreaker

    ExtensionCircuitBreaker.reset_all()
    breaker = ExtensionCircuitBreaker(
        "test_reset",
        failure_threshold=3,
        quarantine_seconds=10,
    )

    breaker.pre_invoke()
    breaker.post_invoke(succeeded=False)
    breaker.pre_invoke()
    breaker.post_invoke(succeeded=False)

    assert breaker.consecutive_failures == 2

    breaker.pre_invoke()
    breaker.post_invoke(succeeded=True)

    assert breaker.consecutive_failures == 0
    assert not breaker.is_quarantined


def test_structured_extension_errors_do_not_quarantine_the_runtime():
    """Domain validation errors must not be treated as subprocess crashes."""
    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()
    extension_key = "structured_error_test"
    script = (
        "import json, sys; "
        "print(json.dumps({'ok': False, 'error_code': 'invalid_config', "
        "'error': 'Invalid query', 'errors': [{'code': 'invalid_config', "
        "'message': 'Invalid query'}]})); sys.exit(2)"
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "bundle_root": str(Path.cwd()),
        "extension_key": extension_key,
        "entrypoint": {"command": [sys.executable, "-c", script]},
    }

    for _ in range(5):
        result = run_extension_invocation(extension, request_payload={}, timeout_seconds=5)
        assert result["status"] == "failed"
        assert result["response"]["ok"] is False

    breaker = ExtensionCircuitBreaker.for_extension(extension_key)
    assert breaker.consecutive_failures == 0
    assert breaker.is_quarantined is False


def test_circuit_breaker_concurrency_limit():
    """Concurrency cap rejects excess parallel calls."""
    from core.infrastructure.extension_runtime.extension_runtime import ExtensionCircuitBreaker

    ExtensionCircuitBreaker.reset_all()
    breaker = ExtensionCircuitBreaker(
        "test_concurrency",
        failure_threshold=99,
        quarantine_seconds=10,
        max_concurrent=2,
    )

    assert breaker.pre_invoke() is None  # slot 1
    assert breaker.pre_invoke() is None  # slot 2
    rejection = breaker.pre_invoke()  # should be rejected
    assert rejection is not None
    assert "concurrency" in rejection

    breaker.post_invoke(succeeded=True)  # free a slot
    assert breaker.pre_invoke() is None  # now allowed


def test_quarantined_extension_rejected_at_invocation_level():
    """run_extension_invocation returns quarantine status without spawning."""
    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()
    breaker = ExtensionCircuitBreaker.for_extension("quarantine_test")
    breaker._lock.acquire()
    breaker._consecutive_failures = breaker.failure_threshold
    breaker._quarantined_until = time.monotonic() + 60
    breaker._lock.release()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "extension_key": "quarantine_test",
            "entrypoint": {"command": ["python3", "-c", "print('should not run')"]},
        },
        request_payload={},
        timeout_seconds=5,
    )

    assert result["status"] == "failed"
    assert result.get("quarantined") is True
    assert "quarantined" in result["detail"]


# ---------------------------------------------------------------------------
# E12-S3 — Audit context propagation
# ---------------------------------------------------------------------------


def test_invocation_carries_audit_context():
    """runtime_context fields are surfaced in the result for audit logging."""
    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": ["python3", "-c", "import json; print(json.dumps({'ok': True}))"]
            },
        },
        request_payload={},
        timeout_seconds=5,
        runtime_context={
            "job_id": 42,
            "workflow_id": "onboarding_flow",
            "group_id": "checker",
            "for_each_id": None,
            "step_id": "step_3",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
            "account_id": "acct_001",
        },
    )

    assert result["status"] == "succeeded"
    ctx = result.get("audit_context", {})
    assert ctx["job_id"] == 42
    assert ctx["workflow_id"] == "onboarding_flow"
    assert ctx["group_id"] == "checker"
    assert ctx["for_each_id"] is None
    assert ctx["step_execution_id"] == "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"
    assert "phase_id" not in ctx
    assert ctx["step_id"] == "step_3"
    assert ctx["account_id"] == "acct_001"


def test_failed_invocation_traceable_to_audit_context():
    """A failed invocation still carries audit context for operator debugging."""
    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    'import sys; print(\'{"error": "boom"}\'); sys.exit(1)',
                ]
            },
        },
        request_payload={},
        timeout_seconds=5,
        runtime_context={
            "job_id": 99,
            "workflow_id": "deploy_flow",
            "account_id": "acct_002",
        },
    )

    assert result["status"] == "failed"
    ctx = result.get("audit_context", {})
    assert ctx["job_id"] == 99
    assert ctx["workflow_id"] == "deploy_flow"
    assert ctx["account_id"] == "acct_002"


def test_invocation_parses_large_json_before_truncating_stdout():
    from core.infrastructure.extension_runtime.extension_runtime import (
        ExtensionCircuitBreaker,
        run_extension_invocation,
    )

    ExtensionCircuitBreaker.reset_all()
    large_payload = {"ok": True, "result": {"blob": "x" * 12000}, "errors": []}

    result = run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(Path.cwd()),
            "entrypoint": {
                "command": [
                    "python3",
                    "-c",
                    (f"import json; print(json.dumps({large_payload!r}))"),
                ]
            },
        },
        request_payload={},
        timeout_seconds=5,
    )

    assert result["status"] == "succeeded"
    assert result["response"]["ok"] is True
    assert result["response"]["result"]["blob"] == "x" * 12000


def test_extension_action_without_an_owning_runtime_entrypoint_fails_closed():
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    result = run_extension_action(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "extension_id": "acme.missing-runtime",
            "bundle_root": str(Path.cwd()),
            "action_definitions": [{"action_id": "invoke_tool"}],
        },
        action_id="invoke_tool",
        input_payload={"tool_name": "measure", "arguments": {"value": "text"}},
    )

    assert result["status"] == "failed"
    assert result["detail"] == "No entrypoint command configured"
    assert result["command"] == []


def test_logger_accepts_audit_context_fields():
    """log_extension_invocation accepts job/workflow/group/step/execution scope."""
    try:
        from tests.support.platform_test_store import PlatformTestStore as PlatformLogger
    except ImportError:
        import pytest

        pytest.skip("psycopg_runtime not available outside Docker")

    # Just verify the method signature accepts the new kwargs without error.
    # Actual DB insert is skipped because _table_exists returns False in tests.
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock()
    mock_conn.cursor.return_value.__exit__ = MagicMock()
    bl = PlatformLogger.__new__(PlatformLogger)
    bl.conn = mock_conn
    bl._table_cache = {}
    bl._column_cache = {}
    bl._column_exists_cache = {}

    # Should not raise — signature accepts new fields
    bl.log_extension_invocation(
        extension_key="test_ext",
        invocation_type="action:deploy",
        status="failed",
        error_text="timeout",
        job_id=42,
        workflow_id="deploy_flow",
        group_id="build",
        step_id="compile",
        step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
        account_id="acct_001",
    )


def test_validate_compiled_manifest_rejects_workflow_trigger_without_explicit_schema():
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    validation = validate_compiled_manifest(
        {
            "manifest_version": 2,
            "manifest_path": "extensions/example/extension.yaml",
            "kind": "notification_provider",
            "features": ["action"],
            "healthcheck": {"command": ["python3", "health.py"]},
            "ui_manifest_ref": "ui/ui_manifest.yaml",
            "supported_events": ["external.task.created"],
            "action_definitions": [
                {
                    "action_id": "ingress_event",
                    "handler": {"mode": "extension"},
                }
            ],
            "workflow_triggers": [
                {
                    "trigger_type_id": "missing_schema",
                    "provider_key": "sample_tasks",
                    "display_name": "Broken Trigger",
                }
            ],
        }
    )

    assert validation["status"] == "invalid"
    codes = {item["code"] for item in validation["errors"]}
    assert "missing_workflow_trigger_config_schema" in codes
    assert "missing_workflow_trigger_ui_schema" in codes


def test_sandboxed_env_excludes_platform_secrets():
    # FS-DEEP-CAND-010: extension subprocesses must not inherit platform DB
    # credentials or the token-signing secret. Only safe runtime vars are allowed.
    from core.infrastructure.extension_runtime import extension_runtime_common as erc

    secret_keys = {
        "FS_DB_DSN",
        "DATABASE_URL",
        "POSTGRES_DSN",
        "FS_OBSERVABILITY_DSN",
        "FS_SECRET_KEY",
    }
    # None of the platform secrets are in the allow-list.
    assert secret_keys.isdisjoint(erc._ENV_ALLOWLIST)
    # Legitimate runtime vars stay allowed, but host PYTHONPATH is not inherited.
    assert {"PATH", "HOME"} <= erc._ENV_ALLOWLIST
    assert "FS_ALLOW_PRIVATE_REMOTE_URLS" in erc._ENV_ALLOWLIST
    assert {
        "FS_EXTENSION_ARTIFACT_MAX_BYTES",
        "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
    } <= erc._ENV_ALLOWLIST
    assert {"SSL_CERT_FILE", "SSL_CERT_DIR"} <= erc._ENV_ALLOWLIST
    assert "PYTHONPATH" not in erc._ENV_ALLOWLIST

    fake_environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/ext",
        "FS_ALLOW_PRIVATE_REMOTE_URLS": "1",
        "SSL_CERT_FILE": "/app/.ssl/cert.pem",
        "SSL_CERT_DIR": "/app/.ssl/certs",
        "PYTHONPATH": "/should/not/leak",
        "FS_DB_DSN": "postgresql://user:pass@db/app",
        "DATABASE_URL": "postgresql://user:pass@db/app",
        "POSTGRES_DSN": "postgresql://user:pass@db/app",
        "FS_OBSERVABILITY_DSN": "postgresql://user:pass@db/obs",
        "FS_SECRET_KEY": "top-secret-signing-key",
    }
    with patch.object(erc, "_runtime_env", fake_environ):
        env = erc._sandboxed_env()

    for key in secret_keys:
        assert key not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/ext"
    assert env["FS_ALLOW_PRIVATE_REMOTE_URLS"] == "1"
    assert env["SSL_CERT_FILE"] == "/app/.ssl/cert.pem"
    assert env["SSL_CERT_DIR"] == "/app/.ssl/certs"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "/should/not/leak" not in env["PYTHONPATH"]
    assert "flowsteward_extension_sdk" not in env["PYTHONPATH"]
    import json as _json

    assert "top-secret-signing-key" not in _json.dumps(env)


def _code_issue_codes(compiled: dict) -> list[str]:
    return [
        issue.get("code")
        for issue in compiled.get("validation_errors") or []
        if str(issue.get("code") or "").startswith("extension_code")
    ]


def test_inline_python_entrypoint_is_reviewed_like_a_file(tmp_path):
    """`python -c "..."` is a supported entrypoint; its code meets the same rules."""
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    in_file = tmp_path / "extensions" / "acme_in_file"
    _write_minimal_extension(in_file, extension_id="acme.in-file", main_source="eval('1 + 1')\n")
    inline = tmp_path / "extensions" / "acme_inline"
    _write_minimal_extension(inline, extension_id="acme.inline")
    manifest = yaml.safe_load((inline / "extension.yaml").read_text())
    manifest["entrypoint"]["command"] = [sys.executable, "-c", "eval('1 + 1')"]
    (inline / "extension.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    assert _code_issue_codes(compile_extension_bundle(in_file)) == ["extension_code_forbidden"]
    inline_compiled = compile_extension_bundle(inline)
    assert _code_issue_codes(inline_compiled) == ["extension_code_forbidden"]
    assert "<entrypoint -c>" in inline_compiled["validation_errors"][0]["message"]
