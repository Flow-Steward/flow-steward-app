"""Authenticated HTTP sessions for live testing runners."""

from __future__ import annotations

import requests

_TOKEN_APPLICATION_JSON = "application/json"


def authenticated_session(
    *,
    base_url: str,
    verify_ssl: bool,
    username: str,
    password: str,
    bearer_token: str,
    timeout: float = 30,
) -> requests.Session:
    """Create and verify one explicitly authenticated API session."""
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    normalized_bearer = str(bearer_token or "").strip()
    has_local_credentials = bool(normalized_username or normalized_password)

    if normalized_bearer and has_local_credentials:
        raise RuntimeError("Choose either username/password or bearer token authentication")
    if has_local_credentials and not (normalized_username and normalized_password):
        raise RuntimeError("Both username and password are required for local authentication")
    if not normalized_bearer and not has_local_credentials:
        raise RuntimeError("Explicit username/password or bearer token authentication is required")

    session = requests.Session()
    root = str(base_url or "").rstrip("/")
    if normalized_bearer:
        session.headers.update({"Authorization": f"Bearer {normalized_bearer}"})
    else:
        login_payload = {"email": normalized_username, "password": normalized_password}
        login_response = session.post(
            f"{root}/api/auth/login",
            json=login_payload,
            headers={"Content-Type": _TOKEN_APPLICATION_JSON, "Accept": _TOKEN_APPLICATION_JSON},
            verify=verify_ssl,
            timeout=timeout,
        )
        if login_response.status_code == 409:
            try:
                response_payload = login_response.json()
            except (TypeError, ValueError):
                response_payload = {}
            options = response_payload.get("account_options", [])
            active = [
                row
                for row in options
                if isinstance(row, dict)
                and bool(row.get("is_active", True))
                and str(row.get("account_id") or "").strip()
            ]
            defaults = [row for row in active if bool(row.get("is_default"))]
            selected = None
            if len(defaults) == 1:
                selected = defaults[0]
            elif len(active) == 1:
                selected = active[0]
            if response_payload.get("requires_account_selection") and selected is not None:
                login_response = session.post(
                    f"{root}/api/auth/login",
                    json={
                        **login_payload,
                        "account_id": str(selected["account_id"]).strip(),
                    },
                    headers={
                        "Content-Type": _TOKEN_APPLICATION_JSON,
                        "Accept": _TOKEN_APPLICATION_JSON,
                    },
                    verify=verify_ssl,
                    timeout=timeout,
                )
        if login_response.status_code != 200:
            raise RuntimeError(f"Authentication failed with HTTP {login_response.status_code}")

    me_response = session.get(
        f"{root}/api/auth/me",
        headers={"Accept": _TOKEN_APPLICATION_JSON},
        verify=verify_ssl,
        timeout=timeout,
    )
    if me_response.status_code != 200:
        raise RuntimeError(
            f"Authenticated session verification failed with HTTP {me_response.status_code}"
        )
    return session


def csrf_headers(session: requests.Session) -> dict[str, str]:
    token = str(session.cookies.get("fs_csrf") or "").strip()
    return {"x-csrf-token": token} if token else {}
