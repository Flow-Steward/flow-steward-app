"""Authoring catalog behavior at the real visibility and contract boundaries."""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceNotFoundError
from core.application.workflows.authoring.catalog import WorkflowAuthoringCatalog
from core.application.workflows.discovery import capability_discovery as discovery_module
from core.application.workflows.discovery.capability_discovery import (
    WorkflowCapabilityDiscovery,
)

SCOPE = {"account_id": DEFAULT_ACCOUNT_ID, "project_id": "project"}


@pytest.fixture(autouse=True)
def clear_knowledge_cache():
    discovery_module._WORKFLOW_KNOWLEDGE_SYNC_CACHE.clear()
    yield
    discovery_module._WORKFLOW_KNOWLEDGE_SYNC_CACHE.clear()


class CatalogDB:
    def __init__(self):
        self.workflows = {}
        self.tools = {}
        self.agents = {}
        self.skills = {}
        self.datasets = {}
        self.connections = []
        self.workflow_reads = []

    def get_project(self, project_id):
        return {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}

    def get_workflow(self, workflow_id, include_trashed=False):
        assert workflow_id, "catalog discovery must not load absent workflow context"
        self.workflow_reads.append(workflow_id)
        return deepcopy(self.workflows.get(workflow_id))

    def get_tool(self, tool_id):
        return deepcopy(self.tools.get(tool_id))

    def get_agent(self, agent_id):
        return deepcopy(self.agents.get(agent_id))

    def get_skill(self, skill_id):
        return deepcopy(self.skills.get(skill_id))

    def get_dataset(self, dataset_id):
        return deepcopy(self.datasets.get(dataset_id))

    def list_workflows_summary(self, **_):
        return list(self.workflows.values())

    def list_tools_summary(self, **_):
        return list(self.tools.values())

    def list_agents_summary(self, **_):
        return list(self.agents.values())

    def list_skills_summary(self, **_):
        return list(self.skills.values())

    def list_project_workflows(self, **_):
        return []

    def list_datasets(self, **_):
        return list(self.datasets.values())

    def list_project_connections(self, **_):
        return self.connections


def tool(tool_id, **extra):
    return {
        "tool_id": tool_id,
        "display_name": "Publish",
        "enabled": True,
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "account",
        **extra,
    }


def test_search_without_any_workflow_uses_permitted_catalog_and_limit():
    db = CatalogDB()
    db.tools = {
        "publish": tool("publish"),
        "foreign": tool("foreign", account_id="other"),
        "disabled": tool("disabled", enabled=False),
    }
    result = WorkflowAuthoringCatalog(db).search(
        scope=SCOPE, project_id="project", query="publish", kinds=["tool"], limit=1
    )
    assert [item["ref"] for item in result["capabilities"]] == ["tool:publish"]
    assert db.workflow_reads == []


def test_discovery_does_not_read_current_workflow_canvas():
    db = CatalogDB()
    db.workflows["current"] = {"workflow_id": "current"}
    discovery = WorkflowCapabilityDiscovery(db)
    discovery.discover(workflow_id="", request_text="publish")
    discovery.discover(workflow_id="current", request_text="publish")
    assert db.workflow_reads == []


def test_describe_resolves_live_operation_contract_without_shortlist_and_redacts_connections():
    db = CatalogDB()
    db.tools["publish"] = tool(
        "publish",
        interface={
            "operations": [
                {
                    "operation_id": "send",
                    "parameters": [{"name": "body", "type": "text", "required": True}],
                    "returns": [{"name": "receipt", "type": "text"}],
                    "connection_type_ids": ["mail"],
                },
                {"operation_id": "list", "parameters": [{"name": "folder", "type": "text"}]},
            ]
        },
    )
    db.connections = [
        {
            "connection_id": "local",
            "project_id": "project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_type_id": "mail",
            "enabled": True,
            "health_status": "ready",
            "connection_config": {
                "password": "secret",  # pragma: allowlist secret - redaction fixture
            },
            "secret_refs": ["secret"],
        },
        {"connection_id": "other", "project_id": "other", "account_id": DEFAULT_ACCOUNT_ID},
    ]
    result = WorkflowAuthoringCatalog(db).describe(
        scope=SCOPE, project_id="project", ref="tool:publish", operation_id="send"
    )
    assert [row["name"] for row in result["inputs"]] == ["body"]
    assert [row["name"] for row in result["outputs"]] == ["receipt"]
    assert [row["connection_id"] for row in result["connections"]] == ["local"]
    assert "secret" not in str(result)
    catalog = WorkflowAuthoringCatalog(db)
    with pytest.raises(ServiceNotFoundError):
        catalog.describe(
            scope=SCOPE, project_id="project", ref="tool:publish", operation_id="unknown"
        )


def test_examples_export_protected_workflow_as_reference_and_enforce_token_workflows():
    db = CatalogDB()
    db.workflows = {
        "protected": {
            "workflow_id": "protected",
            "display_name": "Publish",
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
            "source_install_id": "install",
            "projection_mode": "extension",
            "_phases": [{"phase_id": "secret_internal"}],
        },
        "denied": {
            "workflow_id": "denied",
            "display_name": "Publish",
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
        },
    }
    scope = {**SCOPE, "allowed_workflow_ids": {"protected"}}
    catalog = WorkflowAuthoringCatalog(db)
    result = catalog.examples(scope=scope, project_id="project", query="publish", limit=5)
    assert [row["workflow_id"] for row in result["examples"]] == ["protected"]
    bundle = yaml.safe_load(result["examples"][0]["bundle_yaml"])
    assert bundle["workflows"][0]["portability_kind"] == "extension_workflow_ref"
    assert "secret_internal" not in str(result)
    with pytest.raises(ServiceNotFoundError):
        catalog.describe(scope=scope, project_id="project", ref="workflow:denied")


def test_graph_search_rechecks_live_scope_and_token_restrictions():
    from core.application.workflows.knowledge_projection import (
        WorkflowKnowledgeHit,
        WorkflowKnowledgeNode,
        WorkflowKnowledgeProjection,
    )
    from tests.unit.test_workflow_capability_discovery import (
        _FakeWorkflowKnowledgeBackend,
    )

    db = CatalogDB()
    db.workflows = {
        "allowed": {
            "workflow_id": "allowed",
            "display_name": "Live",
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
        },
        "denied": {
            "workflow_id": "denied",
            "display_name": "Secret",
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
        },
    }
    nodes = [
        WorkflowKnowledgeNode(
            id=f"workflow:{ref}",
            kind="workflow",
            label="Graph match",
            metadata={"workflow_id": ref, "enabled": True},
        )
        for ref in ["allowed", "denied"]
    ]
    hits = [
        WorkflowKnowledgeHit(
            entity_id=node.id,
            entity_kind=node.kind,
            title=node.label,
            score=95,
            snippet="Graph match",
        )
        for node in nodes
    ]
    backend = _FakeWorkflowKnowledgeBackend(
        hits=hits, projection=WorkflowKnowledgeProjection(nodes=nodes)
    )
    catalog = WorkflowAuthoringCatalog(db, workflow_knowledge_backend=backend)
    scope = {**SCOPE, "allowed_workflow_ids": {"allowed"}}
    # Simulates a scope synchronized by the existing offline owner.
    backend.set_scope_sync_digest(
        account_id=DEFAULT_ACCOUNT_ID,
        project_id="project",
        digest=catalog.discovery._catalog_sync_digest(scope=scope, project_id="project"),
    )
    result = catalog.search(
        scope=scope, project_id="project", query="semantic synonym", kinds=["workflow"]
    )
    assert [row["ref"] for row in result["capabilities"]] == ["workflow:allowed"]
    assert result["workflow_knowledge"]["freshness"] == "fresh"
    assert backend.upsert_calls == []
    assert "Secret" not in str(result)


@pytest.mark.parametrize(
    "live_policy",
    [
        {"visible_in_catalog": False},
        {"enabled": False},
        {"deleted_at": "2026-10-07T10:00:00Z"},
        {"new_runs_blocked": True},
        {"availability_status": "unavailable"},
    ],
)
def test_authoring_rechecks_live_policy_for_stale_graph_candidates(live_policy):
    from core.application.workflows.knowledge_projection import (
        WorkflowKnowledgeHit,
        WorkflowKnowledgeNode,
        WorkflowKnowledgeProjection,
    )
    from tests.unit.test_workflow_capability_discovery import _FakeWorkflowKnowledgeBackend

    db = CatalogDB()
    db.workflows["hidden"] = {
        "workflow_id": "hidden",
        "display_name": "Private live label",
        "enabled": True,
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "account",
        **live_policy,
    }
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id="workflow:hidden",
                entity_kind="workflow",
                score=95,
                title="Stale public label",
            )
        ],
        projection=WorkflowKnowledgeProjection(
            nodes=[
                WorkflowKnowledgeNode(
                    id="workflow:hidden",
                    kind="workflow",
                    label="Stale public label",
                    metadata={"workflow_id": "hidden", "enabled": True, "visible_in_catalog": True},
                )
            ]
        ),
    )
    catalog = WorkflowAuthoringCatalog(db, workflow_knowledge_backend=backend)
    backend.set_scope_sync_digest(
        account_id=DEFAULT_ACCOUNT_ID,
        project_id="project",
        digest=catalog.discovery._catalog_sync_digest(scope=SCOPE, project_id="project"),
    )

    discovered = catalog.discovery.discover(
        workflow_id="",
        request_text="semantic synonym",
        candidate_kinds=frozenset({"workflow"}),
        scope=SCOPE,
        project_id="project",
        candidate_limit=100,
    )
    assert [candidate.id for candidate in discovered["candidates"]] == ["hidden"]
    assert discovered["workflow_knowledge"]["freshness"] == "fresh"

    search = catalog.search(
        scope=SCOPE, project_id="project", query="semantic synonym", kinds=["workflow"]
    )
    examples = catalog.examples(scope=SCOPE, project_id="project", query="semantic synonym")

    assert "hidden" in db.workflow_reads
    assert search["capabilities"] == []
    assert search["no_match"] is True
    assert examples["examples"] == []
    assert examples["no_match"] is True
    assert "Private live label" not in str(search)
    with pytest.raises(ServiceNotFoundError):
        catalog.describe(scope=SCOPE, project_id="project", ref="workflow:hidden")


def test_graph_origin_does_not_disclose_protected_or_restricted_step_labels():
    from core.application.workflows.knowledge_projection import (
        WorkflowKnowledgeHit,
        WorkflowKnowledgeNode,
        WorkflowKnowledgeProjection,
    )
    from tests.unit.test_workflow_capability_discovery import (
        _FakeWorkflowKnowledgeBackend,
    )

    db = CatalogDB()
    db.agents["public_agent"] = {
        "agent_id": "public_agent",
        "display_name": "Public agent",
        "enabled": True,
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "account",
    }
    step = WorkflowKnowledgeNode(
        id="step:restricted:secret",
        kind="step",
        label="Private salary export",
        metadata={"workflow_id": "restricted", "agent_id": "public_agent", "enabled": True},
    )
    agent = WorkflowKnowledgeNode(
        id="agent:public_agent",
        kind="agent",
        label="Stale private label",
        metadata={"agent_id": "public_agent", "enabled": True},
    )
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id=step.id, entity_kind=step.kind, title=step.label, score=95
            )
        ],
        projection=WorkflowKnowledgeProjection(nodes=[step, agent]),
    )
    catalog = WorkflowAuthoringCatalog(db, workflow_knowledge_backend=backend)
    scope = {**SCOPE, "allowed_workflow_ids": {"permitted"}}
    backend.set_scope_sync_digest(
        account_id=DEFAULT_ACCOUNT_ID,
        project_id="project",
        digest=catalog.discovery._catalog_sync_digest(scope=scope, project_id="project"),
    )
    result = catalog.search(
        scope=scope, project_id="project", query="semantic synonym", kinds=["agent"]
    )
    assert [row["display_name"] for row in result["capabilities"]] == ["Public agent"]
    assert "Private salary" not in str(result)
    assert "Stale private" not in str(result)
    assert "restricted" not in str(result)


def test_connection_setup_link_uses_existing_project_editor_and_real_connection_type():
    db = CatalogDB()
    db.tools["publish"] = tool(
        "publish",
        interface={"operations": [{"operation_id": "send", "connection_type_ids": ["mail"]}]},
    )
    db.connections = [
        {
            "connection_id": "local",
            "project_id": "project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_type": "mail",
            "enabled": True,
        }
    ]
    described = WorkflowAuthoringCatalog(db).describe(
        scope=SCOPE, project_id="project", ref="tool:publish", operation_id="send"
    )
    assert described["connections"][0]["setup_url"] == "/projects/project?tab=connections"


def test_kind_filter_precedes_discovery_shortlist_limit():
    db = CatalogDB()
    db.tools = {f"tool_{index}": tool(f"tool_{index}") for index in range(120)}
    db.workflows["secondary"] = {
        "workflow_id": "secondary",
        "display_name": "Secondary",
        "description": "Publish report",
        "enabled": True,
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "account",
    }
    found = WorkflowAuthoringCatalog(db).search(
        scope=SCOPE, project_id="project", query="publish", kinds=["workflow"], limit=1
    )
    assert [row["id"] for row in found["capabilities"]] == ["secondary"]


def test_token_denied_workflows_do_not_exhaust_lexical_hydration_budget():
    db = CatalogDB()
    db.workflows = {
        f"denied_{index}": {
            "workflow_id": f"denied_{index}",
            "display_name": "Publish",
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
        }
        for index in range(250)
    }
    db.workflows["permitted"] = {
        "workflow_id": "permitted",
        "display_name": "Secondary",
        "description": "Publish report",
        "enabled": True,
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "account",
    }
    scope = {**SCOPE, "allowed_workflow_ids": {"permitted"}}
    found = WorkflowAuthoringCatalog(db).search(
        scope=scope, project_id="project", query="publish", kinds=["workflow"], limit=1
    )
    assert [row["id"] for row in found["capabilities"]] == ["permitted"]


def test_connection_ready_uses_current_viability_and_setup_exists_without_rows():
    db = CatalogDB()
    db.tools["publish"] = tool(
        "publish",
        interface={"operations": [{"operation_id": "send", "connection_type_ids": ["mail"]}]},
    )
    catalog = WorkflowAuthoringCatalog(db)
    empty = catalog.describe(
        scope=SCOPE, project_id="project", ref="tool:publish", operation_id="send"
    )
    assert empty["setup_url"] == "/projects/project?tab=connections"
    db.connections = [
        {
            "connection_id": "unhealthy",
            "project_id": "project",
            "connection_type": "mail",
            "enabled": True,
            "health_status": "failed",
        }
    ]
    described = catalog.describe(
        scope=SCOPE, project_id="project", ref="tool:publish", operation_id="send"
    )
    assert described["connections"][0]["ready"] is False


class ProjectionStore:
    """The runtime store contract: active, account-owned projection snapshots."""

    def __init__(self, *, account_id=DEFAULT_ACCOUNT_ID):
        self.account_id = account_id
        self.active = True
        self.reads = []
        self.rows = [
            {
                "artifact_kind": kind,
                "artifact_id": local_id,
                "install_id": "install-copy",
                "release_id": "release-copy-v1",
                "projection": {
                    f"{kind}_id": local_id,
                    "display_name": "Launch copywriter" if kind == "agent" else "Write launch copy",
                    "description": "Draft launch copy",
                    "enabled": True,
                    **fields,
                },
            }
            for kind, local_id, fields in [
                ("agent", "copywriter", {"skills": [{"skill_id": "write_copy", "sort_order": 0}]}),
                (
                    "skill",
                    "write_copy",
                    {
                        "expected_inputs": [{"name": "request", "type": "text", "required": False}],
                        "expected_outputs": [{"name": "launch_copy", "type": "text"}],
                    },
                ),
            ]
        ]

    def list_projections(self, *, account_id, only_active):
        assert only_active is True
        self.reads.append(account_id)
        return deepcopy(self.rows) if self.active and account_id == self.account_id else []


PROJECTED_AGENT = "extension.install-copy.copywriter"
PROJECTED_SKILL = "extension.install-copy.write_copy"


def install_projection_store(db, *, account_id=DEFAULT_ACCOUNT_ID):
    store = ProjectionStore(account_id=account_id)
    db.extension_runtime_store = store
    return store


def test_projected_agent_and_skill_search_and_describe_without_physical_rows():
    db = CatalogDB()
    store = install_projection_store(db)
    db.get_skill_inputs = lambda _: []
    db.get_skill_outputs = lambda _: []
    catalog = WorkflowAuthoringCatalog(db)
    for kind, identifier in [("agent", PROJECTED_AGENT), ("skill", PROJECTED_SKILL)]:
        result = catalog.search(
            scope=SCOPE, project_id="project", query="launch", kinds=[kind], limit=1
        )
        assert [row["ref"] for row in result["capabilities"]] == [f"{kind}:{identifier}"]
        description = catalog.describe(
            scope=SCOPE, project_id="project", ref=f"{kind}:{identifier}"
        )
        assert description["id"] == identifier
        if kind == "skill":
            assert [row["name"] for row in description["inputs"]] == ["request"]
            assert [row["name"] for row in description["outputs"]] == ["launch_copy"]
    assert set(store.reads) == {DEFAULT_ACCOUNT_ID}
    assert db.agents == db.skills == {}


@pytest.mark.parametrize(
    "updates",
    [
        {"tenant_id": "foreign", "scope_kind": "tenant"},
        {"project_id": "foreign", "scope_kind": "project"},
        {"enabled": False},
        {"visible_in_catalog": False},
        {"deleted_at": "2026-10-01"},
        {"new_runs_blocked": True},
        {"availability_status": "unavailable"},
        {"extension_dependency_availability": {"new_runs_blocked": True}},
    ],
)
def test_projected_catalog_keeps_scope_and_availability_denials(updates):
    db = CatalogDB()
    store = install_projection_store(db)
    for payload in store.rows:
        payload["projection"].update(updates)
    catalog = WorkflowAuthoringCatalog(db)
    result = catalog.search(
        scope=SCOPE, project_id="project", query="launch", kinds=["agent", "skill"]
    )
    assert result["capabilities"] == []
    for kind, identifier in [("agent", PROJECTED_AGENT), ("skill", PROJECTED_SKILL)]:
        with pytest.raises(ServiceNotFoundError):
            catalog.describe(scope=SCOPE, project_id="project", ref=f"{kind}:{identifier}")


@pytest.mark.parametrize("foreign,inactive", [(True, False), (False, True)])
def test_projected_catalog_refuses_foreign_or_inactive_install_and_stale_bridge(foreign, inactive):
    db = CatalogDB()
    store = install_projection_store(db, account_id="foreign" if foreign else DEFAULT_ACCOUNT_ID)
    store.active = not inactive
    db.agents[PROJECTED_AGENT] = {
        "agent_id": PROJECTED_AGENT,
        "source_install_id": "install-copy",
        "enabled": True,
    }
    db.skills[PROJECTED_SKILL] = {
        "skill_id": PROJECTED_SKILL,
        "source_install_id": "install-copy",
        "enabled": True,
    }
    catalog = WorkflowAuthoringCatalog(db)
    assert (
        catalog.search(scope=SCOPE, project_id="project", query="launch", kinds=["agent", "skill"])[
            "capabilities"
        ]
        == []
    )
    for kind, identifier in [("agent", PROJECTED_AGENT), ("skill", PROJECTED_SKILL)]:
        with pytest.raises(ServiceNotFoundError):
            catalog.describe(scope=SCOPE, project_id="project", ref=f"{kind}:{identifier}")


@pytest.mark.parametrize(
    "kind,identifier", [("agent", PROJECTED_AGENT), ("skill", PROJECTED_SKILL)]
)
def test_projection_describe_uses_snapshot_contract_instead_of_empty_physical_getters(
    kind, identifier
):
    db = CatalogDB()
    store = install_projection_store(db)
    projection = next(row["projection"] for row in store.rows if row["artifact_kind"] == kind)
    projection["expected_inputs"] = [{"name": "request", "type": "text", "required": True}]
    projection["expected_outputs"] = [{"name": "launch_copy", "type": "text"}]
    setattr(db, f"get_{kind}_inputs", lambda _: [])
    setattr(db, f"get_{kind}_outputs", lambda _: [])
    description = WorkflowAuthoringCatalog(db).describe(
        scope=SCOPE, project_id="project", ref=f"{kind}:{identifier}"
    )
    assert description["inputs"] == [{"name": "request", "type": "text", "required": True}]
    assert description["outputs"] == [{"name": "launch_copy", "type": "text"}]
