"""Strict E2E restore story for project history and field rehydration."""

from __future__ import annotations

import uuid

import pytest

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from tests.e2e.helpers import get, post_json, purge_trashed_project, put_json

pytestmark = [pytest.mark.e2e]


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _create_project(*, name: str) -> str:
    resp = post_json(
        "/api/projects",
        {
            "name": name,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
    )
    assert resp.status_code in (200, 201), (
        f"project create failed: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    project_id = str(body.get("id") or body.get("project_id") or "").strip()
    assert project_id, f"project create missing id: {body}"
    return project_id


def _project_detail(project_id: str | int) -> dict:
    resp = get(f"/api/projects/{project_id}")
    assert resp.status_code == 200, f"project detail failed: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, dict)
    return body


def _project_history(project_id: str | int, *, limit: int = 10) -> list[dict]:
    resp = get(f"/api/projects/{project_id}/history?limit={limit}")
    assert resp.status_code == 200, f"project history failed: {resp.status_code} {resp.text[:300]}"
    body = resp.json() or {}
    rows = body if isinstance(body, list) else body.get("items") or body.get("revisions") or []
    assert isinstance(rows, list)
    return [row for row in rows if isinstance(row, dict)]


def _trash_and_hard_delete_project(project_id: str | int) -> None:
    trash_resp = post_json(f"/api/trash/project/{project_id}/trash", {})
    assert trash_resp.status_code == 200, (
        f"project trash cleanup failed: {trash_resp.status_code} {trash_resp.text[:300]}"
    )
    hard_delete_resp = purge_trashed_project(project_id)
    assert hard_delete_resp.status_code == 200, (
        f"project hard-delete cleanup failed: {hard_delete_resp.status_code} {hard_delete_resp.text[:300]}"
    )


@pytest.fixture(scope="module")
def server_available():
    resp = get("/health", timeout=5)
    assert resp.status_code == 200, f"Server health failed: {resp.status_code} {resp.text[:200]}"


@pytest.fixture
def history_story_project(server_available):
    base_name = f"history-story-{_uid()}"
    project_id = _create_project(name=base_name)

    state_b = {
        "name": f"{base_name}-v2",
        "description": "History story revision B",
    }
    state_c = {
        "name": f"{base_name}-v3",
        "description": "History story revision C",
        "project_rules": "Keep weekly summaries short.",
    }

    update_b = put_json(f"/api/projects/{project_id}", state_b)
    assert update_b.status_code == 200, (
        f"state B update failed: {update_b.status_code} {update_b.text[:300]}"
    )

    update_c = put_json(f"/api/projects/{project_id}", state_c)
    assert update_c.status_code == 200, (
        f"state C update failed: {update_c.status_code} {update_c.text[:300]}"
    )

    history_before_restore = _project_history(project_id, limit=10)
    assert len(history_before_restore) >= 3, (
        f"expected at least create + 2 saves; got {history_before_restore}"
    )

    latest_revision = str(history_before_restore[0]["id"]).strip()
    previous_revision = str(history_before_restore[1]["id"]).strip()
    create_revision = str(history_before_restore[2]["id"]).strip()

    try:
        yield {
            "project_id": project_id,
            "base_name": base_name,
            "state_b": {
                "name": state_b["name"],
                "description": state_b["description"],
                "project_rules": "",
            },
            "state_c": {
                "name": state_c["name"],
                "description": state_c["description"],
                "project_rules": state_c["project_rules"],
            },
            "history_before_restore": history_before_restore,
            "latest_revision": latest_revision,
            "previous_revision": previous_revision,
            "create_revision": create_revision,
        }
    finally:
        _trash_and_hard_delete_project(project_id)


class TestProjectHistoryExactRestore:
    def test_history_list_is_reverse_chronological_and_audited(self, history_story_project):
        history_rows = history_story_project["history_before_restore"]
        assert [row["action"] for row in history_rows[:3]] == ["save", "save", "create"]
        created_at = [str(row.get("created_at") or "") for row in history_rows[:3]]
        assert created_at == sorted(created_at, reverse=True)

    def test_history_diff_between_latest_and_previous_revision_is_exact(
        self, history_story_project
    ):
        project_id = history_story_project["project_id"]
        latest_revision = history_story_project["latest_revision"]
        previous_revision = history_story_project["previous_revision"]
        base_name = history_story_project["base_name"]

        diff_resp = get(
            f"/api/projects/{project_id}/history/{latest_revision}/diff?compare_to_revision_id={previous_revision}"
        )
        assert diff_resp.status_code == 200, (
            f"project diff failed: {diff_resp.status_code} {diff_resp.text[:300]}"
        )
        diff_body = diff_resp.json()
        assert diff_body["project_id"] == project_id
        assert diff_body["selected_revision"]["id"] == latest_revision
        assert diff_body["compare_to"]["id"] == previous_revision
        assert diff_body["summary"] == {
            "total": 3,
            "added": 0,
            "removed": 0,
            "changed": 3,
        }
        assert diff_body["changes"] == [
            {
                "path": "project.description",
                "change": "changed",
                "from": "History story revision C",
                "to": "History story revision B",
            },
            {
                "path": "project.name",
                "change": "changed",
                "from": f"{base_name}-v3",
                "to": f"{base_name}-v2",
            },
            {
                "path": "project.project_rules",
                "change": "changed",
                "from": "Keep weekly summaries short.",
                "to": "",
            },
        ]

    def test_restore_rehydrates_exact_fields_and_creates_new_history_event(
        self, history_story_project
    ):
        project_id = history_story_project["project_id"]
        previous_revision = history_story_project["previous_revision"]
        expected_state = history_story_project["state_b"]

        restore_resp = post_json(
            f"/api/projects/{project_id}/history/{previous_revision}/restore", {}
        )
        assert restore_resp.status_code == 200, (
            f"restore failed: {restore_resp.status_code} {restore_resp.text[:300]}"
        )
        restore_body = restore_resp.json()
        assert restore_body["status"] == "restored"
        assert restore_body["project_id"] == project_id
        assert restore_body["restored_from_revision_id"] == previous_revision
        new_revision_id = str(restore_body.get("new_revision_id") or "").strip()
        assert new_revision_id
        assert new_revision_id != previous_revision

        project_body = _project_detail(project_id)
        assert project_body["name"] == expected_state["name"]
        assert project_body["description"] == expected_state["description"]
        assert str(project_body.get("project_rules") or "") == expected_state["project_rules"]

        history_after_restore = _project_history(project_id, limit=10)
        assert history_after_restore[0]["id"] == new_revision_id
        assert history_after_restore[0]["action"] == "restore"
        assert history_after_restore[0]["source"] == f"revision:{previous_revision}"

    def test_restore_nonexistent_revision_returns_404(self, history_story_project):
        project_id = history_story_project["project_id"]
        resp = post_json(
            f"/api/projects/{project_id}/history/ffffffff-ffff-ffff-ffff-ffffffffffff/restore",
            {},
        )
        assert resp.status_code == 404, (
            f"nonexistent revision restore must return 404; got {resp.status_code}"
        )
