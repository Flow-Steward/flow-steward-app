"""Deterministic synthetic catalog indexes used for scale and contract tests.

The builders produce an index of any size. The scale tests ask for 1,000
workflow entries and validate and filter them in memory rather than committing
a megabyte of generated JSON; ``render`` is there for anyone who wants to write
one out while working on the contract.

The published contract itself is documented, with placeholder digests, in
``docs/catalogs/static-catalog-contracts.md``. Generated indexes are
deliberately not committed: their SHA-256 fields are indistinguishable from
secrets to the repository secret gate, and a second copy of the contract would
only drift from the first.

These are metadata fixtures only: no workflow YAML and no extension ZIP is
produced here, and nothing in the product creates 1,000 workflows.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: What the scale tests ask the builders for.
WORKFLOW_CATALOG_SCALE_ITEM_COUNT = 1000
EXTENSION_CATALOG_SCALE_ITEM_COUNT = 40

_ARTIFACT_HOST = "https://catalog.example.test"
_CATEGORIES = ("productivity", "sales", "support", "engineering", "finance")
_INTEGRATIONS = ("gmail", "slack", "acme_store", "acme_cms", "postgres", "webhook")
_TRIGGERS = ("manual", "schedule", "webhook", "event")


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def build_workflow_template_index(
    count: int = WORKFLOW_CATALOG_SCALE_ITEM_COUNT,
    *,
    catalog_revision: str = "2026-09-18.1",
) -> dict[str, Any]:
    """Build a valid workflow template index with *count* metadata entries."""
    items: list[dict[str, Any]] = []
    for index in range(count):
        item_id = f"synthetic_template_{index:04d}"
        category = _CATEGORIES[index % len(_CATEGORIES)]
        integration = _INTEGRATIONS[index % len(_INTEGRATIONS)]
        trigger = _TRIGGERS[index % len(_TRIGGERS)]
        version = f"1.{index % 12}.{index % 5}"
        items.append(
            {
                "item_id": item_id,
                "version": version,
                "name": f"Synthetic template {index:04d}",
                "summary": f"A {category} template driven by {integration}.",
                "description": (
                    f"Synthetic catalog entry {index:04d}. Used for scale testing the "
                    "catalog index contract; it publishes no runnable workflow."
                ),
                "categories": [category],
                "tags": [category, integration, trigger],
                "integrations": [integration],
                "trigger_types": [trigger],
                "setup_requirements": [
                    {
                        "kind": "extension",
                        "label": f"{integration} extension",
                        "ref": f"synthetic_{integration}",
                        "required": True,
                    }
                ],
                "compatible_flow_steward": {"min": "0.1.0", "max": ""},
                "bundle_schema": "platform.workflow_bundle.v2",
                "workflow_yaml_url": f"{_ARTIFACT_HOST}/workflows/{item_id}-{version}.yaml",
                "workflow_yaml_bytes": 1024 + index,
                "workflow_yaml_sha256": _digest(f"workflow:{item_id}:{version}"),
                "publication_status": "deprecated" if index % 97 == 0 else "published",
                "verification_status": "unverified" if index % 13 == 0 else "verified",
            }
        )
    return {
        "schema_version": "workflow_template_catalog/v1",
        "catalog_revision": catalog_revision,
        "published_at": "2026-09-18T00:00:00Z",
        "items": items,
    }


def build_extension_index(
    count: int = EXTENSION_CATALOG_SCALE_ITEM_COUNT,
    *,
    catalog_revision: str = "2026-09-18.1",
) -> dict[str, Any]:
    """Build a valid extension catalog index with *count* metadata entries."""
    items: list[dict[str, Any]] = []
    for index in range(count):
        extension_id = f"synthetic_extension_{index:03d}"
        category = _CATEGORIES[index % len(_CATEGORIES)]
        version = f"2.{index % 11}.{index % 7}"
        items.append(
            {
                "extension_id": extension_id,
                "version": version,
                "name": f"Synthetic extension {index:03d}",
                "summary": f"A {category} connector used for catalog scale tests.",
                "description": f"Synthetic catalog entry {index:03d}. Ships no real release.",
                "categories": [category],
                "capabilities": ["queries", "actions"],
                "repository_url": f"https://github.example.test/flow-steward/{extension_id}",
                "release_zip_url": f"{_ARTIFACT_HOST}/extensions/{extension_id}-{version}.zip",
                "release_zip_bytes": 65536 + index,
                "release_zip_sha256": _digest(f"extension:{extension_id}:{version}"),
                "compatible_flow_steward": {"min": "0.1.0", "max": ""},
                "permissions_summary": ["outbound_http", "project_connection"],
                "setup_summary": "Configure one project connection before enabling.",
                "publication_status": "published",
                "verification_status": "verified",
                "icon": {
                    "url": f"{_ARTIFACT_HOST}/extensions/{extension_id}/icon.png",
                    "sha256": _digest(f"icon:{extension_id}"),
                    "bytes": 2048 + index,
                    "mime_type": "image/png",
                    "size": "128x128",
                },
            }
        )
    return {
        "schema_version": "extension_catalog/v1",
        "catalog_revision": catalog_revision,
        "published_at": "2026-09-18T00:00:00Z",
        "items": items,
    }


def render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


if __name__ == "__main__":  # pragma: no cover - maintenance entrypoint
    print(render(build_workflow_template_index(3)))
