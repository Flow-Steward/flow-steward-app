"""Every catalog read revalidates its index with the host; nothing is a success cache.

The same contract for the three static catalogs (extensions, workflow
templates, MCP servers), each through its own public loader: two reads in a row
both reach the host, an unchanged index costs a conditional request and no
reparse, a changed index shows on the very next read, a failure serves the last
good index marked stale, a failure cooldown spares a dead host, and concurrent
readers share one fetch.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from core.application.extensions import extension_catalog
from core.application.management import mcp_server_catalog
from core.application.workflows import workflow_template_catalog
from core.infrastructure.static_catalog import settings as catalog_settings
from core.infrastructure.static_catalog.http_fetch import (
    CatalogFetchError,
    CatalogNotModifiedError,
    FetchedCatalogIndex,
)
from core.infrastructure.static_catalog.index_cache import (
    CatalogIndexCache,
    CatalogIndexUnavailableError,
    CatalogValidators,
)
from core.infrastructure.static_catalog.settings import CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS
from tests.fixtures.catalogs.synthetic_catalogs import (
    build_extension_index,
    build_workflow_template_index,
)

_MCP_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/catalogs/mcp_server_catalog_index.json"
)


def _mcp_index() -> dict[str, Any]:
    index = json.loads(_MCP_FIXTURE.read_text(encoding="utf-8"))
    for entry in index["servers"]:
        overlay = entry["server"]["_meta"][mcp_server_catalog.CATALOG_META_KEY]
        if overlay.get("icon") and not overlay["icon"].get("sha256"):
            overlay["icon"]["sha256"] = hashlib.sha256(f"icon:{overlay['id']}".encode()).hexdigest()
    return index


@dataclass(frozen=True)
class _Catalog:
    """One catalog's public surface, as the tests drive it."""

    module: ModuleType
    url_key: str
    parse_name: str
    load: Callable[[], Any]
    availability: Callable[[], dict[str, Any]]
    reset: Callable[[], None]
    error: type[Exception]
    unavailable_code: str
    index: Callable[[], dict[str, Any]]


_CATALOGS = {
    "extensions": _Catalog(
        module=extension_catalog,
        url_key="extension_catalog_url",
        parse_name="parse_extension_catalog_index",
        load=extension_catalog.load_extension_catalog,
        availability=extension_catalog.extension_catalog_availability,
        reset=extension_catalog.reset_extension_catalog_cache,
        error=extension_catalog.ExtensionCatalogError,
        unavailable_code="extension_catalog_unavailable",
        index=lambda: build_extension_index(2),
    ),
    "workflow_templates": _Catalog(
        module=workflow_template_catalog,
        url_key="workflow_template_catalog_url",
        parse_name="parse_workflow_template_catalog_index",
        load=workflow_template_catalog.load_workflow_template_catalog,
        availability=workflow_template_catalog.workflow_template_catalog_availability,
        reset=workflow_template_catalog.reset_workflow_template_catalog_cache,
        error=workflow_template_catalog.WorkflowTemplateCatalogError,
        unavailable_code="workflow_template_catalog_unavailable",
        index=lambda: build_workflow_template_index(2),
    ),
    "mcp_servers": _Catalog(
        module=mcp_server_catalog,
        url_key="mcp_server_catalog_url",
        parse_name="parse_mcp_server_catalog_index",
        load=mcp_server_catalog.load_mcp_server_catalog,
        availability=mcp_server_catalog.mcp_server_catalog_availability,
        reset=mcp_server_catalog.reset_mcp_server_catalog_cache,
        error=mcp_server_catalog.McpServerCatalogError,
        unavailable_code="mcp_catalog_unavailable",
        index=_mcp_index,
    ),
}


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Host:
    """Records every index request and answers with the next scripted reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FetchedCatalogIndex:
        self.calls.append({"url": url, **kwargs})
        if not self.replies:
            raise AssertionError("catalog host was called with no scripted reply")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _Parses:
    """Counts how often a catalog parses an index document."""

    def __init__(self, parse: Callable[[Any], Any]) -> None:
        self.parse = parse
        self.count = 0

    def __call__(self, payload: Any) -> Any:
        self.count += 1
        return self.parse(payload)


@pytest.fixture(params=sorted(_CATALOGS))
def subject(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Catalog]:
    entry = _CATALOGS[request.param]
    monkeypatch.setitem(
        catalog_settings.CONFIG, entry.url_key, f"https://catalog.example.test/{request.param}"
    )
    entry.reset()
    yield entry
    entry.reset()


@pytest.fixture
def clock(subject: _Catalog, monkeypatch: pytest.MonkeyPatch) -> _Clock:
    instance = _Clock()
    monkeypatch.setattr(subject.module, "_now", instance)
    return instance


@pytest.fixture
def parses(subject: _Catalog, monkeypatch: pytest.MonkeyPatch) -> _Parses:
    counter = _Parses(getattr(subject.module, subject.parse_name))
    monkeypatch.setattr(subject.module, subject.parse_name, counter)
    return counter


def _host(monkeypatch: pytest.MonkeyPatch, subject: _Catalog, *replies: Any) -> _Host:
    host = _Host(*replies)
    monkeypatch.setattr(subject.module, "fetch_catalog_index_json", host)
    return host


def _reply(payload: dict[str, Any], *, etag: str = "", last_modified: str = "") -> Any:
    return FetchedCatalogIndex(payload=payload, etag=etag, last_modified=last_modified)


def _revised(payload: dict[str, Any], revision: str) -> dict[str, Any]:
    changed = copy.deepcopy(payload)
    changed["catalog_revision"] = revision
    return changed


def test_two_reads_in_a_row_both_ask_the_host_and_a_304_reuses_the_parsed_index(
    subject: _Catalog,
    clock: _Clock,
    parses: _Parses,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host(
        monkeypatch,
        subject,
        _reply(subject.index(), etag='"rev-1"', last_modified="Fri, 16 Oct 2026 10:00:00 GMT"),
        CatalogNotModifiedError("unchanged"),
    )

    first = subject.load()
    second = subject.load()

    assert len(host.calls) == 2
    assert host.calls[0]["etag"] == ""
    assert host.calls[0]["last_modified"] == ""
    assert host.calls[1]["etag"] == '"rev-1"'
    assert host.calls[1]["last_modified"] == "Fri, 16 Oct 2026 10:00:00 GMT"
    assert second is first
    assert parses.count == 1


def test_a_changed_index_shows_on_the_very_next_read(
    subject: _Catalog, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = subject.index()
    _host(
        monkeypatch,
        subject,
        _reply(_revised(payload, "r1"), etag='"rev-1"'),
        _reply(_revised(payload, "r2"), etag='"rev-2"'),
    )

    assert subject.load().catalog_revision == "r1"
    clock.advance(1)
    assert subject.load().catalog_revision == "r2"


def test_a_failure_after_a_good_read_serves_the_last_index_marked_stale(
    subject: _Catalog, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _revised(subject.index(), "r1")
    _host(monkeypatch, subject, _reply(payload), CatalogFetchError("HTTP 503 from the host"))

    first = subject.load()
    clock.advance(1)

    assert subject.load() is first
    availability = subject.availability()
    assert availability["available"] is True
    assert availability["catalog_revision"] == "r1"
    assert availability["stale"] is True
    assert "HTTP 503 from the host" in availability["last_refresh_error"]
    assert "ttl_seconds" not in availability


def test_a_good_read_after_a_failure_clears_the_stale_mark(
    subject: _Catalog, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = subject.index()
    _host(
        monkeypatch,
        subject,
        _reply(_revised(payload, "r1")),
        CatalogFetchError("down"),
        _reply(_revised(payload, "r2")),
    )

    subject.load()
    clock.advance(1)
    subject.load()
    clock.advance(CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS)

    availability = subject.availability()
    assert availability["catalog_revision"] == "r2"
    assert availability["stale"] is False
    assert availability["last_refresh_error"] == ""
    assert availability["fetched_at"]


def test_the_failure_cooldown_spares_a_dead_host_then_asks_again(
    subject: _Catalog, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = subject.index()
    host = _host(
        monkeypatch,
        subject,
        _reply(_revised(payload, "r1")),
        CatalogFetchError("down"),
        _reply(_revised(payload, "r2")),
    )

    subject.load()
    clock.advance(1)
    subject.load()  # fails, starts the cooldown
    clock.advance(CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS - 2)

    assert subject.load().catalog_revision == "r1"
    assert len(host.calls) == 2

    clock.advance(2)
    assert subject.load().catalog_revision == "r2"
    assert len(host.calls) == 3


def test_a_failed_first_read_reports_the_catalog_unavailable_until_the_cooldown_ends(
    subject: _Catalog, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(monkeypatch, subject, CatalogFetchError("down"))

    with pytest.raises(subject.error) as raised:
        subject.load()
    with pytest.raises(subject.error):
        subject.load()

    assert raised.value.code == subject.unavailable_code  # type: ignore[attr-defined]
    assert len(host.calls) == 1


def test_an_invalid_new_index_keeps_the_previous_one_serving(
    subject: _Catalog, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _revised(subject.index(), "r1")
    broken = _revised(payload, "r2")
    broken["schema_version"] = "not_a_catalog/v9"
    _host(monkeypatch, subject, _reply(payload), _reply(broken))

    first = subject.load()
    clock.advance(1)

    assert subject.load() is first
    assert subject.availability()["stale"] is True


def test_concurrent_reads_share_one_fetch(
    subject: _Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    payload = subject.index()

    def _slow_host(url: str, **_kwargs: Any) -> FetchedCatalogIndex:
        calls.append(url)
        started.set()
        assert release.wait(5), "the test never released the host"
        return _reply(payload)

    monkeypatch.setattr(subject.module, "fetch_catalog_index_json", _slow_host)
    results: list[Any] = []
    errors: list[BaseException] = []

    def _read() -> None:
        try:
            results.append(subject.load())
        except BaseException as exc:  # surfaced by the assertion below
            errors.append(exc)

    first = threading.Thread(target=_read)
    first.start()
    assert started.wait(5)
    others = [threading.Thread(target=_read) for _ in range(7)]
    for thread in others:
        thread.start()
    _wait_for_waiters(subject.module._INDEX, count=len(others))
    release.set()
    for thread in [first, *others]:
        thread.join(5)

    assert errors == []
    assert len(calls) == 1
    assert len(results) == 8
    assert all(result is results[0] for result in results)


def _wait_for_waiters(cache: CatalogIndexCache[Any], *, count: int) -> None:
    """Block until ``count`` readers are parked on the in-flight read."""
    deadline = threading.Event()
    for _ in range(500):
        if len(cache._cond._waiters) >= count:  # type: ignore[attr-defined]
            return
        deadline.wait(0.01)
    raise AssertionError("the concurrent readers never waited on the in-flight read")


# ---------------------------------------------------------------------------
# The shared cache on its own
# ---------------------------------------------------------------------------


def test_a_reset_while_a_read_is_in_flight_discards_that_read() -> None:
    """A URL change must not let the old host's index land in the new slot."""
    release = threading.Event()
    started = threading.Event()
    replies = iter(["old", "new"])

    def _fetch(url: str, validators: CatalogValidators) -> FetchedCatalogIndex:
        del validators
        label = next(replies)
        if label == "old":
            started.set()
            assert release.wait(5)
        return FetchedCatalogIndex(payload={"from": url, "label": label}, etag="")

    cache: CatalogIndexCache[dict[str, Any]] = CatalogIndexCache(
        fetch=_fetch, parse=lambda payload: payload
    )
    outcome: list[Any] = []
    reader = threading.Thread(
        target=lambda: outcome.append(_load_or_error(cache, "https://a.example.test/index"))
    )
    reader.start()
    assert started.wait(5)
    cache.reset()
    release.set()
    reader.join(5)

    assert isinstance(outcome[0], CatalogIndexUnavailableError)
    assert cache.load("https://b.example.test/index")["label"] == "new"


def _load_or_error(cache: CatalogIndexCache[Any], url: str) -> Any:
    try:
        return cache.load(url)
    except CatalogIndexUnavailableError as exc:
        return exc


def test_a_discarded_reader_never_returns_the_replacement_publishers_index(monkeypatch) -> None:
    """Let B finish after A is discarded but before A returns to its caller."""
    old_started = threading.Event()
    release_old = threading.Event()
    replacement_finished = threading.Event()
    old_url = "https://old.example.test/index"
    new_url = "https://new.example.test/index"

    def fetch(url: str, _validators: CatalogValidators) -> FetchedCatalogIndex:
        if url == old_url:
            old_started.set()
            assert release_old.wait(5)
        return FetchedCatalogIndex(payload={"publisher": url}, etag="")

    cache: CatalogIndexCache[dict[str, Any]] = CatalogIndexCache(
        fetch=fetch, parse=lambda value: value
    )
    finish = cache._finish

    def finish_before_return(epoch: int, **kwargs: Any) -> None:
        finish(epoch, **kwargs)
        if threading.current_thread().name == "discarded-catalog-reader":
            # This is only a scheduling barrier: the real cache still performs
            # every state transition, publication and final return itself.
            assert replacement_finished.wait(5)

    monkeypatch.setattr(cache, "_finish", finish_before_return)
    old_result: list[Any] = []
    new_result: list[Any] = []
    old_reader = threading.Thread(
        name="discarded-catalog-reader",
        target=lambda: old_result.append(_load_or_error(cache, old_url)),
    )

    def read_replacement() -> None:
        try:
            new_result.append(_load_or_error(cache, new_url))
        finally:
            replacement_finished.set()

    new_reader = threading.Thread(target=read_replacement)
    try:
        old_reader.start()
        assert old_started.wait(5)
        new_reader.start()
        _wait_for_waiters(cache, count=1)
        release_old.set()
        new_reader.join(5)
        old_reader.join(5)
    finally:
        release_old.set()
        replacement_finished.set()
        if old_reader.ident is not None:
            old_reader.join(5)
        if new_reader.ident is not None:
            new_reader.join(5)

    assert not old_reader.is_alive()
    assert not new_reader.is_alive()
    assert new_result == [{"publisher": new_url}]
    assert len(old_result) == 1
    assert isinstance(old_result[0], CatalogIndexUnavailableError)
