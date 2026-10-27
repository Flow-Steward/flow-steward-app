"""
Shared pytest fixtures for the Flow Steward test suite.

Usage:
    docker compose exec fs-app python -m pytest tests/ -v --tb=short
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from tests._db_env import dsn_unreachable_reason, resolve_test_db_dsn
from tests._xdist_isolation import isolate_worker, release_worker

# Before any product module is imported: on a pytest-xdist worker, point every
# DSN at this worker's own copy of the audit database (tests/_xdist_isolation.py).
isolate_worker()

# Ensure the project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)


_WORKFLOW_SPLIT_MODULES = (
    "core.entrypoints.admin.workflows_common",
    "core.entrypoints.admin.workflows_projection",
    "core.entrypoints.admin.workflows_scope",
    "core.entrypoints.admin.workflows_connector_catalog",
    "core.entrypoints.admin.workflows_models",
    "core.entrypoints.admin.workflows_definition_router",
    "core.entrypoints.admin.workflows_routes_listing_import",
    "core.entrypoints.admin.workflows_routes_read",
    "core.entrypoints.admin.workflows_routes_write",
    "core.entrypoints.admin.workflows_routes_history",
    "core.entrypoints.admin.workflows_routes_edit_session",
    "core.entrypoints.admin.workflows_routes_extension",
)


class _WorkflowTestPatchModule(ModuleType):
    """Keep historical aggregate-module patches inside the pytest process only."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module_name in _WORKFLOW_SPLIT_MODULES:
            module = sys.modules.get(module_name)
            if module is not None and hasattr(module, name):
                setattr(module, name, value)


def _initialize_workflow_test_patch_surface() -> None:
    workflow_module = importlib.import_module("core.entrypoints.admin.workflows")
    workflow_module.__class__ = _WorkflowTestPatchModule


_initialize_workflow_test_patch_surface()


def _ensure_langgraph_test_shims() -> None:
    """Provide minimal LangGraph modules for unit tests when optional deps are absent.

    Some older unit tests patch/import `langgraph.types.interrupt`, while newer tests
    stub only `Command`. Full-suite execution must not depend on test order, so we
    make the fallback complete and idempotent here.
    """
    try:
        langgraph_mod = importlib.import_module("langgraph")
    except ImportError:
        langgraph_mod = ModuleType("langgraph")
        sys.modules["langgraph"] = langgraph_mod

    try:
        types_mod = importlib.import_module("langgraph.types")
    except ImportError:
        types_mod = ModuleType("langgraph.types")
        sys.modules["langgraph.types"] = types_mod
    langgraph_mod.types = types_mod

    if not hasattr(types_mod, "Command"):

        class _FakeCommand:
            # Mirror the real langgraph.types.Command keyword-only signature
            # (graph, update, resume, goto). Accepting **kwargs keeps the shim
            # forward-compatible so callers like run_graph_resume(Command(resume=...))
            # don't raise TypeError when the optional dependency is absent.
            def __init__(self, *, graph=None, update=None, resume=None, goto=(), **kwargs):
                self.graph = graph
                self.update = update or {}
                self.resume = resume
                self.goto = goto
                for key, value in kwargs.items():
                    setattr(self, key, value)

        types_mod.Command = _FakeCommand

    if not hasattr(types_mod, "interrupt"):

        def _fake_interrupt(payload):
            return payload

        types_mod.interrupt = _fake_interrupt

    try:
        graph_mod = importlib.import_module("langgraph.graph")
    except ImportError:
        graph_mod = ModuleType("langgraph.graph")
        sys.modules["langgraph.graph"] = graph_mod
    langgraph_mod.graph = graph_mod

    try:
        message_mod = importlib.import_module("langgraph.graph.message")
    except ImportError:
        message_mod = ModuleType("langgraph.graph.message")
        sys.modules["langgraph.graph.message"] = message_mod
    graph_mod.message = message_mod

    if not hasattr(message_mod, "add_messages"):

        def _add_messages(existing, new):
            existing = existing or []
            new = new or []
            return list(existing) + list(new)

        message_mod.add_messages = _add_messages

    try:
        checkpoint_mod = importlib.import_module("langgraph.checkpoint")
    except ImportError:
        checkpoint_mod = ModuleType("langgraph.checkpoint")
        sys.modules["langgraph.checkpoint"] = checkpoint_mod
    langgraph_mod.checkpoint = checkpoint_mod

    try:
        postgres_mod = importlib.import_module("langgraph.checkpoint.postgres")
    except ImportError:
        postgres_mod = ModuleType("langgraph.checkpoint.postgres")
        sys.modules["langgraph.checkpoint.postgres"] = postgres_mod
    checkpoint_mod.postgres = postgres_mod

    if not hasattr(postgres_mod, "PostgresSaver"):

        class _FakePostgresSaver:
            def __init__(self, conn):
                self.conn = conn

        postgres_mod.PostgresSaver = _FakePostgresSaver


_ensure_langgraph_test_shims()


# ---------------------------------------------------------------------------
# pytest.ini configuration (inline)
# ---------------------------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow (skipped with -m 'not slow')")
    config.addinivalue_line("markers", "e2e: mark test as end-to-end (requires full stack)")
    config.addinivalue_line(
        "markers", "e2e_auth_public: mark e2e test that must run without auto-auth patching"
    )
    config.addinivalue_line("markers", "ui: mark test as Playwright UI test")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line(
        "markers",
        "xdist_group(name): run every test with this group name on one xdist worker, "
        "in order; use xdist_group('serial') for a test that cannot share the gate's "
        "resources with a concurrent worker",
    )


def pytest_unconfigure(config):
    release_worker()


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_conn():
    """Direct psycopg_runtime/psycopg3 connection to the Flow Steward test DB."""
    dsn = resolve_test_db_dsn()
    if not dsn:
        pytest.skip("FS_DB_DSN not set — skipping DB-dependent test")
    unreachable = dsn_unreachable_reason(dsn)
    if unreachable:
        pytest.skip(f"Database host is not reachable from this test environment: {unreachable}")
    try:
        import psycopg

        conn = psycopg.connect(dsn)
        conn.autocommit = False
        yield conn
        conn.close()
    except ImportError:
        try:
            from core.infrastructure.db import psycopg_runtime as psycopg_runtime

            conn = psycopg_runtime.connect(dsn)
            conn.autocommit = False
            yield conn
            conn.close()
        except ImportError:
            pytest.skip("psycopg/psycopg_runtime not installed")


@pytest.fixture(scope="session")
def disposable_db():
    """A throwaway canonical PostgreSQL, built and torn down for this session.

    Use this — never `pg_conn` — for tests that write. `pg_conn` points at
    whatever `FS_DB_DSN` names, which on a developer machine is the live dev
    database of the running stack.
    """
    from tests import _disposable_db

    if os.environ.get("FS_TEST_DISPOSABLE_DB", "").strip() in {"0", "false", "no"}:
        pytest.skip("disposable canonical PostgreSQL disabled by FS_TEST_DISPOSABLE_DB")
    audit_database = _disposable_db.isolated_audit_database()
    if audit_database is not None:
        with _disposable_db.redirected_to(audit_database):
            yield audit_database
        return

    reason = _disposable_db.unavailable_reason()
    if reason:
        pytest.skip(f"disposable canonical PostgreSQL unavailable: {reason}")

    database = _disposable_db.start_disposable_db()
    try:
        _disposable_db.apply_canonical_state(database)
        yield database
    finally:
        _disposable_db.stop_disposable_db(database.container)


@pytest.fixture
def disposable_db_conn(disposable_db):
    """An autocommit-off connection to the disposable database, rolled back."""
    import psycopg

    conn = psycopg.connect(disposable_db.dsn)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def default_account(pg_conn):
    """Ensure the default account exists on whatever data plane the test runs on.

    RS5-003 and RS5-005 insert projects referencing ``DEFAULT_ACCOUNT_ID`` but
    never created it, so they only passed on a database somebody had already
    seeded. On a clean isolated audit plane both failed with
    ``ForeignKeyViolation`` on ``projects_account_id_fkey`` — meaning the evidence
    behind two P0 rows was not reproducible, and two runs of the release gate were
    not comparable. A gate has to seed its own prerequisites.

    Idempotent, so it is safe on a seeded plane too.
    """
    from core.application.auth.config import (
        DEFAULT_ACCOUNT_ID,
        DEFAULT_TENANT_ID,
        DEFAULT_WORKSPACE_ID,
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
            VALUES (%s, %s, %s, %s, true)
            ON CONFLICT DO NOTHING
            """,
            (DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID, "default", "Default Account"),
        )
        # Projects also reference the default workspace, so seeding only the
        # account moves the failure one foreign key along instead of fixing it.
        cur.execute(
            """
            INSERT INTO workspaces
                (id, tenant_id, account_id, workspace_key, display_name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                DEFAULT_WORKSPACE_ID,
                DEFAULT_TENANT_ID,
                DEFAULT_ACCOUNT_ID,
                "default",
                "Default Workspace",
            ),
        )
    pg_conn.commit()
    return DEFAULT_ACCOUNT_ID


@pytest.fixture
def two_tenants(disposable_db_conn):
    """Two accounts with overlapping project/workspace names, on a real database.

    Requirement 10 of the remediation epic: every meaningful isolation test uses
    at least two accounts and two projects. The names deliberately overlap so a
    resolver that keys on a human name rather than an id fails the test.
    """
    from tests import _tenant_fixtures

    fixture = _tenant_fixtures.create_two_tenants(disposable_db_conn)
    disposable_db_conn.commit()
    try:
        yield fixture
    finally:
        disposable_db_conn.rollback()
        _tenant_fixtures.delete_two_tenants(disposable_db_conn, fixture)
        disposable_db_conn.commit()


@pytest.fixture
def bl(pg_conn):
    """Flow Steward logger instance for a single test. Rolls back after each test."""
    try:
        from tests.support.platform_test_store import PlatformTestStore as PlatformLogger

        logger_instance = PlatformLogger(pg_conn)
        yield logger_instance
        pg_conn.rollback()
    except Exception as e:
        pytest.skip(f"Flow Steward logger not available: {e}")


@pytest.fixture
def test_project(pg_conn):
    """Insert a test project row, yield project_id, clean up on teardown."""
    project_id = str(uuid.uuid4())
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (id, name)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (project_id, f"Test Project {project_id}"),
            )
            pg_conn.commit()
    except Exception as e:
        pg_conn.rollback()
        pytest.skip(f"Could not create test project: {e}")

    yield project_id

    try:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        pg_conn.commit()
    except Exception:
        pg_conn.rollback()


@pytest.fixture
def test_job(pg_conn, test_project):
    """Factory fixture — call with (task_summary, status) to create a test job."""
    created_job_ids = []

    def _make_job(task_summary: str = "Test task", status: str = "in_progress"):
        thread_id = f"thread-{uuid.uuid4().hex}"
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jobs (project_id, task_id, task_summary, status, thread_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    RETURNING id
                    """,
                    (test_project, f"task-{uuid.uuid4().hex[:8]}", task_summary, status, thread_id),
                )
                row = cur.fetchone()
                real_job_id = row[0]
            pg_conn.commit()
            created_job_ids.append(real_job_id)
            return real_job_id, thread_id
        except Exception as e:
            pg_conn.rollback()
            pytest.skip(f"Could not create test job: {e}")

    yield _make_job

    for job_id in created_job_ids:
        try:
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()


# ---------------------------------------------------------------------------
# Registry fixtures
# ---------------------------------------------------------------------------


def _ensure_registry_dsn_available(db_dsn: str, env: pytest.MonkeyPatch) -> None:
    """Skip without a reachable database; otherwise point FS_DB_DSN at it via *env*.

    *env* undoes the variable when the fixture that owns it ends, so a registry
    fixture never leaves FS_DB_DSN behind for tests that did not request it.
    """
    if not db_dsn:
        pytest.skip("No database DSN configured (FS_DB_DSN / DATABASE_URL)")
    unreachable = dsn_unreachable_reason(db_dsn)
    if unreachable:
        pytest.skip(f"Database host is not reachable from this test environment: {unreachable}")
    if not os.environ.get("FS_DB_DSN"):
        env.setenv("FS_DB_DSN", db_dsn)


@pytest.fixture(scope="session")
def registry():
    """RegistryManager singleton backed by PostgreSQL."""
    try:
        import core.application.registry.registry_manager as rm_module

        rm_module._instance = None  # Reset singleton
        from core.application.registry.registry_manager import RegistryManager

        with pytest.MonkeyPatch.context() as env:
            _ensure_registry_dsn_available(resolve_test_db_dsn(), env)
            instance = RegistryManager.instance()
            yield instance
        # Reset after session
        rm_module._instance = None
    except ImportError as e:
        pytest.skip(f"Registry not available: {e}")


@pytest.fixture
def fresh_registry(monkeypatch):
    """RegistryManager singleton reset before each test."""
    try:
        import core.application.registry.registry_manager as rm_module

        rm_module._instance = None
        from core.application.registry.registry_manager import RegistryManager

        _ensure_registry_dsn_available(resolve_test_db_dsn(), monkeypatch)
        instance = RegistryManager.instance()
        yield instance
        rm_module._instance = None
    except ImportError as e:
        pytest.skip(f"Registry not available: {e}")


# ---------------------------------------------------------------------------
# LLM Mocking fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm():
    """Patch get_chat_model to return a deterministic FakeChatModel."""
    try:
        from langchain_core.messages import AIMessage

        class FakeChatModel:
            """Simple fake that returns a preset response."""

            def __init__(self, response: str = '{"status": "pass", "output": "Test output"}'):
                self._response = response
                self.model_name = "fake-model"

            def invoke(self, messages, **kwargs):
                return AIMessage(content=self._response)

            def bind_tools(self, tools, **kwargs):
                return self

            def with_config(self, **kwargs):
                return self

        fake = FakeChatModel()

        with patch("core.model_provider.get_chat_model", return_value=fake):
            yield fake
    except ImportError:
        # If model_provider is not in expected location, try alternatives
        with patch(
            "core.application.orchestration.agents.common.get_chat_model", return_value=MagicMock()
        ) as mock:
            yield mock


@pytest.fixture
def make_fake_llm():
    """Factory to create FakeChatModel with custom response."""
    AIMessage = pytest.importorskip("langchain_core.messages").AIMessage

    class FakeChatModel:
        def __init__(self, response: str):
            self._response = response
            self.model_name = "fake-model"

        def invoke(self, messages, **kwargs):
            return AIMessage(content=self._response)

        def bind_tools(self, tools, **kwargs):
            return self

        def with_config(self, **kwargs):
            return self

    return FakeChatModel


# ---------------------------------------------------------------------------
# Graph fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def compiled_graph(fresh_registry):
    """Compiled registry graph with in-memory checkpointer for testing."""
    try:
        from langgraph.checkpoint.memory import MemorySaver

        from core.application.orchestration.graph.builder import build_graph_from_registry

        checkpointer = MemorySaver()
        compiled = build_graph_from_registry(fresh_registry, checkpointer=checkpointer)
        yield compiled
    except Exception as e:
        pytest.skip(f"Could not build registry graph: {e}")


# ---------------------------------------------------------------------------
# HTTP client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def http_client():
    """httpx sync client against the FastAPI app."""
    try:
        import httpx

        from core.entrypoints.api.app import app

        with httpx.Client(app=app, base_url="http://testserver") as client:
            yield client
    except Exception as e:
        pytest.skip(f"Could not create HTTP client: {e}")


@pytest.fixture
async def async_http_client():
    """httpx async client against the FastAPI app."""
    try:
        import httpx

        from core.entrypoints.api.app import app

        async with httpx.AsyncClient(app=app, base_url="http://testserver") as client:
            yield client
    except Exception as e:
        pytest.skip(f"Could not create async HTTP client: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wait_for_job_status(
    http_client,
    job_id: int,
    target_statuses: list[str],
    timeout: int = 120,
    interval: int = 5,
) -> dict:
    """Poll job status until it matches one of target_statuses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = http_client.get(f"/api/jobs/{job_id}")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") in target_statuses:
                    return data
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"Job {job_id} did not reach {target_statuses} within {timeout}s")


def poll_until(predicate, timeout: int = 60, interval: float = 1.0):
    """Poll predicate until True or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Minimal orchestrator state factory
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Singleton isolation — reset global caches between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Ensure singleton caches cannot leak state between tests."""
    yield
    # Teardown: clear cached instances
    try:
        from core.application.orchestration.message_bus import MessageBus

        MessageBus._instances.clear()
    except Exception:
        pass
    try:
        import core.application.registry.registry_manager as _rm_mod

        _rm_mod._instance = None
    except Exception:
        pass


def make_state(**overrides) -> dict:
    """Create a minimal orchestrator state dict for testing."""
    base = {
        "task_id": f"test-{uuid.uuid4().hex[:8]}",
        "task_summary": "Test task summary",
        "task_source": "manual",
        "job_id": 0,
        "project_name": "test-project",
        "project_config": {},
        "mode": "brownfield",
        "complexity": "bugfix",
        "workflow_path": [],
        "current_step": 0,
        "needs_user_approval": False,
        "messages": [],
        "status": "in_progress",
        "phase_stack": [],
        "orchestrator_journal": [],
        "agent_messages": [],
        "iteration_count": 0,
        "strategy_version": 0,
        "user_approved": None,
        "review_feedback": None,
    }
    base.update(overrides)
    if not isinstance(base.get("agent_messages"), list):
        base["agent_messages"] = []
    return base
