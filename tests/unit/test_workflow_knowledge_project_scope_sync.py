"""Project-scoped discovery finds the knowledge the background sync wrote.

The 2026-11-17 MCP probe (F11) saw every ``capability_search`` and
``workflow_examples_search`` answer ``freshness: empty`` /
``missing_scope_digest`` for 45 minutes while the sync loop ran. Discovery
reads the knowledge of the caller's project, but the control plane derived its
sync scopes from registry changelog snapshots only, and a workflow snapshot has
no ``project_id`` (workflows are bound to projects through
``project_workflows``). Only the account-wide scope was ever synced.

These tests run the real path with in-memory stores: the control plane's
request, the worker's sync of each requested scope, and the request-path
freshness check of a project-scoped search.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.jobs.workflow_knowledge_sync_requests import (
    enqueue_workflow_knowledge_sync_batch,
)
from core.application.jobs.workflow_knowledge_sync_runtime import run_workflow_knowledge_sync_job
from core.application.workflows import workflow_knowledge_sync as sync_mod
from core.application.workflows.discovery import (
    capability_discovery_helpers,
)
from core.application.workflows.discovery import (
    capability_discovery_knowledge_mixin as knowledge_mixin,
)
from core.application.workflows.discovery.capability_discovery import (
    WorkflowCapabilityDiscovery,
)
from core.application.workflows.knowledge_projection import WorkflowKnowledgeProjection

PROJECT_ID = "73056336-4ecf-44e8-9590-d963f3e84bd2"


def _key(account_id: Any, tenant_id: Any, project_id: Any) -> tuple[str, str, str]:
    return (
        str(account_id or "").strip(),
        str(tenant_id or "").strip() or DEFAULT_TENANT_ID,
        str(project_id or "").strip() or "global",
    )


class _KnowledgeStore:
    """The digest side of the knowledge store, in memory."""

    def __init__(self) -> None:
        self.digests: dict[tuple[str, str, str], str] = {}

    def scope_status(self, *, account_id, tenant_id=None, project_id=None, expected_digest="", **_):
        stored = self.digests.get(_key(account_id, tenant_id, project_id), "")
        freshness = "fresh" if stored and stored == expected_digest else "stale"
        return {"ok": True, "scope": {"freshness": freshness if stored else "empty"}}

    @contextmanager
    def scope_sync_lock(self, **_kwargs):
        yield

    @contextmanager
    def scope_sync_transaction(self):
        yield

    def prepare_projection_write(self, **kwargs):
        return dict(kwargs)

    def apply_prepared_projection(self, *, prepared):
        del prepared

    def upsert_projection(self, **_kwargs):
        return None

    def set_scope_sync_digest(self, *, account_id, tenant_id=None, project_id=None, digest):
        self.digests[_key(account_id, tenant_id, project_id)] = digest

    def get_scope_sync_digest(self, *, account_id, tenant_id=None, project_id=None):
        return self.digests.get(_key(account_id, tenant_id, project_id), "")

    def search(self, **_kwargs):
        return []


class _Registry:
    """A registry with no rows: the catalog digest still depends on the scope."""


class _ControlStore:
    def __init__(self, projects: list[dict[str, str]]) -> None:
        self.projects = projects
        self.project_limits: list[int] = []

    def list_recent_registry_changelog(self, *, entity_types, limit):
        del entity_types, limit
        # The real shape: a workflow snapshot is the registry_workflows row,
        # which has an account but no project.
        return [
            {
                "id": "change-1",
                "entity_type": "workflow",
                "entity_id": "mcp_probe_report",
                "created_at": "2026-11-17T10:00:00Z",
                "snapshot": {"workflow_id": "mcp_probe_report", "account_id": DEFAULT_ACCOUNT_ID},
            }
        ]

    def list_workflow_knowledge_project_scopes(self, *, limit):
        self.project_limits.append(limit)
        return list(self.projects)


class _Events:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def enqueue_domain_event(self, **kwargs):
        self.requests.append(dict(kwargs["payload"]))


@pytest.fixture(autouse=True)
def _no_shared_digest_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # The request path keeps a process-wide digest cache; each test starts empty.
    cache: dict = {}
    monkeypatch.setattr(capability_discovery_helpers, "_WORKFLOW_KNOWLEDGE_SYNC_CACHE", cache)
    monkeypatch.setattr(knowledge_mixin, "_WORKFLOW_KNOWLEDGE_SYNC_CACHE", cache)
    monkeypatch.setattr(
        sync_mod,
        "build_workflow_catalog_projection",
        lambda *_args, **_kwargs: WorkflowKnowledgeProjection(documents=[], nodes=[], edges=[]),
    )


def _sync_once(control: _ControlStore, store: _KnowledgeStore, registry: Any) -> list[dict]:
    events = _Events()
    enqueue_workflow_knowledge_sync_batch(
        changelog_store=control,
        event_store=events,
        changelog_limit=50,
        cadence_seconds=300,
        now=lambda: datetime(2026, 11, 17, 10, 5, tzinfo=UTC),
    )
    (request,) = events.requests
    for scope in request["scopes"]:  # what the worker does with the request
        run_workflow_knowledge_sync_job(scope, db=registry, store=store)
    return request["scopes"]


def _project_search_status(store: _KnowledgeStore, registry: Any) -> dict[str, Any]:
    discovery = WorkflowCapabilityDiscovery(registry, workflow_knowledge_backend=store)
    discovery._workflow_knowledge_candidates(
        workflow_id="",
        request_text="sales report",
        scope={
            "account_id": DEFAULT_ACCOUNT_ID,
            "tenant_id": DEFAULT_TENANT_ID,
            "project_id": PROJECT_ID,
        },
        project_id=PROJECT_ID,
        candidate_limit=5,
    )
    return discovery._last_workflow_knowledge_status


def test_a_project_scoped_search_is_fresh_after_one_sync() -> None:
    control = _ControlStore(
        [{"project_id": PROJECT_ID, "account_id": DEFAULT_ACCOUNT_ID, "tenant_id": ""}]
    )
    store = _KnowledgeStore()
    registry = _Registry()

    scopes = _sync_once(control, store, registry)

    assert {scope["project_id"] for scope in scopes} == {"", PROJECT_ID}
    assert len(control.project_limits) == 1  # one bounded project read per request
    assert control.project_limits[0] > 0
    status = _project_search_status(store, registry)
    assert status["freshness"] == "fresh", status
    assert status["stored_digest"] == status["catalog_digest"]


def test_without_projects_only_the_account_scope_is_synced() -> None:
    store = _KnowledgeStore()
    registry = _Registry()

    scopes = _sync_once(_ControlStore([]), store, registry)

    assert [scope["project_id"] for scope in scopes] == [""]
    assert _project_search_status(store, registry)["reason"] == "missing_scope_digest"


def test_a_project_without_an_id_is_not_a_scope() -> None:
    control = _ControlStore([{"project_id": "", "account_id": DEFAULT_ACCOUNT_ID}])

    scopes = _sync_once(control, _KnowledgeStore(), _Registry())

    assert [scope["project_id"] for scope in scopes] == [""]
