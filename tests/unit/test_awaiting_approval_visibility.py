"""A failed approvals query must not read as "nothing to approve".

Remediation epic 2026-07-29, WS-04 — `AUDIT-038` symptom (d).

The reviewer had a run sitting at a gate and the global approvals drawer said
*"No approvals are waiting."*

`GET /api/jobs/awaiting-approval` wrapped its query in
`except Exception: return JSONResponse([])`. The drawer renders an empty list as
"No approvals are waiting", so **every** failure — a database problem, a scope
resolution error, a serialization bug — was reported to the operator as an
explicit, confident "there is nothing to decide".

That is the worst possible failure mode for this surface. An operator who sees
an error goes looking; an operator told the queue is empty does not.

The handler was also `async def` around a synchronous service call, the same
event-loop-blocking shape as `job_cancel` (RD-002).

The first pass at this fix only changed the *handler*, and the source-shape
assertions below happily passed while the bug survived: the service method
underneath caught the same exception and returned `[]` before the handler could
ever see it. The behavioural cases in `TestTheServiceFailsLoudly` are the ones
that actually pin the property.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path("core/entrypoints/api/jobs.py")
HANDLER = "api_jobs_awaiting_approval"


def handler_node() -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == HANDLER:
            return node
    raise AssertionError(f"{HANDLER} not found — has it been renamed?")


def handler_source() -> str:
    return ast.get_source_segment(SOURCE.read_text(encoding="utf-8"), handler_node()) or ""


class TestFailuresAreNotReportedAsAnEmptyQueue:
    def test_the_handler_does_not_swallow_errors_into_an_empty_list(self):
        """The exact line that produced symptom (d)."""
        source = handler_source()

        assert "JSONResponse([])" not in source, (
            "an empty list renders as 'No approvals are waiting', so returning it "
            "on failure tells the operator there is nothing to decide"
        )

    def test_the_handler_returns_an_error_response_instead(self):
        source = handler_source()

        assert "api_error_response" in source
        assert "fallback_message" in source, (
            "the operator needs to know the queue could not be loaded"
        )

    def test_the_except_clause_binds_the_exception(self):
        """A bare `except Exception:` cannot report anything useful."""
        node = handler_node()
        handlers = [n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]

        assert handlers, "the query is unguarded"
        assert all(h.name for h in handlers), (
            "the exception must be bound so it can reach api_error_response"
        )


class TestTheHandlerDoesNotBlockTheEventLoop:
    def test_it_is_a_threadpool_handler(self):
        """RD-002: an async handler around a sync service call freezes the app."""
        node = handler_node()

        assert isinstance(node, ast.FunctionDef), (
            f"{HANDLER} is async but calls a synchronous service — FastAPI runs "
            "async handlers on the event loop, so one slow query stalls everything"
        )

    def test_it_does_not_await_anything(self):
        """If it grows an await, the `def` above becomes wrong."""
        node = handler_node()
        awaits = [n for n in ast.walk(node) if isinstance(n, (ast.Await, ast.AsyncWith))]

        assert not awaits


class TestTheGuardrailCoversThisShape:
    def test_the_service_call_is_in_the_blocking_list(self):
        """So a future handler calling it as async is caught automatically."""
        from tests.unit.test_async_route_handlers_do_not_block import BLOCKING_CALLS

        assert "list_awaiting_approval_jobs" in BLOCKING_CALLS


class TestScopeIsStillApplied:
    def test_the_query_is_scoped_to_the_caller(self):
        """Fixing visibility must not widen it across accounts."""
        source = handler_source()

        assert "request_scope(request)" in source, (
            "the approvals queue must stay scoped to the caller's account"
        )


@pytest.mark.parametrize(
    "forbidden",
    ["return JSONResponse([])", "return JSONResponse(list())"],
)
def test_no_empty_success_shortcut_anywhere_in_the_handler(forbidden):
    assert forbidden not in handler_source()


class TestTheServiceFailsLoudly:
    """The layer the source-shape assertions above could not see.

    `JobQueryService.list_awaiting_approval_jobs` had its own
    `except Exception: return []`, so fixing only the route left the defect
    intact — the handler's error branch was unreachable.
    """

    @staticmethod
    def _service(logger):
        from core.application.management.management_runtime import create_job_query_service

        return create_job_query_service(get_logger=lambda: logger)

    def test_a_failing_query_raises_instead_of_returning_an_empty_queue(self):
        from unittest.mock import MagicMock

        from core.application.management.errors import ServiceUnavailableError

        logger = MagicMock()
        logger.get_awaiting_approval_jobs.side_effect = RuntimeError("connection reset")

        _raises_callable_145_1 = self._service(logger).list_awaiting_approval_jobs
        with pytest.raises(ServiceUnavailableError):
            _raises_callable_145_1()

    def test_an_unconfigured_database_is_not_an_empty_queue_either(self):
        from core.application.management.errors import ServiceUnavailableError

        _raises_callable_151_1 = self._service(None).list_awaiting_approval_jobs
        with pytest.raises(ServiceUnavailableError):
            _raises_callable_151_1()

    def test_the_original_exception_is_kept_as_the_cause(self):
        """So the correlation id in the response can be traced to a real error."""
        from unittest.mock import MagicMock

        from core.application.management.errors import ServiceUnavailableError

        cause = RuntimeError("connection reset")
        logger = MagicMock()
        logger.get_awaiting_approval_jobs.side_effect = cause

        _raises_callable_164_1 = self._service(logger).list_awaiting_approval_jobs
        with pytest.raises(ServiceUnavailableError) as excinfo:
            _raises_callable_164_1()

        assert excinfo.value.__cause__ is cause

    def test_a_healthy_empty_queue_is_still_an_empty_list(self):
        """Failing loudly must not turn "nothing to approve" into an error."""
        from unittest.mock import MagicMock

        logger = MagicMock()
        logger.get_awaiting_approval_jobs.return_value = []

        assert self._service(logger).list_awaiting_approval_jobs() == []
