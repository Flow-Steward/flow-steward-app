"""Workflow template catalog: index contract, process-local cache, create path."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest

from core.application.workflows import workflow_template_catalog as catalog
from core.application.workflows.workflow_template_catalog import (
    WorkflowTemplateCatalogError,
    load_workflow_template_catalog,
    parse_workflow_template_catalog_index,
    reset_workflow_template_catalog_cache,
    workflow_template_catalog_availability,
)
from core.application.workflows.workflow_template_catalog_create import (
    RESOLUTION_OUTCOME_AMBIGUOUS,
    RESOLUTION_OUTCOME_MISSING,
    RESOLUTION_OUTCOME_RESOLVED,
    WorkflowTemplateCatalogCreateService,
)
from core.infrastructure.static_catalog import CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS
from core.infrastructure.static_catalog import settings as catalog_settings
from core.infrastructure.static_catalog.http_fetch import (
    CatalogArtifactMismatchError,
    CatalogFetchError,
    CatalogNotModifiedError,
    FetchedCatalogIndex,
)
from tests.fixtures.catalogs.synthetic_catalogs import (
    WORKFLOW_CATALOG_SCALE_ITEM_COUNT,
    build_workflow_template_index,
)

CATALOG_URL = "https://catalog.example.test/workflows/index.json"
BUNDLE_YAML = "schema_version: 1\nkind: workflow_bundle\n"


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(catalog_settings.CONFIG, "workflow_template_catalog_url", CATALOG_URL)
    reset_workflow_template_catalog_cache()
    yield
    reset_workflow_template_catalog_cache()


class _Clock:
    """A movable clock so the 24-hour window can be crossed without waiting."""

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
    """Records every index request and answers with a scripted result.

    Once the script runs out the last reply repeats, so "the host is still
    down" needs one entry rather than one per request.
    """

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
    return build_workflow_template_index(count, catalog_revision=revision)


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
    index = parse_workflow_template_catalog_index(json.loads(json.dumps(_index(3))))

    assert index.schema_version == "workflow_template_catalog/v1"
    assert len(index.items) == 3
    assert index.items[0].workflow_yaml_url.startswith("https://")
    assert len(index.items[0].workflow_yaml_sha256) == 64


def test_a_thousand_entry_index_validates_as_one_document() -> None:
    index = parse_workflow_template_catalog_index(_index(WORKFLOW_CATALOG_SCALE_ITEM_COUNT))
    assert len(index.items) == WORKFLOW_CATALOG_SCALE_ITEM_COUNT
    assert index.item("synthetic_template_0999") is not None


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda p: p.update(schema_version="workflow_template_catalog/v2"), "schema_version"),
        (lambda p: p.update(catalog_revision=""), "catalog_revision"),
        (lambda p: p.update(published_at=""), "published_at"),
        (lambda p: p.update(items={}), "items must be a list"),
        (lambda p: p["items"][0].update(version="not-semver"), "semantic version"),
        (lambda p: p["items"][0].update(workflow_yaml_url="http://x.test/a.yaml"), "https"),
        (lambda p: p["items"][0].update(workflow_yaml_sha256="short"), "SHA-256"),
        (lambda p: p["items"][0].update(workflow_yaml_bytes=0), "greater than zero"),
        (lambda p: p["items"][0].update(publication_status="draft"), "publication_status"),
        (lambda p: p["items"][0].update(verification_status="maybe"), "verification_status"),
        (lambda p: p["items"][0].update(item_id=""), "item_id"),
    ],
)
def test_one_bad_field_rejects_the_whole_index(mutate: Any, match: str) -> None:
    payload = _index(2)
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        parse_workflow_template_catalog_index(payload)


@pytest.mark.parametrize(
    ("bundle_schema", "importable"),
    [
        ("platform.workflow_bundle.v2", True),
        ("platform.workflow_bundle.v1", False),
        ("", False),
        (None, False),
    ],
)
def test_an_item_names_its_bundle_schema_and_only_v2_is_importable(
    bundle_schema: Any, importable: bool
) -> None:
    payload = _index(1)
    if bundle_schema is None:
        payload["items"][0].pop("bundle_schema")
    else:
        payload["items"][0]["bundle_schema"] = bundle_schema

    item = parse_workflow_template_catalog_index(payload).items[0]

    assert item.bundle_schema == (bundle_schema or "")
    assert item.importable is importable
    listed = item.as_dict()
    assert listed["bundle_schema"] == (bundle_schema or "")
    assert listed["importable"] is importable


def test_integration_labels_name_ids_without_replacing_them() -> None:
    """A card shows `Acme Storage (FTP/SFTP)`; the filter still matches the id."""
    payload = _index(1)
    payload["integration_labels"] = {
        "acme.transfer": "Acme Storage (FTP/SFTP)",
        "acme.blank": "   ",
    }

    index = parse_workflow_template_catalog_index(payload)

    assert dict(index.integration_labels) == {"acme.transfer": "Acme Storage (FTP/SFTP)"}
    assert index.items[0].integrations  # the ids themselves are untouched


def test_an_index_without_integration_labels_still_validates() -> None:
    assert parse_workflow_template_catalog_index(_index(1)).integration_labels == ()


@pytest.mark.parametrize(
    "labels", [["acme.transfer"], {"acme.transfer": 3}, {"acme.transfer": "x" * 121}]
)
def test_malformed_integration_labels_reject_the_whole_index(labels: Any) -> None:
    payload = _index(1)
    payload["integration_labels"] = labels
    with pytest.raises(ValueError, match="integration_labels"):
        parse_workflow_template_catalog_index(payload)


def test_category_labels_and_dates_reach_the_listing() -> None:
    """The category list and its names are the catalog's; the UI invents none."""
    payload = _index(1)
    payload["category_labels"] = {"email": "Email", "developer-tools": "Developer tools"}
    payload["items"][0]["added_at"] = "2026-09-01T08:00:00Z"
    payload["items"][0]["updated_at"] = "2026-09-20T09:30:00Z"

    index = parse_workflow_template_catalog_index(payload)

    assert dict(index.category_labels) == {"developer-tools": "Developer tools", "email": "Email"}
    item = index.items[0].as_dict()
    assert (item["added_at"], item["updated_at"]) == (
        "2026-09-01T08:00:00Z",
        "2026-09-20T09:30:00Z",
    )


def test_an_index_without_category_labels_or_dates_still_validates() -> None:
    index = parse_workflow_template_catalog_index(_index(1))
    assert index.category_labels == ()
    assert index.items[0].as_dict()["added_at"] == ""


def test_a_malformed_date_rejects_the_whole_index() -> None:
    payload = _index(1)
    payload["items"][0]["added_at"] = "last tuesday"
    with pytest.raises(ValueError, match="added_at"):
        parse_workflow_template_catalog_index(payload)


def test_a_duplicate_item_id_rejects_the_index() -> None:
    payload = _index(2)
    payload["items"][1]["item_id"] = payload["items"][0]["item_id"]
    with pytest.raises(ValueError, match="duplicate item_id"):
        parse_workflow_template_catalog_index(payload)


def test_an_index_above_the_item_ceiling_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "MAX_CATALOG_ITEMS", 3)
    index = _index(4)

    with pytest.raises(ValueError, match="more than 3 items"):
        parse_workflow_template_catalog_index(index)


# ---------------------------------------------------------------------------
# Process-local cache
# ---------------------------------------------------------------------------


def test_first_load_fetches_the_index_and_downloads_no_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_calls: list[str] = []
    monkeypatch.setattr(
        "core.application.workflows.workflow_template_catalog.fetch_catalog_artifact",
        lambda url, **_kwargs: artifact_calls.append(url) or b"",
    )
    host = _install_host(monkeypatch, _reply(_index(3)))

    index = load_workflow_template_catalog()

    assert len(index.items) == 3
    assert [call["url"] for call in host.calls] == [CATALOG_URL]
    assert artifact_calls == []


def test_every_request_asks_the_host_again(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """No freshness window: an approved template must not wait out a cache."""
    host = _install_host(monkeypatch, _reply(_index(1)))

    load_workflow_template_catalog()
    clock.advance(1)
    load_workflow_template_catalog()

    assert len(host.calls) == 2


def test_a_republished_index_shows_on_the_next_request(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    host = _install_host(
        monkeypatch, _reply(_index(1, revision="r1")), _reply(_index(2, revision="r2"))
    )

    load_workflow_template_catalog()
    clock.advance(1)
    refreshed = load_workflow_template_catalog()

    assert refreshed.catalog_revision == "r2"
    assert len(host.calls) == 2


def test_a_stored_etag_is_sent_on_the_refresh_and_304_keeps_the_index(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    host = _install_host(
        monkeypatch,
        _reply(_index(1, revision="r1"), etag='"rev-1"'),
        CatalogNotModifiedError("unchanged"),
    )

    first = load_workflow_template_catalog()
    clock.advance(1)
    second = load_workflow_template_catalog()

    assert host.calls[1]["etag"] == '"rev-1"'
    assert second is first
    # A 304 starts no window: the next request asks again, with the same ETag.
    clock.advance(1)
    load_workflow_template_catalog()
    assert len(host.calls) == 3
    assert host.calls[2]["etag"] == '"rev-1"'


def test_a_host_that_never_sends_an_etag_keeps_working(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    host = _install_host(
        monkeypatch, _reply(_index(1, revision="r1")), _reply(_index(1, revision="r1"))
    )

    load_workflow_template_catalog()
    clock.advance(1)
    refreshed = load_workflow_template_catalog()

    assert host.calls[1]["etag"] == ""
    assert refreshed.catalog_revision == "r1"


def test_a_failed_refresh_serves_the_stale_index(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _install_host(monkeypatch, _reply(_index(1, revision="r1")), CatalogFetchError("host is down"))

    load_workflow_template_catalog()
    clock.advance(1)
    stale = load_workflow_template_catalog()

    assert stale.catalog_revision == "r1"
    availability = workflow_template_catalog_availability()
    assert availability["stale"] is True
    assert "host is down" in availability["last_refresh_error"]


def test_a_failed_first_fetch_reports_the_catalog_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_host(monkeypatch, CatalogFetchError("host is down"))

    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        load_workflow_template_catalog()

    assert raised.value.code == "workflow_template_catalog_unavailable"
    assert raised.value.status_code == 503
    assert workflow_template_catalog_availability()["available"] is False


def test_an_invalid_refresh_keeps_the_previous_index_active(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    broken = _index(3, revision="r2")
    broken["items"][2]["workflow_yaml_sha256"] = "nope"
    _install_host(monkeypatch, _reply(_index(3, revision="r1")), _reply(broken))

    load_workflow_template_catalog()
    clock.advance(1)
    served = load_workflow_template_catalog()

    assert served.catalog_revision == "r1"
    assert len(served.items) == 3


def test_an_unconfigured_catalog_is_reported_rather_than_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(catalog_settings.CONFIG, "workflow_template_catalog_url", "")
    host = _install_host(monkeypatch)

    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        load_workflow_template_catalog()

    assert raised.value.code == "workflow_template_catalog_not_configured"
    assert host.calls == []
    assert workflow_template_catalog_availability()["configured"] is False


def test_a_changed_source_url_drops_the_cached_index(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    host = _install_host(
        monkeypatch, _reply(_index(1, revision="r1")), _reply(_index(1, revision="r2"))
    )

    load_workflow_template_catalog()
    monkeypatch.setitem(
        catalog_settings.CONFIG,
        "workflow_template_catalog_url",
        "https://other.example.test/index.json",
    )
    served = load_workflow_template_catalog()

    assert served.catalog_revision == "r2"
    assert len(host.calls) == 2


def test_a_restart_starts_from_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache is process-local: nothing survives to a second process."""
    host = _install_host(monkeypatch, _reply(_index(1)), _reply(_index(1)))

    load_workflow_template_catalog()
    reset_workflow_template_catalog_cache()  # stands in for a fresh process
    load_workflow_template_catalog()

    assert len(host.calls) == 2


# ---------------------------------------------------------------------------
# Create path
# ---------------------------------------------------------------------------


class _Portability:
    """Records what the catalog adapter hands to the real import service."""

    def __init__(self, *, dependencies: list[dict[str, Any]] | None = None) -> None:
        self.dependencies = dependencies or []
        self.preflight_calls: list[dict[str, Any]] = []
        self.apply_calls: list[dict[str, Any]] = []
        self.workflow_count = 1

    def preflight_import(self, bundle_yaml: str, **kwargs: Any) -> dict[str, Any]:
        self.preflight_calls.append({"bundle_yaml": bundle_yaml, **kwargs})
        resolved = {
            (str(row.get("dependency_type")), str(row.get("ref")))
            for row in kwargs.get("resolutions") or []
        }
        dependencies = [
            {
                **row,
                "status": "resolved"
                if (row["dependency_type"], row["ref"]) in resolved
                else row["status"],
                "blocking": (row["dependency_type"], row["ref"]) not in resolved,
            }
            for row in self.dependencies
        ]
        blocking = [row for row in dependencies if row["blocking"]]
        return {
            "kind": "workflow_import_preflight",
            "can_apply": not blocking,
            "dependencies": dependencies,
            "summary": {"workflow_count": self.workflow_count},
            # The first is the template; the rest are workflows nothing in it calls.
            "workflows": [
                {
                    "workflow_id": f"wf_{index}",
                    "portability_kind": "workflow_definition",
                    "role": "root" if index == 0 else "workflow",
                }
                for index in range(self.workflow_count)
            ],
        }

    def apply_import(self, bundle_yaml: str, **kwargs: Any) -> dict[str, Any]:
        self.apply_calls.append({"bundle_yaml": bundle_yaml, **kwargs})
        return {"status": "imported", "workflow_id": "wf_new"}


class _Install:
    def __init__(self, extension_id: str, version: str, install_id: str, state: str = "active"):
        self.extension_id = extension_id
        self.version = version
        self.install_id = install_id
        self.state = state


class _InstallStore:
    def __init__(self, *records: _Install) -> None:
        self.records = list(records)
        self.account_id = ""

    def list_for_account(self, account_id: str, **_kwargs: Any) -> list[_Install]:
        self.account_id = account_id
        return list(self.records)


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    portability: _Portability,
    store: _InstallStore | None = None,
    artifact: Any = None,
) -> tuple[WorkflowTemplateCatalogCreateService, list[dict[str, Any]]]:
    _install_host(monkeypatch, _reply(_index(3)))
    fetched: list[dict[str, Any]] = []

    def _fetch(url: str, **kwargs: Any) -> bytes:
        fetched.append({"url": url, **kwargs})
        if isinstance(artifact, Exception):
            raise artifact
        return BUNDLE_YAML.encode("utf-8")

    service = WorkflowTemplateCatalogCreateService(
        portability=portability,
        get_install_store=lambda: store or _InstallStore(),
        fetch_artifact=_fetch,
    )
    return service, fetched


def test_preflight_downloads_only_the_selected_item_and_verifies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability()
    service, fetched = _service(monkeypatch, portability=portability)

    result = service.preflight("synthetic_template_0001")

    assert len(fetched) == 1
    call = fetched[0]
    assert call["url"].endswith("synthetic_template_0001-1.1.1.yaml")
    assert call["expected_bytes"] == 1025
    assert (
        call["expected_sha256"]
        == hashlib.sha256(b"workflow:synthetic_template_0001:1.1.1").hexdigest()
    )
    assert portability.preflight_calls[-1]["bundle_yaml"] == BUNDLE_YAML
    assert result["catalog_item"]["item_id"] == "synthetic_template_0001"


def test_a_digest_mismatch_never_reaches_parsing_or_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability()
    service, _fetched = _service(
        monkeypatch,
        portability=portability,
        artifact=CatalogArtifactMismatchError("SHA-256 does not match"),
    )

    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        service.preflight("synthetic_template_0000")

    assert raised.value.status_code == 422
    assert portability.preflight_calls == []
    assert portability.apply_calls == []


@pytest.mark.parametrize("bundle_schema", ["platform.workflow_bundle.v1", None])
def test_a_template_core_cannot_import_is_refused_before_its_file_is_downloaded(
    monkeypatch: pytest.MonkeyPatch, bundle_schema: Any
) -> None:
    payload = _index(3)
    if bundle_schema is None:
        payload["items"][1].pop("bundle_schema")
    else:
        payload["items"][1]["bundle_schema"] = bundle_schema
    portability = _Portability()
    service, fetched = _service(monkeypatch, portability=portability)
    _install_host(monkeypatch, _reply(payload))
    reset_workflow_template_catalog_cache()

    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        service.preflight("synthetic_template_0001")

    assert raised.value.status_code == 409
    assert raised.value.code == "workflow_template_bundle_schema_unsupported"
    assert fetched == []
    assert portability.preflight_calls == []


def test_an_unknown_item_is_a_404_and_downloads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability()
    service, fetched = _service(monkeypatch, portability=portability)

    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        service.preflight("no_such_template")

    assert raised.value.status_code == 404
    assert fetched == []


def test_create_delegates_to_the_existing_portability_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability()
    service, _fetched = _service(monkeypatch, portability=portability)

    result = service.create(
        "synthetic_template_0000",
        author="alice",
        owner_account_id="acc_1",
        owner_project_id="proj_1",
    )

    assert result["workflow_id"] == "wf_new"
    assert result["catalog_item"] == {"item_id": "synthetic_template_0000", "version": "1.0.0"}
    apply_call = portability.apply_calls[-1]
    assert apply_call["bundle_yaml"] == BUNDLE_YAML
    assert apply_call["author"] == "alice"
    assert apply_call["owner_account_id"] == "acc_1"
    assert apply_call["owner_project_id"] == "proj_1"


def _extension_dependency(ref: str, **metadata: Any) -> dict[str, Any]:
    return {
        "dependency_type": "extension",
        "ref": ref,
        "status": "requires_extension_install",
        "metadata": metadata,
    }


def test_a_logical_extension_id_is_mapped_to_the_local_install_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability(dependencies=[_extension_dependency("synthetic_gmail")])
    store = _InstallStore(_Install("synthetic_gmail", "1.2.0", "inst_local"))
    service, _fetched = _service(monkeypatch, portability=portability, store=store)

    result = service.preflight("synthetic_template_0000")

    resolution = portability.preflight_calls[-1]["resolutions"][-1]
    assert resolution["dependency_type"] == "extension"
    assert resolution["ref"] == "synthetic_gmail"
    assert resolution["action"] == "install_extension_required"
    # target_ref is what the existing rewrite path reads.
    assert resolution["target_ref"] == "inst_local"
    assert resolution["target_install_id"] == "inst_local"
    assert result["can_apply"] is True
    assert result["catalog_extension_resolutions"] == [
        {
            "extension_id": "synthetic_gmail",
            "outcome": RESOLUTION_OUTCOME_RESOLVED,
            "message": "",
            "install_id": "inst_local",
            "installed_version": "1.2.0",
        }
    ]


def test_a_missing_extension_leaves_the_existing_blocking_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability(dependencies=[_extension_dependency("synthetic_gmail")])
    service, _fetched = _service(monkeypatch, portability=portability, store=_InstallStore())

    result = service.preflight("synthetic_template_0000")

    assert result["can_apply"] is False
    assert result["catalog_extension_resolutions"][0]["outcome"] == RESOLUTION_OUTCOME_MISSING


def test_a_disabled_extension_install_does_not_satisfy_the_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability(dependencies=[_extension_dependency("synthetic_gmail")])
    store = _InstallStore(_Install("synthetic_gmail", "1.2.0", "inst_local", state="disabled"))
    service, _fetched = _service(monkeypatch, portability=portability, store=store)

    result = service.preflight("synthetic_template_0000")

    assert result["can_apply"] is False
    assert result["catalog_extension_resolutions"][0]["outcome"] == RESOLUTION_OUTCOME_MISSING


def test_two_active_installs_block_instead_of_picking_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability(dependencies=[_extension_dependency("synthetic_gmail")])
    store = _InstallStore(
        _Install("synthetic_gmail", "1.2.0", "inst_a"),
        _Install("synthetic_gmail", "1.3.0", "inst_b"),
    )
    service, _fetched = _service(monkeypatch, portability=portability, store=store)

    result = service.preflight("synthetic_template_0000")

    assert result["can_apply"] is False
    assert result["catalog_extension_resolutions"][0]["outcome"] == RESOLUTION_OUTCOME_AMBIGUOUS
    assert portability.preflight_calls[-1]["resolutions"] == []


def test_an_install_below_the_declared_minimum_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability(
        dependencies=[_extension_dependency("synthetic_gmail", min_version="2.0.0")]
    )
    store = _InstallStore(_Install("synthetic_gmail", "1.9.0", "inst_local"))
    service, _fetched = _service(monkeypatch, portability=portability, store=store)

    result = service.preflight("synthetic_template_0000")

    assert result["can_apply"] is False
    assert result["catalog_extension_resolutions"][0]["outcome"] == "incompatible"


def test_the_install_lookup_is_account_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.application.management.scope import normalize_scope

    portability = _Portability(dependencies=[_extension_dependency("synthetic_gmail")])
    store = _InstallStore(_Install("synthetic_gmail", "1.2.0", "inst_local"))
    service, _fetched = _service(monkeypatch, portability=portability, store=store)

    service.preflight("synthetic_template_0000", scope=normalize_scope(account_id="acc_7"))

    assert store.account_id == "acc_7"


# ---------------------------------------------------------------------------
# The install lookup the portability service cannot answer on its own
# ---------------------------------------------------------------------------


class _RegistryStoreWithoutInstalls:
    """Stands in for the admin registry store, which has no install methods."""

    def __init__(self) -> None:
        self.dsn = "dbname=example"

    def get_workflow(self, workflow_id: str) -> None:
        return None


def _install_aware(store: _InstallStore, account_id: str = "acc_7"):
    from core.application.workflows.workflow_template_catalog_create import (
        InstallAwareWorkflowDb,
    )

    return InstallAwareWorkflowDb(
        _RegistryStoreWithoutInstalls(),
        get_install_store=lambda: store,
        account_id=account_id,
    )


class _PydanticInstall(_Install):
    def model_dump(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "version": self.version,
            "install_id": self.install_id,
            "state": self.state,
        }


def test_the_registry_store_alone_cannot_answer_an_install_lookup() -> None:
    """Pins why the wrapper exists: without it every install id reads as absent."""
    assert not hasattr(_RegistryStoreWithoutInstalls(), "get_extension_by_install_id")


def test_the_wrapper_resolves_an_active_install_and_delegates_the_rest() -> None:
    store = _InstallStore(_PydanticInstall("synthetic_gmail", "1.2.0", "inst_local"))
    db = _install_aware(store)

    assert db.get_extension_by_install_id("inst_local") == {
        "extension_id": "synthetic_gmail",
        "version": "1.2.0",
        "install_id": "inst_local",
        "state": "active",
    }
    assert db.dsn == "dbname=example"
    assert db.get_workflow("anything") is None


def test_the_wrapper_does_not_resolve_a_disabled_install() -> None:
    store = _InstallStore(
        _PydanticInstall("synthetic_gmail", "1.2.0", "inst_local", state="disabled")
    )
    assert _install_aware(store).get_extension_by_install_id("inst_local") is None


def test_the_wrapper_asks_the_store_with_this_accounts_id() -> None:
    store = _InstallStore(_PydanticInstall("synthetic_gmail", "1.2.0", "inst_local"))

    _install_aware(store, account_id="acc_42").get_extension_by_install_id("inst_local")

    assert store.account_id == "acc_42"


def test_the_wrapper_answers_no_for_an_unknown_install_id() -> None:
    store = _InstallStore(_PydanticInstall("synthetic_gmail", "1.2.0", "inst_local"))
    assert _install_aware(store).get_extension_by_install_id("inst_other") is None


def test_a_template_with_nothing_to_map_is_preflighted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability()
    service, _fetched = _service(monkeypatch, portability=portability)

    service.preflight("synthetic_template_0000")

    assert len(portability.preflight_calls) == 1


def test_a_template_with_a_mapped_extension_is_preflighted_with_the_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portability = _Portability(dependencies=[_extension_dependency("synthetic_gmail")])
    store = _InstallStore(_Install("synthetic_gmail", "1.2.0", "inst_local"))
    service, _fetched = _service(monkeypatch, portability=portability, store=store)

    service.preflight("synthetic_template_0000")

    assert len(portability.preflight_calls) == 2
    assert portability.preflight_calls[0]["resolutions"] == []
    assert portability.preflight_calls[1]["resolutions"][-1]["target_ref"] == "inst_local"


def test_a_new_catalog_version_touches_no_local_workflow(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """V1 is copy-only: a refresh is metadata, not a migration.

    The reader holds no workflow store at all, so the strongest statement the
    test can make is that refreshing from 1.0.0 to 1.1.0 goes through the whole
    cache path without the workflow side of the product being consulted once.
    """
    first = _index(1, revision="r1")
    first["items"][0]["version"] = "1.0.0"
    second = _index(1, revision="r2")
    second["items"][0]["version"] = "1.1.0"
    _install_host(monkeypatch, _reply(first), _reply(second))
    portability = _Portability()
    store = _InstallStore()

    load_workflow_template_catalog()
    clock.advance(1)
    refreshed = load_workflow_template_catalog()

    assert refreshed.items[0].version == "1.1.0"
    assert portability.preflight_calls == []
    assert portability.apply_calls == []
    assert store.account_id == ""


# ---------------------------------------------------------------------------
# Refresh cooldown
# ---------------------------------------------------------------------------


def test_a_dead_host_is_not_dialled_again_on_every_request(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A failed refresh must not turn each request into a fresh round trip."""
    host = _install_host(
        monkeypatch, _reply(_index(1, revision="r1")), CatalogFetchError("host is down")
    )

    load_workflow_template_catalog()
    clock.advance(1)
    assert load_workflow_template_catalog().catalog_revision == "r1"
    attempts_after_failure = len(host.calls)

    for _ in range(5):
        assert load_workflow_template_catalog().catalog_revision == "r1"

    assert len(host.calls) == attempts_after_failure


def test_the_cooldown_expires_and_the_next_request_refreshes(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    host = _install_host(
        monkeypatch,
        _reply(_index(1, revision="r1")),
        CatalogFetchError("host is down"),
        _reply(_index(1, revision="r2")),
    )

    load_workflow_template_catalog()
    clock.advance(1)
    load_workflow_template_catalog()
    clock.advance(CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS)

    assert load_workflow_template_catalog().catalog_revision == "r2"
    assert len(host.calls) == 3


def test_a_failed_first_fetch_does_not_retry_inside_the_cooldown(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    host = _install_host(monkeypatch, CatalogFetchError("host is down"))

    for _ in range(4):
        with pytest.raises(WorkflowTemplateCatalogError):
            load_workflow_template_catalog()

    assert len(host.calls) == 1


# ---------------------------------------------------------------------------
# Platform compatibility and the one-workflow promise
# ---------------------------------------------------------------------------


def _compat_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    portability: _Portability,
    platform_version: str = "1.0.0",
    minimum: str = "",
    maximum: str = "",
    workflow_count: int = 1,
):
    payload = _index(1)
    payload["items"][0]["compatible_flow_steward"] = {"min": minimum, "max": maximum}
    _install_host(monkeypatch, _reply(payload))
    portability.workflow_count = workflow_count
    return WorkflowTemplateCatalogCreateService(
        portability=portability,
        get_install_store=_InstallStore,
        fetch_artifact=lambda *_args, **_kwargs: BUNDLE_YAML.encode("utf-8"),
        platform_version=lambda: platform_version,
    )


@pytest.mark.parametrize(
    ("minimum", "maximum", "platform"),
    [("2.0.0", "", "1.0.0"), ("", "0.9.0", "1.0.0")],
)
def test_a_template_outside_the_declared_range_is_refused(
    monkeypatch: pytest.MonkeyPatch, minimum: str, maximum: str, platform: str
) -> None:
    portability = _Portability()
    service = _compat_service(
        monkeypatch,
        portability=portability,
        platform_version=platform,
        minimum=minimum,
        maximum=maximum,
    )

    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        service.preflight("synthetic_template_0000")

    assert raised.value.code == "workflow_template_incompatible"
    assert raised.value.status_code == 409
    assert portability.preflight_calls == []
    assert portability.apply_calls == []


@pytest.mark.parametrize(("minimum", "maximum"), [("1.0.0", "1.0.0"), ("0.1.0", "2.0.0"), ("", "")])
def test_a_template_inside_the_declared_range_is_allowed(
    monkeypatch: pytest.MonkeyPatch, minimum: str, maximum: str
) -> None:
    portability = _Portability()
    service = _compat_service(
        monkeypatch, portability=portability, minimum=minimum, maximum=maximum
    )

    assert service.preflight("synthetic_template_0000")["can_apply"] is True


def test_a_bundle_carrying_workflows_nothing_calls_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template is one workflow and the children it calls, not several templates.

    The importer would create every workflow in the file, so unrelated ones would be
    created without the card saying so. A parent's own children are allowed; that
    case is covered in test_workflow_bundle_children.
    """
    portability = _Portability()
    service = _compat_service(monkeypatch, portability=portability, workflow_count=3)

    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        service.create("synthetic_template_0000", author="alice")

    assert raised.value.code == "workflow_template_multiple_workflows"
    assert raised.value.status_code == 422
    assert portability.apply_calls == []


def test_a_catalog_error_carries_its_reason_where_the_ui_reads_it() -> None:
    """A refused template must say why, not "Request failed with status 409".

    The frontend HTTP client reads `detail.message`, the key the import errors in the
    same area already use. The catalog error put its text only under `error`, so the
    operator saw the status code and never the incompatible version it was refused for.
    """
    error = WorkflowTemplateCatalogError(
        "Workflow template 'x' 1.0.0 is not compatible with Flow Steward 1.0.0.",
        code="workflow_template_incompatible",
        status_code=409,
    )

    detail = error.detail()

    assert detail["message"] == str(error)
    # The existing keys stay, for callers that already read them.
    assert detail["error"] == str(error)
    assert detail["code"] == "workflow_template_incompatible"


def test_the_catalog_route_resolves_installs_inside_the_request_account() -> None:
    """A chosen or mapped extension install was never accepted on the catalog path.

    The catalog route built its portability service without an account. With an install
    store wired in, `_install_exists` refuses to answer without one — the safe direction —
    so every extension stayed "missing" in the catalog dialog while the manual import,
    which passes the account, accepted the same install.
    """
    from unittest.mock import MagicMock, patch

    import core.entrypoints.admin.workflows as workflow_routes
    from core.application.management.scope import normalize_scope
    from core.entrypoints.admin.workflows_routes_catalog import _catalog_create_service

    class _IntegrationStore:
        conn = None

        def get_extension_by_install_id(self, install_id, *, account_id=None):
            if (install_id, account_id) == ("inst-1", "acc_alpha"):
                return {"install_id": "inst-1", "extension_id": "acme.mailbox"}
            return None

    with (
        patch.object(workflow_routes, "_get_db", return_value=MagicMock()),
        patch.object(
            workflow_routes, "_platform_logger_for_admin_catalog", return_value=_IntegrationStore()
        ),
    ):
        service = _catalog_create_service(normalize_scope(account_id="acc_alpha"))

        assert service._portability._install_exists("inst-1") is True


def test_catalog_compatibility_is_checked_against_the_product_version() -> None:
    """`compatible_flow_steward` names a Flow Steward release (the contract's own example
    is `0.2.0`), but the check compared it with the extension host contract version, so a
    0.4.0 install refused templates as "not compatible with Flow Steward 1.0.0"."""
    import inspect

    from core.infrastructure.config.version import APP_VERSION

    default = (
        inspect.signature(WorkflowTemplateCatalogCreateService.__init__)
        .parameters["platform_version"]
        .default
    )
    assert default() == APP_VERSION


def test_an_item_lists_the_child_workflows_it_carries() -> None:
    payload = _index(1)
    payload["items"][0]["includes"] = [{"item_id": "fetch_messages", "name": "Fetch messages"}]

    item = parse_workflow_template_catalog_index(payload).items[0].as_dict()

    assert item["includes"] == [{"item_id": "fetch_messages", "name": "Fetch messages"}]
    assert parse_workflow_template_catalog_index(_index(1)).items[0].as_dict()["includes"] == []


def test_malformed_includes_reject_the_whole_index() -> None:
    payload = _index(1)
    payload["items"][0]["includes"] = [{"name": "no id"}]
    with pytest.raises(ValueError, match="includes"):
        parse_workflow_template_catalog_index(payload)
