"""Workflow import/export portability smoke and negative-contract tests."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest
import yaml

from tests.e2e.helpers import delete, get, post_json, require_ok, unique_id

pytestmark = [pytest.mark.e2e]

_PLACEHOLDER_REFS = {"none", "null", "undefined", "n/a", "-"}
_GENERATED_PREFIXES = ("audit_import_", "agent_", "e2e_", "eval_", "test_")


def _workflow_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("workflows") or payload.get("items") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _workflow_id(row: dict[str, Any]) -> str:
    return str(row.get("workflow_id") or row.get("id") or "").strip()


def _safe_target_id(workflow_id: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_]+", "_", workflow_id).strip("_")[:72]
    return f"audit_import_probe_{suffix or unique_id('workflow')}"


def _product_workflow_ids() -> list[str]:
    resp = get("/api/workflows")
    require_ok(resp, "list workflows for import/export audit", allow_403=True)
    ids = []
    for row in _workflow_rows(resp.json()):
        workflow_id = _workflow_id(row)
        if not workflow_id or workflow_id.startswith(_GENERATED_PREFIXES):
            continue
        ids.append(workflow_id)
    return ids


def _first_importable_export() -> tuple[str, str]:
    for workflow_id in _product_workflow_ids():
        export_resp = get(f"/api/workflows/{workflow_id}/export.yaml", timeout=30)
        if export_resp.status_code != 200:
            continue
        bundle_yaml = export_resp.text
        preflight = post_json(
            "/api/workflows/import/preflight",
            {
                "bundle_yaml": bundle_yaml,
                "target_workflow_id": f"{unique_id('wf_import')}_{workflow_id[:24]}",
            },
            timeout=30,
        )
        if preflight.status_code == 200 and preflight.json().get("can_apply"):
            return workflow_id, bundle_yaml
    pytest.fail("No importable workflow export found for workflow import/export tests")


def _dependency_ref_is_placeholder(dependency: dict[str, Any]) -> bool:
    return str(dependency.get("ref") or "").strip().lower() in _PLACEHOLDER_REFS


def test_exported_workflows_preflight_without_false_none_dependencies():
    """Every product workflow export should preflight without placeholder dependency leaks."""

    failures: list[dict[str, Any]] = []
    audited = 0
    for workflow_id in _product_workflow_ids():
        export_resp = get(f"/api/workflows/{workflow_id}/export.yaml", timeout=30)
        if export_resp.status_code == 404:
            continue
        require_ok(export_resp, f"export workflow {workflow_id}", allow_403=True)
        bundle_yaml = export_resp.text
        preflight_resp = post_json(
            "/api/workflows/import/preflight",
            {
                "bundle_yaml": bundle_yaml,
                "target_workflow_id": _safe_target_id(workflow_id),
            },
            timeout=30,
        )
        require_ok(preflight_resp, f"preflight exported workflow {workflow_id}", allow_403=True)
        preflight = preflight_resp.json()
        dependencies = [
            item for item in preflight.get("dependencies", []) if isinstance(item, dict)
        ]
        placeholder_dependencies = [
            item for item in dependencies if _dependency_ref_is_placeholder(item)
        ]
        unexpected_blocking = [
            item
            for item in dependencies
            if item.get("blocking") and item.get("dependency_type") != "extension_workflow_ref"
        ]
        if placeholder_dependencies or unexpected_blocking:
            failures.append(
                {
                    "workflow_id": workflow_id,
                    "placeholder_dependencies": placeholder_dependencies,
                    "unexpected_blocking": unexpected_blocking,
                }
            )
        audited += 1

    assert audited, "No workflows were available for import/export audit"
    assert not failures, json.dumps(failures[:10], indent=2, sort_keys=True)


def test_import_preflight_negative_contracts_are_explicit():
    workflow_id, bundle_yaml = _first_importable_export()

    malformed = post_json(
        "/api/workflows/import/preflight",
        {"bundle_yaml": "schema_version: [\nkind: workflow_bundle\n"},
    )
    assert malformed.status_code == 400
    assert malformed.json()["detail"]["message"] == "YAML bundle could not be parsed."

    missing_id_bundle = yaml.safe_load(bundle_yaml)
    missing_id_bundle["workflows"][0].pop("workflow_id", None)
    missing_id = post_json(
        "/api/workflows/import/preflight",
        {
            "bundle_yaml": yaml.safe_dump(missing_id_bundle, sort_keys=False),
            "target_workflow_id": f"{unique_id('wf_missing_id')}_{workflow_id[:16]}",
        },
    )
    require_ok(missing_id, "preflight missing workflow id")
    missing_id_dependency = next(
        item
        for item in missing_id.json()["dependencies"]
        if (item.get("metadata") or {}).get("code") == "workflow_id_missing"
    )
    assert missing_id_dependency["status"] == "blocked"
    assert missing_id_dependency["blocking"] is True
    assert missing_id_dependency["message"] == "Workflow definition is missing workflow_id."

    none_child_bundle = yaml.safe_load(bundle_yaml)
    dependencies = none_child_bundle.setdefault("dependencies", {})
    dependencies["child_workflows"] = [
        {
            "workflow_id": "None",
            "display_name": "Missing workflow id",
            "required": True,
            "sources": ["e2e.invalid_child_workflow"],
            "metadata": {"code": "invalid_dependency_ref", "raw_ref": "None"},
        }
    ]
    none_child = post_json(
        "/api/workflows/import/preflight",
        {
            "bundle_yaml": yaml.safe_dump(none_child_bundle, sort_keys=False),
            "target_workflow_id": f"{unique_id('wf_none_child')}_{workflow_id[:16]}",
        },
    )
    require_ok(none_child, "preflight invalid None child workflow")
    none_dependency = next(
        item
        for item in none_child.json()["dependencies"]
        if item.get("dependency_type") == "child_workflow" and item.get("ref") == "None"
    )
    assert none_dependency["status"] == "blocked"
    assert none_dependency["resolution_actions"] == ["abort"]
    assert "without a usable id" in none_dependency["message"]

    conflict = post_json(
        "/api/workflows/import/preflight",
        {"bundle_yaml": bundle_yaml, "target_workflow_id": ""},
    )
    require_ok(conflict, "preflight existing workflow id conflict")
    conflict_dependency = next(
        item
        for item in conflict.json()["dependencies"]
        if (item.get("metadata") or {}).get("code") == "workflow_id_conflict"
    )
    assert conflict_dependency["status"] == "conflict"
    assert conflict_dependency["resolution_actions"] == ["rename_workflow", "abort"]


def test_import_apply_creates_disabled_draft_and_can_be_reopened():
    workflow_id, bundle_yaml = _first_importable_export()
    target_workflow_id = f"{unique_id('wf_imported')}_{workflow_id[:20]}"

    try:
        apply_resp = post_json(
            "/api/workflows/import/apply",
            {
                "bundle_yaml": bundle_yaml,
                "target_workflow_id": target_workflow_id,
                "resolutions": [],
            },
            timeout=30,
        )
        require_ok(apply_resp, "apply workflow import")
        result = apply_resp.json()
        assert result["status"] == "imported"
        assert result["workflow_id"] == target_workflow_id
        assert result["imported_workflows"][0]["enabled"] is False
        assert result["imported_workflows"][0]["import_state"] == "draft"

        detail_resp = get(f"/api/workflows/{target_workflow_id}", timeout=30)
        require_ok(detail_resp, "open imported workflow detail")
        detail = detail_resp.json()
        assert detail["workflow_id"] == target_workflow_id
        assert detail["enabled"] is False
        assert detail["projection_mode"] == "manual"
        assert (detail.get("settings") or {}).get("portability", {}).get("import_state") == "draft"
    finally:
        if not os.getenv("E2E_KEEP_IMPORTED_WORKFLOWS"):
            delete(f"/api/workflows/{target_workflow_id}", timeout=30)
