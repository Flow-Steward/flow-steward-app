"""
UI coverage checks for sidebar menu pages and key subpages.

Purpose:
- Ensure all visible admin sidebar pages resolve in browser
- Ensure high-value subpages (project/job details, registry details) resolve
"""

from __future__ import annotations

import pytest
import requests

pytestmark = [pytest.mark.ui]


def _expected_sidebar_paths(active_test_role: str) -> set[str]:
    expected = {
        "/projects",
        "/jobs",
    }
    if active_test_role in {"admin", "operator"}:
        expected.update(
            {
                "/registry/agents",
                "/registry/tools",
                "/registry/skills",
                "/registry/workflows",
                "/registry/guardrails",
            }
        )
    if active_test_role == "admin":
        expected.update({"/settings", "/extensions", "/monitoring"})
    return expected


def _json_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (
            "items",
            "rows",
            "data",
            "jobs",
            "projects",
            "workflows",
            "agents",
            "tools",
            "skills",
            "guardrails",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _goto_status(page, url: str) -> tuple[int | None, str]:
    response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(200)
    status = response.status if response is not None else None
    return status, page.url


class TestSidebarMenuCoverage:
    def test_sidebar_menu_pages_load(self, page, server_reachable, base_url, active_test_role):
        # Visit dashboard first so authenticated sidebar is rendered.
        page.goto(f"{base_url}/dashboard", wait_until="networkidle")
        links = page.evaluate(
            """() => {
                const rows = [];
                const selectors = [
                    '#sidebar a.sidebar-item[href]',
                    '.ai-topbar-nav a[href]',
                    '#launcher-panel a.launcher-item[href]',
                ];
                for (const el of document.querySelectorAll(selectors.join(', '))) {
                    const href = String(el.getAttribute('href') || '').trim();
                    const label = String(el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (href.startsWith('/')) rows.push({ href, label });
                }
                return rows;
            }"""
        )
        hrefs = {
            str(row.get("href") or "").strip() for row in (links or []) if isinstance(row, dict)
        }
        expected = _expected_sidebar_paths(active_test_role)
        missing_in_sidebar = sorted(path for path in expected if path not in hrefs)
        assert not missing_in_sidebar, f"Sidebar is missing expected links: {missing_in_sidebar}"

        failures: list[str] = []
        for path in sorted(expected):
            status, final_url = _goto_status(page, f"{base_url}{path}")
            if status is None or status >= 400:
                failures.append(f"{path} -> status={status}, final_url={final_url}")
        assert not failures, "Sidebar page load failures:\n" + "\n".join(failures)

    def test_project_and_job_subpages_load(self, page, server_reachable, base_url, test_project_id):
        failures: list[str] = []

        for path in (
            f"/projects/{test_project_id}",
            f"/projects/{test_project_id}?tab=history",
        ):
            status, final_url = _goto_status(page, f"{base_url}{path}")
            if status is None or status >= 400:
                failures.append(f"{path} -> status={status}, final_url={final_url}")

        jobs_resp = requests.get(
            f"{base_url}/api/jobs",
            params={"project_id": test_project_id, "limit": 20},
            verify=False,
            timeout=20,
        )
        if jobs_resp.ok:
            jobs = _json_list(jobs_resp.json())
            job_id = None
            for row in jobs:
                if not isinstance(row, dict):
                    continue
                raw_id = row.get("id") or row.get("job_id")
                if raw_id:
                    job_id = str(raw_id).strip()
                    break
            if job_id:
                status, final_url = _goto_status(page, f"{base_url}/jobs/{job_id}")
                if status is None or status >= 400:
                    failures.append(f"/jobs/{job_id} -> status={status}, final_url={final_url}")

        assert not failures, "Subpage load failures:\n" + "\n".join(failures)

    def test_registry_detail_subpages_load(
        self, page, server_reachable, base_url, active_test_role
    ):
        if active_test_role != "admin":
            pytest.skip("Registry detail API list lookups are admin-only")
        cases = [
            ("/api/agents", "agent_id", "/registry/agents/{id}"),
            ("/api/tools", "tool_id", "/registry/tools/{id}"),
            ("/api/skills", "skill_id", "/registry/skills/{id}"),
            ("/api/workflows", "workflow_id", "/registry/workflows/{id}"),
            ("/api/guardrails", "guardrail_id", "/registry/guardrails/{id}"),
        ]
        failures: list[str] = []

        for api_path, id_key, ui_tpl in cases:
            resp = requests.get(f"{base_url}{api_path}", verify=False, timeout=20)
            if resp.status_code == 404:
                continue
            assert resp.ok, f"{api_path} failed: {resp.status_code}"
            rows = _json_list(resp.json())
            if not rows:
                continue
            first = rows[0] if isinstance(rows[0], dict) else {}
            entity_id = first.get(id_key) or first.get("id")
            if not entity_id:
                continue
            path = ui_tpl.replace("{id}", str(entity_id))
            status, final_url = _goto_status(page, f"{base_url}{path}")
            if status is None or status >= 400:
                failures.append(f"{path} -> status={status}, final_url={final_url}")

        assert not failures, "Registry detail page failures:\n" + "\n".join(failures)
