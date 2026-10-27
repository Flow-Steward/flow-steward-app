"""Bounded HTTPS transport for the public GitHub API."""

from __future__ import annotations

import json
from collections.abc import Callable
from http.client import HTTPSConnection
from urllib.error import URLError
from urllib.parse import urlsplit

from core.application.release.catalog import ReleaseDiscoveryError

GITHUB_API_VERSION = "2026-03-10"
DEFAULT_HTTP_TIMEOUT_SECONDS = 10
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
GITHUB_API_ORIGIN = "https://api.github.com"


class GitHubHTTPError(ReleaseDiscoveryError):
    """GitHub returned an unavailable, unsafe, or malformed response."""


def _github_error(
    message: str,
    *,
    code: str,
    remediation: str,
    retryable: bool,
) -> GitHubHTTPError:
    return GitHubHTTPError(
        message,
        code=code,
        remediation=remediation,
        retryable=retryable,
    )


class GitHubJSONFetcher:
    """GitHub-origin-bound JSON fetcher with an optional owner-only credential."""

    def __init__(self, token_loader: Callable[[], str | None]) -> None:
        self.token_loader = token_loader

    def fetch(self, url: str) -> object:
        parsed = urlsplit(str(url or "").strip())
        if f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" != GITHUB_API_ORIGIN:
            raise _github_error(
                "Release URL does not match the configured GitHub API origin",
                code="release_github_origin_invalid",
                remediation="Use only the configured https://api.github.com release origin.",
                retryable=False,
            )
        try:
            token = self.token_loader()
        except (OSError, ValueError) as exc:
            raise _github_error(
                "GitHub release credential is unavailable or invalid",
                code="release_github_credential_invalid",
                remediation=(
                    "Install a valid owner-only GitHub release token with repository read access."
                ),
                retryable=False,
            ) from exc
        authorization = f"Bearer {token}" if token else None
        return fetch_json_url(url, authorization=authorization)


def fetch_json_url(url: str, *, authorization: str | None = None) -> object:
    try:
        status, raw = _http_get_bytes(url, authorization=authorization)
    except ValueError as exc:
        raise _github_error(
            "GitHub Release request could not be constructed safely",
            code="release_github_request_invalid",
            remediation="Correct the owner-only GitHub release credential and retry.",
            retryable=False,
        ) from exc
    except URLError as exc:
        raise _github_error(
            "GitHub Release discovery is temporarily unavailable",
            code="release_github_unavailable",
            remediation="Restore GitHub connectivity and retry the release operation.",
            retryable=True,
        ) from exc
    body_text = raw.decode("utf-8", errors="ignore").lower()
    if status in {401, 403} and "rate limit" not in body_text:
        raise _github_error(
            f"GitHub Release authentication failed with HTTP {status}",
            code="release_github_authentication_failed",
            remediation=(
                "Install a valid owner-only GitHub release token with repository read access."
            ),
            retryable=False,
        )
    if status == 429 or (status == 403 and "rate limit" in body_text):
        raise _github_error(
            "GitHub Release discovery is rate limited",
            code="release_github_rate_limited",
            remediation="Wait for the GitHub API rate limit to reset, then retry.",
            retryable=True,
        )
    if status == 404:
        raise _github_error(
            "GitHub Release repository or endpoint is unavailable",
            code="release_github_not_found",
            remediation=(
                "Verify the release repository and token access to the private repository."
            ),
            retryable=False,
        )
    if 500 <= status <= 599:
        raise _github_error(
            f"GitHub Release service returned HTTP {status}",
            code="release_github_server_error",
            remediation="Retry after GitHub service availability is restored.",
            retryable=True,
        )
    if status != 200:
        raise _github_error(
            f"GitHub Release endpoint rejected the request with HTTP {status}",
            code="release_github_request_rejected",
            remediation="Correct the release repository or credential configuration and retry.",
            retryable=False,
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _github_error(
            "GitHub Release endpoint returned invalid JSON",
            code="release_github_contract_invalid",
            remediation="Correct the published GitHub release metadata before retrying.",
            retryable=False,
        ) from exc


def _http_get_bytes(url: str, *, authorization: str | None = None) -> tuple[int, bytes]:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.lower() != "https":
        raise _github_error(
            "GitHub Release discovery only supports https URLs",
            code="release_github_origin_invalid",
            remediation="Use only an absolute HTTPS GitHub API URL.",
            retryable=False,
        )
    if not parsed.netloc:
        raise _github_error(
            "GitHub Release discovery requires an absolute URL",
            code="release_github_origin_invalid",
            remediation="Use only an absolute HTTPS GitHub API URL.",
            retryable=False,
        )
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    connection = HTTPSConnection(parsed.netloc, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "flow-steward-release-discovery",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if authorization:
            headers["Authorization"] = authorization
        connection.request(
            "GET",
            path,
            headers=headers,
        )
        response = connection.getresponse()
        body = response.read(DEFAULT_MAX_RESPONSE_BYTES + 1)
        if len(body) > DEFAULT_MAX_RESPONSE_BYTES:
            raise _github_error(
                "GitHub Release discovery response exceeds the allowed size",
                code="release_github_contract_invalid",
                remediation="Correct the published GitHub release metadata before retrying.",
                retryable=False,
            )
        return int(response.status or 0), body
    except OSError as exc:
        raise URLError(str(exc)) from exc
    finally:
        connection.close()


__all__ = ["GitHubHTTPError", "GitHubJSONFetcher", "fetch_json_url"]
