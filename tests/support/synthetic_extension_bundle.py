"""Build a synthetic `acme.*` extension bundle on disk for platform tests.

Core's tests must not depend on which extensions ship. Anything that needs a real
bundle — descriptor import, install/enable, artifact projection — gets one from
here instead of pointing at `extensions/`, so the platform suite keeps working
when bundles move to their own repositories.

The counts are deliberately small and are the test's to choose: what a shipped
bundle happens to contain is that bundle's business, asserted in its own suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.support.extension_icon import write_extension_icon

EXTENSION_ID = "acme.showcase"
BUNDLE_DIR_NAME = "acme_showcase"

#: One artifact of each kind the projection machinery handles, so a test can
#: assert every branch without vendoring a real bundle's contents.
DEFAULT_ARTIFACT_COUNTS = {
    "workflows": 2,
    "agents": 1,
    "skills": 1,
    "tools": 1,
    "datasets": 1,
    "saved_queries": 1,
}
_ARTIFACT_ID_KEYS = {
    "workflows": "workflow_id",
    "agents": "agent_id",
    "skills": "skill_id",
    "tools": "tool_id",
    "datasets": "dataset_id",
    "saved_queries": "query_template_id",
}
_PROJECTION_KINDS = {
    "workflows": "workflow",
    "agents": "agent",
    "skills": "skill",
    "tools": "tool",
    "datasets": "dataset",
    "saved_queries": "saved_query",
}


def projection_counts(counts: dict[str, int] | None = None) -> dict[str, int]:
    """Return the per-kind projection counts a bundle with these artifacts declares."""
    resolved = dict(counts or DEFAULT_ARTIFACT_COUNTS)
    return {_PROJECTION_KINDS[name]: total for name, total in resolved.items()}


def _artifact_body(kind: str, artifact_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        _ARTIFACT_ID_KEYS[kind]: artifact_id,
        "display_name": artifact_id.replace("_", " ").title(),
        "description": f"Synthetic {kind[:-1]} for platform tests.",
        "enabled": True,
    }
    if kind == "workflows":
        # A workflow the readiness validator accepts: an entry step that does real
        # work, routed to an explicit stop_success terminal. Anything less
        # is rejected, which would make every test using this fixture fail for a
        # reason that has nothing to do with what it is testing.
        body["steps"] = [
            {
                "step_id": "fetch_record",
                "display_name": "Fetch Record",
                "step_kind": "http_request",
                "override": {
                    "http_request": {
                        "url": "https://records.example.test/api/records",
                        "method": "GET",
                        "auth_mode": "none",
                        "response_target": "state.records",
                        "timeout_seconds": 20,
                        "expected_statuses": [200],
                    }
                },
            },
            {
                "step_id": "done",
                "display_name": "Done",
                "step_kind": "stop_success",
                "override": {"stop_success": {"reason": "synthetic fixture complete"}},
            },
        ]
        body["groups"] = []
        body["settings"] = {
            "execution_graph": {
                "schema_version": 1,
                "entry": {"kind": "step", "id": "fetch_record"},
                "edges": [
                    {
                        "source": {"kind": "step", "id": "fetch_record"},
                        "outcome": {"name": "success"},
                        "target": {"kind": "step", "id": "done"},
                        "rework": False,
                    }
                ],
            }
        }
    elif kind == "agents":
        # Wired to a tool so the platform's agent projection has an edge to follow.
        body["tools"] = ["acme_tool_1"]
    elif kind == "skills":
        body["input_schema"] = {"type": "object", "properties": {"text": {"type": "string"}}}
        body["output_schema"] = {"type": "object", "properties": {"label": {"type": "string"}}}
    elif kind == "tools":
        body["tool_type"] = "builtin"
    elif kind == "datasets":
        body.update(
            {
                "query_language": "sql",
                "read_only": True,
                "resource_scope_jsonb": {
                    "version": 1,
                    "resources": [{"resource_id": "records", "columns": ["record_id", "status"]}],
                },
            }
        )
    elif kind == "saved_queries":
        body.pop("saved_query_id", None)
        body.update(
            {
                "query_template_id": artifact_id,
                "dataset_id": "acme_dataset_1",
                "query_language": "sql",
                "version": 1,
                "query_definition_jsonb": {
                    "sql": "SELECT record_id, status FROM records WHERE status = 'open' LIMIT 10"
                },
                "params_schema_jsonb": {"type": "object", "properties": {}, "required": []},
            }
        )
    return body


def write_bundle(
    root: Path,
    *,
    extension_id: str = EXTENSION_ID,
    counts: dict[str, int] | None = None,
) -> Path:
    """Write the bundle under ``root`` and return its directory."""
    resolved = dict(counts or DEFAULT_ARTIFACT_COUNTS)
    bundle = root / BUNDLE_DIR_NAME
    (bundle / "ui").mkdir(parents=True, exist_ok=True)

    artifacts: dict[str, list[dict[str, Any]]] = {}
    for kind, total in resolved.items():
        rows: list[dict[str, Any]] = []
        for index in range(1, total + 1):
            artifact_id = f"acme_{_PROJECTION_KINDS[kind]}_{index}"
            ref = f"artifacts/{kind}/{artifact_id}.yaml"
            path = bundle / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(_artifact_body(kind, artifact_id), sort_keys=False),
                encoding="utf-8",
            )
            rows.append({_ARTIFACT_ID_KEYS[kind]: artifact_id, "ref": ref, "publish": True})
        artifacts[kind] = rows

    descriptor = {
        "schema_version": "extension_descriptor_v1",
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": extension_id,
        "release_version": "1.0.0",
        "extension_type": "service_extension",
        "identity": {
            "display_name": "Acme Showcase",
            "description": "Synthetic bundle for platform tests.",
            "tags": ["test"],
        },
        "vendor": {"vendor_name": "Acme Test", "support_url": "https://acme.invalid/issues"},
        "listing": {"visibility": "public_reference", "category": "developer_examples"},
        "distribution": {
            "channel": "private_byo",
            "execution_modes": ["local"],
            "allows_self_hosted": True,
            "requires_connected_control_plane": False,
            "import_sources": ["file"],
        },
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": "0.1.0"},
        },
        "provenance": {"origin": "imported", "notes": "Synthetic fixture."},
        "commerce": {"billing_mode": "free"},
        "artifacts": artifacts,
    }
    (bundle / "extension.yaml").write_text(
        yaml.safe_dump(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": extension_id,
                "kind": "tool_provider",
                "version": "1.0.0",
                "display_name": "Acme Showcase",
                "description": "Synthetic bundle for platform tests.",
                "vendor": {"name": "Acme Test"},
                "features": ["tool"],
                "required_scopes": ["extension:invoke"],
                "entrypoint": {"command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"]},
                "ui_manifest": "ui/ui_manifest.yaml",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (bundle / "extension-descriptor.yaml").write_text(
        yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8"
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n", encoding="utf-8")
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
    write_extension_icon(bundle)
    return bundle


def descriptor_path(root: Path, **kwargs: Any) -> Path:
    """Write the bundle and return the path to its descriptor."""
    return write_bundle(root, **kwargs) / "extension-descriptor.yaml"


#: Contract v2 surfaces a bundle can declare. Written here so platform tests can
#: exercise the projection without a shipped bundle's contract standing in for it.
_CONTRACT_V2_FILES = {
    "operation_manifest": {
        "operations": [
            {
                "operation_id": "fetch_record",
                "title": "Fetch Record",
                "kind": "source",
                "inputs": [{"name": "connection_ref", "type": "connection_ref"}],
                "outputs": [{"name": "record", "type": "object"}],
            }
        ]
    },
    "connection_types": {
        "connection_types": [
            {
                "connection_type_id": "acme_record_source",
                "display_name": "Acme record source",
                "description": "Synthetic connection type for platform tests.",
                "config_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["acme_host"],
                    "properties": {"acme_host": {"type": "string"}},
                },
                "secret_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["password"],
                    "properties": {"password": {"type": "string"}},
                },
            }
        ]
    },
}


def write_contract_v2_bundle(root: Path, *, extension_id: str = "acme.record-source") -> Path:
    """Write a bundle that declares contract v2 surfaces, and return its directory."""
    bundle = root / "acme_record_source"
    (bundle / "contracts").mkdir(parents=True, exist_ok=True)
    (bundle / "ui").mkdir(parents=True, exist_ok=True)
    refs = {}
    for name, payload in _CONTRACT_V2_FILES.items():
        (bundle / "contracts" / f"{name}.yaml").write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        refs[name] = f"contracts/{name}.yaml"
    descriptor = {
        "schema_version": "extension_descriptor_v1",
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": extension_id,
        "release_version": "1.0.0",
        "extension_type": "service_extension",
        "identity": {
            "display_name": "Acme Record Source",
            "description": "Synthetic bundle for platform tests.",
            "tags": ["test"],
        },
        "vendor": {"vendor_name": "Acme Test", "support_url": "https://acme.invalid/issues"},
        "listing": {"visibility": "public_reference", "category": "developer_examples"},
        "distribution": {
            "channel": "private_byo",
            "execution_modes": ["local"],
            "allows_self_hosted": True,
            "requires_connected_control_plane": False,
            "import_sources": ["file"],
        },
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": "0.1.0"},
            "extension_contract_v2": refs,
        },
        "provenance": {"origin": "imported", "notes": "Synthetic fixture."},
        "commerce": {"billing_mode": "free"},
        # A descriptor must declare at least one artifact; one tool is enough to
        # make this a valid bundle without saying anything about its contract.
        "artifacts": {
            "tools": [
                {
                    "tool_id": "acme_tool_1",
                    "ref": "artifacts/tools/acme_tool_1.yaml",
                    "publish": True,
                }
            ]
        },
    }
    (bundle / "artifacts" / "tools").mkdir(parents=True, exist_ok=True)
    (bundle / "artifacts" / "tools" / "acme_tool_1.yaml").write_text(
        yaml.safe_dump(_artifact_body("tools", "acme_tool_1"), sort_keys=False), encoding="utf-8"
    )
    (bundle / "extension-descriptor.yaml").write_text(
        yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8"
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n", encoding="utf-8")
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
    write_extension_icon(bundle)
    return bundle
