#!/usr/bin/env python3
"""Ensure repository-required SonarQube Quality Profile rules are active."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

REQUIRED_RULES = ("python:S104", "javascript:S104", "typescript:S104")
LANGUAGE_BY_RULE_REPOSITORY = {
    "python": "py",
    "javascript": "js",
    "typescript": "ts",
}
MANAGED_PROFILE_BY_LANGUAGE = {
    "py": "Flow Steward Python",
    "js": "Flow Steward JavaScript",
    "ts": "Flow Steward TypeScript",
}


@dataclass(frozen=True)
class EnsureResult:
    language: str
    profile_name: str
    profile_key: str
    rule: str
    profile_action: str
    rule_action: str


class SonarApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"SonarQube API returned HTTP {status}: {body[:400]}")


class SonarApiClient(Protocol):
    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]: ...


class SonarClient:
    def __init__(self, host_url: str, token: str) -> None:
        parsed_host = urlparse(host_url)
        if parsed_host.scheme not in {"http", "https"}:
            raise SystemExit(f"[sonar-rules] Unsupported URL scheme: {parsed_host.scheme!r}")
        self.host_url = host_url.rstrip("/")
        self.auth_header = "Basic " + base64.b64encode(f"{token}:".encode()).decode("ascii")

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request("GET", path, params)

    def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, params)

    def _request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.host_url}{path}"
        data = None
        if method == "GET":
            url = f"{url}?{urlencode(params, doseq=True)}"
        else:
            data = urlencode(params, doseq=True).encode("utf-8")

        req = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urlopen(req, timeout=60) as resp:  # nosec B310
                payload = resp.read()
        except HTTPError as exc:
            body = _read_http_error_body(exc)
            raise SonarApiError(exc.code, body) from exc
        except URLError as exc:
            raise SystemExit(f"[sonar-rules] Failed to reach SonarQube: {exc}") from exc

        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return {}


def _read_http_error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return "<unavailable>"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Activate repository-required SonarQube Quality Profile rules."
    )
    parser.add_argument(
        "--host-url",
        default=(
            _runtime_env.get("SONAR_EXPORT_HOST_URL")
            or _runtime_env.get("SONAR_WAIT_URL")
            or _runtime_env.get("SONAR_HOST_URL")
            or "http://localhost:9000"
        ),
        help="SonarQube base URL accessible from this machine.",
    )
    parser.add_argument(
        "--rules",
        default=",".join(REQUIRED_RULES),
        help="Comma-separated rule keys to ensure.",
    )
    return parser.parse_args()


def get_token(explicit_token: str = "") -> str:
    token = str(explicit_token or "").strip() or (_runtime_env.get("SONAR_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "[sonar-rules] SONAR_TOKEN is not set. Use a token that can administer "
            "quality profiles on the local SonarQube instance."
        )
    return token


def parse_rule_list(raw_rules: str) -> list[str]:
    return [rule.strip() for rule in raw_rules.split(",") if rule.strip()]


def language_for_rule(rule: str) -> str:
    repository = rule.split(":", 1)[0]
    try:
        return LANGUAGE_BY_RULE_REPOSITORY[repository]
    except KeyError as exc:
        raise SystemExit(f"[sonar-rules] Unsupported rule repository: {repository!r}") from exc


def search_profiles(client: SonarApiClient, language: str) -> list[dict[str, Any]]:
    payload = client.get("/api/qualityprofiles/search", {"language": language})
    return list(payload.get("profiles") or [])


def find_profile_by_name(
    profiles: list[dict[str, Any]], profile_name: str
) -> dict[str, Any] | None:
    for profile in profiles:
        if str(profile.get("name") or "") == profile_name:
            return profile
    return None


def find_default_profile(profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    for profile in profiles:
        if profile.get("isDefault") is True:
            return profile
    return profiles[0] if profiles else None


def profile_key(profile: dict[str, Any]) -> str:
    key = str(profile.get("key") or "")
    if not key:
        raise SystemExit("[sonar-rules] SonarQube profile response did not include a key.")
    return key


def set_default_profile(client: SonarApiClient, language: str, profile_name: str) -> None:
    client.post(
        "/api/qualityprofiles/set_default",
        {"language": language, "qualityProfile": profile_name},
    )


def copy_profile(
    client: SonarApiClient, source_profile: dict[str, Any], target_profile_name: str
) -> dict[str, Any]:
    payload = client.post(
        "/api/qualityprofiles/copy",
        {"fromKey": profile_key(source_profile), "toName": target_profile_name},
    )
    copied = payload.get("profile") or payload
    if isinstance(copied, dict) and copied.get("key"):
        return copied

    profiles = search_profiles(client, str(source_profile.get("language") or ""))
    existing = find_profile_by_name(profiles, target_profile_name)
    if existing:
        return existing
    raise SystemExit(f"[sonar-rules] Failed to create quality profile {target_profile_name!r}.")


def ensure_managed_profile(client: SonarApiClient, language: str) -> tuple[dict[str, Any], str]:
    target_profile_name = MANAGED_PROFILE_BY_LANGUAGE[language]
    profiles = search_profiles(client, language)
    managed_profile = find_profile_by_name(profiles, target_profile_name)
    if managed_profile:
        if managed_profile.get("isDefault") is not True:
            set_default_profile(client, language, target_profile_name)
            return managed_profile, "set-existing-default"
        return managed_profile, "reused-default"

    source_profile = find_default_profile(profiles)
    if not source_profile:
        raise SystemExit(f"[sonar-rules] No SonarQube quality profile found for {language!r}.")

    copied_profile = copy_profile(client, source_profile, target_profile_name)
    set_default_profile(client, language, target_profile_name)
    return copied_profile, f"copied:{source_profile.get('name') or profile_key(source_profile)}"


def activate_rule(client: SonarApiClient, profile: dict[str, Any], rule: str) -> str:
    try:
        client.post(
            "/api/qualityprofiles/activate_rule",
            {"key": profile_key(profile), "rule": rule},
        )
    except SonarApiError as exc:
        body = exc.body.lower()
        if exc.status == 400 and "already" in body and "activ" in body:
            return "already-active"
        raise
    return "activated"


def ensure_rules(client: SonarApiClient, rules: list[str]) -> list[EnsureResult]:
    results: list[EnsureResult] = []
    profile_cache: dict[str, tuple[dict[str, Any], str]] = {}
    for rule in rules:
        language = language_for_rule(rule)
        if language not in profile_cache:
            profile_cache[language] = ensure_managed_profile(client, language)
        managed_profile, profile_action = profile_cache[language]
        rule_action = activate_rule(client, managed_profile, rule)
        results.append(
            EnsureResult(
                language=language,
                profile_name=str(managed_profile.get("name") or ""),
                profile_key=profile_key(managed_profile),
                rule=rule,
                profile_action=profile_action,
                rule_action=rule_action,
            )
        )
    return results


def ensure_sonar_rules(*, host_url: str, rules: str, sonar_token: str) -> int:
    """Ensure Sonar rules from a typed operation boundary."""
    client = SonarClient(str(host_url), get_token(sonar_token))
    try:
        results = ensure_rules(client, parse_rule_list(str(rules)))
    except SonarApiError as exc:
        raise SystemExit(
            f"[sonar-rules] HTTP {exc.status} from SonarQube API. "
            "Use a SONAR_TOKEN that can administer quality profiles."
        ) from exc
    for result in results:
        print(
            "[sonar-rules] "
            f"{result.rule} -> {result.profile_name} "
            f"({result.profile_action}, {result.rule_action})"
        )
    return 0


def main() -> int:
    args = parse_args()
    return ensure_sonar_rules(host_url=str(args.host_url), rules=str(args.rules), sonar_token="")


if __name__ == "__main__":
    sys.exit(main())
