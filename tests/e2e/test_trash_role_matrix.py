"""Strict E2E role matrix for the real trash lifecycle routes."""

from __future__ import annotations

import os
import uuid

import pytest

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import anon_get, get, post_json, purge_trashed_project

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_ACTIVE_ROLE = str(os.getenv("E2E_BACKEND_ROLE") or "admin").strip().lower()


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _skip_unless_role(*roles: str):
    if _ACTIVE_ROLE not in roles:
        pytest.skip(
            f"Test requires role in {roles!r}; active role is '{_ACTIVE_ROLE}'. "
            "Re-run with E2E_BACKEND_ROLE=<role>."
        )


def _trash_path(entity_type: str, entity_id: str | int, action: str) -> str:
    return f"/api/trash/{entity_type}/{entity_id}/{action}"


def _trash_items(*, entity_type: str | None = None, limit: int = 200) -> list[dict]:
    query = f"/api/trash?limit={limit}"
    if entity_type:
        query += f"&entity_type={entity_type}"
    resp = get(query)
    assert resp.status_code == 200, f"trash list failed: {resp.status_code} {resp.text[:300]}"
    body = resp.json() or {}
    items = body if isinstance(body, list) else body.get("items") or body.get("trash") or []
    return [item for item in items if isinstance(item, dict)]


def _project_exists(project_id: str | int) -> bool:
    resp = get(f"/api/projects/{project_id}")
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    pytest.fail(f"project readback returned {resp.status_code}: {resp.text[:300]}")
    return False


def _create_disposable_project() -> str:
    name = f"trash-test-{_uid()}"
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


@pytest.fixture(scope="module")
def server_available():
    resp = get("/health", timeout=5)
    assert resp.status_code == 200, f"Server health failed: {resp.status_code} {resp.text[:200]}"


class TestTrashListAccess:
    def test_anonymous_cannot_list_trash(self, server_available):
        resp = anon_get("/api/trash")
        assert resp.status_code in (401, 403, 302), (
            f"anonymous /api/trash must be denied; got {resp.status_code}"
        )

    def test_trash_list_is_admin_only_and_has_stable_shape(self, server_available):
        resp = get("/api/trash?limit=5&entity_type=project")
        if _ACTIVE_ROLE != "admin":
            assert resp.status_code == 403
            return

        assert resp.status_code == 200, f"trash list failed: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert isinstance(body, dict)
        assert "items" in body
        assert isinstance(body["items"], list)
        assert "total" in body
        for item in body["items"][:5]:
            assert isinstance(item, dict)
            assert str(item.get("entity_type") or "").strip()
            assert str(item.get("entity_id") or "").strip()


class TestTrashLifecycleRoutes:
    def test_admin_can_trash_restore_and_hard_delete_project_via_real_routes(
        self, server_available
    ):
        _skip_unless_role("admin")
        project_id = _create_disposable_project()

        trash_resp = post_json(_trash_path("project", project_id, "trash"), {})
        assert trash_resp.status_code == 200, (
            f"trash route failed: {trash_resp.status_code} {trash_resp.text[:300]}"
        )
        assert trash_resp.json() == {
            "status": "trashed",
            "entity_type": "project",
            "entity_id": str(project_id),
        }
        trashed_ids = {
            str(item.get("entity_id") or "") for item in _trash_items(entity_type="project")
        }
        assert str(project_id) in trashed_ids

        restore_resp = post_json(_trash_path("project", project_id, "restore"), {})
        assert restore_resp.status_code == 200, (
            f"restore route failed: {restore_resp.status_code} {restore_resp.text[:300]}"
        )
        assert restore_resp.json() == {
            "status": "restored",
            "entity_type": "project",
            "entity_id": str(project_id),
        }
        assert _project_exists(project_id) is True
        restored_ids = {
            str(item.get("entity_id") or "") for item in _trash_items(entity_type="project")
        }
        assert str(project_id) not in restored_ids

        retrash_resp = post_json(_trash_path("project", project_id, "trash"), {})
        assert retrash_resp.status_code == 200
        refused_resp = post_json(_trash_path("project", project_id, "hard-delete"), {})
        assert refused_resp.status_code == 409, (
            f"the direct project hard-delete must be refused: {refused_resp.status_code}"
        )
        hard_delete_resp = purge_trashed_project(project_id)
        assert hard_delete_resp.status_code == 200, (
            f"project purge failed: {hard_delete_resp.status_code} {hard_delete_resp.text[:300]}"
        )
        assert hard_delete_resp.json() == {
            "status": "hard_deleted",
            "entity_type": "project",
            "entity_id": str(project_id),
        }
        assert _project_exists(project_id) is False
        deleted_ids = {
            str(item.get("entity_id") or "") for item in _trash_items(entity_type="project")
        }
        assert str(project_id) not in deleted_ids

    def test_viewer_real_trash_routes_are_forbidden(self, server_available):
        _skip_unless_role("viewer")
        for action in ("trash", "restore", "hard-delete"):
            resp = post_json(_trash_path("project", 999999999, action), {})
            assert resp.status_code == 403, (
                f"viewer must be forbidden on {action}; got {resp.status_code}"
            )

    def test_operator_restore_and_hard_delete_are_forbidden(self, server_available):
        _skip_unless_role("operator")
        for action in ("restore", "hard-delete"):
            resp = post_json(_trash_path("project", 999999999, action), {})
            assert resp.status_code == 403, (
                f"operator must be forbidden on {action}; got {resp.status_code}"
            )

    def test_operator_can_trash_via_real_route(self, server_available):
        _skip_unless_role("operator")
        project_id = _create_disposable_project()
        trash_resp = post_json(_trash_path("project", project_id, "trash"), {})
        assert trash_resp.status_code == 200, (
            f"operator trash route failed: {trash_resp.status_code} {trash_resp.text[:300]}"
        )
        assert trash_resp.json() == {
            "status": "trashed",
            "entity_type": "project",
            "entity_id": str(project_id),
        }
        # The global trash inventory is intentionally admin-only.  The operator
        # contract ends at the successful soft-delete and must not widen into
        # visibility of other accounts' trashed entities.
        list_resp = get("/api/trash?limit=5&entity_type=project")
        assert list_resp.status_code == 403

    def test_nonexistent_entity_routes_return_404_for_admin(self, server_available):
        _skip_unless_role("admin")
        # The direct project hard-delete refuses every project; a purge starts at its plan.
        expected_status = {
            "trash": 404,
            "restore": 404,
            "hard-delete": 409,
            "plan": 404,
        }
        for action, status_code in expected_status.items():
            payload = {"action_type": "hard_delete"} if action == "plan" else {}
            resp = post_json(_trash_path("project", 999999999, action), payload)
            assert resp.status_code == status_code, (
                f"nonexistent {action} must return {status_code}; got {resp.status_code}"
            )
