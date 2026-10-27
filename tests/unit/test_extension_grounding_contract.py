"""Capability discovery and integration grounding read an installed contract.

Against a descriptor built here, not a shipped bundle: what is under test is how
the platform reads a contract, and pointing it at an extension made this suite
assert that extension's operations. What a real bundle declares is pinned in its
own suite.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.application.workflows.discovery.capability_contracts import tool_operations
from core.application.workflows.discovery.capability_discovery import (
    WorkflowCapabilityDiscovery,
)
from core.application.workflows.discovery.capability_discovery_helpers import _tokens
from core.application.workflows.discovery.scoped_capability_catalog import (
    ScopedCapabilityCatalog,
)
from tests.support.synthetic_grounding_descriptor import (
    CONNECTOR_ID,
    EXTENSION_ID,
    FILTER_PROPERTIES,
    GROUNDING_EFFECTS,
    OPERATION_KINDS,
    installed_release_descriptor,
)

EXPECTED_OPERATION_KINDS = OPERATION_KINDS
EXPECTED_GROUNDING_EFFECTS = GROUNDING_EFFECTS


def _installed_release_descriptor() -> dict:
    return installed_release_descriptor()


class _InstalledExtensionDB:
    def __init__(self, descriptor: dict) -> None:
        install = SimpleNamespace(
            account_id="acc-alpha",
            extension_id=EXTENSION_ID,
            install_id="install-acme",
            release_id="release-acme",
            state="active",
            visibility="account",
        )
        release = SimpleNamespace(
            extension_id=EXTENSION_ID,
            release_id="release-acme",
            descriptor=descriptor,
        )
        self.extension_install_store = SimpleNamespace(
            list_for_account=lambda *_args, **_kwargs: [install]
        )
        self.extension_runtime_store = SimpleNamespace(
            get_release=lambda release_id: release if release_id == "release-acme" else None
        )

    @staticmethod
    def list_tools_summary(**_kwargs) -> list[dict]:
        return []

    @staticmethod
    def get_tool(_tool_id: str) -> None:
        return None


def test_an_installed_extension_is_scoped_and_preserves_its_grounding_contract() -> None:
    descriptor = _installed_release_descriptor()
    source_search = next(
        operation
        for operation in descriptor["operation_manifest"]
        if operation["operation_id"] == "search_records"
    )
    source_filters = next(row for row in source_search["inputs"] if row["name"] == "filters")
    db = _InstalledExtensionDB(descriptor)
    scoped_catalog = ScopedCapabilityCatalog(db)
    scope = {"account_id": "acc-alpha", "project_id": "project-alpha"}
    summaries = scoped_catalog.tool_summaries(scope=scope, project_id="project-alpha")
    assert {row["tool_id"] for row in summaries} == {CONNECTOR_ID}
    hydrated = scoped_catalog.hydrate_visible_capabilities(
        scope=scope,
        project_id="project-alpha",
        capability_refs=[{"type": "tool", "id": CONNECTOR_ID}],
    )[("tool", CONNECTOR_ID, "")]
    assert hydrated["artifact_policies"] == descriptor["artifact_policies"]
    assert hydrated["external_effects"] == descriptor["external_effects"]
    assert hydrated.get("operation_recipes", []) == []
    operations = {row["operation_id"]: row for row in tool_operations(hydrated)}
    filters = next(
        row for row in operations["search_records"]["input_rows"] if row["name"] == "filters"
    )
    assert filters["schema"] == source_filters["schema"]
    assert "raw_search" not in filters["schema"]["properties"]
    assert {row["name"] for row in operations["get_attachment"]["output_rows"]} == {
        "attachment_artifact_handle",
        "attachment_metadata",
    }


def test_an_installed_filter_schema_reaches_discovery_and_live_contract() -> None:
    descriptor = _installed_release_descriptor()
    db = _InstalledExtensionDB(descriptor)
    discovery = WorkflowCapabilityDiscovery(db)

    candidates = discovery._tool_candidates(
        _tokens("acme record source search records by label owner and unread status"),
        scope={"account_id": "acc-alpha", "project_id": "project-alpha"},
        project_id="project-alpha",
    )
    search_candidate = next(
        candidate for candidate in candidates if candidate.operation_id == "search_records"
    )
    filters_contract = next(
        row for row in search_candidate.input_contracts if row.get("name") == "filters"
    )
    assert set(filters_contract["schema"]["properties"]) == set(FILTER_PROPERTIES)

    scoped = ScopedCapabilityCatalog(db)
    tool = scoped.assert_capability_visible(
        scope={"account_id": "acc-alpha", "project_id": "project-alpha"},
        project_id="project-alpha",
        capability_ref={"type": "tool", "id": CONNECTOR_ID},
    )
    described = next(
        row for row in tool_operations(tool) if row["operation_id"] == "search_records"
    )
    described_filters = next(row for row in described["input_rows"] if row["name"] == "filters")
    assert described_filters["schema"] == filters_contract["schema"]
    assert "raw_search" not in described_filters["schema"]["properties"]
