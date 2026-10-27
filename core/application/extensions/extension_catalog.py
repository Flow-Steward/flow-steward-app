"""Read-only extension catalog: index contract, validation, and cache.

Independent of the workflow template catalog in every respect that matters —
its own schema version, its own item shape, its own process-local cache — so
the two can be published, versioned, and broken separately. The only shared
code is ``core/infrastructure/static_catalog``: the bounded HTTP helper and the
revalidating index cache.

Cache behaviour is the static catalogs' one contract (``index_cache``): every
read revalidates the index with the host, a ``304`` reuses the parsed index,
validation is all-or-nothing, the last good index serves (marked stale) when
the host fails, nothing is persisted. This catalog keeps its own state.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, urlparse

from core.application.extensions.default_icon import is_default_extension_icon
from core.application.extensions.semver import normalize_semver
from core.application.extensions.url_safety import (
    assert_safe_remote_https_url,
    open_pinned_url,
)
from core.infrastructure.extension_runtime.runtime_target import (
    SUPPORTED_EXTENSION_RUNTIME_TARGETS,
    extension_runtime_target,
)
from core.infrastructure.static_catalog import (
    MAX_CATALOG_INDEX_BYTES,
    MAX_CATALOG_ITEMS,
    MAX_EXTENSION_RELEASE_ZIP_BYTES,
    extension_catalog_url,
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

#: This area's binding of the repository's SSRF guard onto the bounded catalog
#: transport. Infrastructure owns the transport and must not import the
#: application layer, so the guard is supplied here rather than imported there.
_URL_GUARD = CatalogUrlGuard(
    assert_safe_remote_https_url=assert_safe_remote_https_url,
    open_pinned_url=open_pinned_url,
)


def fetch_extension_release_artifact(
    url: str,
    *,
    expected_bytes: int,
    expected_sha256: str,
    purpose: str,
) -> bytes:
    """Fetch and verify one extension catalog artifact under this area's guard."""
    return fetch_catalog_artifact(
        url,
        guard=_URL_GUARD,
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
        max_bytes=MAX_EXTENSION_RELEASE_ZIP_BYTES,
        purpose=purpose,
    )


SCHEMA_VERSION = "extension_catalog/v1"
PUBLICATION_STATUSES = frozenset({"published", "deprecated"})
VERIFICATION_STATUSES = frozenset({"verified", "unverified"})
_FETCH_PURPOSE = "extension catalog index"
_ICON_PURPOSE = "extension catalog icon"
_SHA256_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


class ExtensionCatalogError(RuntimeError):
    """The configured extension catalog cannot be served."""

    def __init__(self, message: str, *, code: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def detail(self) -> dict[str, Any]:
        # `message` is where the frontend HTTP client reads a reason, as it does for
        # the workflow import errors; without it the operator saw only the status.
        return {"error": str(self), "code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class ExtensionCatalogItem:
    """One published extension release, as the index describes it."""

    extension_id: str
    version: str
    name: str
    summary: str
    description: str
    categories: tuple[str, ...]
    capabilities: tuple[str, ...]
    repository_url: str
    release_zip_url: str
    release_zip_bytes: int
    release_zip_sha256: str
    permissions_summary: tuple[str, ...]
    setup_summary: str
    publication_status: str
    verification_status: str
    #: The extension's logo, the bundle's icon.png as the catalog published it.
    icon: CatalogIcon
    compatible_min_version: str = ""
    compatible_max_version: str = ""
    #: Runtime targets with their own release archive. Empty for a portable
    #: extension; when set, ``release_zip_*`` already name this host's archive.
    runtime_targets: tuple[str, ...] = ()
    #: False when the entry ships per-target archives and none is for this host.
    available_for_host: bool = True
    #: When the extension entered the catalog, and when this release did.
    added_at: str = ""
    updated_at: str = ""
    #: Free search words the index publishes; matched by search, never a facet.
    tags: tuple[str, ...] = ()

    @property
    def _own_logo(self) -> bool:
        """Whether the published icon is the author's, not the scaffold default."""
        return not is_default_extension_icon(sha256=self.icon.sha256)

    def as_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "version": self.version,
            "name": self.name,
            "summary": self.summary,
            "description": self.description,
            "categories": list(self.categories),
            "capabilities": list(self.capabilities),
            "repository_url": self.repository_url,
            "release_zip_url": self.release_zip_url,
            "release_zip_bytes": self.release_zip_bytes,
            "release_zip_sha256": self.release_zip_sha256,
            "permissions_summary": list(self.permissions_summary),
            "setup_summary": self.setup_summary,
            "compatible_flow_steward": {
                "min": self.compatible_min_version,
                "max": self.compatible_max_version,
            },
            "publication_status": self.publication_status,
            "verification_status": self.verification_status,
            "runtime_targets": list(self.runtime_targets),
            "available_for_host": self.available_for_host,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
            # The browser loads the logo from Core, never from the catalog host;
            # the digest in the URL lets a new logo replace a cached one.
            # The scaffold's default glyph is no logo of its own (default_icon).
            "has_icon": self._own_logo,
            "icon_url": (
                f"/api/extensions/catalog/{quote(self.extension_id, safe='')}/icon"
                f"?v={self.icon.sha256[:16]}"
                if self._own_logo
                else ""
            ),
        }


#: Names for the capability ids an index lists. The ids are the extension
#: contract's own, so Core names them, the way it names nothing else in an
#: index; an id missing here is shown as itself.
CAPABILITY_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "tool": "Agent tools",
        "action": "Workflow actions",
        "query": "Data queries",
        "dataset_connector": "Dataset connectors",
        "llm_provider": "AI model providers",
        "notify": "Notifications",
        "workflow_trigger": "Workflow triggers",
        "ingress_event": "Incoming events",
        "artifact_bundle": "Bundled assets",
    }
)

#: Names for the permission scopes an extension asks for, as Core grants them.
PERMISSION_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "extension:invoke": "Run its own operations",
        "artifact:read": "Read artifacts",
        "artifact:write": "Create artifacts",
        "read:projects": "Read projects",
        "notify:dispatch": "Send notifications",
        "event:subscribe": "Receive events",
        "read:events": "Read events",
    }
)


@dataclass(frozen=True, slots=True)
class ExtensionCatalogIndex:
    """A validated index document."""

    schema_version: str
    catalog_revision: str
    published_at: str
    items: tuple[ExtensionCatalogItem, ...]
    #: Names for the category ids; the list is shared with the workflow catalog.
    category_labels: tuple[tuple[str, str], ...] = ()

    def item(self, extension_id: str) -> ExtensionCatalogItem | None:
        normalized = str(extension_id or "").strip()
        for entry in self.items:
            if entry.extension_id == normalized:
                return entry
        return None


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
_INDEX: CatalogIndexCache[ExtensionCatalogIndex] = CatalogIndexCache(
    fetch=lambda url, validators: _fetch_index(url, validators),
    parse=lambda payload: parse_extension_catalog_index(payload),
    clock=lambda: _now(),
)
_ICONS = CatalogIconCache()


def reset_extension_catalog_cache() -> None:
    """Drop the process-local index and icons. Used by tests."""
    _INDEX.reset()
    _ICONS.clear()


def _catalog_url() -> str:
    url = extension_catalog_url()
    if not url:
        raise ExtensionCatalogError(
            "Extension catalog is not configured.",
            code="extension_catalog_not_configured",
        )
    return url


def load_extension_catalog() -> ExtensionCatalogIndex:
    """Return the current index, revalidated with the catalog host on this call.

    Serves the last good index, marked stale, when the host fails; raises
    :class:`ExtensionCatalogError` only when there is nothing to serve.
    """
    try:
        return _INDEX.load(_catalog_url())
    except CatalogIndexUnavailableError as exc:
        raise ExtensionCatalogError(
            "Extension catalog is unavailable.",
            code="extension_catalog_unavailable",
        ) from exc


def cached_extension_catalog_names() -> dict[str, str]:
    """Each listed extension's name from the index this process holds, never fetched.

    Empty when no catalog is configured or none has been read yet. For a caller
    on a hot path, such as the workflow list naming a missing extension, which
    must not wait on the catalog host.
    """
    url = extension_catalog_url()
    if not url:
        return {}
    index = _INDEX.current(url)
    if index is None:
        return {}
    return {item.extension_id: item.name for item in index.items if item.name}


def extension_catalog_availability(index: ExtensionCatalogIndex | None = None) -> dict[str, Any]:
    """Describe the catalog for the UI without raising when it is unusable.

    ``index`` is one the caller has just loaded; it is described as is rather
    than read again.
    """
    if not extension_catalog_url():
        return {
            "kind": "extension_catalog_availability",
            "configured": False,
            "available": False,
            "reason": "extension_catalog_not_configured",
            "item_count": 0,
            "stale": False,
        }
    if index is None:
        try:
            index = load_extension_catalog()
        except ExtensionCatalogError as exc:
            return {
                "kind": "extension_catalog_availability",
                "configured": True,
                "available": False,
                "reason": exc.code,
                "message": str(exc),
                "item_count": 0,
                "stale": False,
            }
    return {
        "kind": "extension_catalog_availability",
        "configured": True,
        "available": True,
        "schema_version": index.schema_version,
        "catalog_revision": index.catalog_revision,
        "published_at": index.published_at,
        "item_count": len(index.items),
        **_INDEX.freshness(),
    }


def extension_catalog_icon(extension_id: str) -> bytes:
    """The entry's logo, fetched once and verified against the index digest.

    The icon URL carries the digest the listing just read, so the index that
    listing read names it; only an entry it does not list asks the host again.
    """
    index = _INDEX.current(_catalog_url())
    item = index.item(extension_id) if index is not None else None
    if item is None:
        item = load_extension_catalog().item(extension_id)
    if item is None:
        raise ExtensionCatalogError(
            "This catalog extension is not listed.",
            code="extension_catalog_icon_not_found",
            status_code=404,
        )
    try:
        return _ICONS.fetch(
            item.icon, fetch=fetch_catalog_artifact, guard=_URL_GUARD, purpose=_ICON_PURPOSE
        )
    except CatalogFetchError as exc:
        raise ExtensionCatalogError(
            "The catalog icon could not be fetched.", code="extension_catalog_icon_unavailable"
        ) from exc
    except IconError as exc:
        raise ExtensionCatalogError(
            "The catalog icon is not a PNG.", code="extension_catalog_icon_unavailable"
        ) from exc


# ---------------------------------------------------------------------------
# Index contract
# ---------------------------------------------------------------------------


def _host_runtime_target() -> str:
    """This process's runtime target, or '' where none is supported (a dev Mac)."""
    try:
        return extension_runtime_target()
    except ValueError:
        return ""


def parse_extension_catalog_index(
    payload: Any, *, runtime_target: str | None = None
) -> ExtensionCatalogIndex:
    """Validate a decoded index document, or raise ``ValueError``.

    An entry may publish one archive per runtime target (an extension that ships
    a native executable). Its ``release_zip_*`` fields are then resolved to the
    archive for ``runtime_target`` — this host's, by default — so listing,
    pinning and install all name the one archive this host can run.
    """
    host_target = _host_runtime_target() if runtime_target is None else runtime_target
    if not isinstance(payload, dict):
        raise ValueError("catalog index must be an object")
    schema_version = _text(payload.get("schema_version"))
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"catalog index schema_version must be '{SCHEMA_VERSION}'")
    catalog_revision = _required_text(payload.get("catalog_revision"), "catalog_revision")
    published_at = _required_text(payload.get("published_at"), "published_at")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("catalog index items must be a list")
    if len(raw_items) > MAX_CATALOG_ITEMS:
        raise ValueError(f"catalog index carries more than {MAX_CATALOG_ITEMS} items")

    items: list[ExtensionCatalogItem] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_items):
        item = _parse_item(raw, position=position, runtime_target=host_target)
        if item.extension_id in seen:
            raise ValueError(f"items[{position}]: duplicate extension_id '{item.extension_id}'")
        seen.add(item.extension_id)
        items.append(item)

    return ExtensionCatalogIndex(
        schema_version=schema_version,
        catalog_revision=catalog_revision,
        published_at=published_at,
        items=tuple(items),
        category_labels=_category_labels(payload.get("category_labels")),
    )


#: A category name is a short label on a chip, not prose.
_MAX_CATEGORY_LABEL_LENGTH = 120


def _category_labels(raw: Any) -> tuple[tuple[str, str], ...]:
    """The optional category id -> name map; ids stay what filters match on."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError("category_labels must be an object of id to label")
    labels: list[tuple[str, str]] = []
    for key, value in raw.items():
        category_id = _text(key)
        if not isinstance(value, str):
            raise ValueError(f"category_labels.{category_id} must be a string")
        label = value.strip()
        if not category_id or not label:
            continue
        if len(label) > _MAX_CATEGORY_LABEL_LENGTH:
            raise ValueError(
                f"category_labels.{category_id} is longer than "
                f"{_MAX_CATEGORY_LABEL_LENGTH} characters"
            )
        labels.append((category_id, label))
    return tuple(sorted(labels))


def _optional_timestamp(value: Any, field_name: str) -> str:
    """An optional ISO 8601 date-time; indexes published before the field have none."""
    text = _text(value)
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 date-time") from exc
    return text


def _release_targets(raw: Any, where: str) -> dict[str, tuple[str, int, str]]:
    """``release_targets`` as target -> (url, bytes, sha256); empty when absent."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}.release_targets must be an object")
    targets: dict[str, tuple[str, int, str]] = {}
    for target, artifact in raw.items():
        field_path = f"{where}.release_targets.{target}"
        if target not in SUPPORTED_EXTENSION_RUNTIME_TARGETS:
            raise ValueError(
                f"{field_path}: runtime target must be one of "
                f"{', '.join(SUPPORTED_EXTENSION_RUNTIME_TARGETS)}"
            )
        if not isinstance(artifact, dict):
            raise ValueError(f"{field_path} must be an object")
        targets[target] = (
            _absolute_https(artifact.get("release_zip_url"), f"{field_path}.release_zip_url"),
            _positive_int(artifact.get("release_zip_bytes"), f"{field_path}.release_zip_bytes"),
            _sha256(artifact.get("release_zip_sha256"), f"{field_path}.release_zip_sha256"),
        )
    return targets


def _parse_item(raw: Any, *, position: int, runtime_target: str) -> ExtensionCatalogItem:
    where = f"items[{position}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    compatible = raw.get("compatible_flow_steward") or {}
    if not isinstance(compatible, dict):
        raise ValueError(f"{where}.compatible_flow_steward must be an object")
    release = (
        _absolute_https(raw.get("release_zip_url"), f"{where}.release_zip_url"),
        _positive_int(raw.get("release_zip_bytes"), f"{where}.release_zip_bytes"),
        _sha256(raw.get("release_zip_sha256"), f"{where}.release_zip_sha256"),
    )
    targets = _release_targets(raw.get("release_targets"), where)
    if targets:
        # The top-level archive stays required: a product that predates
        # release_targets reads it and installs it as before.
        release = targets.get(runtime_target, release)
    return ExtensionCatalogItem(
        extension_id=_required_text(raw.get("extension_id"), f"{where}.extension_id"),
        version=_semver(raw.get("version"), f"{where}.version"),
        name=_required_text(raw.get("name"), f"{where}.name"),
        summary=_required_text(raw.get("summary"), f"{where}.summary"),
        description=_text(raw.get("description")),
        categories=_string_tuple(raw.get("categories"), f"{where}.categories"),
        capabilities=_string_tuple(raw.get("capabilities"), f"{where}.capabilities"),
        repository_url=_absolute_https(raw.get("repository_url"), f"{where}.repository_url"),
        release_zip_url=release[0],
        release_zip_bytes=release[1],
        release_zip_sha256=release[2],
        permissions_summary=_string_tuple(
            raw.get("permissions_summary"), f"{where}.permissions_summary"
        ),
        setup_summary=_text(raw.get("setup_summary")),
        compatible_min_version=_optional_semver(compatible.get("min"), f"{where}.compatible.min"),
        compatible_max_version=_optional_semver(compatible.get("max"), f"{where}.compatible.max"),
        publication_status=_enum(
            raw.get("publication_status"), PUBLICATION_STATUSES, f"{where}.publication_status"
        ),
        verification_status=_enum(
            raw.get("verification_status"), VERIFICATION_STATUSES, f"{where}.verification_status"
        ),
        icon=parse_catalog_icon(raw.get("icon"), f"{where}.icon"),
        runtime_targets=tuple(sorted(targets)),
        available_for_host=not targets or runtime_target in targets,
        added_at=_optional_timestamp(raw.get("added_at"), f"{where}.added_at"),
        updated_at=_optional_timestamp(raw.get("updated_at"), f"{where}.updated_at"),
        tags=_string_tuple(raw.get("tags"), f"{where}.tags"),
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _required_text(value: Any, field_path: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f"{field_path} is required")
    return text


def _string_tuple(value: Any, field_path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_path} must be a list of strings")
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"{field_path} must be a list of strings")
        text = entry.strip()
        if text and text not in items:
            items.append(text)
    return tuple(items)


def _semver(value: Any, field_path: str) -> str:
    try:
        return normalize_semver(value, field_path)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _optional_semver(value: Any, field_path: str) -> str:
    text = _text(value)
    return _semver(text, field_path) if text else ""


def _absolute_https(value: Any, field_path: str) -> str:
    text = _required_text(value, field_path)
    parsed = urlparse(text)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"{field_path} must be an absolute https URL")
    return text


def _positive_int(value: Any, field_path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_path} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_path} must be greater than zero")
    return value


def _sha256(value: Any, field_path: str) -> str:
    text = _text(value).lower()
    if len(text) != _SHA256_HEX_LENGTH or any(char not in _HEX_DIGITS for char in text):
        raise ValueError(f"{field_path} must be a hex SHA-256 digest")
    return text


def _enum(value: Any, allowed: frozenset[str], field_path: str) -> str:
    text = _text(value).lower()
    if text not in allowed:
        raise ValueError(f"{field_path} must be one of {sorted(allowed)}")
    return text


__all__ = [
    "CAPABILITY_LABELS",
    "PERMISSION_LABELS",
    "PUBLICATION_STATUSES",
    "SCHEMA_VERSION",
    "VERIFICATION_STATUSES",
    "ExtensionCatalogError",
    "ExtensionCatalogIndex",
    "ExtensionCatalogItem",
    "cached_extension_catalog_names",
    "extension_catalog_availability",
    "extension_catalog_icon",
    "fetch_extension_release_artifact",
    "load_extension_catalog",
    "parse_extension_catalog_index",
    "reset_extension_catalog_cache",
]
