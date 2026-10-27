"""
Playwright fixtures for UI tests.

Run:
    docker compose exec fs-app python -m pytest tests/ui/ -v --timeout=60

Note: Playwright must be installed:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import os
import re
import threading
import time
from uuid import UUID

import pytest
import requests

from core.application.tooling.testing.runtime_env import (
    apply_test_lane_defaults,
    resolve_base_url,
    resolve_test_env,
)
from tests.e2e.neutral_registry import ensure_neutral_registry, ensure_project_links

pytestmark = [pytest.mark.ui]

_EXPLICIT_ACTIVE_TEST_ROLE = (
    str(os.getenv("E2E_UI_ROLE") or os.getenv("NIGHTFIRE_AUTH_ROLE") or "").strip().lower()
)

_RESOLVED_UI_TEST_ENV = apply_test_lane_defaults(os.environ, lane="ui", include_dotenv=True)

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
_REGISTRY_BASELINE_READY = False
_REGISTRY_BASELINE_LOCK = threading.Lock()

LIVE_PAGE_PATTERNS = (
    (
        re.compile(r"/dashboard(?:[?#].*)?$"),
        "[data-testid='cmd-center'], #lane-needs-attention, #lane-working, #kpi-strip, h1",
    ),
    (
        re.compile(r"/security(?:[?#].*)?$"),
        "h2:has-text('Security policy'), h2:has-text('Security posture'), h1",
    ),
    (
        re.compile(r"/security/projects/[^/?#]+(?:[?#].*)?$"),
        "h2:has-text('Security policy'), h2:has-text('Security posture'), h1",
    ),
    (
        re.compile(r"/monitoring(?:[?#].*)?$"),
        "[data-section='monitoring'], [data-section='monitoring-overview'], button[data-section='monitoring-events'], h1",
    ),
    (
        re.compile(r"/settings/security(?:[?#].*)?$"),
        "h2:has-text('Security policy'), h2:has-text('Security posture'), h1",
    ),
    (
        re.compile(r"/settings/account(?:[?#].*)?$"),
        "#token-grid, nav button:has-text('Personal tokens'), h2:has-text('Profile')",
    ),
    (
        re.compile(r"/settings/users(?:[?#].*)?$"),
        "#users-grid, #search, h1",
    ),
    (
        re.compile(r"/settings(?:/[^?#]+)?(?:[?#].*)?$"),
        "nav, h1, h2",
    ),
    (
        re.compile(r"/extensions(?:/[^?#]+)?(?:[?#].*)?$"),
        "[data-ext-tab='installed'], #extensions-installed-content, h1",
    ),
    (
        re.compile(r"/projects/[^/?#]+(?:[?#].*)?$"),
        "h1",
    ),
    (
        re.compile(r"/registry/workflows/[^/?#]+(?:[?#].*)?$"),
        "#workflow-route-summary, [data-workflow-overview-action], h1",
    ),
    (
        re.compile(r"/registry/agents/[^/?#]+(?:[?#].*)?$"),
        "button:has-text('Save config'), button:has-text('Back to agents'), h1",
    ),
    (
        re.compile(r"/registry/tools/[^/?#]+(?:[?#].*)?$"),
        "#tool-panel-config input[name='display_name'], button:has-text('Save Config'), a:has-text('Back to tools'), h1",
    ),
    (
        re.compile(r"/registry/skills/[^/?#]+(?:[?#].*)?$"),
        "#skill-panel-config input[name='display_name'], button:has-text('Save Config'), a:has-text('Back to skills'), h1",
    ),
    (
        re.compile(r"/registry/data-contracts/[^/?#]+(?:[?#].*)?$"),
        "a:has-text('Back to data contracts'), [data-testid='entity-header'], h1",
    ),
    (
        re.compile(r"/registry/published-blocks/[^/?#]+(?:[?#].*)?$"),
        "a:has-text('Back to published blocks'), [data-testid='entity-header'], h1",
    ),
    (
        re.compile(r"/registry/guardrails/[^/?#]+(?:[?#].*)?$"),
        "button:has-text('Save Config'), a:has-text('Back to guardrails'), h1",
    ),
    (
        re.compile(r"/registry/changelog(?:[?#].*)?$"),
        "input[type='search'], button:has-text('Restore selected revision'), h1",
    ),
)

LIVE_GRID_PAGE_PATTERNS = (
    (re.compile(r"/projects(?:[?#].*)?$"), "projects-grid"),
    (re.compile(r"/trash(?:[?#].*)?$"), "trash-grid"),
    (re.compile(r"/jobs(?:[?#].*)?$"), "jobs-grid"),
    (re.compile(r"/settings/account(?:[?#].*)?$"), "token-grid"),
    (re.compile(r"/settings/users(?:[?#].*)?$"), "users-grid"),
    (re.compile(r"/monitoring\?tab=events(?:[#].*)?$"), "events-grid"),
    (re.compile(r"/monitoring\?tab=processes(?:[#].*)?$"), "proc-jobs-grid"),
    (re.compile(r"/registry/workflows(?:[?#].*)?$"), "workflows-catalog-grid"),
    (re.compile(r"/registry/tools(?:[?#].*)?$"), "tools-grid"),
    (re.compile(r"/registry/fields(?:[?#].*)?$"), "fields-grid"),
    (re.compile(r"/registry/agents(?:[?#].*)?$"), "agents-grid"),
    (re.compile(r"/registry/skills(?:[?#].*)?$"), "skills-grid"),
    (re.compile(r"/registry/data-contracts(?:[?#].*)?$"), "contracts-grid"),
    (re.compile(r"/registry/published-blocks(?:[?#].*)?$"), "published-blocks-grid"),
    (re.compile(r"/registry/guardrails(?:[?#].*)?$"), "guardrails-grid"),
)


def _live_page_ready_selector(url: str) -> str:
    value = str(url or "")
    for pattern, selector in LIVE_PAGE_PATTERNS:
        if pattern.search(value):
            return selector
    return ""


def _live_grid_ready_id(url: str) -> str:
    value = str(url or "")
    for pattern, grid_id in LIVE_GRID_PAGE_PATTERNS:
        if pattern.search(value):
            return grid_id
    return ""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _bootstrap_headers() -> dict[str, str]:
    token = str(os.getenv("FS_AUTH_BOOTSTRAP_TOKEN", "") or "").strip()
    if not token:
        return {}
    return {"x-platform-setup-token": token}


def _seed_credentials(role: str | None = None) -> dict[str, str]:
    resolved = resolve_test_env(os.environ, lane="ui", role=role, include_dotenv=True)
    return {
        "username": resolved.username,
        "password": resolved.password,
        "display_name": resolved.display_name,
        "role": resolved.role,
    }


def _csrf_headers(session: requests.Session) -> dict[str, str]:
    token = str(session.cookies.get("fs_csrf") or "").strip()
    if not token:
        token = str(session.headers.get("x-csrf-token") or "").strip()
    return {"x-csrf-token": token} if token else {}


def _login_with_credentials(
    base_url: str,
    *,
    creds: dict[str, str],
    verify_ssl: bool,
) -> tuple[requests.Session, requests.Response]:
    session = requests.Session()
    response = _request_with_retries(
        session,
        "POST",
        f"{base_url}/api/auth/login",
        json={"email": creds["username"], "password": creds["password"]},
        verify=verify_ssl,
        timeout=20,
    )
    return session, response


def _response_roles(response: requests.Response) -> list[str]:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    user = payload.get("user") if isinstance(payload, dict) else {}
    if not isinstance(user, dict):
        return []
    roles = user.get("roles")
    if not isinstance(roles, list):
        return []
    return [str(role or "").strip().lower() for role in roles if str(role or "").strip()]


def _ensure_admin_auth_session(base_url: str, *, verify_ssl: bool) -> requests.Session | None:
    admin_creds = _seed_credentials("admin")
    try:
        session, login_resp = _login_with_credentials(
            base_url, creds=admin_creds, verify_ssl=verify_ssl
        )
    except requests.RequestException as exc:
        pytest.skip(f"UI auth endpoint unavailable while logging in admin user: {exc}")
    if login_resp.status_code == 200 and "admin" in _response_roles(login_resp):
        try:
            token = str((login_resp.json() or {}).get("csrf_token") or "").strip()
        except Exception:
            token = ""
        if token:
            session.headers["x-csrf-token"] = token
        return session
    if login_resp.status_code == 400 and "local auth is disabled" in login_resp.text.lower():
        return None
    pytest.skip(
        "UI lane requires the seeded admin credentials from .env; "
        f"login for {admin_creds['username']!r} returned {login_resp.status_code}."
    )


def _find_auth_user(
    base_url: str,
    *,
    admin_session: requests.Session,
    username: str,
    verify_ssl: bool,
) -> dict[str, object] | None:
    resp = _request_with_retries(
        admin_session,
        "GET",
        f"{base_url}/api/auth/users",
        params={"search": username, "per_page": 200},
        verify=verify_ssl,
        timeout=20,
    )
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    rows = payload.get("users") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    username_normalized = str(username or "").strip().lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("username") or "").strip().lower() == username_normalized:
            return dict(row)
    return None


def _ensure_role_bound_user(
    base_url: str, *, role: str, verify_ssl: bool
) -> requests.Session | None:
    creds = _seed_credentials(role)
    try:
        session, login_resp = _login_with_credentials(base_url, creds=creds, verify_ssl=verify_ssl)
    except requests.RequestException as exc:
        pytest.skip(f"UI auth endpoint unavailable while logging in {role} user: {exc}")
    if login_resp.status_code == 200:
        try:
            token = str((login_resp.json() or {}).get("csrf_token") or "").strip()
        except Exception:
            token = ""
        if token:
            session.headers["x-csrf-token"] = token
        roles = _response_roles(login_resp)
        if role in roles:
            return session
        pytest.skip(
            f"Seeded UI role user {creds['username']!r} authenticated without the expected {role!r} role."
        )
    elif login_resp.status_code == 400 and "local auth is disabled" in login_resp.text.lower():
        return None
    pytest.skip(
        "UI lane requires pre-seeded role credentials from .env; "
        f"login for {creds['username']!r} returned {login_resp.status_code}."
    )


def _request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    backoff_seconds: float = 1.0,
    **kwargs,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code >= 500 and attempt < attempts:
                time.sleep(backoff_seconds * attempt)
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            time.sleep(backoff_seconds * attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{method} {url} failed without response")


def _ensure_ui_auth_session(base_url: str) -> tuple[requests.Session, bool] | tuple[None, bool]:
    verify_ssl = _env_flag("E2E_VERIFY_SSL", False)
    if not _env_flag("E2E_UI_ENFORCE_AUTH", True):
        return None, verify_ssl

    session = _ensure_role_bound_user(
        base_url,
        role=_RESOLVED_UI_TEST_ENV.role,
        verify_ssl=verify_ssl,
    )
    return session, verify_ssl


def _ensure_registry_baseline(base_url: str, session: requests.Session, verify_ssl: bool) -> None:
    """Provision the neutral agent, skill and workflows the UI suites run against.

    The built-in registry catalog is empty in 1.0. Provisioning uses the seeded
    admin session so non-admin UI lanes still find the neutral entities.
    """
    global _REGISTRY_BASELINE_READY
    if _REGISTRY_BASELINE_READY:
        return
    with _REGISTRY_BASELINE_LOCK:
        if _REGISTRY_BASELINE_READY:
            return
        provisioning_session = _ensure_admin_auth_session(base_url, verify_ssl=verify_ssl)
        if provisioning_session is None:
            provisioning_session = session

        def _request(method: str, url: str, **kwargs) -> requests.Response:
            if str(method).upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                headers = dict(kwargs.get("headers") or {})
                headers.update(_csrf_headers(provisioning_session))
                kwargs["headers"] = headers
            return _request_with_retries(
                provisioning_session,
                method,
                url,
                verify=verify_ssl,
                timeout=20,
                **kwargs,
            )

        ensure_neutral_registry(_request, base_url)
        for lane in ("backend", "ui"):
            lane_env = resolve_test_env(os.environ, lane=lane, include_dotenv=True)
            ensure_project_links(_request, base_url, str(lane_env.project_id or ""))
        _REGISTRY_BASELINE_READY = True


# ---------------------------------------------------------------------------
# Playwright fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright_available():
    """Skip all UI tests if Playwright is not installed."""
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        pytest.skip(
            "Playwright not installed; run: pip install playwright && playwright install chromium"
        )


@pytest.fixture(scope="session")
def browser(playwright_available):
    """Browser instance shared across all UI tests in the session."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as e:
            message = str(e)
            if "Executable doesn't exist" in message or "playwright install" in message.lower():
                pytest.skip(
                    "Playwright browser executable is missing; run: playwright install chromium"
                )
            # Some restricted/sandboxed macOS environments cannot register
            # Chromium Mach ports and fail before first page creation.
            # Treat this as an environment blocker (skip), not a product regression.
            permission_markers = (
                "bootstrap_check_in",
                "Permission denied (1100)",
                "MachPortRendezvous",
            )
            if any(marker in message for marker in permission_markers):
                pytest.skip(
                    "Playwright Chromium launch is blocked by host sandbox permissions "
                    "(Mach port bootstrap)."
                )
            raise
        yield browser
        browser.close()


@pytest.fixture
def context(browser, base_url, ui_auth_session):
    """New browser context per test — fresh cookies, storage."""
    ctx = browser.new_context(
        ignore_https_errors=True,  # Accept self-signed cert
        viewport={"width": 1280, "height": 800},
    )
    session, _ = ui_auth_session
    if session is not None:
        cookies = [
            {
                "name": item.name,
                "value": item.value,
                "url": base_url,
                "secure": base_url.startswith("https://"),
                "httpOnly": item.name != "fs_csrf",
            }
            for item in session.cookies
        ]
        if cookies:
            ctx.add_cookies(cookies)
    yield ctx
    ctx.close()


@pytest.fixture
def page(context):
    """New page per test."""
    p = context.new_page()
    # Default timeout
    p.set_default_timeout(30_000)
    original_goto = p.goto
    original_wait_for_load_state = p.wait_for_load_state

    def _normalize_wait_until(url: str, wait_until: str | None) -> str | None:
        if wait_until == "networkidle" and (
            _live_page_ready_selector(url) or _live_grid_ready_id(url)
        ):
            return "domcontentloaded"
        return wait_until

    def _wait_for_live_page_ready(url: str, timeout: int) -> None:
        grid_id = _live_grid_ready_id(url)
        if grid_id:
            p.locator(f"#{grid_id}").first.wait_for(state="attached", timeout=min(timeout, 10_000))
            p.wait_for_function(
                """(gridId) => {
                    const grid = document.getElementById(gridId);
                    if (!grid) return false;
                    if (!grid.shadowRoot) return true;
                    const hasRows = grid.shadowRoot.querySelectorAll('tbody tr').length > 0;
                    const hasEmptyState = !!grid.shadowRoot.querySelector('.empty-state, [data-empty-state], .bk-empty');
                    const hasSearch = !!grid.shadowRoot.querySelector('.search input');
                    return hasRows || hasEmptyState || hasSearch;
                }""",
                arg=grid_id,
                timeout=min(timeout, 15_000),
            )
            if grid_id == "proc-jobs-grid":
                p.locator("#proc-workers-grid").first.wait_for(
                    state="attached", timeout=min(timeout, 10_000)
                )
            return

        selector = _live_page_ready_selector(url)
        if selector:
            p.locator(selector).first.wait_for(state="visible", timeout=min(timeout, 30_000))
            return

        return

    def goto_with_live_page_support(url, *args, **kwargs):
        timeout = int(kwargs.get("timeout") or 30_000)
        kwargs["wait_until"] = _normalize_wait_until(str(url), kwargs.get("wait_until"))
        response = original_goto(url, *args, **kwargs)
        _wait_for_live_page_ready(str(url), timeout)
        return response

    def wait_for_load_state_with_live_page_support(state=None, *args, **kwargs):
        normalized_state = state
        if state == "networkidle" and (
            _live_page_ready_selector(p.url) or _live_grid_ready_id(p.url)
        ):
            normalized_state = "load"
        return original_wait_for_load_state(normalized_state, *args, **kwargs)

    p.goto = goto_with_live_page_support
    p.wait_for_load_state = wait_for_load_state_with_live_page_support
    yield p
    p.close()


@pytest.fixture(scope="session")
def base_url():
    """Base URL of the Flow Steward server."""
    return BASE_URL


@pytest.fixture(scope="session")
def ui_auth_session(base_url):
    session, verify_ssl = _ensure_ui_auth_session(base_url)
    if session is not None:
        _ensure_registry_baseline(base_url, session, verify_ssl)
    return session, verify_ssl


@pytest.fixture(scope="session")
def active_test_role():
    return (
        str(os.getenv("E2E_UI_ROLE") or os.getenv("NIGHTFIRE_AUTH_ROLE") or "admin").strip().lower()
        or "admin"
    )


@pytest.fixture(scope="session")
def explicit_active_test_role():
    if _EXPLICIT_ACTIVE_TEST_ROLE in {"admin", "operator", "viewer"}:
        return _EXPLICIT_ACTIVE_TEST_ROLE
    return ""


@pytest.fixture(autouse=True)
def ui_authenticated_requests(request, ui_auth_session):
    """Patch requests.* for UI tests so authenticated API calls work by default."""
    if not request.node.get_closest_marker("ui"):
        yield
        return

    session, verify_ssl = ui_auth_session
    if session is None:
        yield
        return

    def _wrap(method_name: str):
        method = getattr(session, method_name)

        def _call(url, *args, **kwargs):
            kwargs.setdefault("verify", verify_ssl)
            method_upper = method_name.upper()
            if method_upper in {"POST", "PUT", "PATCH", "DELETE", "REQUEST"}:
                headers = dict(kwargs.get("headers") or {})
                csrf_token = str(session.cookies.get("fs_csrf") or "")
                if csrf_token and "x-csrf-token" not in {
                    str(k).lower(): v for k, v in headers.items()
                }:
                    headers["x-csrf-token"] = csrf_token
                kwargs["headers"] = headers
            return method(url, *args, **kwargs)

        return _call

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(requests, "request", _wrap("request"))
    monkeypatch.setattr(requests, "get", _wrap("get"))
    monkeypatch.setattr(requests, "post", _wrap("post"))
    monkeypatch.setattr(requests, "put", _wrap("put"))
    monkeypatch.setattr(requests, "patch", _wrap("patch"))
    monkeypatch.setattr(requests, "delete", _wrap("delete"))
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="module")
def server_reachable(base_url):
    """Skip module if server is not reachable."""
    try:
        session = requests.Session()
        resp = _request_with_retries(
            session,
            "GET",
            f"{base_url}/health",
            attempts=6,
            backoff_seconds=2.0,
            verify=False,
            timeout=10,
        )
    except Exception as e:
        pytest.skip(f"Flow Steward server not reachable: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server health endpoint returned {resp.status_code}")


@pytest.fixture(scope="module")
def test_project_id():
    """Return test project ID or skip."""
    pid = str(_RESOLVED_UI_TEST_ENV.project_id or "").strip()
    backend_env = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
    backend_pid = str(backend_env.project_id or "").strip()

    def _is_uuid_like(value: str) -> bool:
        try:
            UUID(str(value or "").strip())
            return True
        except Exception:
            return False

    if not _is_uuid_like(pid) and _is_uuid_like(backend_pid):
        pid = backend_pid
    if not pid:
        pytest.skip("TEST_PROJECT_UI_ID not set")
    return pid
