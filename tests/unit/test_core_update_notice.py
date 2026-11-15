from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from core.application.release.catalog import PublishedRelease, ReleaseDiscoveryError, ReleaseVersion
from core.application.release.update_notice import CoreUpdateQuery


def published(version: str) -> PublishedRelease:
    parsed = ReleaseVersion.parse(version)
    return PublishedRelease(
        version=parsed,
        tag=parsed.tag,
        commit_sha="a" * 40,
        published_at="2026-11-14T08:00:00Z",
        source=f"https://github.com/Flow-Steward/flow-steward-app/releases/tag/{parsed.tag}",
        repository="Flow-Steward/flow-steward-app",
    )


@pytest.mark.parametrize(
    ("current", "latest", "status"),
    [
        ("0.9.17", "0.9.18", "update_available"),
        ("0.9.9", "0.10.0", "update_available"),
        ("0.9.18", "0.9.18", "current"),
        ("0.10.0", "0.9.18", "current"),
        ("development", "0.9.18", "unavailable"),
    ],
)
def test_compares_the_running_core_with_published_stable_versions(current, latest, status):
    result = CoreUpdateQuery(
        current_version=current, latest_release=lambda: published(latest)
    ).status()

    assert result.status == status
    assert result.current_version == current
    if status == "update_available":
        assert result.latest_version == latest
        assert (
            result.release_url
            == f"https://github.com/Flow-Steward/flow-steward-app/releases/tag/v{latest}"
        )


def test_success_cache_expires_and_discovers_a_subsequent_release():
    now = [0.0]
    releases = iter([published("0.9.18"), published("0.9.19")])
    query = CoreUpdateQuery(
        current_version="0.9.17", latest_release=lambda: next(releases), clock=lambda: now[0]
    )

    assert query.status().latest_version == "0.9.18"
    now[0] = 3599
    assert query.status().latest_version == "0.9.18"
    now[0] = 3601
    assert query.status().latest_version == "0.9.19"


def test_failure_is_cached_without_claiming_current_or_exposing_remote_errors():
    now = [0.0]
    failures = [True]

    def latest():
        if failures.pop():
            raise OSError("private upstream error token=must-not-escape")
        return published("0.9.18")

    query = CoreUpdateQuery(current_version="0.9.17", latest_release=latest, clock=lambda: now[0])
    result = query.status()
    assert result.status == "unavailable"
    assert "must-not-escape" not in result.model_dump_json()
    failures.append(False)
    now[0] = 299
    assert query.status().status == "unavailable"
    now[0] = 301
    assert query.status().status == "update_available"


def test_concurrent_cold_checks_do_not_queue_behind_remote_io():
    entered, release = Event(), Event()

    def latest():
        entered.set()
        release.wait(2)
        return published("0.9.18")

    query = CoreUpdateQuery(current_version="0.9.17", latest_release=latest)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(query.status)
        try:
            assert entered.wait(1)
            assert executor.submit(query.status).result(timeout=1).status == "unavailable"
        finally:
            release.set()
        assert first.result(timeout=1).status == "update_available"
    assert query.status().latest_version == "0.9.18"


def test_release_notes_url_is_derived_from_the_official_version_not_remote_url():
    release = published("0.9.18")
    release = replace(release, source="javascript:alert(1)")
    result = CoreUpdateQuery(current_version="0.9.17", latest_release=lambda: release).status()
    assert (
        result.release_url
        == "https://github.com/Flow-Steward/flow-steward-app/releases/tag/v0.9.18"
    )


@pytest.mark.parametrize("changes", [{"repository": "another/core"}, {"tag": "v0.9.19"}])
def test_mismatched_release_identity_does_not_announce_an_update(changes):
    release = replace(published("0.9.18"), **changes)
    result = CoreUpdateQuery(current_version="0.9.17", latest_release=lambda: release).status()
    assert result.status == "unavailable"
    assert result.release_url is None


def test_public_transport_reuses_stable_discovery_without_private_credentials(monkeypatch):
    from core.entrypoints.api import system_core_updates
    from core.infrastructure.release import github

    def fetch(url, *, authorization=None):
        assert authorization is None
        if "/releases?" in url:
            return [
                {"tag_name": "v0.11.0", "draft": True, "prerelease": False},
                {"tag_name": "v0.10.0", "draft": False, "prerelease": True},
                {
                    "tag_name": "v0.9.18",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-11-14T08:00:00Z",
                },
            ]
        assert url.endswith("/git/ref/tags/v0.9.18")
        return {"object": {"type": "commit", "sha": "a" * 40}}

    monkeypatch.setattr(github, "fetch_json_url", fetch)
    assert str(system_core_updates.latest_core_release().version) == "0.9.18"


def test_transport_stops_starting_remote_requests_after_its_budget(monkeypatch):
    from core.entrypoints.api import system_core_updates
    from core.infrastructure.release import github

    now, urls = [0.0], []

    def fetch(url, *, authorization=None):
        urls.append(url)
        now[0] = 11
        return [
            {
                "tag_name": "v0.9.18",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-11-14T08:00:00Z",
            }
        ]

    monkeypatch.setattr(github, "fetch_json_url", fetch)
    monkeypatch.setattr(github, "monotonic", lambda: now[0])
    with pytest.raises(ReleaseDiscoveryError, match="time budget"):
        system_core_updates.latest_core_release()
    assert len(urls) == 1


def test_truncated_http_response_uses_the_failure_cache(monkeypatch):
    from http.client import IncompleteRead

    from core.entrypoints.api import system_core_updates
    from core.infrastructure.release import github

    calls = []

    def fetch(url, *, authorization=None):
        calls.append(url)
        raise IncompleteRead(b"private partial response")

    monkeypatch.setattr(github, "fetch_json_url", fetch)
    query = CoreUpdateQuery(
        current_version="0.9.17", latest_release=system_core_updates.latest_core_release
    )
    assert query.status().status == "unavailable"
    assert "private" not in query.status().model_dump_json()
    assert len(calls) == 1
