"""External release-discovery adapters."""

from core.infrastructure.release.github import GitHubHTTPError, GitHubJSONFetcher, fetch_json_url
from core.infrastructure.release.github_credentials import GitHubReleaseCredentialLoader

__all__ = [
    "GitHubHTTPError",
    "GitHubJSONFetcher",
    "GitHubReleaseCredentialLoader",
    "fetch_json_url",
]
