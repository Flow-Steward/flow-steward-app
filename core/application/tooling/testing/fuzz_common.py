#!/usr/bin/env python3
"""Shared helpers for standalone fuzz scripts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_TOKEN_APPLICATION_JSON = "application/json"

DEFAULT_APP_BASE_URL = "http://localhost:8099"
DEFAULT_MCP_BASE_URL = "http://localhost:8100"
DEFAULT_REPORT_DIR = Path("logs/fuzz")

TRUTHY_VALUES = {"1", "true", "yes", "on"}
LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1"}
ResultStatus = Literal["pass", "warn", "fail", "skip"]


def clean(value: object) -> str:
    return str(value or "").strip()


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = clean(_runtime_env.get(name))
    if not raw:
        return default
    return raw.lower() in TRUTHY_VALUES


def _normalize_local_scheme(raw_url: str) -> str:
    value = clean(raw_url)
    if not value:
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    host = clean(parsed.hostname).lower()
    if host not in LOCALHOST_HOSTS:
        return value
    if parsed.scheme == "https" and not env_flag("E2E_VERIFY_SSL", default=False):
        return urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return value


def resolve_app_base_url(*, default: str = DEFAULT_APP_BASE_URL) -> str:
    raw = (
        clean(_runtime_env.get("TEST_RUNNER_BASE_URL"))
        or clean(_runtime_env.get("APP_BASE_URL"))
        or clean(default)
    )
    return _normalize_local_scheme(raw).rstrip("/")


def resolve_mcp_base_url(*, default: str = DEFAULT_MCP_BASE_URL) -> str:
    explicit = clean(_runtime_env.get("MCP_BASE_URL"))
    if explicit:
        return _normalize_local_scheme(explicit).rstrip("/")
    return _normalize_local_scheme(clean(default)).rstrip("/")


def resolve_ssl_verify() -> bool | str:
    if not env_flag("E2E_VERIFY_SSL", default=False):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    cert_override = clean(_runtime_env.get("E2E_CA_CERT"))
    if cert_override:
        cert_path = Path(cert_override).expanduser()
        if cert_path.is_file():
            return str(cert_path)
    return True


def resolve_credentials(
    *,
    role: str = "admin",
    username_override: str = "",
    password_override: str = "",
) -> tuple[str, str]:
    normalized_role = clean(role).lower() or "admin"
    username = (
        clean(username_override)
        or clean(_runtime_env.get("FUZZ_USERNAME"))
        or clean(_runtime_env.get(f"E2E_{normalized_role.upper()}_USERNAME"))
        or f"e2e_{normalized_role}"
    )
    password = (
        clean(password_override)
        or clean(_runtime_env.get("FUZZ_PASSWORD"))
        or clean(_runtime_env.get(f"E2E_{normalized_role.upper()}_PASSWORD"))
        or f"e2e_{normalized_role}_password_123!"
    )
    return username, password


def login_session(
    *,
    base_url: str,
    verify: bool | str,
    username: str,
    password: str,
    timeout: float,
) -> tuple[requests.Session, str]:
    session = requests.Session()
    login_payload = {"email": username, "password": password}
    response = session.post(
        f"{base_url}/api/auth/login",
        json=login_payload,
        headers={"Content-Type": _TOKEN_APPLICATION_JSON, "Accept": _TOKEN_APPLICATION_JSON},
        timeout=timeout,
        verify=verify,
    )
    if response.status_code == 409:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        options = payload.get("account_options") if isinstance(payload, dict) else None
        active = [
            row
            for row in options or []
            if isinstance(row, dict)
            and bool(row.get("is_active", True))
            and clean(row.get("account_id"))
        ]
        defaults = [row for row in active if bool(row.get("is_default"))]
        selected = None
        if len(defaults) == 1:
            selected = defaults[0]
        elif len(active) == 1:
            selected = active[0]
        if payload.get("requires_account_selection") and selected is not None:
            response = session.post(
                f"{base_url}/api/auth/login",
                json={**login_payload, "account_id": clean(selected["account_id"])},
                headers={
                    "Content-Type": _TOKEN_APPLICATION_JSON,
                    "Accept": _TOKEN_APPLICATION_JSON,
                },
                timeout=timeout,
                verify=verify,
            )
    if response.status_code != 200:
        detail = response.text[:300] if response.text else "<empty body>"
        raise RuntimeError(f"Login failed: HTTP {response.status_code}: {detail}")
    token = clean(session.cookies.get("fs_session"))
    if not token:
        raise RuntimeError("Login succeeded but fs_session cookie is missing")
    return session, token


def csrf_headers(session: requests.Session) -> dict[str, str]:
    token = clean(session.cookies.get("fs_csrf"))
    return {"x-csrf-token": token} if token else {}


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def default_report_path(prefix: str, *, suffix: str = ".json") -> Path:
    return DEFAULT_REPORT_DIR / f"{prefix}-{utc_stamp()}{suffix}"


@dataclass(slots=True)
class CheckResult:
    name: str
    status: ResultStatus
    detail: str
    http_status: int | None = None
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.elapsed_ms is not None:
            payload["elapsed_ms"] = self.elapsed_ms
        return payload


def status_counts(results: list[CheckResult]) -> dict[str, int]:
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def write_report(
    *,
    path: Path,
    lane: str,
    seed: int,
    base_url: str,
    results: list[CheckResult],
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    counts = status_counts(results)
    payload: dict[str, Any] = {
        "lane": lane,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "seed": seed,
        "base_url": base_url,
        "summary": {
            "total": len(results),
            "pass": counts.get("pass", 0),
            "warn": counts.get("warn", 0),
            "fail": counts.get("fail", 0),
            "skip": counts.get("skip", 0),
        },
        "results": [result.to_dict() for result in results],
    }
    if extras:
        payload["extras"] = extras
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return payload


def exit_code(results: list[CheckResult], *, fail_on_warn: bool = False) -> int:
    has_fail = any(result.status == "fail" for result in results)
    if has_fail:
        return 1
    if fail_on_warn and any(result.status == "warn" for result in results):
        return 2
    return 0


def print_results(results: list[CheckResult]) -> None:
    for result in results:
        http_suffix = f" [http={result.http_status}]" if result.http_status is not None else ""
        print(f"[{result.status.upper()}] {result.name}{http_suffix} :: {result.detail}")
