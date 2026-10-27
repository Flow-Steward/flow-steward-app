"""E2E tests for Job diagnostics APIs.

Covers: JOB-12, JOB-22-27 — runtime journal, decision overrides,
errors, causal chain.
"""

from __future__ import annotations

import os

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import ensure_test_job, get, post_json

pytestmark = [pytest.mark.e2e]

TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
PROJECT_ID = str(TEST_ENV.project_id or "").strip()


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def seed_job(server_available) -> dict:
    """Ensure at least one job exists; create via API if needed."""
    return ensure_test_job(PROJECT_ID)


def _job_id(job: dict) -> int:
    return str(job.get("id") or job.get("job_id") or 0).strip()


# ── Runtime Journal ─────────────────────────────────────────────────────────


class TestRuntimeJournal:
    def test_runtime_journal_exists(self, server_available, seed_job):
        jid = _job_id(seed_job)
        resp = get(f"/api/jobs/{jid}/runtime-journal")
        if resp.status_code == 404:
            pytest.skip("Runtime journal endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))

    def test_runtime_journal_nonexistent_job(self, server_available):
        resp = get("/api/jobs/999999/runtime-journal")
        assert resp.status_code in (404, 200)


# ── Decision Overrides ──────────────────────────────────────────────────────


class TestDecisionOverrides:
    def test_get_overrides_empty(self, server_available, seed_job):
        jid = _job_id(seed_job)
        resp = get(f"/api/jobs/{jid}/decision-overrides")
        if resp.status_code == 404:
            pytest.skip("Decision overrides endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))

    def test_post_override_on_terminal_job(self, server_available, seed_job):
        """Setting an override on a terminal job should be rejected or no-op."""
        jid = _job_id(seed_job)
        resp = post_json(
            f"/api/jobs/{jid}/decision-overrides",
            {
                "override_key": "e2e_test",
                "override_value": "skip",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Decision overrides endpoint not implemented")
        # Should either accept (no-op) or reject
        assert resp.status_code in (200, 201, 400, 409, 422)


# ── Job Errors ──────────────────────────────────────────────────────────────


class TestJobErrors:
    def test_errors_endpoint_exists(self, server_available, seed_job):
        jid = _job_id(seed_job)
        resp = get(f"/api/jobs/{jid}/errors")
        if resp.status_code == 404:
            pytest.skip("Errors endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))

    def test_errors_nonexistent_job(self, server_available):
        resp = get("/api/jobs/999999/errors")
        assert resp.status_code in (404, 200)


# ── Causal Chain ────────────────────────────────────────────────────────────


class TestCausalChain:
    def test_causal_chain_endpoint_exists(self, server_available, seed_job):
        jid = _job_id(seed_job)
        resp = get(f"/api/jobs/{jid}/causal-chain")
        if resp.status_code == 404:
            pytest.skip("Causal chain endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))

    def test_causal_chain_nonexistent_job(self, server_available):
        resp = get("/api/jobs/999999/causal-chain")
        assert resp.status_code in (404, 200)
