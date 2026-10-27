"""Published stable GitHub Release discovery contracts."""

from __future__ import annotations

import pytest

from core.application.release.catalog import (
    GitHubReleaseDiscovery,
    ReleaseDiscoveryError,
    ReleaseError,
    ReleaseNotFoundError,
    ReleaseVersion,
)
from core.infrastructure.release.github import GitHubHTTPError, fetch_json_url


def test_release_discovery_rejects_non_https_transport() -> None:
    with pytest.raises(GitHubHTTPError, match="only supports https"):
        fetch_json_url("http://127.0.0.1/releases.json")


def test_release_version_requires_canonical_numeric_triplet() -> None:
    assert str(ReleaseVersion.parse("1.0.10")) == "1.0.10"
    assert ReleaseVersion.parse("1.0.10") > ReleaseVersion.parse("1.0.9")
    for invalid in ("v1.0.0", "1.0", "1.0.0-rc1", "01.0.0", "1.0.0 "):
        with pytest.raises(ReleaseError, match=r"X\.Y\.Z"):
            ReleaseVersion.parse(invalid)


def test_github_discovery_selects_only_published_stable_exact_tags() -> None:
    payload = [
        _github_release("v1.0.9"),
        _github_release("v1.0.10"),
        _github_release("v9.0.0", draft=True),
        _github_release("v8.0.0", prerelease=True),
        _github_release("1.7.0"),
        _github_release("v2.0.0-rc1"),
    ]
    discovery = GitHubReleaseDiscovery(
        repository="example/flow-steward",
        fetch_json=lambda _url: payload,
        resolve_tag_commit=lambda _repository, tag: {"v1.0.10": "a" * 40}[tag],
    )

    release = discovery.latest()

    assert str(release.version) == "1.0.10"
    assert release.tag == "v1.0.10"
    assert release.commit_sha == "a" * 40
    assert release.source == "https://github.com/example/flow-steward/releases/tag/v1.0.10"


def test_github_discovery_uses_configured_repository_and_exact_version() -> None:
    requested_urls: list[str] = []

    def fetch(url: str) -> object:
        requested_urls.append(url)
        return [_github_release("v3.2.1")]

    discovery = GitHubReleaseDiscovery(
        repository="acme/releases",
        fetch_json=fetch,
        resolve_tag_commit=lambda repository, tag: (
            "b" * 40 if (repository, tag) == ("acme/releases", "v3.2.1") else ""
        ),
    )

    release = discovery.for_version("3.2.1")

    assert release.commit_sha == "b" * 40
    assert requested_urls == [
        "https://api.github.com/repos/acme/releases/releases?per_page=100&page=1"
    ]
    with pytest.raises(ReleaseError, match=r"X\.Y\.Z"):
        discovery.for_version("v3.2.1")


def test_github_discovery_rejects_missing_release_and_non_full_commit() -> None:
    missing = GitHubReleaseDiscovery(
        repository="acme/releases",
        fetch_json=lambda _url: [_github_release("v1.0.0")],
        resolve_tag_commit=lambda _repository, _tag: "a" * 40,
    )
    with pytest.raises(ReleaseNotFoundError, match=r"2\.0\.0"):
        missing.for_version("2.0.0")

    short_sha = GitHubReleaseDiscovery(
        repository="acme/releases",
        fetch_json=lambda _url: [_github_release("v1.0.0")],
        resolve_tag_commit=lambda _repository, _tag: "deadbee",
    )
    with pytest.raises(ReleaseError, match="full 40-character"):
        short_sha.latest()


def test_github_discovery_rejects_non_list_and_oversized_pagination() -> None:
    invalid = GitHubReleaseDiscovery(
        repository="acme/releases",
        fetch_json=lambda _url: {"tag_name": "v1.0.0"},
        resolve_tag_commit=lambda _repository, _tag: "a" * 40,
    )
    with pytest.raises(ReleaseDiscoveryError, match="JSON list") as malformed:
        invalid.latest()
    assert malformed.value.code == "release_github_contract_invalid"
    assert malformed.value.retryable is False

    full_page = [_github_release(f"v1.0.{index}") for index in range(100)]
    bounded = GitHubReleaseDiscovery(
        repository="acme/releases",
        fetch_json=lambda _url: full_page,
        resolve_tag_commit=lambda _repository, _tag: "a" * 40,
        max_pages=1,
    )
    with pytest.raises(ReleaseDiscoveryError, match="pagination limit") as exceeded:
        bounded.latest()
    assert exceeded.value.code == "release_github_contract_invalid"
    assert exceeded.value.retryable is False


def _github_release(
    tag: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": "2026-09-25T10:00:00Z",
        "html_url": f"https://github.com/example/flow-steward/releases/tag/{tag}",
    }
