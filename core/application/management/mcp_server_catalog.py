"""Read-only MCP server catalog: index contract, validation, cache, and icons.

The index has the shape of the official MCP Registry's ``GET /v0.1/servers``
response, so the Flow Steward catalog (github.com/Flow-Steward/mcps) and a
company's private registry are read the same way. What a reviewer verified —
the hosts an endpoint may point at, how a user signs in, the vendor page that
backs an Official badge — sits in each record under
``server._meta["io.github.flow-steward/catalog"]``. A record without that block
was never reviewed and is not offered for install.

Cache behaviour is the static catalogs' one contract (``index_cache``: every
read revalidates the index with the host, a ``304`` reuses the parsed index,
all-or-nothing validation, the last good index serves marked stale when the
host fails, no persistence), in a separate cache with separate state. The only
shared code is ``core/infrastructure/static_catalog``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from core.application.extensions.url_safety import (
    assert_safe_remote_https_url,
    open_pinned_url,
)
from core.infrastructure.static_catalog import (
    MAX_CATALOG_INDEX_BYTES,
    MAX_CATALOG_ITEMS,
    mcp_server_catalog_url,
)
from core.infrastructure.static_catalog.http_fetch import (
    CatalogFetchError,
    CatalogUrlGuard,
    FetchedCatalogIndex,
    fetch_catalog_artifact,
    fetch_catalog_index_json,
)
from core.infrastructure.static_catalog.icons import (
    CatalogIcon,
    CatalogIconCache,
    IconError,
    parse_catalog_icon,
)
from core.infrastructure.static_catalog.index_cache import (
    CatalogIndexCache,
    CatalogIndexUnavailableError,
    CatalogValidators,
)

_URL_GUARD = CatalogUrlGuard(
    assert_safe_remote_https_url=assert_safe_remote_https_url,
    open_pinned_url=open_pinned_url,
)

SCHEMA_VERSION = "mcp_server_catalog/v1"
CATALOG_META_KEY = "io.github.flow-steward/catalog"
OFFICIAL_META_KEY = "io.modelcontextprotocol.registry/official"
AUTH_MODES = frozenset({"none", "headers", "oauth2"})
CLIENT_REGISTRATIONS = frozenset({"dcr", "client_metadata", "user_supplied"})
LISTED_STATUSES = frozenset({"active", "deprecated"})
_FETCH_PURPOSE = "MCP server catalog index"
_ICON_PURPOSE = "MCP server catalog icon"
_ITEM_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_REMOTE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_HOST_PATTERN = re.compile(r"^(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+$")
_URL_VARIABLE = re.compile(r"\{(\w+)\}", re.ASCII)
_MAX_TEXT = 4000
_MAX_KEYWORDS = 20


class McpServerCatalogError(RuntimeError):
    """The configured MCP server catalog cannot be served."""

    def __init__(self, message: str, *, code: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def detail(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class McpCatalogVariable:
    """A ``{name}`` in an endpoint URL that the user fills in."""

    name: str
    description: str
    required: bool
    choices: tuple[str, ...]
    default: str
    # What the install form calls the field ("Freshdesk subdomain"); the reviewer
    # sets it in the catalog, the name stays the key the URL uses.
    label: str = ""


@dataclass(frozen=True, slots=True)
class McpCatalogHeader:
    """A request header the server record asks for (an API key, a tenant id)."""

    name: str
    description: str
    required: bool
    secret: bool
    label: str = ""


@dataclass(frozen=True, slots=True)
class McpCatalogRemote:
    """One endpoint of a catalog server and how a user signs in to it."""

    remote_id: str
    label: str
    url: str
    allowed_hosts: tuple[str, ...]
    user_defined_host: bool
    auth_mode: str
    client_registration: str
    requested_scopes: tuple[str, ...]
    authorization_parameters: tuple[tuple[str, str], ...]
    refresh_token_required: bool
    setup_url: str
    variables: tuple[McpCatalogVariable, ...]
    headers: tuple[McpCatalogHeader, ...]

    def dump(self) -> dict[str, Any]:
        return {
            "remote_id": self.remote_id,
            "label": self.label,
            "url": self.url,
            "allowed_hosts": list(self.allowed_hosts),
            "user_defined_host": self.user_defined_host,
            "auth": {
                "mode": self.auth_mode,
                "client_registration": self.client_registration,
                "requested_scopes": list(self.requested_scopes),
                "authorization_parameters": dict(self.authorization_parameters),
                "refresh_token_required": self.refresh_token_required,
                "setup_url": self.setup_url,
            },
            "variables": [
                {
                    "name": v.name,
                    "description": v.description,
                    "required": v.required,
                    "choices": list(v.choices),
                    "default": v.default,
                    "label": v.label,
                }
                for v in self.variables
            ],
            "headers": [
                {
                    "name": h.name,
                    "description": h.description,
                    "required": h.required,
                    "secret": h.secret,
                    "label": h.label,
                }
                for h in self.headers
            ],
        }


@dataclass(frozen=True, slots=True)
class McpCatalogItem:
    """One reviewed MCP server, as the index describes it."""

    item_id: str
    revision: int
    status: str
    server_name: str
    server_version: str
    title: str
    summary: str
    description: str
    category: str
    website_url: str
    help_url: str
    preview_note: str
    official_vendor: str
    official_evidence_url: str
    icon: CatalogIcon | None
    remotes: tuple[McpCatalogRemote, ...]
    entry_url: str
    updated_at: str
    # Other names people search for: products under one vendor entry ("Jira" for
    # Atlassian), former names ("Sendinblue"), the vendor of a product.
    keywords: tuple[str, ...] = ()
    # When the server first appeared in the catalog (optional, like ``updated_at``):
    # the cards sort by it and wear New for their first weeks.
    added_at: str = ""

    def remote(self, remote_id: str) -> McpCatalogRemote | None:
        normalized = str(remote_id or "").strip()
        return next((r for r in self.remotes if r.remote_id == normalized), None)

    def dump(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "revision": self.revision,
            "status": self.status,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "title": self.title,
            "summary": self.summary,
            "description": self.description,
            "category": self.category,
            "website_url": self.website_url,
            "help_url": self.help_url,
            "preview_note": self.preview_note,
            "official": (
                {"vendor": self.official_vendor, "evidence_url": self.official_evidence_url}
                if self.official_vendor
                else None
            ),
            "has_icon": self.icon is not None,
            "remotes": [remote.dump() for remote in self.remotes],
            "entry_url": self.entry_url,
            "updated_at": self.updated_at,
            "added_at": self.added_at,
            "keywords": list(self.keywords),
        }

    def dump_card(self) -> dict[str, Any]:
        """What a catalog card and the filters need; the full entry is fetched on open."""
        return {
            "item_id": self.item_id,
            "revision": self.revision,
            "status": self.status,
            "server_name": self.server_name,
            "title": self.title,
            "summary": self.summary,
            "category": self.category,
            "preview_note": self.preview_note,
            "official": (
                {"vendor": self.official_vendor, "evidence_url": self.official_evidence_url}
                if self.official_vendor
                else None
            ),
            "has_icon": self.icon is not None,
            "sign_in": sorted({remote.auth_mode for remote in self.remotes}),
            "keywords": list(self.keywords),
            "updated_at": self.updated_at,
            "added_at": self.added_at,
        }


@dataclass(frozen=True, slots=True)
class McpCatalogIndex:
    schema_version: str
    catalog_revision: str
    published_at: str
    items: tuple[McpCatalogItem, ...]
    category_labels: tuple[tuple[str, str], ...] = ()
    #: Registry records without the catalog block (a plain registry); counted only.
    skipped_count: int = 0

    def item(self, item_id: str) -> McpCatalogItem | None:
        normalized = str(item_id or "").strip()
        return next((entry for entry in self.items if entry.item_id == normalized), None)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _fetch_index(url: str, validators: CatalogValidators) -> FetchedCatalogIndex:
    return fetch_catalog_index_json(
        url,
        guard=_URL_GUARD,
        max_bytes=MAX_CATALOG_INDEX_BYTES,
        purpose=_FETCH_PURPOSE,
        etag=validators.etag,
        last_modified=validators.last_modified,
    )


#: Everything one process remembers about the catalog. Never persisted. The
#: lambdas resolve this module's names on every call, so tests can replace them.
_INDEX: CatalogIndexCache[McpCatalogIndex] = CatalogIndexCache(
    fetch=lambda url, validators: _fetch_index(url, validators),
    parse=lambda payload: parse_mcp_server_catalog_index(payload),
    clock=lambda: _now(),
)
_ICONS = CatalogIconCache()


def reset_mcp_server_catalog_cache() -> None:
    """Drop the process-local index and icons. Used by tests."""
    _INDEX.reset()
    _ICONS.clear()


def _catalog_url() -> str:
    url = mcp_server_catalog_url()
    if not url:
        raise McpServerCatalogError(
            "MCP server catalog is not configured.", code="mcp_catalog_not_configured"
        )
    return url


def load_mcp_server_catalog() -> McpCatalogIndex:
    """Return the current index, revalidated with the catalog host on this call."""
    try:
        return _INDEX.load(_catalog_url())
    except CatalogIndexUnavailableError as exc:
        raise McpServerCatalogError(
            "MCP server catalog is unavailable.", code="mcp_catalog_unavailable"
        ) from exc


def mcp_server_catalog_availability() -> dict[str, Any]:
    """Describe the catalog for the UI without raising when it is unusable."""
    base = {"kind": "mcp_server_catalog_availability", "item_count": 0, "stale": False}
    if not mcp_server_catalog_url():
        return {
            **base,
            "configured": False,
            "available": False,
            "reason": "mcp_catalog_not_configured",
        }
    try:
        index = load_mcp_server_catalog()
    except McpServerCatalogError as exc:
        return {
            **base,
            "configured": True,
            "available": False,
            "reason": exc.code,
            "message": str(exc),
        }
    return {
        **base,
        "configured": True,
        "available": True,
        "schema_version": index.schema_version,
        "catalog_revision": index.catalog_revision,
        "published_at": index.published_at,
        "item_count": len(index.items),
        "skipped_count": index.skipped_count,
        **_INDEX.freshness(),
    }


def mcp_server_catalog_icon(item_id: str) -> bytes:
    """The item's icon, fetched once and verified against the index digest.

    The icon URL carries the digest the listing just read, so the index that
    listing read names it; only an item it does not list asks the host again.
    """
    index = _INDEX.current(_catalog_url())
    item = index.item(item_id) if index is not None else None
    if item is None:
        item = load_mcp_server_catalog().item(item_id)
    if item is None or item.icon is None:
        raise McpServerCatalogError(
            "This catalog server has no icon.", code="mcp_catalog_icon_not_found", status_code=404
        )
    try:
        return _ICONS.fetch(
            item.icon, fetch=fetch_catalog_artifact, guard=_URL_GUARD, purpose=_ICON_PURPOSE
        )
    except CatalogFetchError as exc:
        raise McpServerCatalogError(
            "The catalog icon could not be fetched.", code="mcp_catalog_icon_unavailable"
        ) from exc
    except IconError as exc:
        raise McpServerCatalogError(
            "The catalog icon is not a PNG.", code="mcp_catalog_icon_unavailable"
        ) from exc


# ---------------------------------------------------------------------------
# Index contract
# ---------------------------------------------------------------------------


def _text(value: Any, *, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip() if isinstance(value, (str, int, float)) else ""
    return text[:limit]


def _required_text(value: Any, where: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f"{where} is required")
    return text


def _https(value: Any, where: str, *, optional: bool = True) -> str:
    text = _text(value)
    if not text:
        if optional:
            return ""
        raise ValueError(f"{where} is required")
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{where} must be an absolute https URL")
    return text


def _bool(value: Any, where: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be true or false")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{where} must be a list of strings")
    return tuple(v.strip() for v in value)


def host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    """Exact host names, or ``*.example.com`` for any subdomain (not the apex)."""
    normalized = str(host or "").lower().rstrip(".")
    for pattern in patterns:
        if pattern.startswith("*."):
            if normalized.endswith(pattern[1:]):
                return True
        elif normalized == pattern:
            return True
    return False


def _variables(raw: Any, where: str) -> tuple[McpCatalogVariable, ...]:
    if raw in (None, {}):
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{where}.variables must be an object")
    variables = []
    for name, spec in sorted(raw.items()):
        spec = spec if isinstance(spec, dict) else {}
        variables.append(
            McpCatalogVariable(
                name=_required_text(name, f"{where}.variables"),
                description=_text(spec.get("description")),
                required=bool(spec.get("isRequired")),
                choices=tuple(_text(c) for c in spec.get("choices") or [] if _text(c)),
                default=_text(spec.get("default")),
            )
        )
    return tuple(variables)


def _headers(raw: Any, where: str) -> tuple[McpCatalogHeader, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{where}.headers must be a list")
    headers = []
    seen: set[str] = set()
    for position, spec in enumerate(raw):
        if not isinstance(spec, dict):
            raise ValueError(f"{where}.headers[{position}] must be an object")
        name = _required_text(spec.get("name"), f"{where}.headers[{position}].name")
        if name.lower() in seen:
            raise ValueError(f"{where}.headers: {name} is declared twice")
        seen.add(name.lower())
        headers.append(
            McpCatalogHeader(
                name=name,
                description=_text(spec.get("description")),
                required=bool(spec.get("isRequired")),
                secret=bool(spec.get("isSecret")),
            )
        )
    return tuple(headers)


def _remote(raw: Any, where: str, upstream: dict[str, dict[str, Any]]) -> McpCatalogRemote:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    remote_id = _required_text(raw.get("remote_id"), f"{where}.remote_id")
    if not _REMOTE_ID.match(remote_id):
        raise ValueError(f"{where}.remote_id is not a catalog id")
    url = _required_text(raw.get("url"), f"{where}.url")
    record = upstream.get(url)
    if record is None:
        raise ValueError(f"{where}.url is not a streamable-http remote of the server record")
    _https(_URL_VARIABLE.sub("x", url), f"{where}.url", optional=False)
    allowed_hosts = _strings(raw.get("allowed_hosts"), f"{where}.allowed_hosts")
    if not all(_HOST_PATTERN.match(h) for h in allowed_hosts):
        raise ValueError(f"{where}.allowed_hosts must list host names")
    user_defined_host = _bool(raw.get("user_defined_host"), f"{where}.user_defined_host")
    if not user_defined_host and not allowed_hosts:
        raise ValueError(f"{where}.allowed_hosts is required unless the user defines the host")
    variables = _variables(record.get("variables"), where)
    declared = {v.name for v in variables}
    for name in _URL_VARIABLE.findall(url):
        if name not in declared:
            raise ValueError(f"{where}.url uses an undeclared variable {name}")
    auth = raw.get("auth")
    if not isinstance(auth, dict):
        raise ValueError(f"{where}.auth must be an object")
    mode = _text(auth.get("mode"))
    if mode not in AUTH_MODES:
        raise ValueError(f"{where}.auth.mode must be one of {sorted(AUTH_MODES)}")
    registration = _text(auth.get("client_registration"))
    parameters = auth.get("authorization_parameters") or {}
    if not isinstance(parameters, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parameters.items()
    ):
        raise ValueError(f"{where}.auth.authorization_parameters must map names to strings")
    if mode == "oauth2" and registration not in CLIENT_REGISTRATIONS:
        raise ValueError(
            f"{where}.auth.client_registration must be one of {sorted(CLIENT_REGISTRATIONS)}"
        )
    headers = _headers(record.get("headers"), where)
    labels = _field_labels(raw.get("field_labels"), where)
    variables = tuple(
        dataclasses.replace(v, label=labels.get(v.name.lower(), "")) for v in variables
    )
    headers = tuple(dataclasses.replace(h, label=labels.get(h.name.lower(), "")) for h in headers)
    if mode == "headers" and not headers:
        raise ValueError(f"{where}: auth.mode headers needs headers in the server record")
    return McpCatalogRemote(
        remote_id=remote_id,
        label=_text(raw.get("label"), limit=200) or remote_id,
        url=url,
        allowed_hosts=allowed_hosts,
        user_defined_host=user_defined_host,
        auth_mode=mode,
        client_registration=registration if mode == "oauth2" else "",
        requested_scopes=_strings(auth.get("requested_scopes"), f"{where}.auth.requested_scopes")
        if mode == "oauth2"
        else (),
        authorization_parameters=tuple(sorted(parameters.items())) if mode == "oauth2" else (),
        refresh_token_required=(
            _bool(auth.get("refresh_token_required"), f"{where}.auth.refresh_token_required")
            if mode == "oauth2"
            else False
        ),
        setup_url=_https(auth.get("setup_url"), f"{where}.auth.setup_url"),
        variables=variables,
        headers=headers,
    )


def _field_labels(raw: Any, where: str) -> dict[str, str]:
    """Reviewer-written names for the fields a user fills in, keyed by variable or header name."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}.field_labels must map field names to labels")
    return {
        _text(name).lower(): _text(label, limit=120) for name, label in raw.items() if _text(name)
    }


def _icon(raw: Any, where: str) -> CatalogIcon | None:
    if raw in (None, {}):
        return None
    return parse_catalog_icon(raw, f"{where}.icon")


def _item(record: Any, where: str) -> McpCatalogItem | None:
    """One ``servers[]`` record; None when it carries no reviewed block."""
    if not isinstance(record, dict):
        raise ValueError(f"{where} must be an object")
    server = record.get("server")
    if not isinstance(server, dict):
        raise ValueError(f"{where}.server must be an object")
    raw_meta = server.get("_meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    overlay = meta.get(CATALOG_META_KEY)
    if overlay is None:
        return None
    if not isinstance(overlay, dict):
        raise ValueError(f"{where}: the catalog block must be an object")
    where = f"{where}[{_text(overlay.get('id')) or '?'}]"
    item_id = _required_text(overlay.get("id"), f"{where}.id")
    if not _ITEM_ID.match(item_id):
        raise ValueError(f"{where}.id is not a catalog id")
    revision = overlay.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError(f"{where}.revision must be a whole number from 1")
    raw_registry_meta = record.get("_meta")
    registry_meta: dict[str, Any] = raw_registry_meta if isinstance(raw_registry_meta, dict) else {}
    official_registry = registry_meta.get(OFFICIAL_META_KEY)
    status = _text(overlay.get("status")) or "active"
    if isinstance(official_registry, dict) and _text(official_registry.get("status")):
        status = _text(official_registry.get("status"))
    upstream = {
        _text(remote.get("url")): remote
        for remote in server.get("remotes") or []
        if isinstance(remote, dict) and remote.get("type") == "streamable-http"
    }
    raw_remotes = overlay.get("remotes")
    if not isinstance(raw_remotes, list) or not raw_remotes:
        raise ValueError(f"{where}.remotes must list at least one endpoint")
    remotes = tuple(
        _remote(raw, f"{where}.remotes[{position}]", upstream)
        for position, raw in enumerate(raw_remotes)
    )
    if len({remote.remote_id for remote in remotes}) != len(remotes):
        raise ValueError(f"{where}.remotes repeat a remote_id")
    title = _required_text(overlay.get("title"), f"{where}.title")
    official = overlay.get("official")
    vendor = evidence = ""
    if isinstance(official, dict):
        vendor = _text(official.get("vendor"), limit=200)
        evidence = _https(official.get("evidence_url"), f"{where}.official.evidence_url")
        if not (vendor and evidence):
            vendor = evidence = ""
    description = _text(overlay.get("description"))
    return McpCatalogItem(
        item_id=item_id,
        revision=revision,
        status=status,
        server_name=_required_text(server.get("name"), f"{where}.server.name"),
        server_version=_text(server.get("version")),
        title=title,
        summary=_text(overlay.get("summary"), limit=300) or description[:160],
        description=description,
        category=_text(overlay.get("category"), limit=100),
        website_url=_https(overlay.get("website_url"), f"{where}.website_url"),
        help_url=_https(overlay.get("help_url"), f"{where}.help_url"),
        preview_note=_text(overlay.get("preview_note"), limit=300),
        official_vendor=vendor,
        official_evidence_url=evidence,
        icon=_icon(overlay.get("icon"), where),
        remotes=remotes,
        entry_url=_https(overlay.get("entry_url"), f"{where}.entry_url"),
        updated_at=_text(overlay.get("updated_at"), limit=40),
        added_at=_text(overlay.get("added_at"), limit=40),
        keywords=_keywords(overlay.get("keywords"), where),
    )


def _keywords(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > _MAX_KEYWORDS:
        raise ValueError(f"{where}.keywords must list at most {_MAX_KEYWORDS} search terms")
    out: list[str] = []
    for value in raw:
        keyword = _text(value, limit=60)
        if keyword and keyword.lower() not in {k.lower() for k in out}:
            out.append(keyword)
    return tuple(out)


def _category_labels(raw: Any) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError("category_labels must be an object of id to label")
    return tuple(
        sorted(
            (_text(key, limit=100), _text(value, limit=120))
            for key, value in raw.items()
            if _text(key) and isinstance(value, str) and value.strip()
        )
    )


def parse_mcp_server_catalog_index(payload: Any) -> McpCatalogIndex:
    """Validate a decoded index document, or raise ``ValueError``.

    A Flow Steward catalog declares ``schema_version``; a plain Registry
    response does not, and is read the same way. Records without the catalog
    block are skipped and counted; a deleted server is never listed.
    """
    if not isinstance(payload, dict):
        raise ValueError("catalog index must be an object")
    schema_version = _text(payload.get("schema_version"))
    if schema_version and schema_version != SCHEMA_VERSION:
        raise ValueError(f"catalog index schema_version must be '{SCHEMA_VERSION}'")
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list):
        raise ValueError("catalog index servers must be a list")
    if len(raw_servers) > MAX_CATALOG_ITEMS:
        raise ValueError(f"catalog index carries more than {MAX_CATALOG_ITEMS} servers")
    items: list[McpCatalogItem] = []
    seen: set[str] = set()
    skipped = 0
    for position, record in enumerate(raw_servers):
        item = _item(record, f"servers[{position}]")
        if item is None:
            skipped += 1
            continue
        if item.item_id in seen:
            raise ValueError(f"servers[{position}]: duplicate catalog id '{item.item_id}'")
        seen.add(item.item_id)
        if item.status in LISTED_STATUSES:
            items.append(item)
    published_at = _text(payload.get("published_at"), limit=40)
    revision = _text(payload.get("catalog_revision"), limit=100)
    if not revision:
        revision = hashlib.sha256(
            repr(sorted((i.item_id, i.revision) for i in items)).encode()
        ).hexdigest()[:12]
    return McpCatalogIndex(
        schema_version=schema_version or "registry/v0.1",
        catalog_revision=revision,
        published_at=published_at,
        items=tuple(items),
        category_labels=_category_labels(payload.get("category_labels")),
        skipped_count=skipped,
    )


__all__ = [
    "AUTH_MODES",
    "CATALOG_META_KEY",
    "SCHEMA_VERSION",
    "McpCatalogHeader",
    "McpCatalogIndex",
    "McpCatalogItem",
    "McpCatalogRemote",
    "McpCatalogVariable",
    "McpServerCatalogError",
    "host_allowed",
    "load_mcp_server_catalog",
    "mcp_server_catalog_availability",
    "mcp_server_catalog_icon",
    "parse_mcp_server_catalog_index",
    "reset_mcp_server_catalog_cache",
]
