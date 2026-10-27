"""MCP server catalog: index contract, cache, icons, and install."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.management import mcp_server_catalog as catalog
from core.application.management import mcp_server_catalog_install as install_module
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.mcp_server_catalog import (
    McpServerCatalogError,
    parse_mcp_server_catalog_index,
)
from core.application.management.mcp_server_catalog_install import (
    McpServerCatalogService,
    resolve_headers,
    resolve_remote_url,
)
from core.application.management.query_models import McpConnectorListView, McpConnectorView
from core.application.management.scope import ScopeContext
from core.entrypoints.api.mcp_server_catalog import McpCatalogInstallRequest
from core.infrastructure.static_catalog import settings as catalog_settings
from core.infrastructure.static_catalog.http_fetch import (
    CatalogFetchError,
    CatalogNotModifiedError,
    FetchedCatalogIndex,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/catalogs/mcp_server_catalog_index.json"
CATALOG_URL = "https://catalog.example.test/mcps/index.json"
META = catalog.CATALOG_META_KEY


def published_index() -> dict[str, Any]:
    """The index github.com/Flow-Steward/mcps published, icon digests aside.

    The fixture leaves each icon's sha256 empty and it is derived here: 64-char
    hex literals in a committed file read as secrets to the secret scan.
    """
    index = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for entry in index["servers"]:
        overlay = entry["server"]["_meta"][META]
        if overlay.get("icon") and not overlay["icon"].get("sha256"):
            overlay["icon"]["sha256"] = hashlib.sha256(f"icon:{overlay['id']}".encode()).hexdigest()
    return index


def record(index: dict[str, Any], item_id: str) -> dict[str, Any]:
    return next(r for r in index["servers"] if r["server"]["_meta"][META]["id"] == item_id)


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(catalog_settings.CONFIG, "mcp_server_catalog_url", CATALOG_URL)
    catalog.reset_mcp_server_catalog_cache()
    yield
    catalog.reset_mcp_server_catalog_cache()


@pytest.fixture
def scope() -> ScopeContext:
    return ScopeContext(
        account_id="acc_alpha",
        project_id="00000000-0000-0000-0000-000000000001",
        tenant_id="tenant_alpha",
        actor_id="ops",
        principal_type="operator",
    )


# ---------------------------------------------------------------------------
# index contract
# ---------------------------------------------------------------------------


def test_the_published_catalog_parses_with_every_reviewed_setting() -> None:
    index = parse_mcp_server_catalog_index(published_index())

    assert index.schema_version == "mcp_server_catalog/v1"
    assert len(index.items) == 16
    assert index.skipped_count == 0
    assert dict(index.category_labels)["productivity"] == "Productivity"

    notion = index.item("notion")
    assert notion is not None
    assert notion.server_name == "com.notion/mcp"
    assert notion.official_vendor == "Notion"
    assert notion.official_evidence_url.startswith("https://developers.notion.com/")
    assert notion.icon is not None
    assert notion.icon.size == "128x128"
    (remote,) = notion.remotes
    assert (remote.remote_id, remote.url, remote.auth_mode, remote.client_registration) == (
        "default",
        "https://mcp.notion.com/mcp",
        "oauth2",
        "dcr",
    )
    assert remote.allowed_hosts == ("mcp.notion.com",)

    gmail = index.item("google-gmail")
    assert gmail is not None
    (gmail_remote,) = gmail.remotes
    assert gmail_remote.client_registration == "user_supplied"
    assert "https://www.googleapis.com/auth/gmail.modify" in gmail_remote.requested_scopes
    assert dict(gmail_remote.authorization_parameters) == {
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    assert gmail_remote.refresh_token_required is True
    assert gmail.preview_note == "Developer Preview"

    monday = index.item("monday")
    assert monday is not None
    assert [(r.remote_id, r.auth_mode) for r in monday.remotes] == [
        ("default", "oauth2"),
        ("api-token", "headers"),
    ]
    assert [h.name for h in monday.remote("api-token").headers] == ["Authorization"]


def test_a_card_carries_what_the_list_needs_and_not_the_endpoints() -> None:
    card = parse_mcp_server_catalog_index(published_index()).item("notion").dump_card()
    assert card["sign_in"] == ["oauth2"]
    assert card["official"]["vendor"] == "Notion"
    assert "remotes" not in card
    assert "description" not in card
    assert "tier" not in card


def test_a_malformed_reviewed_record_fails_the_whole_index() -> None:
    index = published_index()
    record(index, "notion")["server"]["_meta"][META]["remotes"][0]["auth"]["mode"] = "TODO"
    with pytest.raises(ValueError, match=r"auth\.mode"):
        parse_mcp_server_catalog_index(index)


def test_a_plain_registry_response_lists_only_reviewed_records() -> None:
    index = published_index()
    reviewed = record(index, "notion")
    unreviewed = copy.deepcopy(reviewed)
    unreviewed["server"]["_meta"] = {}
    deleted = copy.deepcopy(record(index, "linear"))
    deleted["_meta"]["io.modelcontextprotocol.registry/official"]["status"] = "deleted"
    deprecated = copy.deepcopy(record(index, "stripe"))
    deprecated["_meta"]["io.modelcontextprotocol.registry/official"]["status"] = "deprecated"

    parsed = parse_mcp_server_catalog_index(
        {"servers": [reviewed, unreviewed, deleted, deprecated], "metadata": {"count": 4}}
    )

    assert parsed.schema_version == "registry/v0.1"
    assert [i.item_id for i in parsed.items] == ["notion", "stripe"]
    assert parsed.item("stripe").status == "deprecated"
    assert parsed.skipped_count == 1
    assert parsed.catalog_revision


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda i: i.update(schema_version="mcp_server_catalog/v9"), "schema_version must be"),
        (lambda i: i.update(servers={}), "servers must be a list"),
        (
            lambda i: record(i, "notion")["server"]["_meta"][META]["remotes"][0].update(
                url="https://evil.example.com/mcp"
            ),
            "not a streamable-http remote of the server record",
        ),
        (
            lambda i: record(i, "notion")["server"]["_meta"][META]["remotes"][0]["auth"].update(
                mode="TODO"
            ),
            "auth.mode must be one of",
        ),
        (
            lambda i: record(i, "notion")["server"]["_meta"][META]["remotes"][0].update(
                allowed_hosts=[]
            ),
            "allowed_hosts is required",
        ),
        (
            lambda i: record(i, "notion")["server"]["_meta"][META]["icon"].update(bytes=10**9),
            "icon.bytes must be",
        ),
        (
            lambda i: record(i, "linear")["server"]["_meta"][META].update(id="notion"),
            "duplicate catalog id 'notion'",
        ),
    ],
)
def test_a_bad_index_is_refused_as_a_whole(mutate, message) -> None:
    index = published_index()
    mutate(index)
    with pytest.raises(ValueError, match=message):
        parse_mcp_server_catalog_index(index)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[str]:
    calls: list[str] = []

    def fake_fetch(url, *, guard, max_bytes, purpose, etag="", last_modified=""):
        calls.append(etag)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FetchedCatalogIndex(payload=outcome, etag='"v1"')

    monkeypatch.setattr(catalog, "fetch_catalog_index_json", fake_fetch)
    return calls


def test_every_read_revalidates_the_index_conditionally(monkeypatch) -> None:
    calls = _stub_fetch(monkeypatch, [published_index(), CatalogNotModifiedError("304")])

    first = catalog.load_mcp_server_catalog()
    assert catalog.load_mcp_server_catalog() is first
    assert calls == ["", '"v1"']


def test_a_failed_refresh_keeps_serving_the_last_index(monkeypatch) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(catalog, "_now", lambda: now[0])
    _stub_fetch(monkeypatch, [published_index(), CatalogFetchError("host down")])
    first = catalog.load_mcp_server_catalog()
    now[0] += 1
    assert catalog.load_mcp_server_catalog() is first
    availability = catalog.mcp_server_catalog_availability()
    assert availability["available"] is True
    assert availability["stale"] is True


def test_an_unconfigured_catalog_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setitem(catalog_settings.CONFIG, "mcp_server_catalog_url", "")
    assert catalog.mcp_server_catalog_availability()["reason"] == "mcp_catalog_not_configured"
    with pytest.raises(McpServerCatalogError, match="not configured"):
        catalog.load_mcp_server_catalog()


def test_icons_are_verified_once_then_served_from_memory(monkeypatch) -> None:
    # The listing read names the icon; only an id it does not list asks again.
    index_reads = _stub_fetch(monkeypatch, [published_index(), CatalogNotModifiedError("304")])
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 10
    fetched: list[dict[str, Any]] = []

    def fake_artifact(url, **kwargs):
        fetched.append({"url": url, **kwargs})
        return png

    monkeypatch.setattr(catalog, "fetch_catalog_artifact", fake_artifact)
    notion_icon = catalog.load_mcp_server_catalog().item("notion").icon

    assert catalog.mcp_server_catalog_icon("notion") == png
    assert catalog.mcp_server_catalog_icon("notion") == png
    assert len(fetched) == 1
    assert len(index_reads) == 1
    assert fetched[0]["url"] == notion_icon.url
    assert fetched[0]["expected_sha256"] == notion_icon.sha256
    assert fetched[0]["expected_bytes"] == notion_icon.bytes

    with pytest.raises(McpServerCatalogError) as missing:
        catalog.mcp_server_catalog_icon("no-such-server")
    assert missing.value.status_code == 404
    assert len(index_reads) == 2


def test_an_icon_that_is_not_a_png_is_refused(monkeypatch) -> None:
    _stub_fetch(monkeypatch, [published_index()])
    monkeypatch.setattr(catalog, "fetch_catalog_artifact", lambda url, **kw: b"<svg onload=x>")
    with pytest.raises(McpServerCatalogError, match="not a PNG"):
        catalog.mcp_server_catalog_icon("notion")


# ---------------------------------------------------------------------------
# URL and header resolution
# ---------------------------------------------------------------------------


def _remote(**overrides) -> catalog.McpCatalogRemote:
    base = dict(  # noqa: C408 - keyword form mirrors the dataclass fields
        remote_id="default",
        label="Example",
        url="https://{api_host}/mcp/{tenant}",
        allowed_hosts=("api.example.com", "*.eu.example.com"),
        user_defined_host=False,
        auth_mode="headers",
        client_registration="",
        requested_scopes=(),
        authorization_parameters=(),
        refresh_token_required=False,
        setup_url="",
        variables=(
            catalog.McpCatalogVariable(
                "api_host", "", True, ("api.example.com", "api.eu.example.com"), ""
            ),
            catalog.McpCatalogVariable("tenant", "", True, (), ""),
        ),
        headers=(
            catalog.McpCatalogHeader("Authorization", "", True, True),
            catalog.McpCatalogHeader("X-Region", "", False, False),
        ),
    )
    base.update(overrides)
    return catalog.McpCatalogRemote(**base)


def test_variables_resolve_inside_their_choices_and_allowed_hosts() -> None:
    url = resolve_remote_url(_remote(), {"api_host": "api.eu.example.com", "tenant": "acme"})
    assert url == "https://api.eu.example.com/mcp/acme"


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"api_host": "evil.test", "tenant": "acme"}, "must be one of"),
        ({"api_host": "api.example.com"}, "tenant is required"),
        ({"api_host": "api.example.com", "tenant": "a/../b"}, "characters a URL cannot take"),
        ({"api_host": "api.example.com", "tenant": "a", "port": "1"}, "Unknown variables: port"),
    ],
)
def test_variables_cannot_steer_the_endpoint(variables, message) -> None:
    remote = _remote()
    with pytest.raises(ServiceValidationError, match=message):
        resolve_remote_url(remote, variables)


def test_a_free_host_variable_is_refused_outside_allowed_hosts_unless_user_defined() -> None:
    free = (catalog.McpCatalogVariable("api_host", "", True, (), ""),)
    remote = _remote(url="https://{api_host}/mcp", variables=free)
    with pytest.raises(ServiceValidationError, match="not an address this catalog server may use"):
        resolve_remote_url(remote, {"api_host": "attacker.test"})
    self_hosted = _remote(
        url="https://{api_host}/mcp", variables=free, allowed_hosts=(), user_defined_host=True
    )
    assert (
        resolve_remote_url(self_hosted, {"api_host": "gitlab.acme.test"})
        == "https://gitlab.acme.test/mcp"
    )


def test_headers_must_be_declared_and_required_ones_present() -> None:
    remote = _remote()
    assert resolve_headers(remote, {"authorization": "Bearer t", "X-Region": ""}) == {
        "Authorization": "Bearer t"
    }
    with pytest.raises(ServiceValidationError, match="X-Other is not a header"):
        resolve_headers(remote, {"Authorization": "Bearer t", "X-Other": "1"})
    with pytest.raises(ServiceValidationError, match="Authorization is required"):
        resolve_headers(remote, {"X-Region": "eu"})
    with pytest.raises(ServiceValidationError, match="invalid value"):
        resolve_headers(remote, {"Authorization": "Bearer t\r\nX-Injected: 1"})
    oauth = _remote(auth_mode="oauth2", client_registration="dcr")
    with pytest.raises(ServiceValidationError, match="not used when you sign in with OAuth"):
        resolve_headers(oauth, {"Authorization": "Bearer t"})


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


class _CreatedConnectors:
    """Stands in for McpConnectorCommandService.create_connector."""

    def __init__(self) -> None:
        self.inputs: list[Any] = []

    def __call__(self, inp, *, scope=None):
        self.inputs.append(inp)
        return McpConnectorView(
            connector_id=f"mcp_{len(self.inputs):016x}",
            display_name=inp.display_name,
            base_url=inp.base_url,
            auth_mode=inp.auth_mode,
            catalog_item_id=inp.catalog_item_id,
            catalog_remote_id=inp.catalog_remote_id,
            catalog_item_revision=inp.catalog_item_revision,
        )


@pytest.fixture
def created(monkeypatch) -> _CreatedConnectors:
    recorder = _CreatedConnectors()
    monkeypatch.setattr(install_module.McpConnectorCommandService, "create_connector", recorder)
    return recorder


def _service(**kwargs) -> McpServerCatalogService:
    index = parse_mcp_server_catalog_index(published_index())
    return McpServerCatalogService(
        get_logger=lambda: object(), load_catalog=lambda: index, **kwargs
    )


def test_an_oauth_install_creates_a_connector_with_the_reviewed_settings(created, scope) -> None:
    result = _service().install("notion", remote_id="default", item_revision=1, scope=scope)

    (inp,) = created.inputs
    assert inp.base_url == "https://mcp.notion.com/mcp"
    assert inp.auth_mode == "oauth2"
    assert inp.headers == {}
    assert inp.oauth_config == {
        "requested_scopes": [],
        "authorization_parameters": {},
        "refresh_token_required": False,
    }
    assert (inp.catalog_item_id, inp.catalog_remote_id, inp.catalog_item_revision) == (
        "notion",
        "default",
        1,
    )
    assert inp.display_name == "Notion"
    assert result["next_step"] == "oauth_connect"
    assert result["connector"]["catalog_item_id"] == "notion"


def test_google_keeps_its_scopes_and_asks_for_the_users_own_client(created, scope) -> None:
    result = _service().install("google-gmail", remote_id="default", item_revision=1, scope=scope)

    (inp,) = created.inputs
    assert inp.base_url == "https://gmailmcp.googleapis.com/mcp/v1"
    assert inp.oauth_config["requested_scopes"] == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
    ]
    assert inp.oauth_config["authorization_parameters"]["access_type"] == "offline"
    assert inp.oauth_config["refresh_token_required"] is True
    assert result["next_step"] == "oauth_client"
    assert (
        result["setup_url"]
        == "https://developers.google.com/workspace/guides/configure-mcp-servers"
    )


def test_the_oauth_settings_the_user_edited_replace_the_catalogs(created, scope) -> None:
    _service().install(
        "google-gmail",
        remote_id="default",
        item_revision=1,
        scope=scope,
        requested_scopes=["https://www.googleapis.com/auth/gmail.readonly", " "],
        authorization_parameters={"access_type": "offline", "login_hint": "me@example.com"},
        refresh_token_required=False,
    )

    (inp,) = created.inputs
    assert inp.oauth_config == {
        "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "authorization_parameters": {"access_type": "offline", "login_hint": "me@example.com"},
        "refresh_token_required": False,
    }
    # The endpoint never comes from the request.
    assert inp.base_url == "https://gmailmcp.googleapis.com/mcp/v1"


def test_oauth_settings_are_refused_for_a_header_sign_in(created, scope) -> None:
    service = _service()
    with pytest.raises(ServiceValidationError, match="OAuth settings are only used"):
        service.install(
            "monday",
            remote_id="api-token",
            item_revision=1,
            scope=scope,
            headers={"Authorization": "Bearer t"},
            requested_scopes=["boards:read"],
        )
    assert created.inputs == []


def test_client_credentials_given_at_install_are_saved_on_the_new_connector(
    created, scope, monkeypatch
) -> None:
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        install_module.McpConnectorStore,
        "get_connector_row",
        lambda self, bl, connector_id, **kw: {"connector_id": connector_id, **kw},
    )
    service = _service(save_oauth_credentials=lambda bl, **kw: saved.append(kw))

    result = service.install(
        "google-gmail",
        remote_id="default",
        item_revision=1,
        scope=scope,
        client_id="client.apps.googleusercontent.com",
        client_secret="shh",  # pragma: allowlist secret
    )

    assert saved[0]["client_id"] == "client.apps.googleusercontent.com"
    assert saved[0]["connector_row"]["connector_id"] == result["connector"]["connector_id"]
    assert result["next_step"] == "oauth_connect"
    assert result["client_error"] == ""


def test_a_refused_client_does_not_undo_the_install(created, scope, monkeypatch) -> None:
    monkeypatch.setattr(
        install_module.McpConnectorStore,
        "get_connector_row",
        lambda self, bl, cid, **kw: {"connector_id": cid},
    )

    def refuse(bl, **kw):
        raise RuntimeError("client rejected")

    result = _service(save_oauth_credentials=refuse).install(
        "google-gmail",
        remote_id="default",
        item_revision=1,
        scope=scope,
        client_id="x",
        client_secret="y",
    )
    assert len(created.inputs) == 1
    assert result["next_step"] == "oauth_client"
    assert result["client_error"] == "client rejected"


def test_an_api_token_install_stores_the_header_as_a_bearer_connector(created, scope) -> None:
    result = _service().install(
        "monday",
        remote_id="api-token",
        item_revision=1,
        scope=scope,
        headers={"Authorization": "Bearer monday-token"},
    )
    (inp,) = created.inputs
    assert inp.auth_mode == "bearer"
    assert inp.headers == {"Authorization": "Bearer monday-token"}
    assert inp.oauth_config == {}
    assert inp.display_name == "monday.com (monday.com API token)"
    assert result["next_step"] == "sync"


def test_a_changed_entry_is_refused_with_the_current_one(created, scope) -> None:
    service = _service()
    with pytest.raises(ServiceConflictError) as conflict:
        service.install("notion", remote_id="default", item_revision=7, scope=scope)
    assert conflict.value.payload["code"] == "mcp_catalog_item_changed"
    assert conflict.value.payload["current_revision"] == 1
    assert conflict.value.payload["item"]["item_id"] == "notion"
    assert created.inputs == []


def test_unknown_entries_and_endpoints_are_refused(created, scope) -> None:
    service = _service()
    with pytest.raises(ServiceNotFoundError):
        service.install("nope", remote_id="default", item_revision=1, scope=scope)
    service = _service()
    with pytest.raises(ServiceValidationError, match="has no endpoint 'other'"):
        service.install("notion", remote_id="other", item_revision=1, scope=scope)
    service = _service()
    with pytest.raises(ServiceValidationError, match="only used for OAuth sign-in"):
        service.install(
            "monday",
            remote_id="api-token",
            item_revision=1,
            scope=scope,
            headers={"Authorization": "Bearer t"},
            client_id="x",
        )
    assert created.inputs == []


def test_install_needs_the_same_role_as_creating_a_connector(created) -> None:
    viewer = ScopeContext(
        account_id="acc_alpha",
        project_id="00000000-0000-0000-0000-000000000001",
        tenant_id="tenant_alpha",
        actor_id="viewer",
        principal_type="viewer",
    )
    service = _service()
    with pytest.raises(ServiceForbiddenError):
        service.install("notion", remote_id="default", item_revision=1, scope=viewer)
    assert created.inputs == []


def test_the_listing_shows_every_connector_made_from_an_entry(scope, monkeypatch) -> None:
    connectors = McpConnectorListView(
        connectors=[
            McpConnectorView(
                connector_id="mcp_a",
                display_name="Notion",
                catalog_item_id="notion",
                catalog_remote_id="default",
                catalog_item_revision=1,
            ),
            McpConnectorView(
                connector_id="mcp_b",
                display_name="Notion (second workspace)",
                catalog_item_id="notion",
                catalog_remote_id="default",
                catalog_item_revision=1,
            ),
            McpConnectorView(connector_id="mcp_c", display_name="Custom"),
        ],
        total=3,
    )
    monkeypatch.setattr(
        install_module.McpConnectorQueryService,
        "list_connectors",
        lambda self, scope=None: connectors,
    )

    listing = _service().list_catalog(scopes=[scope])

    items = {item["item_id"]: item for item in listing["items"]}
    assert [c["connector_id"] for c in items["notion"]["installed"]] == ["mcp_a", "mcp_b"]
    assert items["linear"]["installed"] == []
    assert listing["category_labels"]["finance"] == "Finance"
    assert items["notion"]["has_icon"] is True
    assert items["notion"]["sign_in"] == ["oauth2"]
    # Cards only: the full entry, with its endpoints, is fetched when opened.
    assert "remotes" not in items["notion"]
    assert "icon" not in items["notion"]


def test_one_entry_is_served_in_full_with_its_connections(scope, monkeypatch) -> None:
    connectors = McpConnectorListView(
        connectors=[
            McpConnectorView(
                connector_id="mcp_a",
                display_name="Notion",
                catalog_item_id="notion",
                catalog_remote_id="default",
                catalog_item_revision=1,
            ),
        ],
        total=1,
    )
    monkeypatch.setattr(
        install_module.McpConnectorQueryService,
        "list_connectors",
        lambda self, scope=None: connectors,
    )

    entry = _service().get_item("notion", scopes=[scope])

    assert entry["remotes"][0]["url"] == "https://mcp.notion.com/mcp"
    assert entry["installed"][0]["connector_id"] == "mcp_a"
    service = _service()
    with pytest.raises(ServiceNotFoundError):
        service.get_item("missing", scopes=[scope])


def test_the_install_request_takes_no_provenance_or_endpoint_from_the_client() -> None:
    with pytest.raises(ValueError):
        McpCatalogInstallRequest.model_validate(
            {"remote_id": "default", "item_revision": 1, "base_url": "https://evil.test/mcp"}
        )
    with pytest.raises(ValueError):
        McpCatalogInstallRequest.model_validate(
            {"remote_id": "default", "item_revision": 1, "catalog_item_id": "notion"}
        )


def test_the_public_create_request_cannot_claim_catalog_provenance() -> None:
    from core.application.management.requests import McpConnectorCreateRequest

    with pytest.raises(ValueError):
        McpConnectorCreateRequest.model_validate(
            {"display_name": "x", "base_url": "https://x.test/mcp", "catalog_item_id": "notion"}
        )


def test_icon_route_serves_png_with_a_locked_down_policy(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.entrypoints.api import mcp_server_catalog as routes

    monkeypatch.setattr(routes, "mcp_server_catalog_icon", lambda item_id: b"\x89PNG\r\n\x1a\nxx")
    app = FastAPI()
    app.include_router(
        routes.create_mcp_server_catalog_router(
            get_logger=MagicMock,
            request_scope=MagicMock(),
            project_query_service=MagicMock,
            service_error_response=MagicMock(),
            api_error_response=MagicMock(),
        )
    )
    response = TestClient(app).get("/api/mcp/catalog/notion/icon")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_keywords_are_searchable_names_on_the_card_and_entry() -> None:
    index = published_index()
    overlay = record(index, "atlassian")["server"]["_meta"][META]
    overlay["keywords"] = ["Jira", "Confluence", " jira ", ""]

    item = parse_mcp_server_catalog_index(index).item("atlassian")

    assert item is not None
    assert item.keywords == ("Jira", "Confluence")  # trimmed, blank and repeats dropped
    assert item.dump_card()["keywords"] == ["Jira", "Confluence"]
    assert item.dump()["keywords"] == ["Jira", "Confluence"]
    assert parse_mcp_server_catalog_index(published_index()).item("notion").keywords == ()

    overlay["keywords"] = "Jira"
    with pytest.raises(ValueError, match=r"keywords"):
        parse_mcp_server_catalog_index(index)


def test_added_at_reaches_the_card_and_the_entry_when_the_index_carries_it() -> None:
    index = published_index()
    overlay = record(index, "notion")["server"]["_meta"][META]
    overlay["added_at"] = "2026-10-20T08:00:00Z"
    item = parse_mcp_server_catalog_index(index).item("notion")
    assert item is not None
    assert item.dump_card()["added_at"] == "2026-10-20T08:00:00Z"
    assert item.dump()["added_at"] == "2026-10-20T08:00:00Z"

    del overlay["added_at"]
    without = parse_mcp_server_catalog_index(index).item("notion")
    assert without is not None
    assert without.dump_card()["added_at"] == ""


def test_field_labels_name_the_form_fields_without_changing_their_keys() -> None:
    index = published_index()
    entry = record(index, "monday")
    overlay = entry["server"]["_meta"][META]
    api_remote = next(r for r in overlay["remotes"] if r["auth"]["mode"] == "headers")
    api_remote["field_labels"] = {"authorization": "monday.com API token"}

    item = parse_mcp_server_catalog_index(index).item("monday")

    assert item is not None
    remote = item.remote(api_remote["remote_id"])
    (header,) = remote.headers
    assert header.name == "Authorization"
    assert header.label == "monday.com API token"
    assert remote.dump()["headers"][0]["label"] == "monday.com API token"
    oauth = next(r for r in item.remotes if r.auth_mode == "oauth2")
    assert all(h.label == "" for h in oauth.headers)

    api_remote["field_labels"] = ["Authorization"]
    with pytest.raises(ValueError, match=r"field_labels"):
        parse_mcp_server_catalog_index(index)


def test_accessible_project_ids_reads_every_page_once() -> None:
    from types import SimpleNamespace

    from core.application.management.mcp_connector_common import accessible_project_ids

    pages = {
        1: SimpleNamespace(
            projects=[SimpleNamespace(id="p1"), SimpleNamespace(id="p2")], total_pages=2
        ),
        2: SimpleNamespace(
            projects=[SimpleNamespace(id="p2"), SimpleNamespace(id="")], total_pages=2
        ),
    }
    calls: list[int] = []

    def list_projects(*, page: int, per_page: int, scope: Any) -> Any:
        calls.append(page)
        return pages[page]

    assert accessible_project_ids(list_projects, scope=object()) == ["p1", "p2"]
    assert calls == [1, 2]


def test_connectors_of_several_projects_are_read_each_with_its_own_scope(monkeypatch) -> None:
    seen: list[str] = []

    def list_connectors(self, *, scope=None):
        seen.append(scope.project_id)
        return McpConnectorListView(
            connectors=[
                McpConnectorView(
                    connector_id=f"mcp_{scope.project_id}", project_id=scope.project_id
                )
            ],
            total=1,
        )

    monkeypatch.setattr(install_module.McpConnectorQueryService, "list_connectors", list_connectors)
    scopes = [
        ScopeContext(account_id="acc_alpha", project_id=pid, principal_type="admin")
        for pid in ("p1", "p2")
    ]

    listing = install_module.McpConnectorQueryService(
        get_logger=lambda: object()
    ).list_connectors_in_projects(scopes=scopes)

    assert seen == ["p1", "p2"]
    assert [c.project_id for c in listing.connectors] == ["p1", "p2"]
