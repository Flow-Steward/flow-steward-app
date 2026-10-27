"""Read-only workflow template catalog: index contract, validation, and cache.

This module owns one thing — turning a remote JSON index into a validated,
in-memory list of workflow template descriptors — and it owns it alone. It does
not know about extensions, and the extension catalog does not know about it.
The only shared code is the bounded HTTP helper in
``core/infrastructure/static_catalog``.

Cache behaviour is the static catalogs' one contract
(``core/infrastructure/static_catalog/index_cache``):

* there is no freshness window: every catalog read revalidates the index with
  the host, and a ``304`` reuses the parsed index — there is no background
  worker, scheduler, or control loop;
* the in-memory index is replaced only after the whole new document
  validates, so one bad item leaves the previous index serving;
* a failed read over a usable index serves that index, marked stale;
* a failure with nothing cached reports the catalog unavailable;
* nothing is written to the database or the filesystem, so a restart simply
  fetches again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from core.application.extensions.semver import normalize_semver
from core.application.extensions.url_safety import (
    assert_safe_remote_https_url,
    open_pinned_url,
)
from core.application.workflows.workflow_portability_common import (
    SCHEMA_VERSION as IMPORTABLE_BUNDLE_SCHEMA,
)
from core.infrastructure.static_catalog import (
    MAX_CATALOG_INDEX_BYTES,
    MAX_CATALOG_ITEMS,
    MAX_WORKFLOW_TEMPLATE_YAML_BYTES,
    workflow_template_catalog_url,
)
from core.infrastructure.static_catalog.http_fetch import (
    CatalogUrlGuard,
    FetchedCatalogIndex,
    fetch_catalog_artifact,
    fetch_catalog_index_json,
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


def fetch_workflow_template_artifact(
    url: str,
    *,
    expected_bytes: int,
    expected_sha256: str,
    purpose: str,
) -> bytes:
    """Fetch and verify one workflow template catalog artifact under this area's guard."""
    return fetch_catalog_artifact(
        url,
        guard=_URL_GUARD,
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
        max_bytes=MAX_WORKFLOW_TEMPLATE_YAML_BYTES,
        purpose=purpose,
    )


SCHEMA_VERSION = "workflow_template_catalog/v1"
PUBLICATION_STATUSES = frozenset({"published", "deprecated"})
VERIFICATION_STATUSES = frozenset({"verified", "unverified"})
_FETCH_PURPOSE = "workflow template catalog index"
_SHA256_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


class WorkflowTemplateCatalogError(RuntimeError):
    """The configured workflow template catalog cannot be served."""

    def __init__(self, message: str, *, code: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def detail(self) -> dict[str, Any]:
        # `message` is where the frontend HTTP client reads a reason, as it does for
        # the workflow import errors; without it the operator saw only the status.
        return {"error": str(self), "code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class WorkflowTemplateSetupRequirement:
    """One thing an operator must have in place before the template can run."""

    kind: str
    label: str
    ref: str = ""
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "ref": self.ref, "required": self.required}


@dataclass(frozen=True, slots=True)
class WorkflowTemplateCatalogItem:
    """One published workflow template, as the index describes it."""

    item_id: str
    version: str
    name: str
    summary: str
    description: str
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    integrations: tuple[str, ...]
    trigger_types: tuple[str, ...]
    setup_requirements: tuple[WorkflowTemplateSetupRequirement, ...]
    workflow_yaml_url: str
    workflow_yaml_bytes: int
    workflow_yaml_sha256: str
    publication_status: str
    verification_status: str
    compatible_min_version: str = ""
    compatible_max_version: str = ""
    #: When the template first appeared in the catalog, and when this version did.
    added_at: str = ""
    updated_at: str = ""
    #: The child workflows the template carries and creates with itself: (id, name).
    includes: tuple[tuple[str, str], ...] = ()
    #: The portability bundle format of the template file; "" when the index does not say.
    bundle_schema: str = ""

    @property
    def importable(self) -> bool:
        """Whether this Core can import the template file; an unnamed format is unknown."""
        return self.bundle_schema == IMPORTABLE_BUNDLE_SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "version": self.version,
            "name": self.name,
            "summary": self.summary,
            "description": self.description,
            "categories": list(self.categories),
            "tags": list(self.tags),
            "integrations": list(self.integrations),
            "trigger_types": list(self.trigger_types),
            "setup_requirements": [item.as_dict() for item in self.setup_requirements],
            "compatible_flow_steward": {
                "min": self.compatible_min_version,
                "max": self.compatible_max_version,
            },
            "workflow_yaml_url": self.workflow_yaml_url,
            "workflow_yaml_bytes": self.workflow_yaml_bytes,
            "workflow_yaml_sha256": self.workflow_yaml_sha256,
            "publication_status": self.publication_status,
            "verification_status": self.verification_status,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
            "includes": [{"item_id": item_id, "name": name} for item_id, name in self.includes],
            "bundle_schema": self.bundle_schema,
            "importable": self.importable,
        }


@dataclass(frozen=True, slots=True)
class WorkflowTemplateCatalogIndex:
    """A validated index document."""

    schema_version: str
    catalog_revision: str
    published_at: str
    items: tuple[WorkflowTemplateCatalogItem, ...]
    #: Human names for the integration ids items reference; ids stay the filter key.
    integration_labels: tuple[tuple[str, str], ...] = ()
    #: Names for the category ids; the list is shared with the extension catalog.
    category_labels: tuple[tuple[str, str], ...] = ()

    def item(self, item_id: str) -> WorkflowTemplateCatalogItem | None:
        normalized = str(item_id or "").strip()
        for entry in self.items:
            if entry.item_id == normalized:
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
_INDEX: CatalogIndexCache[WorkflowTemplateCatalogIndex] = CatalogIndexCache(
    fetch=lambda url, validators: _fetch_index(url, validators),
    parse=lambda payload: parse_workflow_template_catalog_index(payload),
    clock=lambda: _now(),
)


def reset_workflow_template_catalog_cache() -> None:
    """Drop the process-local index. Used by tests."""
    _INDEX.reset()


def load_workflow_template_catalog() -> WorkflowTemplateCatalogIndex:
    """Return the current index, revalidated with the catalog host on this call.

    Raises :class:`WorkflowTemplateCatalogError` only when there is nothing to
    serve: no configured URL, or a failed read with no good index cached.
    """
    url = workflow_template_catalog_url()
    if not url:
        raise WorkflowTemplateCatalogError(
            "Workflow template catalog is not configured.",
            code="workflow_template_catalog_not_configured",
        )
    try:
        return _INDEX.load(url)
    except CatalogIndexUnavailableError as exc:
        raise WorkflowTemplateCatalogError(
            "Workflow template catalog is unavailable.",
            code="workflow_template_catalog_unavailable",
        ) from exc


def workflow_template_catalog_availability(
    index: WorkflowTemplateCatalogIndex | None = None,
) -> dict[str, Any]:
    """Describe the catalog for the UI without raising when it is unusable.

    ``index`` is one the caller has just loaded; it is described as is rather
    than read again.
    """
    if not workflow_template_catalog_url():
        return {
            "kind": "workflow_template_catalog_availability",
            "configured": False,
            "available": False,
            "reason": "workflow_template_catalog_not_configured",
            "item_count": 0,
            "stale": False,
        }
    if index is None:
        try:
            index = load_workflow_template_catalog()
        except WorkflowTemplateCatalogError as exc:
            return {
                "kind": "workflow_template_catalog_availability",
                "configured": True,
                "available": False,
                "reason": exc.code,
                "message": str(exc),
                "item_count": 0,
                "stale": False,
            }
    return {
        "kind": "workflow_template_catalog_availability",
        "configured": True,
        "available": True,
        "schema_version": index.schema_version,
        "catalog_revision": index.catalog_revision,
        "published_at": index.published_at,
        "item_count": len(index.items),
        **_INDEX.freshness(),
    }


# ---------------------------------------------------------------------------
# Index contract
# ---------------------------------------------------------------------------


def parse_workflow_template_catalog_index(payload: Any) -> WorkflowTemplateCatalogIndex:
    """Validate a decoded index document, or raise ``ValueError``.

    Validation is all-or-nothing on purpose: a catalog that silently drops the
    entries it could not read is a catalog nobody can reason about.
    """
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

    items: list[WorkflowTemplateCatalogItem] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_items):
        item = _parse_item(raw, position=position)
        if item.item_id in seen:
            raise ValueError(f"items[{position}]: duplicate item_id '{item.item_id}'")
        seen.add(item.item_id)
        items.append(item)

    return WorkflowTemplateCatalogIndex(
        schema_version=schema_version,
        catalog_revision=catalog_revision,
        published_at=published_at,
        items=tuple(items),
        integration_labels=_integration_labels(payload.get("integration_labels")),
        category_labels=_integration_labels(
            payload.get("category_labels"), field_name="category_labels"
        ),
    )


def _parse_item(raw: Any, *, position: int) -> WorkflowTemplateCatalogItem:
    where = f"items[{position}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    compatible = raw.get("compatible_flow_steward") or {}
    if not isinstance(compatible, dict):
        raise ValueError(f"{where}.compatible_flow_steward must be an object")
    return WorkflowTemplateCatalogItem(
        item_id=_required_text(raw.get("item_id"), f"{where}.item_id"),
        version=_semver(raw.get("version"), f"{where}.version"),
        name=_required_text(raw.get("name"), f"{where}.name"),
        summary=_required_text(raw.get("summary"), f"{where}.summary"),
        description=_text(raw.get("description")),
        categories=_string_tuple(raw.get("categories"), f"{where}.categories"),
        tags=_string_tuple(raw.get("tags"), f"{where}.tags"),
        integrations=_string_tuple(raw.get("integrations"), f"{where}.integrations"),
        trigger_types=_string_tuple(raw.get("trigger_types"), f"{where}.trigger_types"),
        setup_requirements=_setup_requirements(raw.get("setup_requirements"), where=where),
        compatible_min_version=_optional_semver(compatible.get("min"), f"{where}.compatible.min"),
        compatible_max_version=_optional_semver(compatible.get("max"), f"{where}.compatible.max"),
        workflow_yaml_url=_absolute_https(
            raw.get("workflow_yaml_url"), f"{where}.workflow_yaml_url"
        ),
        workflow_yaml_bytes=_positive_int(
            raw.get("workflow_yaml_bytes"), f"{where}.workflow_yaml_bytes"
        ),
        workflow_yaml_sha256=_sha256(
            raw.get("workflow_yaml_sha256"), f"{where}.workflow_yaml_sha256"
        ),
        publication_status=_enum(
            raw.get("publication_status"), PUBLICATION_STATUSES, f"{where}.publication_status"
        ),
        verification_status=_enum(
            raw.get("verification_status"), VERIFICATION_STATUSES, f"{where}.verification_status"
        ),
        added_at=_optional_timestamp(raw.get("added_at"), f"{where}.added_at"),
        updated_at=_optional_timestamp(raw.get("updated_at"), f"{where}.updated_at"),
        includes=_includes(raw.get("includes"), f"{where}.includes"),
        bundle_schema=_text(raw.get("bundle_schema")),
    )


def _includes(value: Any, field_name: str) -> tuple[tuple[str, str], ...]:
    """The child workflows an item carries; indexes published before the field have none."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    rows: list[tuple[str, str]] = []
    for position, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"{field_name}[{position}] must be an object")
        item_id = _required_text(entry.get("item_id"), f"{field_name}[{position}].item_id")
        rows.append((item_id, _text(entry.get("name")) or item_id))
    return tuple(rows)


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


#: A label is a product name shown on a card, not prose.
_MAX_INTEGRATION_LABEL_LENGTH = 120


def _integration_labels(
    raw: Any, *, field_name: str = "integration_labels"
) -> tuple[tuple[str, str], ...]:
    """Validate an optional id -> human name map (integration or category ids).

    `items[].integrations` stays a list of ids: an id is what a filter and an install
    match on, while the name a person recognises (``Acme Storage (FTP/SFTP)``) can
    differ from it and can change without changing what it identifies.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name} must be an object of id to label")
    labels: list[tuple[str, str]] = []
    for key, value in raw.items():
        integration_id = _text(key)
        if not isinstance(value, str):
            raise ValueError(f"{field_name}.{integration_id} must be a string")
        label = value.strip()
        if not integration_id or not label:
            continue
        if len(label) > _MAX_INTEGRATION_LABEL_LENGTH:
            raise ValueError(
                f"{field_name}.{integration_id} is longer than "
                f"{_MAX_INTEGRATION_LABEL_LENGTH} characters"
            )
        labels.append((integration_id, label))
    return tuple(sorted(labels))


def _setup_requirements(raw: Any, *, where: str) -> tuple[WorkflowTemplateSetupRequirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{where}.setup_requirements must be a list")
    parsed: list[WorkflowTemplateSetupRequirement] = []
    for position, entry in enumerate(raw):
        field_path = f"{where}.setup_requirements[{position}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{field_path} must be an object")
        parsed.append(
            WorkflowTemplateSetupRequirement(
                kind=_required_text(entry.get("kind"), f"{field_path}.kind"),
                label=_required_text(entry.get("label"), f"{field_path}.label"),
                ref=_text(entry.get("ref")),
                required=bool(entry.get("required", True)),
            )
        )
    return tuple(parsed)


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
    "IMPORTABLE_BUNDLE_SCHEMA",
    "PUBLICATION_STATUSES",
    "SCHEMA_VERSION",
    "VERIFICATION_STATUSES",
    "WorkflowTemplateCatalogError",
    "WorkflowTemplateCatalogIndex",
    "WorkflowTemplateCatalogItem",
    "WorkflowTemplateSetupRequirement",
    "fetch_workflow_template_artifact",
    "load_workflow_template_catalog",
    "parse_workflow_template_catalog_index",
    "reset_workflow_template_catalog_cache",
    "workflow_template_catalog_availability",
]
