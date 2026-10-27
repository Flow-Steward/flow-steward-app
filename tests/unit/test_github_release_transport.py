from __future__ import annotations

import pytest

from core.infrastructure.release import github


def test_authenticated_fetcher_sends_bearer_only_to_github_api(monkeypatch) -> None:
    observed: list[tuple[str, str | None]] = []

    def get(url: str, *, authorization: str | None = None):
        observed.append((url, authorization))
        return 200, b"[]"

    monkeypatch.setattr(github, "_http_get_bytes", get)
    fetcher = github.GitHubJSONFetcher(lambda: "private-token")

    assert fetcher.fetch("https://api.github.com/repos/example/private/releases") == []
    assert observed == [
        (
            "https://api.github.com/repos/example/private/releases",
            "Bearer private-token",
        )
    ]

    with pytest.raises(github.GitHubHTTPError, match="configured GitHub API origin"):
        fetcher.fetch("https://example.invalid/repos/example/private/releases")
    assert len(observed) == 1


def test_anonymous_fetcher_omits_authorization(monkeypatch) -> None:
    observed: list[str | None] = []

    def get(_url: str, *, authorization: str | None = None):
        observed.append(authorization)
        return 200, b"{}"

    monkeypatch.setattr(github, "_http_get_bytes", get)

    assert github.GitHubJSONFetcher(lambda: None).fetch("https://api.github.com/rate_limit") == {}
    assert observed == [None]


def test_header_construction_failure_is_secret_free(monkeypatch) -> None:
    secret = "private-token-that-must-not-appear"  # pragma: allowlist secret

    def reject_header(_url: str, *, authorization: str | None = None):
        raise ValueError(f"Invalid header value {authorization!r}")

    monkeypatch.setattr(github, "_http_get_bytes", reject_header)

    fetcher = github.GitHubJSONFetcher(lambda: secret)
    with pytest.raises(github.GitHubHTTPError, match="request could not be constructed") as raised:
        fetcher.fetch("https://api.github.com/repos/example/private/releases")

    assert secret not in str(raised.value)


def test_invalid_credential_is_a_nonretryable_typed_discovery_failure() -> None:
    def invalid_credential() -> str:
        raise ValueError("unsafe credential details")

    fetcher = github.GitHubJSONFetcher(invalid_credential)
    with pytest.raises(github.GitHubHTTPError) as raised:
        fetcher.fetch("https://api.github.com/repos/example/private/releases")

    assert raised.value.code == "release_github_credential_invalid"
    assert raised.value.retryable is False
    assert "owner-only" in raised.value.remediation
    assert "unsafe credential details" not in str(raised.value)


def test_real_http_header_boundary_never_echoes_invalid_authorization() -> None:
    secret = "private-token-that-must-not-appear"  # pragma: allowlist secret

    with pytest.raises(github.GitHubHTTPError) as raised:
        github.fetch_json_url(
            "https://127.0.0.1:1/repos/example/private/releases",
            authorization=f"Bearer {secret}\nsecond-line",
        )

    assert raised.value.code == "release_github_request_invalid"
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("status", "body", "code", "retryable"),
    (
        (401, b'{"message":"bad credentials"}', "release_github_authentication_failed", False),
        (403, b'{"message":"API rate limit exceeded"}', "release_github_rate_limited", True),
        (404, b'{"message":"not found"}', "release_github_not_found", False),
        (429, b"", "release_github_rate_limited", True),
        (503, b"", "release_github_server_error", True),
    ),
)
def test_github_statuses_map_to_typed_actionable_errors(
    monkeypatch, status, body, code, retryable
) -> None:
    monkeypatch.setattr(
        github,
        "_http_get_bytes",
        lambda _url, *, authorization=None: (status, body),
    )

    fetcher = github.GitHubJSONFetcher(lambda: "private-token")
    with pytest.raises(github.GitHubHTTPError) as raised:
        fetcher.fetch("https://api.github.com/repos/example/private/releases")

    assert raised.value.code == code
    assert raised.value.retryable is retryable
    assert raised.value.remediation
    assert "private-token" not in str(raised.value)


def test_invalid_json_is_a_nonretryable_release_contract_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        github,
        "_http_get_bytes",
        lambda _url, *, authorization=None: (200, b"not-json"),
    )

    fetcher = github.GitHubJSONFetcher(lambda: None)
    with pytest.raises(github.GitHubHTTPError) as raised:
        fetcher.fetch("https://api.github.com/repos/example/private/releases")

    assert raised.value.code == "release_github_contract_invalid"
    assert raised.value.retryable is False
