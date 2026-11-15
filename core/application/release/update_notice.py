"""Cached, advisory Core release announcements, independent of upgrade readiness."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.application.release.catalog import (
    DEFAULT_RELEASE_REPOSITORY,
    PublishedRelease,
    ReleaseError,
    ReleaseVersion,
)

SUCCESS_CACHE_SECONDS = 60 * 60
FAILURE_CACHE_SECONDS = 5 * 60


class CoreUpdateStatus(BaseModel):
    """Public metadata only; never an installation or upgrade authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["update_available", "current", "unavailable"]
    current_version: str
    latest_version: str | None = None
    release_url: str | None = None


class CoreUpdateQuery:
    """Share one bounded check per process without queuing requests behind I/O."""

    def __init__(
        self,
        *,
        current_version: str,
        latest_release: Callable[[], PublishedRelease],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.current_version = current_version
        self._latest_release = latest_release
        self._clock = clock
        self._cache: tuple[float, CoreUpdateStatus] | None = None
        self._lock = Lock()

    def status(self) -> CoreUpdateStatus:
        cached = self._cache
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        if not self._lock.acquire(blocking=False):
            return self._unavailable()
        try:
            cached = self._cache
            if cached is not None and self._clock() < cached[0]:
                return cached[1]
            result = self._discover()
            ttl = FAILURE_CACHE_SECONDS if result.status == "unavailable" else SUCCESS_CACHE_SECONDS
            self._cache = (self._clock() + ttl, result)
            return result
        finally:
            self._lock.release()

    def _unavailable(self) -> CoreUpdateStatus:
        return CoreUpdateStatus(status="unavailable", current_version=self.current_version)

    def _discover(self) -> CoreUpdateStatus:
        try:
            current = ReleaseVersion.parse(self.current_version)
            published = self._latest_release()
            latest = ReleaseVersion.parse(str(published.version))
            if published.repository != DEFAULT_RELEASE_REPOSITORY or published.tag != latest.tag:
                return self._unavailable()
        except (ReleaseError, OSError, ValueError):
            return self._unavailable()
        return CoreUpdateStatus(
            status="update_available" if latest > current else "current",
            current_version=self.current_version,
            latest_version=str(latest),
            release_url=f"https://github.com/{DEFAULT_RELEASE_REPOSITORY}/releases/tag/{latest.tag}",
        )
