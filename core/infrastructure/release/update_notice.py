"""Public GitHub metadata transport for the optional administrator notice."""

from __future__ import annotations

from functools import lru_cache
from http.client import HTTPException
from time import monotonic

from core.application.release.catalog import (
    GitHubReleaseDiscovery,
    PublishedRelease,
    ReleaseDiscoveryError,
)
from core.application.release.update_notice import CoreUpdateQuery, CoreUpdateStatus
from core.infrastructure.config.version import APP_VERSION
from core.infrastructure.release.github import GitHubJSONFetcher

# The shared transport times out each HTTPS request after ten seconds. Do not
# start further page/tag requests after this budget: total discovery is bounded
# by this budget plus the final in-flight request's transport timeout.
DISCOVERY_START_BUDGET_SECONDS = 10


def latest_core_release() -> PublishedRelease:
    """Use the CLI's stable-release selection policy without private credentials."""
    deadline = monotonic() + DISCOVERY_START_BUDGET_SECONDS
    fetcher = GitHubJSONFetcher(token_loader=lambda: None)

    def fetch(url: str) -> object:
        if monotonic() >= deadline:
            raise ReleaseDiscoveryError("Core release metadata check exceeded its time budget")
        try:
            return fetcher.fetch(url)
        except HTTPException as exc:
            raise ReleaseDiscoveryError("Core release metadata response is unavailable") from exc

    return GitHubReleaseDiscovery(fetch_json=fetch).latest()


@lru_cache(maxsize=1)
def _query() -> CoreUpdateQuery:
    return CoreUpdateQuery(current_version=APP_VERSION, latest_release=latest_core_release)


def core_update_status() -> CoreUpdateStatus:
    """Reuse the process-local query without filesystem or image inspection."""
    return _query().status()
