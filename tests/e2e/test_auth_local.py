from __future__ import annotations

import os
import time

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_auth_public]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _seed_credentials() -> dict[str, str]:
    resolved = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
    return {
        "username": resolved.username,
        "password": resolved.password,
        "display_name": resolved.display_name,
    }


def _bootstrap_headers() -> dict[str, str]:
    token = str(os.getenv("FS_AUTH_BOOTSTRAP_TOKEN", "") or "").strip()
    if not token:
        return {}
    return {"x-platform-setup-token": token}


def _request_with_retry(
    session: requests.Session, method: str, url: str, *, attempts: int = 3, **kwargs
):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            time.sleep(float(attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{method} {url} failed without response")


@pytest.mark.e2e
def test_auth_bootstrap_login_logout_flow():
    session = requests.Session()
    creds = _seed_credentials()

    try:
        landing = _request_with_retry(
            session,
            "GET",
            f"{BASE_URL}/",
            verify=VERIFY_SSL,
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        pytest.skip(f"E2E auth target unavailable: {exc}")
    if landing.status_code in (302, 303):
        assert landing.headers.get("location") in ("/bootstrap", "/login")
    else:
        assert landing.status_code == 200
        lowered = landing.text.lower()
        assert "<html" in lowered
        assert '<div id="root"></div>' in lowered
        assert "/app/assets/index" in lowered

    login_resp = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": creds["username"], "password": creds["password"]},
        verify=VERIFY_SSL,
        timeout=20,
    )
    if login_resp.status_code == 400 and "local auth is disabled" in login_resp.text.lower():
        pytest.skip("Local auth disabled in target environment")

    if login_resp.status_code != 200:
        bootstrap_resp = session.post(
            f"{BASE_URL}/api/auth/bootstrap",
            headers=_bootstrap_headers(),
            json={
                "email": creds["username"],
                "password": creds["password"],
                "display_name": creds["display_name"],
            },
            verify=VERIFY_SSL,
            timeout=20,
        )
        assert bootstrap_resp.status_code in (200, 409)
        login_resp = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": creds["username"], "password": creds["password"]},
            verify=VERIFY_SSL,
            timeout=20,
        )

    assert login_resp.status_code == 200
    assert "set-cookie" in login_resp.headers
    csrf_token = (login_resp.json() or {}).get("csrf_token") or session.cookies.get("fs_csrf") or ""
    me_resp = session.get(f"{BASE_URL}/api/auth/me", verify=VERIFY_SSL, timeout=20)
    assert me_resp.status_code == 200
    assert me_resp.json().get("authenticated") is True

    logout_resp = session.post(
        f"{BASE_URL}/api/auth/logout",
        headers={"x-csrf-token": str(csrf_token)},
        verify=VERIFY_SSL,
        timeout=20,
    )
    assert logout_resp.status_code == 200
    assert logout_resp.json().get("status") == "logged_out"
