"""
UI tests for the approval flow.

These tests intentionally do not submit approval decisions. Awaiting approvals
may point at real paused jobs in a live developer stack, and POSTing a decision
resumes workflow execution.

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_approval_flow.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import expect

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


@pytest.fixture
def awaiting_job(page, server_reachable, base_url):
    """Return an awaiting approval job through the authenticated browser context."""
    page.goto(f"{base_url}/projects", wait_until="networkidle")
    response = page.context.request.get(f"{base_url}/api/jobs/awaiting-approval")
    if response.status in (401, 403):
        pytest.skip("Authenticated browser context cannot read awaiting approvals")
    assert response.status < 400, (
        f"GET /api/jobs/awaiting-approval returned {response.status}: {response.text()[:300]}"
    )

    payload = response.json()
    jobs = payload if isinstance(payload, list) else payload.get("jobs", [])
    if not jobs:
        pytest.skip("No jobs currently awaiting approval")
    return jobs[0]


def _job_id(job: dict) -> str:
    value = job.get("id") or job.get("job_id")
    assert value, f"Awaiting approval row is missing job id: {job!r}"
    return str(value)


class TestApprovalBell:
    """Approval notification bell in the navigation."""

    def test_bell_icon_visible(self, page, server_reachable, base_url):
        page.goto(f"{base_url}/projects", wait_until="networkidle")

        expect(page.get_by_test_id("approval-bell")).to_be_visible(timeout=10_000)

    def test_bell_shows_count_and_links_when_jobs_awaiting(
        self, page, server_reachable, base_url, awaiting_job
    ):
        page.goto(f"{base_url}/projects", wait_until="networkidle")

        badge = page.get_by_test_id("approval-badge")
        expect(badge).to_be_visible(timeout=10_000)
        count_text = badge.inner_text().strip()
        assert count_text.isdigit()
        assert int(count_text) > 0

        page.get_by_test_id("approval-bell").click()
        expect(page.get_by_test_id("approval-job-list")).to_be_visible(timeout=10_000)
        expect(
            page.locator(f"[data-testid='approval-job-link'][href='/jobs/{_job_id(awaiting_job)}']")
        ).to_be_visible()


class TestApprovalPage:
    """Approval workspace content on Job Detail."""

    def test_approval_workspace_loads(self, page, server_reachable, base_url, awaiting_job):
        page.goto(f"{base_url}/jobs/{_job_id(awaiting_job)}", wait_until="domcontentloaded")

        expect(page.get_by_test_id("job-review-workspace")).to_be_visible(timeout=15_000)

    def test_approval_workspace_shows_content_and_comment_box(
        self, page, server_reachable, base_url, awaiting_job
    ):
        page.goto(f"{base_url}/jobs/{_job_id(awaiting_job)}", wait_until="domcontentloaded")

        workspace = page.get_by_test_id("job-review-workspace")
        expect(workspace).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("approval-comment-input")).to_be_visible(timeout=10_000)
        assert len(workspace.inner_text().strip()) > 80

    def test_approve_and_reject_controls_are_visible_and_enabled(
        self, page, server_reachable, base_url, awaiting_job
    ):
        page.goto(f"{base_url}/jobs/{_job_id(awaiting_job)}", wait_until="domcontentloaded")

        approve = page.get_by_test_id("approval-approve-button")
        reject = page.get_by_test_id("approval-reject-button")
        expect(approve).to_be_visible(timeout=15_000)
        expect(reject).to_be_visible(timeout=15_000)
        expect(approve).to_be_enabled()
        expect(reject).to_be_enabled()

    def test_reject_comment_area_is_available_without_submitting(
        self, page, server_reachable, base_url, awaiting_job
    ):
        page.goto(f"{base_url}/jobs/{_job_id(awaiting_job)}", wait_until="domcontentloaded")

        expect(page.get_by_test_id("approval-reject-button")).to_be_visible(timeout=15_000)
        comment = page.get_by_test_id("approval-comment-input")
        expect(comment).to_be_visible()
        comment.fill("Needs a small adjustment before approval.")
        expect(comment).to_have_value("Needs a small adjustment before approval.")
