from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock, Thread

from core.application.jobs.workflow_knowledge_sync_runtime import run_workflow_knowledge_sync_job
from core.application.workflows import workflow_knowledge_sync as sync_mod
from core.application.workflows.knowledge_projection import (
    WorkflowKnowledgeDocument,
    WorkflowKnowledgeEdge,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
)


@dataclass
class _FakeStore:
    calls: list[str]

    @contextmanager
    def scope_sync_lock(self, **_kwargs):
        yield

    @contextmanager
    def scope_sync_transaction(self):
        yield

    def scope_status(self, **kwargs):
        self.calls.append(f"status:{kwargs.get('expected_digest')}")
        assert kwargs.get("tenant_id") == "tenant_test"
        return {
            "ok": True,
            "graph": {"available": True},
            "vector": {"available": True},
            "scope": {
                "freshness": (
                    "fresh" if "digest" in self.calls or "status:seed" in self.calls else "empty"
                ),
                "counts": {"documents": 1, "nodes": 1, "edges": 1},
            },
        }

    def upsert_projection(self, **kwargs):
        assert kwargs["account_id"] == "acct_test"
        assert kwargs["tenant_id"] == "tenant_test"
        assert kwargs["project_id"] == "proj_test"
        self.calls.append("upsert")

    def prepare_projection_write(self, **kwargs):
        self.calls.append("prepare")
        return dict(kwargs)

    def apply_prepared_projection(self, *, prepared):
        assert prepared["account_id"] == "acct_test"
        self.calls.append("apply")

    def set_scope_sync_digest(self, **kwargs):
        assert kwargs["digest"] == "digest-123"
        assert kwargs["tenant_id"] == "tenant_test"
        self.calls.append("digest")


def _projection() -> WorkflowKnowledgeProjection:
    return WorkflowKnowledgeProjection(
        documents=[
            WorkflowKnowledgeDocument(
                id="doc:tool:weather",
                entity_id="tool:weather",
                entity_kind="tool",
                title="Weather",
                text="Fetch weather.",
            )
        ],
        nodes=[
            WorkflowKnowledgeNode(
                id="tool:weather",
                kind="tool",
                label="Weather",
            )
        ],
        edges=[
            WorkflowKnowledgeEdge(
                id="rel:weather:forecast",
                kind="produces_output",
                source_id="tool:weather",
                target_id="io_field:forecast",
            )
        ],
    )


def test_sync_workflow_knowledge_catalog_upserts_projection_and_digest(monkeypatch):
    store = _FakeStore(calls=[])
    monkeypatch.setattr(sync_mod, "catalog_sync_digest", lambda *_args, **_kwargs: "digest-123")
    monkeypatch.setattr(
        sync_mod, "build_workflow_catalog_projection", lambda *_args, **_kwargs: _projection()
    )

    result = sync_mod.sync_workflow_knowledge_catalog(
        store=store,
        db=object(),
        account_id="acct_test",
        tenant_id="tenant_test",
        project_id="proj_test",
    )

    assert result["projection_counts"] == {"documents": 1, "nodes": 1, "edges": 1}
    assert result["catalog_digest"] == "digest-123"
    assert store.calls == ["status:digest-123", "upsert", "digest", "status:digest-123"]


def test_sync_workflow_knowledge_catalog_dry_run_does_not_write(monkeypatch):
    store = _FakeStore(calls=[])
    monkeypatch.setattr(sync_mod, "catalog_sync_digest", lambda *_args, **_kwargs: "digest-123")
    monkeypatch.setattr(
        sync_mod, "build_workflow_catalog_projection", lambda *_args, **_kwargs: _projection()
    )

    result = sync_mod.sync_workflow_knowledge_catalog(
        store=store,
        db=object(),
        account_id="acct_test",
        tenant_id="tenant_test",
        project_id="proj_test",
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["projection_counts"] == {"documents": 1, "nodes": 1, "edges": 1}
    assert store.calls == ["status:digest-123"]


def test_sync_workflow_knowledge_catalog_if_stale_skips_fresh_scope(monkeypatch):
    store = _FakeStore(calls=["status:seed", "status:seed"])
    monkeypatch.setattr(sync_mod, "catalog_sync_digest", lambda *_args, **_kwargs: "digest-123")

    result = sync_mod.sync_workflow_knowledge_catalog_if_stale(
        store=store,
        db=object(),
        account_id="acct_test",
        tenant_id="tenant_test",
        project_id="proj_test",
    )

    assert result["skipped"] is True
    assert result["skip_reason"] == "workflow_knowledge_scope_fresh"
    assert store.calls == ["status:seed", "status:seed", "status:digest-123"]


def test_sync_workflow_knowledge_catalog_if_stale_refreshes_empty_scope(monkeypatch):
    store = _FakeStore(calls=[])
    monkeypatch.setattr(sync_mod, "catalog_sync_digest", lambda *_args, **_kwargs: "digest-123")
    monkeypatch.setattr(
        sync_mod, "build_workflow_catalog_projection", lambda *_args, **_kwargs: _projection()
    )

    result = sync_mod.sync_workflow_knowledge_catalog_if_stale(
        store=store,
        db=object(),
        account_id="acct_test",
        tenant_id="tenant_test",
        project_id="proj_test",
    )

    assert result["skipped"] is False
    assert result["trigger"] == "workflow_knowledge_scope_not_fresh"
    assert store.calls == [
        "status:digest-123",
        "status:digest-123",
        "prepare",
        "status:digest-123",
        "apply",
        "digest",
        "status:digest-123",
    ]


def test_if_stale_builds_projection_under_lock_but_before_scope_transaction(monkeypatch):
    class _LockTrackingStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__(calls=[])
            self.locked = False
            self.in_transaction = False

        @contextmanager
        def scope_sync_lock(self, **_kwargs):
            assert self.locked is False
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

        @contextmanager
        def scope_sync_transaction(self):
            assert self.locked is True
            assert self.in_transaction is False
            self.in_transaction = True
            try:
                yield
            finally:
                self.in_transaction = False

    store = _LockTrackingStore()

    def _digest(*_args, **_kwargs):
        assert store.in_transaction is False, "catalog discovery ran inside the transaction"
        return "digest-123"

    def _build(*_args, **_kwargs):
        assert store.locked is True, "projection build must be serialized"
        assert store.in_transaction is False, "projection build ran inside the transaction"
        return _projection()

    original_prepare = store.prepare_projection_write

    def _prepare(**kwargs):
        assert store.locked is True
        assert store.in_transaction is False, "embedding preparation ran inside transaction"
        return original_prepare(**kwargs)

    def _apply(*, prepared):
        assert store.locked is True
        assert store.in_transaction is True
        store.calls.append("apply")

    monkeypatch.setattr(sync_mod, "catalog_sync_digest", _digest)
    monkeypatch.setattr(sync_mod, "build_workflow_catalog_projection", _build)
    monkeypatch.setattr(store, "prepare_projection_write", _prepare)
    monkeypatch.setattr(store, "apply_prepared_projection", _apply)

    result = sync_mod.sync_workflow_knowledge_catalog_if_stale(
        store=store,
        db=object(),
        account_id="acct_test",
        tenant_id="tenant_test",
        project_id="proj_test",
    )

    assert result["skipped"] is False
    assert store.locked is False


def test_concurrent_if_stale_calls_replace_one_shared_projection_once(monkeypatch):
    class _SharedProjectionStore:
        def __init__(self) -> None:
            self.digest = ""
            self.projection_writes = 0
            self.sql_state: dict[str, object] = {}
            self.age_state: dict[str, object] = {}
            self._lock = Lock()
            self._scope_lock = Lock()

        @contextmanager
        def scope_sync_lock(self, **_kwargs):
            with self._scope_lock:
                yield

        @contextmanager
        def scope_sync_transaction(self):
            yield

        def scope_status(self, **_kwargs):
            with self._lock:
                freshness = "fresh" if self.digest == "digest-123" else "empty"
            return {
                "scope": {
                    "freshness": freshness,
                    "counts": {
                        "documents": len(self.sql_state),
                        "nodes": len(self.sql_state),
                        "edges": len(self.age_state),
                    },
                }
            }

        def upsert_projection(self, *, projection, **_kwargs):
            with self._lock:
                self.projection_writes += 1
                self.sql_state.clear()
                self.age_state.clear()
                self.sql_state.update({item.id: item for item in projection.nodes})
                self.age_state.update({item.id: item for item in projection.edges})

        def prepare_projection_write(self, *, projection, **_kwargs):
            return projection

        def apply_prepared_projection(self, *, prepared):
            self.upsert_projection(projection=prepared)

        def set_scope_sync_digest(self, *, digest: str, **_kwargs):
            with self._lock:
                self.digest = digest

    store = _SharedProjectionStore()
    monkeypatch.setattr(sync_mod, "catalog_sync_digest", lambda *_args, **_kwargs: "digest-123")
    monkeypatch.setattr(
        sync_mod, "build_workflow_catalog_projection", lambda *_args, **_kwargs: _projection()
    )
    results: list[dict] = []

    def _sync() -> None:
        results.append(
            sync_mod.sync_workflow_knowledge_catalog_if_stale(
                store=store,
                db=object(),
                account_id="acct_test",
                tenant_id="tenant_test",
                project_id="proj_test",
            )
        )

    workers = [Thread(target=_sync), Thread(target=_sync)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 2
    assert store.projection_writes == 1
    assert len(store.sql_state) == 1
    assert len(store.age_state) == 1


def test_run_workflow_knowledge_sync_job_uses_if_stale_path(monkeypatch):
    store = _FakeStore(calls=[])
    monkeypatch.setattr(sync_mod, "catalog_sync_digest", lambda *_args, **_kwargs: "digest-123")
    monkeypatch.setattr(
        sync_mod, "build_workflow_catalog_projection", lambda *_args, **_kwargs: _projection()
    )

    result = run_workflow_knowledge_sync_job(
        {
            "account_id": "acct_test",
            "tenant_id": "tenant_test",
            "project_id": "proj_test",
            "include_capability_catalog": "true",
        },
        db=object(),
        store=store,
    )

    assert result["skipped"] is False
    assert result["trigger"] == "workflow_knowledge_scope_not_fresh"


def test_sync_workflow_knowledge_catalog_builds_scoped_projection(monkeypatch):
    class _ScopedDB:
        def __init__(self):
            self.summary_calls: list[dict[str, str | bool | None]] = []

        def list_workflows_summary(
            self,
            enabled_only: bool = False,
            *,
            account_id: str | None = None,
            tenant_id: str | None = None,
            project_id: str | None = None,
        ):
            if account_id != "acct_test" or tenant_id != "tenant_test" or project_id != "proj_test":
                raise AssertionError("workflow projection used a global workflow listing")
            self.summary_calls.append(
                {
                    "enabled_only": enabled_only,
                    "account_id": account_id,
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                }
            )
            return [{"workflow_id": "scoped_flow", "enabled": True, "step_count": 1}]

        def get_workflow(self, workflow_id: str):
            if workflow_id != "scoped_flow":
                return None
            return {
                "workflow_id": "scoped_flow",
                "display_name": "Scoped Flow",
                "description": "Visible only in the scoped projection.",
                "enabled": True,
                "_phases": [
                    {
                        "phase_id": "main",
                        "display_name": "Main",
                        "category": "action",
                        "_steps": [],
                    }
                ],
            }

        def list_step_kind_profiles(self, enabled_only: bool = False):
            del enabled_only
            return []

    store = _FakeStore(calls=[])
    db = _ScopedDB()
    monkeypatch.setattr(sync_mod, "catalog_sync_digest", lambda *_args, **_kwargs: "digest-123")
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        lambda _db, workflow_id: {
            "workflow": {"id": workflow_id, "display_name": workflow_id, "enabled": True},
            "interface_contract": {},
            "output_contract": {},
            "phases": [],
            "steps": [],
            "edges": [],
            "automation_rules": [],
        },
    )

    result = sync_mod.sync_workflow_knowledge_catalog(
        store=store,
        db=db,
        account_id="acct_test",
        tenant_id="tenant_test",
        project_id="proj_test",
        dry_run=True,
        include_capability_catalog=False,
    )

    assert db.summary_calls == [
        {
            "enabled_only": False,
            "account_id": "acct_test",
            "tenant_id": "tenant_test",
            "project_id": "proj_test",
        }
    ]
    assert result["projection_counts"]["documents"] >= 1
    assert store.calls == ["status:digest-123"]
