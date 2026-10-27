#!/usr/bin/env python3
"""Seed the isolated real-stack extension OAuth browser acceptance environment."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from core.application.auth.config import AUTH_BOOTSTRAP_TOKEN_HEADER

ADMIN_EMAIL = "oauth-admin@example.test"
OPERATOR_EMAIL = "oauth-operator@example.test"
VIEWER_EMAIL = "oauth-viewer@example.test"
ADMIN_PASSWORD = "OAuthAcceptanceAdmin_123!"  # pragma: allowlist secret
OPERATOR_PASSWORD = "OAuthAcceptanceOperator_123!"  # pragma: allowlist secret
VIEWER_PASSWORD = "OAuthAcceptanceViewer_123!"  # pragma: allowlist secret
PROJECT_NAMES = ("OAuth Acceptance Alpha", "OAuth Acceptance Beta")


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} failed with {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def _bootstrap_admin(base_url: str, bootstrap_token: str) -> None:
    response = httpx.post(
        f"{base_url}/api/auth/bootstrap",
        headers={AUTH_BOOTSTRAP_TOKEN_HEADER: bootstrap_token},
        json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "display_name": "OAuth Acceptance Admin",
        },
        verify=True,
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"bootstrap failed: {response.status_code} {response.text}")


@contextmanager
def _login(base_url: str, email: str, password: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url, verify=True, timeout=30) as client:
        payload = _request(
            client,
            "POST",
            "/api/auth/login",
            json={"email": email, "password": password, "remember_me": False},
        )
        client.headers["x-csrf-token"] = str(payload["csrf_token"])
        authenticated = _request(client, "GET", "/api/auth/me")
        if not authenticated.get("authenticated"):
            raise RuntimeError("seed login did not establish an authenticated TLS session")
        yield client


def _workspace_id(client: httpx.Client) -> str:
    payload = _request(client, "GET", "/api/workspaces")
    rows = payload if isinstance(payload, list) else payload.get("workspaces", [])
    if not rows:
        raise RuntimeError("default workspace is unavailable")
    return str(rows[0].get("id") or rows[0].get("workspace_id") or "")


def _project(client: httpx.Client, *, name: str, workspace_id: str) -> dict[str, Any]:
    listed = _request(client, "GET", "/api/v2/projects?page=1&per_page=200")
    rows = listed.get("projects", []) if isinstance(listed, dict) else []
    existing = next((row for row in rows if row.get("name") == name), None)
    if existing:
        return dict(existing)
    return dict(
        _request(
            client,
            "POST",
            "/api/projects",
            json={"name": name, "workspace_id": workspace_id},
        )
    )


def _install_id(
    client: httpx.Client,
    *,
    extension_id: str,
    ensure_enabled: bool = False,
) -> str:
    response = client.get(f"/api/extensions/{extension_id}")
    if response.status_code == 404:
        raise RuntimeError(
            f"required pre-provisioned extension is missing from startup inventory: {extension_id}"
        )
    if response.status_code >= 400:
        raise RuntimeError(f"installed extension lookup failed: {response.text}")
    detail = response.json()
    if str(detail.get("extension_id") or "") != extension_id:
        raise RuntimeError("installed extension lookup returned the wrong extension")
    if ensure_enabled and detail.get("state") != "active":
        enabled = _request(client, "POST", f"/api/extensions/{extension_id}/enable")
        if enabled.get("state") != "active":
            raise RuntimeError("consumer extension enable response was not active")
        response = client.get(f"/api/extensions/{extension_id}")
        if response.status_code >= 400:
            raise RuntimeError(f"enabled extension lookup failed: {response.text}")
        detail = response.json()
        if str(detail.get("extension_id") or "") != extension_id:
            raise RuntimeError("enabled extension lookup returned the wrong extension")
        if detail.get("state") != "active":
            raise RuntimeError("consumer extension did not become enabled")
    install_id = str(detail.get("install_id") or detail.get("extension_install_id") or "")
    if not install_id:
        raise RuntimeError("installed extension did not expose install_id")
    return install_id


def _create_user(client: httpx.Client, *, email: str, password: str, role: str) -> None:
    listed = _request(client, "GET", "/api/auth/users?page=1&per_page=200")
    if any(row.get("email") == email for row in listed.get("users", [])):
        return
    _request(
        client,
        "POST",
        "/api/auth/users",
        json={
            "email": email,
            "password": password,
            "display_name": f"OAuth {role.title()}",
            "roles": [role],
            "is_active": True,
            "must_change_password": False,
        },
    )


def _create_configuration(
    client: httpx.Client,
    *,
    project_id: str,
    install_id: str,
    display_name: str,
) -> dict[str, Any]:
    prefix = f"/api/projects/{project_id}/extensions/{install_id}/oauth/example"
    rows = _request(client, "GET", f"{prefix}/configurations")
    existing = next((row for row in rows if row.get("display_name") == display_name), None)
    if existing:
        return dict(existing)
    client_secret = "fixture-" + "credential"
    return dict(
        _request(
            client,
            "PUT",
            f"{prefix}/configurations",
            json={
                "display_name": display_name,
                "values": {"client_id": f"{display_name.lower().replace(' ', '-')}-client"},
                "secret_replacements": {"client_secret": client_secret},
            },
        )
    )


def _consumer_extensions(root: Path) -> list[tuple[str, Path]]:
    discovered: list[tuple[str, Path]] = []
    for policy in sorted(root.glob("*/tests/oauth-browser-seed.json")):
        value = json.loads(policy.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"schema_version", "extension_id"}:
            raise RuntimeError(f"invalid OAuth browser seed declaration: {policy}")
        extension_id = value.get("extension_id")
        if (
            value.get("schema_version") != 1
            or not isinstance(extension_id, str)
            or not extension_id
        ):
            raise RuntimeError(f"invalid OAuth browser seed declaration: {policy}")
        discovered.append((extension_id, policy.parents[1]))
    return discovered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://traefik:8099")
    parser.add_argument("--bootstrap-token", required=True)
    parser.add_argument("--consumer-extensions-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    last_readiness_failure = "no response received"
    for _attempt in range(90):
        try:
            response = httpx.get(f"{args.base_url}/health", verify=True, timeout=2)
            if response.status_code < 500:
                break
            last_readiness_failure = f"HTTP {response.status_code}"
        except Exception as exc:
            last_readiness_failure = f"{type(exc).__name__}: {exc}"[:300]
        time.sleep(1)
    else:
        raise RuntimeError(
            "Flow Steward app did not become ready after 90 attempts; "
            f"last readiness failure: {last_readiness_failure}"
        )

    _bootstrap_admin(args.base_url, args.bootstrap_token)
    with _login(args.base_url, ADMIN_EMAIL, ADMIN_PASSWORD) as admin:
        _create_user(admin, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD, role="operator")
        _create_user(admin, email=VIEWER_EMAIL, password=VIEWER_PASSWORD, role="viewer")
        workspace_id = _workspace_id(admin)
        projects = [_project(admin, name=name, workspace_id=workspace_id) for name in PROJECT_NAMES]
        install_id = _install_id(
            admin,
            extension_id="fixture.extension-oauth",
            ensure_enabled=True,
        )
        consumer_installs = {
            extension_id: _install_id(
                admin,
                extension_id=extension_id,
                ensure_enabled=True,
            )
            for extension_id, _extension_root in _consumer_extensions(args.consumer_extensions_root)
        }
    with _login(args.base_url, OPERATOR_EMAIL, OPERATOR_PASSWORD) as operator:
        alpha_id = str(projects[0].get("id") or projects[0].get("project_id") or "")
        beta_id = str(projects[1].get("id") or projects[1].get("project_id") or "")
        configurations = [
            _create_configuration(
                operator,
                project_id=alpha_id,
                install_id=install_id,
                display_name=name,
            )
            for name in ("Primary application", "Backup application")
        ]
        _create_configuration(
            operator,
            project_id=beta_id,
            install_id=install_id,
            display_name="Isolated application",
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "base_url": args.base_url,
                "extension_id": "fixture.extension-oauth",
                "install_id": install_id,
                "consumer_extensions": consumer_installs,
                "alpha_project_id": alpha_id,
                "beta_project_id": beta_id,
                "alpha_configuration_ids": [row["configuration_id"] for row in configurations],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
