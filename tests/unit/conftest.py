"""Unit-test conftest: install stubs before any test module is collected.

This conftest is loaded by pytest before collecting any test module inside
tests/unit/, so stub installations here take effect before any top-level
import in those modules runs.

Stubs/patches installed here:
- dotenv.load_dotenv: silenced to a no-op so that core/config.py importing at
  module level does NOT load the project .env file into os.environ.  Loading
  .env would set FS_DB_DSN (the Docker DSN) which would cause unit tests
  that call _require_db() to try (and fail) to connect to a non-existent DB
  instead of properly skipping.  This is a no-op in Docker where real env vars
  are injected directly into the container's environment.
- psycopg_runtime (via _psycopg_runtime_stub.install_psycopg_runtime_stub): provides a minimal
  psycopg_runtime surface for import-only unit tests.  The stub is a no-op when the
  real package is already importable, so this is safe in environments that have
  a live psycopg_runtime installed (e.g. inside the Docker container).
- langchain_core.tools: minimal stub providing a pass-through @tool decorator;
  only installed when langchain_core is genuinely absent.
"""

from __future__ import annotations

import contextlib
import os
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# dotenv.load_dotenv: silence before core/config.py is imported
#
# core/config.py calls load_dotenv() at module import time.  In Docker this
# is fine because real env vars shadow any .env values.  Outside Docker
# (local dev host) this loads the project .env which sets FS_DB_DSN to
# the Docker service hostname, causing unit tests that call _require_db() to
# attempt a real DB connection and fail instead of skipping cleanly.
#
# We suppress load_dotenv ONLY when the environment variable is not already
# set so that explicit export on the host still wins.
# ---------------------------------------------------------------------------
# The root conftest normally runs first; this keeps the per-worker database
# redirect ahead of _PRESET_DB_ENV below even when it does not (idempotent).
from tests._xdist_isolation import isolate_worker

isolate_worker()

try:
    import dotenv as _dotenv_mod

    _real_load_dotenv = _dotenv_mod.load_dotenv

    # DB DSN variables that, if loaded from .env into os.environ, would cause
    # unit tests to believe a real DB is available and attempt connections via
    # the stub (which correctly refuses them), producing failures instead of
    # clean skips.  Only these keys are suppressed; all other .env values load
    # normally so test fixtures that depend on non-DB config work correctly.
    _SUPPRESS_FROM_DOTENV = frozenset(
        {
            "FS_DB_DSN",
            "DATABASE_URL",
            "OBSERVABILITY_DB_DSN",
            "FS_OBSERVABILITY_DSN",
        }
    )
    _PRESET_DB_ENV: dict[str, str] = {}
    for _key in _SUPPRESS_FROM_DOTENV:
        _value = os.getenv(_key)
        if _value:
            _PRESET_DB_ENV[_key] = _value

    def _selective_load_dotenv(*_args, **_kwargs):
        """Load .env while preserving explicitly-provided runtime DB env values."""
        result = _real_load_dotenv(*_args, **_kwargs)
        for key in _SUPPRESS_FROM_DOTENV:
            preset = _PRESET_DB_ENV.get(key, "")
            if preset:
                os.environ[key] = preset
            else:
                os.environ.pop(key, None)
        return result

    _dotenv_mod.load_dotenv = _selective_load_dotenv
except ImportError:
    pass  # dotenv not installed; core/config.py will also fail to import it


@pytest.fixture(autouse=True)
def _preserve_runtime_db_env():
    """Keep unit-test DB env stable even if individual tests mutate os.environ."""
    original = {key: os.environ.get(key) for key in _SUPPRESS_FROM_DOTENV}
    for key, preset in _PRESET_DB_ENV.items():
        os.environ[key] = preset
    try:
        yield
    finally:
        for key in _SUPPRESS_FROM_DOTENV:
            previous = original.get(key)
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


_DEFAULT_EXTENSION_STORE_MODULE = "core.application.extensions.service_support"
_DEFAULT_EXTENSION_STORE_CACHES = (
    "_default_extension_install_store",
    "_default_extension_runtime_store",
)


@pytest.fixture(autouse=True)
def _isolate_default_extension_stores():
    """Start every test without a cached default extension install or runtime store.

    ``service_support`` caches the default stores per process, and a service given
    a logger with a connection caches a DB-backed store on it. A later test that
    builds a service without a logger gets that store back and reads its rows from
    another test's double, so it passes or fails by which test ran before it.
    The module is not imported here: a test that never loads it has nothing to
    leak, and import-boundary tests must not see it loaded for them.

    The caches are saved and restored by hand, not through ``monkeypatch``:
    requesting ``monkeypatch`` from a conftest-wide autouse fixture would set it
    up before every module's own autouse fixtures, so their teardown would run
    before the test's own patches are undone.
    """
    module = sys.modules.get(_DEFAULT_EXTENSION_STORE_MODULE)
    saved: dict[str, object] = {}
    if module is not None:
        for name in _DEFAULT_EXTENSION_STORE_CACHES:
            saved[name] = getattr(module, name)
            setattr(module, name, None)
    yield
    module = sys.modules.get(_DEFAULT_EXTENSION_STORE_MODULE)
    if module is not None:
        for name in _DEFAULT_EXTENSION_STORE_CACHES:
            setattr(module, name, saved.get(name))


# Ensure project root is on sys.path (conftest.py at tests/ level also does
# this, but conftest loading order is not guaranteed to be parent-first when
# running a sub-directory directly).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.unit._psycopg_runtime_stub import install_psycopg_runtime_stub  # noqa: E402

install_psycopg_runtime_stub()


# ---------------------------------------------------------------------------
# langchain_core stub (only when absent from environment)
#
# Provides the minimum surface used by production code that is imported at
# module level by unit-tested files:
#   from langchain_core.tools import tool
#   from langchain_core.messages import AIMessage, HumanMessage, ...
#   from langchain_core.callbacks import BaseCallbackHandler
#
# The stub is a proper package (has __path__) so submodule imports work.
# All stubs are no-ops / thin data classes — only enough for collection and
# import-path exercising, not for testing real LangChain behaviour.
# ---------------------------------------------------------------------------


def _install_langchain_core_stub() -> None:
    try:
        import langchain_core as _real  # noqa: F401

        return
    except ImportError:
        pass

    import tempfile

    # ---- tools submodule ----

    class _ToolWrapper:
        """Minimal LangChain Tool stub: preserves .invoke(), .func, and direct call.

        Real LangChain StructuredTool exposes the wrapped callable as .func so
        unit tests that call tool.func(...) directly reach the Python implementation
        without going through pydantic validation.
        """

        def __init__(self, func):
            self._func = func
            self.func = func  # LangChain StructuredTool-compatible
            self.__name__ = getattr(func, "__name__", "tool")
            self.name = self.__name__
            self.__doc__ = getattr(func, "__doc__", "")
            self.description = self.__doc__
            self.args_schema = self._build_args_schema(func)

        @staticmethod
        def _build_args_schema(func):
            import inspect
            from typing import Any

            from pydantic import create_model

            fields = {}
            for name, parameter in inspect.signature(func).parameters.items():
                annotation = (
                    parameter.annotation
                    if parameter.annotation is not inspect.Parameter.empty
                    else Any
                )
                default = ... if parameter.default is inspect.Parameter.empty else parameter.default
                fields[name] = (annotation, default)
            return create_model(f"{getattr(func, '__name__', 'tool')}_Args", **fields)

        def invoke(self, input_data, **_kwargs):
            if isinstance(input_data, dict):
                return self._func(**input_data)
            return self._func(input_data)

        def __call__(self, *args, **kwargs):
            return self._func(*args, **kwargs)

    def tool(_func_or_name=None, **_kwargs):
        """Minimal @tool decorator stub; returns a _ToolWrapper with .invoke().

        Handles all three LangChain calling conventions:
          @tool                          → tool(fn)       → _func_or_name is callable
          @tool(extras=...)              → tool(**kw)(fn) → _func_or_name is None
          tool("name", description=...)(fn)               → _func_or_name is str
        """
        if callable(_func_or_name):
            # Called as @tool directly — wrap the function
            return _ToolWrapper(_func_or_name)

        # Called as @tool(...) or tool("name", ...) — return a decorator factory
        _name_override = _func_or_name if isinstance(_func_or_name, str) else None

        def _decorator(f):
            wrapper = _ToolWrapper(f)
            if _name_override:
                wrapper.name = _name_override
                wrapper.__name__ = _name_override
            return wrapper

        return _decorator

    class _BaseTool:
        pass

    class _StructuredTool(_BaseTool, _ToolWrapper):
        """Minimal StructuredTool stub: supports .from_function() classmethod."""

        def __init__(self, func=None, **kw):
            if func is not None:
                super().__init__(func)
            else:
                # Called with keywords only — create a no-op function
                def _noop(**_kwargs):
                    return ""

                super().__init__(_noop)
            self.name = kw.get("name", self.__name__)
            self.description = kw.get("description", "")

        @classmethod
        def from_function(cls, func, name=None, description="", args_schema=None, **_kw):
            inst = cls(func)
            inst.name = name or getattr(func, "__name__", "tool")
            inst.description = description
            return inst

    # ---- messages submodule ----

    class _BaseMessage:
        def __init__(self, content="", **_kw):
            self.content = content
            self.additional_kwargs = _kw.get("additional_kwargs", {})
            self.type = "base"

        def __repr__(self):
            return f"{self.__class__.__name__}(content={self.content!r})"

    class _AIMessage(_BaseMessage):
        def __init__(self, content="", **kw):
            super().__init__(content, **kw)
            self.type = "ai"
            self.tool_calls = kw.get("tool_calls", [])

    class _HumanMessage(_BaseMessage):
        def __init__(self, content="", **kw):
            super().__init__(content, **kw)
            self.type = "human"

    class _SystemMessage(_BaseMessage):
        def __init__(self, content="", **kw):
            super().__init__(content, **kw)
            self.type = "system"

    class _ToolMessage(_BaseMessage):
        def __init__(self, content="", tool_call_id="", **kw):
            super().__init__(content, **kw)
            self.type = "tool"
            self.tool_call_id = tool_call_id

    _AIMessage.__name__ = "AIMessage"
    _HumanMessage.__name__ = "HumanMessage"
    _SystemMessage.__name__ = "SystemMessage"
    _ToolMessage.__name__ = "ToolMessage"

    # ---- callbacks submodule ----

    class _BaseCallbackHandler:
        pass

    # ---- assemble package ----
    # A real package must have __path__ set so Python recognises submodule imports.
    _tmpdir = tempfile.mkdtemp(prefix="langchain_core_stub_")

    def _make_pkg(name: str) -> types.ModuleType:
        mod = sys.modules.get(name) or types.ModuleType(name)
        mod.__path__ = [_tmpdir]  # makes Python treat it as a package
        mod.__package__ = name
        return mod

    lc_mod = _make_pkg("langchain_core")

    tools_mod = _make_pkg("langchain_core.tools")
    tools_mod.tool = tool
    tools_mod.BaseTool = _BaseTool
    tools_mod.StructuredTool = _StructuredTool
    lc_mod.tools = tools_mod

    messages_mod = _make_pkg("langchain_core.messages")
    messages_mod.BaseMessage = _BaseMessage
    messages_mod.AIMessage = _AIMessage
    messages_mod.HumanMessage = _HumanMessage
    messages_mod.SystemMessage = _SystemMessage
    messages_mod.ToolMessage = _ToolMessage
    lc_mod.messages = messages_mod

    callbacks_mod = _make_pkg("langchain_core.callbacks")
    callbacks_mod.BaseCallbackHandler = _BaseCallbackHandler
    lc_mod.callbacks = callbacks_mod

    # ---- prompts submodule ----

    class _ChatPromptTemplate:
        """Minimal stub for langchain_core.prompts.ChatPromptTemplate."""

        def __init__(self):
            self._messages = []

        @classmethod
        def from_messages(cls, messages, **_kw):
            instance = cls()
            instance._messages = list(messages)
            return instance

        def invoke(self, variables, **_kw):
            return self.format_messages(**variables)

        def format_messages(self, **kwargs):
            result = []
            for msg in self._messages:
                if isinstance(msg, _MessagesPlaceholder):
                    result.extend(kwargs.get(msg.variable_name, []))
                elif isinstance(msg, (list, tuple)) and len(msg) == 2:
                    role, content = msg
                    if isinstance(content, str):
                        with contextlib.suppress(KeyError, ValueError, IndexError):
                            content = content.format(**kwargs)
                    if role == "system":
                        result.append(_SystemMessage(content=content))
                    elif role == "human":
                        result.append(_HumanMessage(content=content))
                    elif role == "ai":
                        result.append(_AIMessage(content=content))
                    else:
                        result.append(_BaseMessage(content=content))
                elif isinstance(msg, _BaseMessage):
                    result.append(msg)
            return result

    class _MessagesPlaceholder:
        def __init__(self, variable_name="history", optional=False, **_kw):
            self.variable_name = variable_name
            self.optional = optional

    prompts_mod = _make_pkg("langchain_core.prompts")
    prompts_mod.ChatPromptTemplate = _ChatPromptTemplate
    prompts_mod.MessagesPlaceholder = _MessagesPlaceholder
    lc_mod.prompts = prompts_mod

    sys.modules["langchain_core"] = lc_mod
    sys.modules["langchain_core.tools"] = tools_mod
    sys.modules["langchain_core.messages"] = messages_mod
    sys.modules["langchain_core.callbacks"] = callbacks_mod
    sys.modules["langchain_core.prompts"] = prompts_mod


_install_langchain_core_stub()


# ---------------------------------------------------------------------------
# langchain_google_genai stub (only when absent or broken in environment)
# ---------------------------------------------------------------------------


def _install_langchain_google_genai_stub() -> None:
    try:
        import langchain_google_genai as _real  # noqa: F401

        return
    except Exception:
        for module_name in list(sys.modules):
            if module_name == "langchain_google_genai" or module_name.startswith(
                "langchain_google_genai."
            ):
                sys.modules.pop(module_name, None)

    mod = sys.modules.get("langchain_google_genai") or types.ModuleType("langchain_google_genai")

    class _ChatGoogleGenerativeAI:
        def __init__(self, **_kw):
            self.model_name = "gemini-stub"

        def invoke(self, messages, **_kw):
            return None

        def bind_tools(self, tools, **_kw):
            return self

    mod.ChatGoogleGenerativeAI = _ChatGoogleGenerativeAI
    sys.modules["langchain_google_genai"] = mod


_install_langchain_google_genai_stub()


# ---------------------------------------------------------------------------
# pytest-xdist groups for per-process caches (ADR 0037)
#
# Some modules share an expensive per-process cache: the parsed core/ source
# index the architecture and guardrail scans read, and the environment-variable
# scan the env-audit tests read. On separate workers each worker would build and
# hold its own copy (about 0.7 GB RSS for the source index), so the cost would be
# paid once per worker instead of once. Grouping them keeps them on one worker
# under --dist loadgroup. A module-level xdist_group mark takes precedence.
# ---------------------------------------------------------------------------
_SHARED_CACHE_GROUPS: dict[str, frozenset[str]] = {
    "source-scans": frozenset(
        {
            "test_retry_architecture.py",
            "test_dead_test_detector.py",
            "lifecycle/test_task10_architecture.py",
            "test_runtime_role_distribution_contract.py",
            "test_pre_010_cleanup_guardrails.py",
            "test_extension_browser_authority_install.py",
            "test_extension_service_cutover.py",
            "test_model_client_construction_boundary.py",
            "test_failure_reason_vocabulary.py",
            "test_compact_operator_advice.py",
            "test_state_writer_architecture.py",
            "test_entrypoint_db_dependency_budget.py",
            "test_layer_dependency_budget.py",
            "test_state_reader_runtime.py",
            "test_worker_state_init.py",
            "test_llm_provider_extension_architecture.py",
            "test_postgres_only_release_boundary.py",
        }
    ),
    "env-scans": frozenset(
        {
            "test_env_registry.py",
            "test_env_artifact_sync.py",
            "test_config_registry_architecture_budget.py",
        }
    ),
}
# A module that imports one of these cache owners joins its group without being
# listed above, so a new source scan cannot silently build its own copy.
_CACHE_OWNER_GROUPS: dict[str, str] = {
    "tests.support.source_index": "source-scans",
    "core.infrastructure.config.env_audit": "env-scans",
}
_UNIT_ROOT = Path(__file__).resolve().parent


def _shared_cache_group(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(_UNIT_ROOT).as_posix()
    except ValueError:
        return None
    for group, modules in _SHARED_CACHE_GROUPS.items():
        if relative in modules:
            return group
    return None


def _imported_cache_group(module: types.ModuleType | None) -> str | None:
    if module is None:
        return None
    for value in vars(module).values():
        owner = value.__name__ if isinstance(value, types.ModuleType) else None
        owner = owner or getattr(value, "__module__", None)
        if isinstance(owner, str) and owner in _CACHE_OWNER_GROUPS:
            return _CACHE_OWNER_GROUPS[owner]
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Mark shared-cache modules before xdist reads the xdist_group marks."""
    del config
    groups: dict[Path, str | None] = {}
    for item in items:
        if item.get_closest_marker("xdist_group") is not None:
            continue
        if item.path not in groups:
            module = getattr(item, "module", None)
            groups[item.path] = _shared_cache_group(item.path) or _imported_cache_group(module)
        group = groups[item.path]
        if group is not None:
            item.add_marker(pytest.mark.xdist_group(group))


@pytest.fixture
def audit_db():
    """A store on lint.sh's disposable audit database, with one fresh project.

    Yields ``(store, conn, workflow_id, project_id)``: a registry store and an
    autocommit connection on that database, an unused workflow id, and a
    project row; all of them are removed afterwards. Writes are allowed only
    against lint.sh's own disposable, canonical audit database; this fixture
    never falls back to a developer or product database.
    """
    import uuid

    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from core.application.registry.db_store import RegistryDBStore
    from tests._xdist_isolation import audit_database_name

    assert os.environ.get("FS_AUDIT_ISOLATED") == "1"
    dsn = os.environ["FS_DB_DSN"]
    info = conninfo_to_dict(dsn)
    assert (info.get("host"), info.get("dbname"), info.get("user")) == (
        "pg-audit",
        audit_database_name(),
        "audit",
    )
    workflow_id = f"unit_snapshot_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        project_id = conn.execute(
            "INSERT INTO projects (name) VALUES (%s) RETURNING id", (f"snapshot {workflow_id}",)
        ).fetchone()[0]
        store = RegistryDBStore(dsn)
        try:
            yield store, conn, workflow_id, project_id
        finally:
            conn.execute("DELETE FROM jobs WHERE project_id = %s", (project_id,))
            conn.execute(
                "DELETE FROM workflow_definition_snapshots WHERE workflow_id = %s", (workflow_id,)
            )
            conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))
            conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
