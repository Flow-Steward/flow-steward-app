"""Async route handlers must not make blocking calls.

Remediation epic 2026-07-29 — RD-002.

`POST /api/jobs/{job_id}/cancel` was declared `async def` and then called the
synchronous `job_command_service().cancel_job(...)` directly. FastAPI runs an
`async def` handler **on the event loop**; a plain `def` handler it runs in a
threadpool. So a blocking call inside an async handler does not just stall that
one request — it freezes the entire application.

Observed: `fs-app` served a login at 12:43:13, two more GETs, and then answered
nothing at all. `/api/health` went from 67ms to a hard timeout. Every transport
tried afterwards — REST, CLI, MCP, and the browser — was talking to a dead app,
which is why the failure first looked like "cancel is broken everywhere".

Any authenticated user clicking *Cancel* could take the whole API down, so this
is a denial of service reachable through a normal UI control, not merely a
broken button.

The fix is the idiomatic one: declare the handler `def` and let FastAPI put it in
the threadpool. This test pins the rule for every route module so the pattern
cannot come back somewhere else.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_DIRS = (
    Path(__file__).resolve().parents[2] / "core" / "entrypoints" / "api",
    Path(__file__).resolve().parents[2] / "core" / "entrypoints" / "admin",
)

#: Service methods that reach the database, the queue, or the network
#: synchronously. Not exhaustive by design — it is a tripwire for the shapes we
#: have actually been burned by, and new entries are cheap to add.
BLOCKING_CALLS = frozenset(
    {
        "cancel_job",
        "restart_job",
        "acknowledge_job",
        "unacknowledge_job",
        "kill_job",
        "retry_job",
        "submit_job",
        "enqueue_job",
        # Added after the approvals-drawer endpoint was found with the same
        # shape as the cancel handler.
        "list_awaiting_approval_jobs",
    }
)

#: Decorators that mark a function as an HTTP route handler.
ROUTE_DECORATORS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _is_route_handler(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in ROUTE_DECORATORS:
            return True
    return False


def _awaits_something(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, (ast.Await, ast.AsyncWith, ast.AsyncFor)):
            return True
    return False


def _called_attributes(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def _route_modules() -> list[Path]:
    modules: list[Path] = []
    for directory in API_DIRS:
        if directory.is_dir():
            modules.extend(sorted(directory.glob("*.py")))
    return modules


def _async_handlers_with_blocking_calls() -> list[str]:
    offenders: list[str] = []
    for path in _route_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or not _is_route_handler(node):
                continue
            if _awaits_something(node):
                # It awaits, so it yields control; a blocking call inside is a
                # narrower problem than freezing the loop outright.
                continue
            blocking = _called_attributes(node) & BLOCKING_CALLS
            if blocking:
                offenders.append(
                    f"{path.name}:{node.lineno} {node.name}() calls {sorted(blocking)}"
                )
    return offenders


def test_no_async_route_handler_makes_a_blocking_service_call():
    """RD-002: this is what took the whole API down."""
    offenders = _async_handlers_with_blocking_calls()

    assert not offenders, (
        "async def route handlers run on the event loop, so a synchronous "
        "service call inside one freezes the entire application. Declare these "
        "handlers `def` so FastAPI runs them in a threadpool:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "handler",
    ["job_cancel", "job_restart", "job_acknowledge", "job_unacknowledge"],
)
def test_the_job_control_handlers_are_threadpool_handlers(handler):
    """Pin the specific four, so a later refactor cannot quietly re-async them."""
    source = (API_DIRS[0] / "jobs_control.py").read_text(encoding="utf-8")

    assert f"    def {handler}(" in source, f"{handler} is missing or renamed"
    assert f"    async def {handler}(" not in source, (
        f"{handler} is async again — it calls a blocking service and will freeze the event loop"
    )


def test_the_scan_actually_finds_route_handlers():
    """A tripwire that silently matches nothing is worse than no tripwire."""
    handlers = 0
    for path in _route_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        handlers += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and _is_route_handler(node)
        )

    assert handlers > 50, f"only {handlers} route handlers found — the scan is not working"


def test_the_project_oauth_callback_runs_in_fastapi_threadpool():
    """Token exchange and post-connect extension testing are synchronous I/O."""
    source = (API_DIRS[0] / "integrations_connectors.py").read_text(encoding="utf-8")

    assert "    def api_project_connection_oauth_callback(" in source
    assert "    async def api_project_connection_oauth_callback(" not in source, (
        "the OAuth callback performs synchronous token/network/runtime work and "
        "must run in FastAPI's threadpool"
    )


def test_the_extension_uninstall_confirm_runs_in_fastapi_threadpool():
    """Remote OAuth revocation after local cleanup must not block the event loop."""
    source = (API_DIRS[0] / "integrations_extension_routes.py").read_text(encoding="utf-8")

    assert "    def api_extension_uninstall_confirm(" in source
    assert "    async def api_extension_uninstall_confirm(" not in source, (
        "confirmed uninstall performs synchronous best-effort OAuth revocation and "
        "must run in FastAPI's threadpool"
    )


def test_the_detector_would_have_caught_the_original_defect(tmp_path):
    """Prove the tripwire fires, rather than trusting that it would."""
    module = tmp_path / "fake_routes.py"
    module.write_text(
        "@router.post('/api/jobs/{job_id}/cancel')\n"
        "async def job_cancel(job_id, request):\n"
        "    job_command_service().cancel_job(job_id)\n"
        "    return {'cancelled': True}\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))

    assert _is_route_handler(node)
    assert not _awaits_something(node)
    assert _called_attributes(node) & BLOCKING_CALLS == {"cancel_job"}
