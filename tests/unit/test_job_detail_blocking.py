"""AC17-R155: one job's detail froze the whole API process.

Measured on a live Scalable and on a local Compact: GET /api/jobs/{id} for a finished
four-step, 200-row job took 1.4-2 s, and /health/live, answered in 0.014 s alone, took
5.4 s while four of those requests were in flight. Profiled inside the image, 1.48 of
1.6 s went to the diagnostics preview, which redacts every leaf and resolved the
security policy again for each one: global policy, a deep copy of the default, the
project policy and a system-setting query -- 3085 SQL statements for one GET. And the
route is `async def`, so all of it ran on the event loop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.application.reports.job_detail_contract import build_job_detail_contract
from core.infrastructure.security import integration
from core.infrastructure.security.policy import SecurityPolicy

_ROWS = [
    {"sku": f"SKU-{index:06d}", "title": f"Item {index}", "quantity": index % 97}
    for index in range(200)
]


class _Dump:
    def __init__(self, payload):
        self._payload = payload

    def dump(self):
        return self._payload


class _FinishedJob:
    """A completed job whose step carries the 200 rows, as the benchmark's does."""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        message = {
            "id": "msg-stage-3",
            "step_id": "work",
            "from_agent": "transform",
            "status": "pass",
            "summary": "Transformed 200 rows.",
            "timestamp": "2026-09-23T14:23:52Z",
            "artifacts": {"stage_3": _ROWS},
            "errors": [],
        }
        self._message = message

    async def get_job_status(self, job_id, *, scope=None, include_workflow_result=False):
        _ = scope
        time.sleep(self.delay_seconds)  # a synchronous store, as the real one is
        return _Dump(
            {
                "id": str(job_id),
                "status": "completed",
                "project_id": "proj-1",
                "thread_id": "thread-1",
                "task_summary": "AC19 profile benchmark job",
                "current_step_id": "work",
            }
        )

    def get_logs_page(self, job_id, *, initial_limit=50, scope=None):
        _ = (initial_limit, scope)
        return _Dump(
            {
                "job": {"id": str(job_id), "project_id": "proj-1", "status": "completed"},
                "runtime": {"current_step_id": "work"},
                "messages": [self._message],
                "events": [],
                "decision_overrides": [],
                "job_errors": [],
            }
        )


def _build(service: _FinishedJob) -> dict:
    return asyncio.run(
        build_job_detail_contract(
            job_id="job-1",
            scope=None,
            job_query_service=service,
            approval_query_service=None,
            get_thread_state_values=None,
            user_role="operator",
        )
    )


@pytest.fixture
def policy_lookups(monkeypatch: pytest.MonkeyPatch) -> dict:
    counts: dict = {"project": 0, "global": 0, "scans": 0, "keys": set()}

    def project_context(project_id):
        counts["project"] += 1
        return "account-1", None

    def global_policy(account_id):
        counts["global"] += 1
        return SecurityPolicy()

    scan = integration.scan_outbound

    def counted_scan(*args, **kwargs):
        counts["scans"] += 1
        counts["keys"].add(kwargs.get("project_id"))
        return scan(*args, **kwargs)

    monkeypatch.setattr(integration, "security_enabled", lambda: True)
    monkeypatch.setattr(integration, "load_project_policy_context", project_context)
    monkeypatch.setattr(integration, "load_global_policy", global_policy)
    monkeypatch.setattr(integration, "scan_outbound", counted_scan)
    return counts


def test_one_job_detail_resolves_each_projects_security_policy_once(
    policy_lookups: dict,
) -> None:
    _build(_FinishedJob())

    assert policy_lookups["scans"] >= 100  # non-vacuity: the rows were redacted leaf by leaf
    # Scans run with and without the job's project; each resolves once.
    assert policy_lookups["project"] == len(policy_lookups["keys"] - {None}) == 1
    assert policy_lookups["global"] == len(policy_lookups["keys"])


def test_a_policy_that_failed_to_resolve_is_not_remembered(
    policy_lookups: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What failed once is resolved again for the next scan -- as without the memo --
    rather than every later leaf reusing the failure or a half-built policy."""
    failures = iter([True])

    def flaky(account_id):
        policy_lookups["global"] += 1
        if next(failures, False):
            raise RuntimeError("policy store unavailable")
        return SecurityPolicy()

    monkeypatch.setattr(integration, "load_global_policy", flaky)

    _build(_FinishedJob())

    # one failed attempt, then one resolution per key
    assert policy_lookups["global"] == 1 + len(policy_lookups["keys"])


def test_building_a_job_detail_does_not_hold_the_event_loop() -> None:
    """A health probe answered beside a slow detail, not after it."""
    service = _FinishedJob(delay_seconds=0.5)

    async def scenario() -> tuple[float, float]:
        started = time.monotonic()
        build = asyncio.ensure_future(
            build_job_detail_contract(
                job_id="job-1",
                scope=None,
                job_query_service=service,
                approval_query_service=None,
                get_thread_state_values=None,
                user_role="operator",
            )
        )
        await asyncio.sleep(0.05)
        probe = time.monotonic() - started
        await build
        return probe, time.monotonic() - started

    probe, total = asyncio.run(scenario())

    assert total >= 0.5  # non-vacuity: the build really took its time
    assert probe < 0.3, "the event loop waited for the job detail"
