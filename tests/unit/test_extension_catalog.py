"""Extension catalog: index contract, its own cache, SemVer state, install path."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest

from core.application.extensions import extension_catalog as catalog
from core.application.extensions.extension_catalog import (
    CAPABILITY_LABELS,
    PERMISSION_LABELS,
    ExtensionCatalogError,
    extension_catalog_availability,
    load_extension_catalog,
    parse_extension_catalog_index,
    reset_extension_catalog_cache,
)
from core.application.extensions.extension_catalog_install import (
    STATE_INCOMPATIBLE,
    STATE_INSTALL,
    STATE_INSTALLED,
    STATE_INSTALLED_DISABLED,
    STATE_INSTALLED_NEWER,
    STATE_UPDATE_AVAILABLE,
    ExtensionCatalogInstallService,
    catalog_display_state,
)
from core.application.management.scope import normalize_scope
from core.infrastructure.static_catalog import settings as catalog_settings
from core.infrastructure.static_catalog.http_fetch import (
    CatalogArtifactMismatchError,
    CatalogFetchError,
    CatalogNotModifiedError,
    FetchedCatalogIndex,
)
from tests.fixtures.catalogs.synthetic_catalogs import (
    EXTENSION_CATALOG_SCALE_ITEM_COUNT,
    build_extension_index,
)

CATALOG_URL = "https://catalog.example.test/extensions/index.json"


def _digest_for(extension_id: str, version: str) -> str:
    """The digest the synthetic builder publishes for this release."""
    return hashlib.sha256(f"extension:{extension_id}:{version}".encode()).hexdigest()


_DIGEST_2_0_0 = _digest_for("synthetic_extension_000", "2.0.0")


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(catalog_settings.CONFIG, "extension_catalog_url", CATALOG_URL)
    reset_extension_catalog_cache()
    yield
    reset_extension_catalog_cache()


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    instance = _Clock()
    monkeypatch.setattr(catalog, "_now", instance)
    return instance


class _Host:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FetchedCatalogIndex:
        self.calls.append({"url": url, **kwargs})
        if not self.replies:
            raise AssertionError("catalog host was called with no scripted reply")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _index(count: int = 2, *, revision: str = "r1") -> dict[str, Any]:
    return build_extension_index(count, catalog_revision=revision)


def _reply(payload: dict[str, Any], etag: str = "") -> FetchedCatalogIndex:
    return FetchedCatalogIndex(payload=payload, etag=etag)


def _install_host(monkeypatch: pytest.MonkeyPatch, *replies: Any) -> _Host:
    host = _Host(*replies)
    monkeypatch.setattr(catalog, "fetch_catalog_index_json", host)
    return host


# ---------------------------------------------------------------------------
# Index contract
# ---------------------------------------------------------------------------


def test_a_documented_index_document_validates() -> None:
    """The contract as `docs/catalogs/static-catalog-contracts.md` states it."""
    index = parse_extension_catalog_index(json.loads(json.dumps(_index(3))))

    assert index.schema_version == "extension_catalog/v1"
    assert len(index.items) == 3
    assert index.items[0].release_zip_url.startswith("https://")
    assert len(index.items[0].release_zip_sha256) == 64


def test_the_extension_index_uses_its_own_schema_version() -> None:
    """The two catalogs are versioned apart; neither accepts the other's index."""
    workflow_shaped = _index(1)
    workflow_shaped["schema_version"] = "workflow_template_catalog/v1"
    with pytest.raises(ValueError, match="extension_catalog/v1"):
        parse_extension_catalog_index(workflow_shaped)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda p: p.update(catalog_revision=""), "catalog_revision"),
        (lambda p: p["items"][0].update(extension_id=""), "extension_id"),
        (lambda p: p["items"][0].update(version="v2"), "semantic version"),
        (lambda p: p["items"][0].update(release_zip_url="http://x.test/a.zip"), "https"),
        (lambda p: p["items"][0].update(release_zip_sha256="nope"), "SHA-256"),
        (lambda p: p["items"][0].update(release_zip_bytes=-1), "greater than zero"),
        (lambda p: p["items"][0].update(repository_url="git@github.test:a/b"), "https"),
        (lambda p: p["items"][0].update(publication_status="beta"), "publication_status"),
    ],
)
def test_one_bad_field_rejects_the_whole_index(mutate: Any, match: str) -> None:
    payload = _index(2)
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        parse_extension_catalog_index(payload)


def test_a_duplicate_extension_id_rejects_the_index() -> None:
    payload = _index(2)
    payload["items"][1]["extension_id"] = payload["items"][0]["extension_id"]
    with pytest.raises(ValueError, match="duplicate extension_id"):
        parse_extension_catalog_index(payload)


def test_the_synthetic_extension_index_validates_at_scale() -> None:
    index = parse_extension_catalog_index(_index(EXTENSION_CATALOG_SCALE_ITEM_COUNT))
    assert len(index.items) == EXTENSION_CATALOG_SCALE_ITEM_COUNT


# ---------------------------------------------------------------------------
# Cache, kept separate from the workflow catalog's
# ---------------------------------------------------------------------------


def test_first_load_fetches_the_index_and_downloads_no_zip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_calls: list[str] = []
    monkeypatch.setattr(
        "core.application.extensions.extension_catalog.fetch_catalog_artifact",
        lambda url, **_kwargs: artifact_calls.append(url) or b"",
    )
    host = _install_host(monkeypatch, _reply(_index(3)))

    index = load_extension_catalog()

    assert len(index.items) == 3
    assert [call["url"] for call in host.calls] == [CATALOG_URL]
    assert artifact_calls == []


def test_every_request_asks_the_host_again(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """No freshness window: an approved extension must not wait out a cache."""
    host = _install_host(monkeypatch, _reply(_index(1)))

    load_extension_catalog()
    clock.advance(1)
    load_extension_catalog()

    assert len(host.calls) == 2


def test_a_stored_etag_is_sent_on_the_refresh(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    host = _install_host(
        monkeypatch, _reply(_index(1), etag='"ext-1"'), CatalogNotModifiedError("unchanged")
    )

    first = load_extension_catalog()
    clock.advance(1)

    assert load_extension_catalog() is first
    assert host.calls[1]["etag"] == '"ext-1"'


def test_a_failed_refresh_serves_the_stale_index(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _install_host(monkeypatch, _reply(_index(1, revision="r1")), CatalogFetchError("down"))

    load_extension_catalog()
    clock.advance(1)

    assert load_extension_catalog().catalog_revision == "r1"
    assert extension_catalog_availability()["stale"] is True


def test_a_failed_first_fetch_reports_the_catalog_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_host(monkeypatch, CatalogFetchError("down"))

    with pytest.raises(ExtensionCatalogError) as raised:
        load_extension_catalog()

    assert raised.value.code == "extension_catalog_unavailable"


def test_an_invalid_refresh_keeps_the_previous_index_active(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    broken = _index(2, revision="r2")
    broken["items"][1]["release_zip_sha256"] = "nope"
    _install_host(monkeypatch, _reply(_index(2, revision="r1")), _reply(broken))

    load_extension_catalog()
    clock.advance(1)

    assert load_extension_catalog().catalog_revision == "r1"


def test_an_unconfigured_catalog_is_reported_rather_than_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(catalog_settings.CONFIG, "extension_catalog_url", "")
    host = _install_host(monkeypatch)

    availability = extension_catalog_availability()

    assert availability == {
        "kind": "extension_catalog_availability",
        "configured": False,
        "available": False,
        "reason": "extension_catalog_not_configured",
        "item_count": 0,
        "stale": False,
    }
    assert host.calls == []


# ---------------------------------------------------------------------------
# SemVer display state
# ---------------------------------------------------------------------------


class _Install:
    def __init__(
        self,
        extension_id: str,
        version: str,
        install_id: str,
        state: str = "active",
        release_id: str = "",
    ):
        self.extension_id = extension_id
        self.version = version
        self.install_id = install_id
        self.state = state
        self.release_id = release_id


def _item(version: str = "1.0.0", *, minimum: str = "", maximum: str = "") -> Any:
    payload = build_extension_index(1)
    payload["items"][0]["version"] = version
    payload["items"][0]["compatible_flow_steward"] = {"min": minimum, "max": maximum}
    return parse_extension_catalog_index(payload).items[0]


@pytest.mark.parametrize(
    ("catalog_version", "install", "expected"),
    [
        ("1.0.0", None, STATE_INSTALL),
        ("1.0.0", _Install("e", "1.0.0", "i"), STATE_INSTALLED),
        # Lexically '1.9.0' > '1.10.0'; by SemVer it is older.
        ("1.10.0", _Install("e", "1.9.0", "i"), STATE_UPDATE_AVAILABLE),
        ("1.9.0", _Install("e", "1.10.0", "i"), STATE_INSTALLED_NEWER),
        ("1.0.0", _Install("e", "1.0.0", "i", state="disabled"), STATE_INSTALLED_DISABLED),
        # A turned-off install can be updated; it stays off afterwards.
        ("2.0.0", _Install("e", "1.0.0", "i", state="disabled"), STATE_UPDATE_AVAILABLE),
        ("1.0.0", _Install("e", "2.0.0", "i", state="disabled"), STATE_INSTALLED_DISABLED),
    ],
)
def test_display_state_is_computed_from_semver(
    catalog_version: str, install: Any, expected: str
) -> None:
    assert (
        catalog_display_state(_item(catalog_version), install, platform_version="1.0.0") == expected
    )


def test_an_unreadable_installed_version_does_not_claim_an_update() -> None:
    state = catalog_display_state(
        _item("2.0.0"), _Install("e", "nightly", "i"), platform_version="1.0.0"
    )
    assert state == STATE_INSTALLED


@pytest.mark.parametrize(
    ("minimum", "maximum", "platform", "expected"),
    [
        ("2.0.0", "", "1.0.0", STATE_INCOMPATIBLE),
        ("", "0.9.0", "1.0.0", STATE_INCOMPATIBLE),
        ("1.0.0", "1.0.0", "1.0.0", STATE_INSTALL),
        ("", "", "1.0.0", STATE_INSTALL),
    ],
)
def test_an_incompatible_release_cannot_be_offered_for_install(
    minimum: str, maximum: str, platform: str, expected: str
) -> None:
    item = _item("1.0.0", minimum=minimum, maximum=maximum)
    assert catalog_display_state(item, None, platform_version=platform) == expected


def test_an_incompatible_release_does_not_hide_what_is_installed_locally() -> None:
    item = _item("2.0.0", minimum="9.0.0")
    state = catalog_display_state(item, _Install("e", "3.0.0", "i"), platform_version="1.0.0")
    assert state == STATE_INSTALLED_NEWER


# ---------------------------------------------------------------------------
# Listing and install delegation
# ---------------------------------------------------------------------------


class _InstallStore:
    def __init__(self, *records: _Install) -> None:
        self.records = list(records)
        self.account_id = ""

    def list_for_account(self, account_id: str, **_kwargs: Any) -> list[_Install]:
        self.account_id = account_id
        return list(self.records)


class _Result:
    def dump(self) -> dict[str, Any]:
        return {"status": "installed"}


class _BindingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def install_extension_archive(self, **kwargs: Any) -> _Result:
        self.calls.append(kwargs)
        return _Result()


def _archive(extension_id: str = "synthetic_extension_000", version: str = "2.0.0") -> bytes:
    """A minimal archive whose manifest declares the release it claims to be."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(
            f"{extension_id}/extension.yaml",
            f"manifest_version: 2\nextension_id: {extension_id}\nversion: {version}\n",
        )
    return buffer.getvalue()


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _InstallStore,
    binding: _BindingService,
    artifact: Any = None,
    platform_version: str = "1.0.0",
    runtime_store: Any = None,
) -> tuple[ExtensionCatalogInstallService, list[dict[str, Any]]]:
    _install_host(monkeypatch, _reply(_index(3)))
    fetched: list[dict[str, Any]] = []

    payload = _archive() if artifact is None else artifact

    def _fetch(url: str, **kwargs: Any) -> bytes:
        fetched.append({"url": url, **kwargs})
        if isinstance(payload, Exception):
            raise payload
        return payload

    service = ExtensionCatalogInstallService(
        get_install_store=lambda: store,
        get_binding_service=lambda: binding,
        fetch_artifact=_fetch,
        platform_version=lambda: platform_version,
        get_runtime_store=(lambda: runtime_store) if runtime_store is not None else None,
    )
    return service, fetched


def test_the_listing_joins_the_catalog_with_this_accounts_installs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InstallStore(_Install("synthetic_extension_001", "2.0.0", "inst_a"))
    service, fetched = _service(monkeypatch, store=store, binding=_BindingService())

    listing = service.list_catalog(scope=normalize_scope(account_id="acc_7"))

    assert store.account_id == "acc_7"
    assert fetched == []
    states = {row["extension_id"]: row["display_state"] for row in listing["items"]}
    assert states["synthetic_extension_000"] == STATE_INSTALL
    assert states["synthetic_extension_001"] == STATE_UPDATE_AVAILABLE
    installed = next(
        row for row in listing["items"] if row["extension_id"] == "synthetic_extension_001"
    )
    assert installed["installed_version"] == "2.0.0"
    assert installed["installed_install_id"] == "inst_a"


def test_the_listing_reports_an_unavailable_catalog_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_host(monkeypatch, CatalogFetchError("down"))
    service = ExtensionCatalogInstallService(
        get_install_store=lambda: _InstallStore(),
        get_binding_service=lambda: _BindingService(),
    )

    listing = service.list_catalog(scope=normalize_scope(account_id="acc_7"))

    assert listing["items"] == []
    assert listing["availability"]["available"] is False


def test_an_active_install_wins_over_a_stale_disabled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InstallStore(
        _Install("synthetic_extension_000", "1.0.0", "inst_old", state="disabled"),
        _Install("synthetic_extension_000", "2.0.0", "inst_new"),
    )
    service, _fetched = _service(monkeypatch, store=store, binding=_BindingService())

    listing = service.list_catalog()
    row = next(
        item for item in listing["items"] if item["extension_id"] == "synthetic_extension_000"
    )

    assert row["installed_install_id"] == "inst_new"


def test_a_verified_archive_reaches_the_existing_installer_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _BindingService()
    service, fetched = _service(monkeypatch, store=_InstallStore(), binding=binding)

    payload = service.install_from_catalog(
        "synthetic_extension_000",
        version="2.0.0",
        release_sha256=_digest_for("synthetic_extension_000", "2.0.0"),
        scope=normalize_scope(account_id="acc_7"),
    )

    assert payload.dump() == {"status": "installed"}
    assert len(fetched) == 1
    assert (
        fetched[0]["expected_sha256"]
        == hashlib.sha256(b"extension:synthetic_extension_000:2.0.0").hexdigest()
    )
    assert fetched[0]["expected_bytes"] == 65536
    assert len(binding.calls) == 1
    assert binding.calls[0]["filename"] == "synthetic_extension_000-2.0.0.zip"
    assert binding.calls[0]["content"] == _archive()
    # Verification is catalog information only; it grants the install nothing.
    assert "install_origin" not in binding.calls[0]


def test_the_browser_authority_approval_hash_reaches_the_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _BindingService()
    service, _fetched = _service(monkeypatch, store=_InstallStore(), binding=binding)

    service.install_from_catalog(
        "synthetic_extension_000",
        version="2.0.0",
        release_sha256=_digest_for("synthetic_extension_000", "2.0.0"),
        approved_browser_authority_impact_hash="a" * 64,
    )

    assert binding.calls[0]["approved_browser_authority_impact_hash"] == "a" * 64


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        # The version moved on.
        ("1.0.0", _DIGEST_2_0_0),
        # The version is the same but the release was republished: a version
        # check alone would wave this through and install different bytes.
        ("2.0.0", "f" * 64),
    ],
)
def test_installing_a_release_the_catalog_no_longer_offers_is_refused(
    monkeypatch: pytest.MonkeyPatch, version: str, digest: str
) -> None:
    """The listing and the install can be served by two independent caches."""
    binding = _BindingService()
    service, fetched = _service(monkeypatch, store=_InstallStore(), binding=binding)

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            "synthetic_extension_000", version=version, release_sha256=digest
        )

    assert raised.value.code == "extension_catalog_release_changed"
    assert raised.value.status_code == 409
    assert fetched == []
    assert binding.calls == []


@pytest.mark.parametrize(("version", "digest"), [("", _DIGEST_2_0_0), ("2.0.0", "")])
def test_an_install_that_pins_nothing_is_refused(
    monkeypatch: pytest.MonkeyPatch, version: str, digest: str
) -> None:
    binding = _BindingService()
    service, fetched = _service(monkeypatch, store=_InstallStore(), binding=binding)

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            "synthetic_extension_000", version=version, release_sha256=digest
        )

    assert raised.value.code == "extension_catalog_release_pin_required"
    assert fetched == []
    assert binding.calls == []


@pytest.mark.parametrize("state", ["active", "disabled"])
def test_a_newer_catalog_release_updates_through_the_same_archive_installer(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """An update is the uploaded-ZIP path: the same pinned, verified bytes, once."""
    binding = _BindingService()
    store = _InstallStore(_Install("synthetic_extension_000", "1.0.0", "inst_a", state=state))
    service, fetched = _service(monkeypatch, store=store, binding=binding)

    service.install_from_catalog(
        "synthetic_extension_000",
        version="2.0.0",
        release_sha256=_DIGEST_2_0_0,
        approved_browser_authority_impact_hash="b" * 64,
    )

    assert len(fetched) == 1
    assert fetched[0]["expected_sha256"] == _DIGEST_2_0_0
    assert len(binding.calls) == 1
    assert binding.calls[0]["filename"] == "synthetic_extension_000-2.0.0.zip"
    assert binding.calls[0]["content"] == _archive()
    assert binding.calls[0]["approved_browser_authority_impact_hash"] == "b" * 64


@pytest.mark.parametrize(
    ("installed", "code"),
    [
        ("2.0.0", "extension_catalog_already_installed"),
        ("3.0.0", "extension_catalog_downgrade_refused"),
        ("nightly", "extension_catalog_installed_version_unreadable"),
    ],
)
def test_a_reinstall_or_downgrade_from_the_catalog_is_refused_before_download(
    monkeypatch: pytest.MonkeyPatch, installed: str, code: str
) -> None:
    binding = _BindingService()
    store = _InstallStore(_Install("synthetic_extension_000", installed, "inst_a"))
    service, fetched = _service(monkeypatch, store=store, binding=binding)

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            "synthetic_extension_000", version="2.0.0", release_sha256=_DIGEST_2_0_0
        )

    assert raised.value.code == code
    assert raised.value.status_code == 409
    assert installed in str(raised.value) or code == "extension_catalog_already_installed"
    assert fetched == []
    assert binding.calls == []


class _RuntimeStore:
    def __init__(self, **scopes_by_release: list[str]) -> None:
        self.scopes_by_release = scopes_by_release

    def get_release(self, release_id: str) -> Any:
        scopes = self.scopes_by_release.get(release_id)
        if scopes is None:
            return None
        return type("Release", (), {"descriptor": {"required_scopes": scopes}})()


def test_an_update_names_the_permissions_the_newer_release_adds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InstallStore(
        _Install("synthetic_extension_001", "1.0.0", "inst_a", release_id="rel_old"),
    )
    runtime = _RuntimeStore(rel_old=["extension:invoke"])
    service, _fetched = _service(
        monkeypatch, store=store, binding=_BindingService(), runtime_store=runtime
    )

    listing = service.list_catalog()
    row = next(
        item for item in listing["items"] if item["extension_id"] == "synthetic_extension_001"
    )

    assert row["display_state"] == STATE_UPDATE_AVAILABLE
    assert row["added_permissions"] == sorted(
        set(row["permissions_summary"]) - {"extension:invoke"}
    )
    assert row["added_permissions"], "the synthetic index declares more than invoke"
    others = [
        item for item in listing["items"] if item["extension_id"] != "synthetic_extension_001"
    ]
    assert all(item["added_permissions"] == [] for item in others)


def test_an_update_whose_installed_permissions_are_unknown_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InstallStore(
        _Install("synthetic_extension_001", "1.0.0", "inst_a", release_id="rel_gone"),
    )
    service, _fetched = _service(
        monkeypatch, store=store, binding=_BindingService(), runtime_store=_RuntimeStore()
    )

    row = next(
        item
        for item in service.list_catalog()["items"]
        if item["extension_id"] == "synthetic_extension_001"
    )

    assert row["display_state"] == STATE_UPDATE_AVAILABLE
    assert row["added_permissions"] == []


@pytest.mark.parametrize(
    "failure",
    [
        CatalogArtifactMismatchError("size does not match"),
        CatalogArtifactMismatchError("SHA-256 does not match"),
    ],
)
def test_a_mismatched_archive_never_reaches_the_installer(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    binding = _BindingService()
    service, _fetched = _service(
        monkeypatch, store=_InstallStore(), binding=binding, artifact=failure
    )

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            "synthetic_extension_000", version="2.0.0", release_sha256=_DIGEST_2_0_0
        )

    assert raised.value.status_code == 422
    assert binding.calls == []


@pytest.mark.parametrize(
    ("archive", "code"),
    [
        (_archive("other_extension", "2.0.0"), "extension_catalog_archive_mismatch"),
        (_archive("synthetic_extension_000", "9.9.9"), "extension_catalog_archive_mismatch"),
        (b"not a zip at all", "extension_catalog_archive_unreadable"),
        (
            zipfile.ZipFile(io.BytesIO(), "w").close() or b"PK\x05\x06" + b"\x00" * 18,
            "extension_catalog_archive_manifest_missing",
        ),
    ],
    # Explicit ids: `_archive` stamps the current time into the zip bytes, and an
    # id derived from them differs between xdist workers that collect a second
    # apart, which aborts the parallel suite.
    ids=["other-extension", "other-version", "not-a-zip", "no-manifest"],
)
def test_an_archive_that_is_not_the_offered_release_never_reaches_the_installer(
    monkeypatch: pytest.MonkeyPatch, archive: bytes, code: str
) -> None:
    """The digest proves the bytes are ours; the manifest proves what they are."""
    binding = _BindingService()
    service, _fetched = _service(
        monkeypatch, store=_InstallStore(), binding=binding, artifact=archive
    )

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            "synthetic_extension_000", version="2.0.0", release_sha256=_DIGEST_2_0_0
        )

    assert raised.value.code == code
    assert raised.value.status_code == 422
    assert binding.calls == []


def test_an_incompatible_release_is_refused_before_it_is_downloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _BindingService()
    service, fetched = _service(
        monkeypatch, store=_InstallStore(), binding=binding, platform_version="0.0.1"
    )
    monkeypatch.setattr(
        service,
        "_require_item",
        lambda extension_id: _item("1.0.0", minimum="9.0.0"),
    )

    release_sha256 = _item("1.0.0").release_zip_sha256

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            "synthetic_extension_000",
            version="1.0.0",
            release_sha256=release_sha256,
        )

    assert raised.value.code == "extension_catalog_release_incompatible"
    assert fetched == []
    assert binding.calls == []


def test_an_unknown_extension_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _BindingService()
    service, fetched = _service(monkeypatch, store=_InstallStore(), binding=binding)

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            "no_such_extension", version="2.0.0", release_sha256=_DIGEST_2_0_0
        )

    assert raised.value.status_code == 404
    assert fetched == []
    assert binding.calls == []


def test_an_extension_catalog_error_carries_its_reason_where_the_ui_reads_it() -> None:
    """The Marketplace shows `detail.message`; the reason must be there, not only in `error`."""
    error = ExtensionCatalogError(
        "The extension catalog is unavailable.", code="extension_catalog_unavailable"
    )

    detail = error.detail()

    assert detail["message"] == str(error)
    assert detail["error"] == str(error)
    assert detail["code"] == "extension_catalog_unavailable"


def test_extension_catalog_compatibility_uses_the_product_version() -> None:
    """A catalog release's `compatible_flow_steward` is a product range, as for workflows."""
    import inspect

    from core.application.extensions.extension_catalog_install import (
        ExtensionCatalogInstallService,
    )
    from core.infrastructure.config.version import APP_VERSION

    default = (
        inspect.signature(ExtensionCatalogInstallService.__init__)
        .parameters["platform_version"]
        .default
    )
    assert default() == APP_VERSION


# ---------------------------------------------------------------------------
# Per-target releases (extensions that ship a native executable)
# ---------------------------------------------------------------------------

_AMD64_SHA = "a" * 64
_ARM64_SHA = "b" * 64


def _native_index() -> dict[str, Any]:
    payload = build_extension_index(1)
    entry = payload["items"][0]
    entry["release_zip_url"] = "https://catalog.example.test/native-linux-amd64.zip"
    entry["release_zip_sha256"] = _AMD64_SHA
    entry["release_targets"] = {
        "linux-amd64": {
            "release_zip_url": "https://catalog.example.test/native-linux-amd64.zip",
            "release_zip_bytes": 36 * 1024 * 1024,
            "release_zip_sha256": _AMD64_SHA,
        },
        "linux-arm64": {
            "release_zip_url": "https://catalog.example.test/native-linux-arm64.zip",
            "release_zip_bytes": 32 * 1024 * 1024,
            "release_zip_sha256": _ARM64_SHA,
        },
    }
    return payload


def test_a_per_target_entry_offers_this_hosts_archive() -> None:
    """Listing, pin and download must all name the archive this host can run."""
    item = parse_extension_catalog_index(_native_index(), runtime_target="linux-arm64").items[0]

    assert item.release_zip_url.endswith("native-linux-arm64.zip")
    assert item.release_zip_sha256 == _ARM64_SHA
    assert item.release_zip_bytes == 32 * 1024 * 1024
    assert item.runtime_targets == ("linux-amd64", "linux-arm64")
    assert item.available_for_host is True
    assert item.as_dict()["release_zip_sha256"] == _ARM64_SHA


def test_a_portable_entry_is_available_on_every_host() -> None:
    item = parse_extension_catalog_index(build_extension_index(1), runtime_target="").items[0]

    assert item.runtime_targets == ()
    assert item.available_for_host is True


@pytest.mark.parametrize("host", ["", "linux-riscv64"])
def test_a_per_target_entry_without_this_hosts_archive_is_incompatible(host: str) -> None:
    payload = _native_index()
    del payload["items"][0]["release_targets"]["linux-arm64"]
    item = parse_extension_catalog_index(payload, runtime_target=host).items[0]

    assert item.available_for_host is False
    assert catalog_display_state(item, None, platform_version="1.0.0") == STATE_INCOMPATIBLE


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda targets: targets.update({"windows-amd64": targets["linux-amd64"]}),
            "runtime target",
        ),
        (lambda targets: targets["linux-arm64"].update(release_zip_sha256="nothex"), "sha256"),
        (
            lambda targets: targets["linux-arm64"].update(release_zip_url="http://x.test/a.zip"),
            "https",
        ),
    ],
)
def test_a_malformed_release_target_rejects_the_index(mutate: Any, match: str) -> None:
    payload = _native_index()
    mutate(payload["items"][0]["release_targets"])

    with pytest.raises(ValueError, match=match):
        parse_extension_catalog_index(payload, runtime_target="linux-amd64")


def test_a_release_with_no_archive_for_this_host_is_refused_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _BindingService()
    service, fetched = _service(monkeypatch, store=_InstallStore(), binding=binding)
    payload = _native_index()
    del payload["items"][0]["release_targets"]["linux-arm64"]
    item = parse_extension_catalog_index(payload, runtime_target="linux-arm64").items[0]
    monkeypatch.setattr(service, "_require_item", lambda extension_id: item)

    with pytest.raises(ExtensionCatalogError) as raised:
        service.install_from_catalog(
            item.extension_id, version=item.version, release_sha256=item.release_zip_sha256
        )

    assert raised.value.code == "extension_catalog_release_target_unavailable"
    assert "linux-amd64 only" in str(raised.value)
    assert fetched == []
    assert binding.calls == []


def test_the_install_downloads_the_archive_for_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _BindingService()
    item = parse_extension_catalog_index(_native_index(), runtime_target="linux-arm64").items[0]
    service, fetched = _service(
        monkeypatch,
        store=_InstallStore(),
        binding=binding,
        artifact=_archive(item.extension_id, item.version),
    )
    monkeypatch.setattr(service, "_require_item", lambda extension_id: item)

    service.install_from_catalog(item.extension_id, version=item.version, release_sha256=_ARM64_SHA)

    assert [call["url"] for call in fetched] == [
        "https://catalog.example.test/native-linux-arm64.zip"
    ]
    assert fetched[0]["expected_sha256"] == _ARM64_SHA
    assert len(binding.calls) == 1


def test_category_labels_and_dates_reach_the_index() -> None:
    payload = _index(1)
    payload["category_labels"] = {"data": "Data", "messaging": "Messaging"}
    payload["items"][0]["added_at"] = "2026-09-01T08:00:00Z"
    payload["items"][0]["updated_at"] = "2026-09-20T09:30:00Z"

    index = parse_extension_catalog_index(payload)

    assert dict(index.category_labels) == {"data": "Data", "messaging": "Messaging"}
    item = index.items[0].as_dict()
    assert (item["added_at"], item["updated_at"]) == (
        "2026-09-01T08:00:00Z",
        "2026-09-20T09:30:00Z",
    )


def test_an_extension_index_without_labels_or_dates_still_validates() -> None:
    index = parse_extension_catalog_index(_index(1))
    assert index.category_labels == ()
    assert index.items[0].updated_at == ""
    assert index.items[0].as_dict()["tags"] == []


def test_tags_reach_the_listing_so_search_can_match_them() -> None:
    payload = _index(1)
    payload["items"][0]["tags"] = ["crm", " contacts ", "crm", ""]

    item = parse_extension_catalog_index(payload).items[0]

    # Trimmed, de-duplicated and in the index's order, like every other list.
    assert item.as_dict()["tags"] == ["crm", "contacts"]


@pytest.mark.parametrize("tags", ["crm", [1], {"crm": True}])
def test_malformed_tags_reject_the_whole_index(tags: Any) -> None:
    payload = _index(1)
    payload["items"][0]["tags"] = tags
    with pytest.raises(ValueError, match=r"items\[0\]\.tags"):
        parse_extension_catalog_index(payload)


def test_the_listing_names_capabilities_and_permissions_from_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fetched = _service(monkeypatch, store=_InstallStore(), binding=_BindingService())

    listing = service.list_catalog(scope=normalize_scope(account_id="acc_7"))

    assert listing["capability_labels"] == dict(CAPABILITY_LABELS)
    assert listing["permission_labels"] == dict(PERMISSION_LABELS)
    assert listing["capability_labels"]["query"] == "Data queries"
    assert listing["permission_labels"]["extension:invoke"] == "Run its own operations"
    # Every value is a name to show; the ids stay what the items carry.
    assert all(isinstance(label, str) and label.strip() for label in CAPABILITY_LABELS.values())
    assert all(isinstance(label, str) and label.strip() for label in PERMISSION_LABELS.values())


@pytest.mark.parametrize("labels", [["data"], {"data": 3}])
def test_malformed_category_labels_reject_the_whole_index(labels: Any) -> None:
    payload = _index(1)
    payload["category_labels"] = labels
    with pytest.raises(ValueError, match="category_labels"):
        parse_extension_catalog_index(payload)
