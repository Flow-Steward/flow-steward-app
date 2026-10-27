from __future__ import annotations

import copy
from functools import cache
from pathlib import Path

import pytest
import yaml

FIXTURE_EXTENSIONS_ROOT = Path(__file__).parent.parent / "fixtures" / "extensions"


def _discover_task_ingress_extension_ids() -> tuple[str, ...]:
    extension_ids: list[str] = []
    for bundle_root in sorted(FIXTURE_EXTENSIONS_ROOT.iterdir()):
        if not bundle_root.is_dir():
            continue
        manifest_path = bundle_root / "extension.yaml"
        if not manifest_path.is_file():
            continue
        try:
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        kind = str(payload.get("kind") or "").strip().lower()
        if kind != "task_provider":
            continue
        features = {str(item or "").strip().lower() for item in (payload.get("features") or [])}
        if "task_ingress" not in features:
            continue
        extension_ids.append(bundle_root.name)
    return tuple(extension_ids)


TASK_INGRESS_EXTENSION_IDS: tuple[str, ...] = _discover_task_ingress_extension_ids()


def _bundle_root(extension_id: str) -> Path:
    return FIXTURE_EXTENSIONS_ROOT / extension_id


@cache
def _compiled_manifest(extension_id: str) -> dict:
    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    compiled = compile_extension_bundle(_bundle_root(extension_id), expected_kind="task_provider")
    assert compiled["status"] == "valid", f"{extension_id} must compile cleanly for conformance"
    return copy.deepcopy(compiled.get("compiled_manifest") or {})


def _runtime_extension(extension_id: str) -> dict:
    manifest = _compiled_manifest(extension_id)
    return {
        "extension_key": str(manifest.get("extension_id") or extension_id),
        "bundle_root": str(_bundle_root(extension_id)),
        "entrypoint": manifest.get("entrypoint") or {},
        "compiled_manifest": manifest,
        "query_definitions": manifest.get("query_definitions") or [],
        "action_definitions": manifest.get("action_definitions") or [],
    }


def _declared_result_fields(operation: dict) -> set[str]:
    payload = operation if isinstance(operation, dict) else {}
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    values: list[str] = []
    for source in (payload.get("result_fields"), contract.get("result_fields")):
        if isinstance(source, list):
            values.extend(str(item or "").strip() for item in source if str(item or "").strip())
    return set(values)


def _declared_error_codes(operation: dict) -> set[str]:
    payload = operation if isinstance(operation, dict) else {}
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    values: list[str] = []
    for source in (payload.get("error_codes"), contract.get("error_codes")):
        if isinstance(source, list):
            values.extend(str(item or "").strip().lower().replace("-", "_") for item in source)
    return {item for item in values if item}


def test_task_ingress_discovery_finds_reference_bundles() -> None:
    expected = {"fixture_task_ingress_provider"}
    discovered = set(TASK_INGRESS_EXTENSION_IDS)
    assert discovered, "No task_ingress fixture extensions discovered under tests/fixtures/extensions/*"
    assert expected.issubset(discovered), (
        f"Missing expected reference bundles. discovered={sorted(discovered)} "
        f"expected_subset={sorted(expected)}"
    )


@pytest.mark.parametrize("extension_id", TASK_INGRESS_EXTENSION_IDS)
def test_task_ingress_manifest_declares_profile_contract(extension_id: str):
    from core.infrastructure.extension_runtime.extension_runtime import (
        TASK_INGRESS_ERROR_CODES,
        TASK_INGRESS_FEATURE_ID,
        TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES,
        TASK_INGRESS_REQUIRED_ACTION_IDS,
        TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS,
        TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES,
        TASK_INGRESS_REQUIRED_QUERY_IDS,
        TASK_INGRESS_REQUIRED_QUERY_RESULT_FIELDS,
    )

    manifest = _compiled_manifest(extension_id)
    features = {str(item or "").strip() for item in (manifest.get("features") or [])}
    actions = {
        str(row.get("action_id") or "").strip(): row
        for row in (manifest.get("action_definitions") or [])
        if isinstance(row, dict) and str(row.get("action_id") or "").strip()
    }
    queries = {
        str(row.get("query_id") or "").strip(): row
        for row in (manifest.get("query_definitions") or [])
        if isinstance(row, dict) and str(row.get("query_id") or "").strip()
    }

    assert TASK_INGRESS_FEATURE_ID in features
    for action_id in TASK_INGRESS_REQUIRED_ACTION_IDS:
        assert action_id in actions
    for query_id in TASK_INGRESS_REQUIRED_QUERY_IDS:
        assert query_id in queries

    for action_id, required_fields in TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS.items():
        result_fields = _declared_result_fields(actions[action_id])
        for required_field in required_fields:
            assert required_field in result_fields
    for query_id, required_fields in TASK_INGRESS_REQUIRED_QUERY_RESULT_FIELDS.items():
        result_fields = _declared_result_fields(queries[query_id])
        for required_field in required_fields:
            assert required_field in result_fields

    allowed_error_codes = {
        str(code).strip().lower().replace("-", "_") for code in TASK_INGRESS_ERROR_CODES
    }
    for action_id, action_payload in actions.items():
        for error_code in _declared_error_codes(action_payload):
            assert error_code in allowed_error_codes, (
                f"{extension_id}:{action_id} declares unsupported error code '{error_code}'"
            )
    for query_id, query_payload in queries.items():
        for error_code in _declared_error_codes(query_payload):
            assert error_code in allowed_error_codes, (
                f"{extension_id}:{query_id} declares unsupported error code '{error_code}'"
            )

    for action_id, required_error_codes in TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES.items():
        declared_error_codes = _declared_error_codes(actions[action_id])
        for required_error_code in required_error_codes:
            normalized_error_code = str(required_error_code).strip().lower().replace("-", "_")
            assert normalized_error_code in declared_error_codes
    for query_id, required_error_codes in TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES.items():
        declared_error_codes = _declared_error_codes(queries[query_id])
        for required_error_code in required_error_codes:
            normalized_error_code = str(required_error_code).strip().lower().replace("-", "_")
            assert normalized_error_code in declared_error_codes


@pytest.mark.parametrize("extension_id", TASK_INGRESS_EXTENSION_IDS)
def test_task_ingress_normalize_action_returns_canonical_result(extension_id: str):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    invocation = run_extension_action(
        _runtime_extension(extension_id),
        action_id="normalize_task_action",
        input_payload={
            "project_id": "7",
            "task_id": "task-1",
            "id": "task-1",
            "title": "Task title",
            "description": "Task description",
            "attachments": [],
            "task": {
                "id": "task-1",
                "name": "Task title",
                "description": "Task description",
                "attachments": [],
            },
        },
    )

    assert invocation["status"] == "succeeded"
    response = invocation.get("response") or {}
    assert response.get("ok") is True
    result = response.get("result") or {}
    normalized = result.get("normalized") or {}
    assert isinstance(normalized, dict)
    assert str(normalized.get("task_id") or "").strip()


@pytest.mark.parametrize("extension_id", TASK_INGRESS_EXTENSION_IDS)
def test_task_ingress_fetch_attachments_returns_result_attachments(extension_id: str):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    invocation = run_extension_action(
        _runtime_extension(extension_id),
        action_id="fetch_attachments",
        input_payload={"attachments": []},
    )

    assert invocation["status"] == "succeeded"
    response = invocation.get("response") or {}
    assert response.get("ok") is True
    result = response.get("result") or {}
    assert isinstance(result.get("attachments"), list)


@pytest.mark.parametrize("extension_id", TASK_INGRESS_EXTENSION_IDS)
def test_task_ingress_sync_completion_status_returns_result_status(extension_id: str):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    invocation = run_extension_action(
        _runtime_extension(extension_id),
        action_id="sync_completion_status",
        input_payload={"task_id": "task-1", "final_status": "in_progress"},
    )

    assert invocation["status"] == "succeeded"
    response = invocation.get("response") or {}
    assert response.get("ok") is True
    result = response.get("result") or {}
    assert str(result.get("status") or "").strip()


@pytest.mark.parametrize("extension_id", TASK_INGRESS_EXTENSION_IDS)
def test_task_ingress_provider_health_query_returns_status_and_configured(extension_id: str):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

    invocation = run_extension_query(
        _runtime_extension(extension_id),
        query_id="provider_health",
        params={},
    )

    assert invocation["status"] == "succeeded"
    response = invocation.get("response") or {}
    assert response.get("ok") is True
    result = response.get("result") or {}
    assert str(result.get("status") or "").strip()
    assert isinstance(result.get("configured"), bool)
