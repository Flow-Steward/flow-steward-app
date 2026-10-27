"""
UI tests for Registry Skills CRUD.

Tests:
- List: loads (cards grouped by category), shows agent avatars
- Create: modal, submits
- Detail: form editable, instructions editing, inputs/outputs JSON, save persists

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_registry_skills.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _assert_registry_skill_status(resp, active_test_role: str):
    if active_test_role in {"admin", "operator"}:
        assert resp.ok or resp.status_code == 404
        return
    assert resp.status_code == 403


@pytest.fixture(scope="module")
def registry_mode_enabled(base_url, ui_auth_session):
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    try:
        resp = _get(f"{base_url}/api/skills", verify=verify_ssl, timeout=5)
        if resp.status_code == 404:
            pytest.skip("Registry mode not enabled")
    except Exception as e:
        pytest.skip(f"Registry API not reachable: {e}")


@pytest.fixture(scope="module")
def first_skill_id(base_url, registry_mode_enabled, ui_auth_session, active_test_role):
    if active_test_role != "admin":
        pytest.skip("Registry skill API is admin-only")
    """Get first skill ID from core.application.registry."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/skills", verify=verify_ssl, timeout=5)
    if not resp.ok:
        pytest.skip("Cannot fetch skills list")
    data = resp.json()
    caps = data if isinstance(data, list) else data.get("skills", [])
    if not caps:
        pytest.skip("No skills in registry")
    return caps[0].get("skill_id") or caps[0].get("id")


class TestRegistrySkillsList:
    """Skills list."""

    def test_skills_api_returns_list(self, base_url, registry_mode_enabled, active_test_role):
        """GET /api/skills returns a list."""
        resp = requests.get(f"{base_url}/api/skills", verify=False, timeout=5)
        _assert_registry_skill_status(resp, active_test_role)
        if not resp.ok:
            return
        data = resp.json()
        caps = data if isinstance(data, list) else data.get("skills", [])
        assert isinstance(caps, list)

    def test_skills_have_id(self, base_url, registry_mode_enabled):
        """Each skill has skill_id."""
        resp = requests.get(f"{base_url}/api/skills", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            caps = data if isinstance(data, list) else data.get("skills", [])
            if caps:
                assert "skill_id" in caps[0] or "id" in caps[0]

    def test_skills_have_category(self, base_url, registry_mode_enabled):
        """Skills have category field for grouping."""
        resp = requests.get(f"{base_url}/api/skills", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            caps = data if isinstance(data, list) else data.get("skills", [])
            if caps:
                has_category = any("category" in cap for cap in caps)
                assert has_category, "At least one returned skill must include a category field"

    def test_filter_by_phase(self, base_url, registry_mode_enabled, active_test_role):
        """GET /api/skills?phase=analysis filters by phase."""
        resp = requests.get(
            f"{base_url}/api/skills?phase=analysis",
            verify=False,
            timeout=5,
        )
        if active_test_role in {"admin", "operator"}:
            assert resp.status_code in (200, 400, 422)
        else:
            assert resp.status_code == 403


class TestRegistrySkillDetail:
    """Skill detail API."""

    def test_skill_detail_returns_data(
        self, base_url, registry_mode_enabled, first_skill_id, active_test_role
    ):
        """GET /api/skills/{id} returns data."""
        resp = requests.get(
            f"{base_url}/api/skills/{first_skill_id}",
            verify=False,
            timeout=5,
        )
        _assert_registry_skill_status(resp, active_test_role)
        if not resp.ok:
            return
        assert isinstance(resp.json(), dict)

    def test_skill_detail_has_display_name(self, base_url, registry_mode_enabled, first_skill_id):
        """Skill detail has display_name."""
        resp = requests.get(
            f"{base_url}/api/skills/{first_skill_id}",
            verify=False,
            timeout=5,
        )
        if resp.ok:
            data = resp.json()
            assert "display_name" in data or "name" in data

    def test_skill_not_found_returns_404(self, base_url, registry_mode_enabled, active_test_role):
        """Unknown skill returns 404."""
        resp = requests.get(
            f"{base_url}/api/skills/nonexistent_cap_xyz_abc",
            verify=False,
            timeout=5,
        )
        if active_test_role in {"admin", "operator"}:
            assert resp.status_code == 404
        else:
            assert resp.status_code == 403


class TestRegistrySkillCreate:
    """Skill creation."""

    def test_create_skill_requires_fields(self, base_url, registry_mode_enabled, active_test_role):
        """Creating skill without required fields returns 400/422."""
        resp = requests.post(
            f"{base_url}/api/skills",
            json={"missing": "fields"},
            verify=False,
            timeout=5,
        )
        if active_test_role == "admin":
            assert resp.status_code in (400, 422, 409)
        else:
            assert resp.status_code == 403

    def test_create_skill_with_instructions(
        self, base_url, registry_mode_enabled, active_test_role
    ):
        """Creating skill with instructions field works."""
        test_cap = {
            "skill_id": "test_ui_cap_xyz_delete_me",
            "display_name": "Test UI Skill",
            "description": "Test skill created by UI tests",
            "category": "testing",
            "instructions": "This skill is used for testing purposes only.",
            "enabled": False,
        }
        resp = requests.post(
            f"{base_url}/api/skills",
            json=test_cap,
            verify=False,
            timeout=5,
        )
        if resp.status_code in (200, 201):
            requests.delete(
                f"{base_url}/api/skills/test_ui_cap_xyz_delete_me",
                verify=False,
                timeout=5,
            )
        if active_test_role == "admin":
            assert resp.status_code in (200, 201, 409, 422)
        else:
            assert resp.status_code == 403


class TestRegistrySkillToggle:
    """Skill enable/disable."""

    def test_toggle_endpoint_responds(self, base_url, registry_mode_enabled, first_skill_id):
        """Toggle endpoint responds without 404."""
        resp = requests.post(
            f"{base_url}/api/skills/{first_skill_id}/toggle",
            verify=False,
            timeout=5,
        )
        assert resp.status_code not in (404,)


class TestRegistrySkillHistory:
    """Skill history."""

    def test_history_endpoint_responds(self, base_url, registry_mode_enabled, first_skill_id):
        """History endpoint responds."""
        resp = requests.get(
            f"{base_url}/api/skills/{first_skill_id}/history",
            verify=False,
            timeout=5,
        )
        assert resp.status_code in (200, 404)


class TestRegistrySkillLifecycleRS5002:
    """RS5-002 in the browser: purge a trashed skill through the Trash page.

    The API half of this row proves the dependency guard: trashing a skill that
    an agent references is refused with HTTP 409, the reported dependents span
    agent / workflow_step / published_block, and the skill only becomes
    removable once the owning agent is gone.

    That refusal is NOT reachable through the UI, and the reason is structural
    rather than a gap in this test:
      * the frontend has no call that trashes a registry skill — the only
        delete affordances are Restore and Purge on the Trash page, which act on
        entities that are already trashed;
      * an agent cannot be attached to a trashed skill (POST /api/agents returns
        400), so a trashed skill can never acquire the live dependent that would
        produce the conflict.
    So the UI can own the half that is reachable — purge removes a trashed skill
    and the row disappears — while the guard itself stays proven at the API
    level. This is recorded on the RS5-002 ledger row.
    """

    @staticmethod
    def _create_trashed_skill(base_url, session, verify_ssl, skill_id: str) -> None:
        payload = {
            "skill_id": skill_id,
            "display_name": "RS5-002 UI probe",
            "description": "ui regression fixture",
            "category": "analysis",
            "skill_type": "prompt",
            "instructions": ["x"],
            "expected_inputs": [{"name": "x", "type": "text", "required": True}],
            "expected_outputs": [{"name": "y", "type": "text", "required": True}],
        }
        created = session.post(
            f"{base_url}/api/skills", json=payload, verify=verify_ssl, timeout=20
        )
        assert created.ok, f"fixture skill not created: {created.status_code} {created.text[:200]}"
        trashed = session.post(
            f"{base_url}/api/trash/registry_skill/{skill_id}/trash",
            json={},
            verify=verify_ssl,
            timeout=20,
        )
        assert trashed.ok, f"fixture skill not trashed: {trashed.status_code}"

    def test_trash_page_purges_a_trashed_skill(
        self, page, base_url, ui_auth_session, registry_mode_enabled, active_test_role
    ):
        if active_test_role != "admin":
            pytest.skip("Trash purge is admin-only")
        session, verify_ssl = ui_auth_session
        if session is None:
            pytest.skip("No authenticated UI session")

        skill_id = f"rs5002ui_{os.urandom(4).hex()}"
        self._create_trashed_skill(base_url, session, verify_ssl, skill_id)

        try:
            response = page.goto(f"{base_url}/trash", wait_until="domcontentloaded")
            assert response is not None, (
                f"/trash did not render: {(response.status if response else 'no response')}"
            )
            assert response.ok, (
                f"/trash did not render: {(response.status if response else 'no response')}"
            )
            grid = page.locator('[data-testid="trash-grid"]')
            grid.wait_for(state="attached")

            # The grid lists newest-deleted first, so a freshly trashed skill is
            # on the first page; match the row by its skill id rather than by
            # position so the assertion does not depend on ordering.
            row = page.locator('[data-testid="trash-grid"] tbody tr', has_text=skill_id).first
            row.wait_for(state="visible", timeout=20_000)

            # Purge sits in the row's action menu and asks through the canon
            # confirmation dialog; there is no native confirm() to accept.
            row.locator('button[aria-label^="Actions for "]').click()
            page.get_by_role("menuitem", name="Purge", exact=True).click()
            confirmation = page.get_by_role("alertdialog")
            confirmation.wait_for(state="visible")
            assert confirmation.get_by_text("This cannot be undone.").is_visible()
            confirmation.get_by_role("button", name="Purge skill").click()
            confirmation.wait_for(state="hidden", timeout=20_000)

            # The purge must actually remove it, not merely close a dialog.
            deadline = 30
            for _ in range(deadline):
                probe = session.get(
                    f"{base_url}/api/skills/{skill_id}", verify=verify_ssl, timeout=10
                )
                if probe.status_code == 404:
                    break
                page.wait_for_timeout(1000)
            else:
                pytest.fail("purged skill is still served by the API")

            row.wait_for(state="detached", timeout=20_000)
        finally:
            session.post(
                f"{base_url}/api/trash/registry_skill/{skill_id}/hard-delete",
                json={},
                verify=verify_ssl,
                timeout=20,
            )
