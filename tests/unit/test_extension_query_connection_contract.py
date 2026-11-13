"""Compile-time rules for queries and pages that run with a connection (ADR 0067)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

from core.application.extensions.extension_descriptor import DEFAULT_FS_RUNTIME_VERSION
from core.application.extensions.query_connection_contract import (
    query_connection_type_ids,
    validate_query_connection_contract,
)


def _manifest(**overrides) -> dict:
    manifest = {
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": "1.7.0"},
        },
        "connection_types": [
            {"connection_type_id": "service_profile"},
            {"connection_type_id": "backup_profile"},
        ],
        "external_effects": [],
        "query_definitions": [
            {
                "query_id": "runs.list",
                "connection": {"connection_type_ids": ["service_profile"]},
                "handler": {"mode": "extension"},
            },
            {"query_id": "about", "handler": {"mode": "extension"}},
        ],
        "ui_manifest": {
            "pages": [
                {
                    "page_id": "runs",
                    "project_scoped": True,
                    "connection": {"connection_type_ids": ["service_profile"]},
                    "refresh_seconds": 30,
                    "components": [
                        {"component_id": "runs_grid", "type": "grid", "query_id": "runs.list"},
                        {"component_id": "about", "type": "markdown", "query_id": "about"},
                    ],
                }
            ]
        },
    }
    manifest.update(overrides)
    return manifest


def _codes(manifest: dict) -> list[str]:
    issues: list[dict] = []
    validate_query_connection_contract(manifest, issues=issues, manifest_path="extension.yaml")
    return [issue["code"] for issue in issues if issue["severity"] == "error"]


def _query(manifest: dict, query_id: str = "runs.list") -> dict:
    return next(row for row in manifest["query_definitions"] if row["query_id"] == query_id)


def _page(manifest: dict) -> dict:
    return manifest["ui_manifest"]["pages"][0]


def test_a_valid_declaration_passes():
    assert _codes(_manifest()) == []


def test_the_host_speaks_contract_1_7_0():
    assert DEFAULT_FS_RUNTIME_VERSION == "1.7.0"


@pytest.mark.parametrize(
    "connection",
    [
        {},
        {"connection_type_ids": []},
        {"connection_type_ids": "service_profile"},
        {"connection_type_id": "service_profile"},
        {"connection_type_ids": ["service_profile", "service_profile"]},
        {"connection_type_ids": ["service_profile"], "required": False},
        {"connection_type_ids": [""]},
        ["service_profile"],
    ],
)
def test_a_malformed_query_connection_is_refused(connection):
    manifest = _manifest()
    _query(manifest)["connection"] = connection

    assert "query_connection_invalid" in _codes(manifest)
    assert query_connection_type_ids(_query(manifest)) == frozenset()


def test_a_query_connection_names_only_own_connection_types():
    manifest = _manifest()
    _query(manifest)["connection"] = {"connection_type_ids": ["service_profile", "foreign"]}

    assert _codes(manifest) == ["query_connection_type_unknown"]


@pytest.mark.parametrize("mode", ["platform", "core", "workflow"])
def test_a_connection_query_is_an_extension_handler(mode):
    manifest = _manifest()
    _query(manifest)["handler"] = {"mode": mode}

    assert _codes(manifest) == ["query_connection_handler_mode"]


def test_a_connection_query_is_not_an_external_effect():
    manifest = _manifest(external_effects=[{"operation_id": "runs.list"}])

    assert _codes(manifest) == ["query_connection_external_effect"]


@pytest.mark.parametrize("platform_min", ["", "1.6.0", "1.6.9", "not-a-version"])
def test_a_connection_query_requires_host_1_7_0(platform_min):
    manifest = _manifest()
    manifest["runtime"]["compatibility"] = {"platform_min": platform_min}

    assert _codes(manifest) == ["query_connection_host_minimum"]


def test_a_bundle_without_connection_queries_keeps_any_minimum():
    manifest = _manifest()
    del _query(manifest)["connection"]
    del _page(manifest)["connection"]
    manifest["runtime"]["compatibility"] = {"platform_min": "1.0.0"}

    assert _codes(manifest) == []


def test_a_connection_query_on_a_page_without_connection_is_refused():
    manifest = _manifest()
    del _page(manifest)["connection"]

    assert _codes(manifest) == ["page_connection_missing"]


def test_a_connection_query_used_as_a_load_query_or_button_counts_as_used():
    manifest = _manifest()
    del _page(manifest)["connection"]
    _page(manifest)["components"] = [
        {
            "component_id": "settings",
            "type": "form",
            "data": {"load_query": "about"},
            "actions": [{"label": "Reload", "kind": "query", "query_id": "runs.list"}],
        }
    ]

    assert _codes(manifest) == ["page_connection_missing"]


def test_every_connection_query_on_a_page_accepts_every_page_type():
    manifest = _manifest()
    _page(manifest)["connection"] = {"connection_type_ids": ["service_profile", "backup_profile"]}

    assert _codes(manifest) == ["page_connection_type_mismatch"]


def test_a_page_type_subset_of_the_query_types_is_accepted():
    manifest = _manifest()
    _query(manifest)["connection"] = {"connection_type_ids": ["service_profile", "backup_profile"]}

    assert _codes(manifest) == []


@pytest.mark.parametrize(
    "connection",
    [{}, {"connection_type_ids": []}, {"connection_type_ids": ["foreign"]}, "service_profile"],
)
def test_a_malformed_page_connection_is_refused(connection):
    manifest = _manifest()
    _page(manifest)["connection"] = connection

    assert "page_connection_invalid" in _codes(manifest)


def test_a_connection_page_is_project_scoped():
    manifest = _manifest()
    _page(manifest)["project_scoped"] = False

    assert _codes(manifest) == ["page_connection_requires_project"]


def test_an_ordinary_query_may_appear_on_a_connection_page():
    manifest = _manifest()
    _page(manifest)["components"] = [
        {"component_id": "about", "type": "markdown", "query_id": "about"}
    ]

    assert _codes(manifest) == []


@pytest.mark.parametrize("value", [9, 3601, 0, -10, 30.5, "30", True, None])
def test_refresh_seconds_is_bounded(value):
    manifest = _manifest()
    _page(manifest)["refresh_seconds"] = value

    assert _codes(manifest) == ["page_refresh_invalid"]


@pytest.mark.parametrize("value", [10, 3600])
def test_refresh_seconds_accepts_its_bounds(value):
    manifest = _manifest()
    _page(manifest)["refresh_seconds"] = value

    assert _codes(manifest) == []


def test_refresh_seconds_needs_a_query_on_the_page():
    manifest = _manifest()
    page = _page(manifest)
    del page["connection"]
    page["components"] = [{"component_id": "help", "type": "markdown", "data": {"text": "Hi"}}]

    assert _codes(manifest) == ["page_refresh_invalid"]


# --- end to end: the compiler keeps the declarations and refuses a bad bundle ---------------

_CONNECTION_TYPE = {
    "connection_type_id": "service_profile",
    "display_name": "Service profile",
    "config_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"base_url": {"type": "string"}},
    },
    "secret_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"service_token": {"type": "string"}},
    },
}


def _write_bundle(root: Path, *, platform_min: str = "1.7.0") -> Path:
    bundle = root / "extensions" / "example_connector"
    (bundle / "ui" / "queries").mkdir(parents=True)
    (bundle / "ui" / "pages").mkdir(parents=True)
    manifest = {
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": platform_min},
            "extension_contract_v2": {"connection_types": [copy.deepcopy(_CONNECTION_TYPE)]},
        },
        "manifest_version": 2,
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": "example.connector",
        "kind": "tool_provider",
        "version": "1.0.0",
        "display_name": "Example connector",
        "description": "Fixture",
        "vendor": {"name": "Example"},
        "features": ["tool"],
        "required_scopes": ["extension:invoke"],
        "entrypoint": {"mode": "subprocess", "command": [sys.executable, "main.py"]},
        "health": {"command": [sys.executable, "health.py"], "timeout_seconds": 5},
        "ui_manifest": "ui/ui_manifest.yaml",
        "query_manifest": "ui/queries/queries.yaml",
    }
    files = {
        "extension.yaml": manifest,
        "ui/ui_manifest.yaml": {"version": "1", "pages": [{"ref": "pages/runs.yaml"}]},
        "ui/pages/runs.yaml": {
            "page_id": "runs",
            "title": "Runs",
            "connection": {"connection_type_ids": ["service_profile"]},
            "refresh_seconds": 30,
            "components": [
                {
                    "component_id": "runs_grid",
                    "type": "grid",
                    "query_id": "runs.list",
                    "columns": [{"key": "run_id", "label": "Run", "type": "text"}],
                }
            ],
        },
        "ui/queries/queries.yaml": {
            "queries": [
                {
                    "query_id": "runs.list",
                    "title": "Runs",
                    "connection": {"connection_type_ids": ["service_profile"]},
                    "handler": {"mode": "extension"},
                }
            ]
        },
    }
    for relative, payload in files.items():
        (bundle / relative).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    (bundle / "main.py").write_text("print('{}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
    return bundle


def test_the_compiler_keeps_query_and_page_connections(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    compiled = compile_extension_bundle(_write_bundle(tmp_path))

    assert compiled["status"] == "valid", (
        compiled.get("compile_errors"),
        compiled.get("validation_errors"),
    )
    query = compiled["compiled_manifest"]["query_definitions"][0]
    assert query["connection"] == {"connection_type_ids": ["service_profile"]}
    page = compiled["compiled_manifest"]["ui_manifest"]["pages"][0]
    assert page["connection"] == {"connection_type_ids": ["service_profile"]}
    assert page["refresh_seconds"] == 30


def test_the_compiler_refuses_a_connection_query_below_host_1_7_0(tmp_path):
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    compiled = compile_extension_bundle(_write_bundle(tmp_path, platform_min="1.6.0"))

    assert compiled["status"] == "invalid"
    assert "query_connection_host_minimum" in {
        issue["code"] for issue in compiled["validation_errors"]
    }
