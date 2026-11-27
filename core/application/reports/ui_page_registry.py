"""Internal settings-page and shell-navigation registration helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from core.application.auth.route_permissions import module_api_prefix
from core.application.settings.platform_contracts import INSTALLATION_ADMIN_PERMISSION
from core.application.settings.ui_i18n import (
    normalize_ui_locale,
    resolve_ui_locale,
    translate_ui_text,
)

_COMMUNITY_SAFE_EDITIONS = ("community", "private")

# Control-surface kinds rendered by a renderer that ships in the Core frontend
# bundle (ADR 0082, section 6). A page declaring one of these kinds needs no
# Vite-compiled ``component_key``.
HOST_RENDERER_KINDS = frozenset({"resource_admin", "resource_report", "status_document"})

_RESOURCE_ADMIN_FIELD_TYPES = frozenset(
    {
        "text",
        "textarea",
        "number",
        "select",
        "checkbox",
        "csv",
        "json",
        "secret",
        "reference",
        "multi_choice",
    }
)
# Choice fields: ``select`` and ``multi_choice`` list their options, ``reference``
# (and ``multi_choice`` instead of a list) loads them from ``options_path``.
_RESOURCE_ADMIN_CHOICE_TYPES = frozenset({"select", "reference", "multi_choice"})
# Optional plain-language texts of a field; ``help`` has a catalogue key too.
_RESOURCE_ADMIN_FIELD_TEXTS = ("label", "help", "placeholder", "empty_options_label")
# resource_report: a read-only, paged, filterable table (ADR 0082, section 6).
_RESOURCE_REPORT_COLUMN_TYPES = frozenset({"datetime", "text", "badge", "link"})
_RESOURCE_REPORT_FILTER_TYPES = frozenset({"text", "date", "select"})
_RESOURCE_REPORT_PAGE_SIZE_MIN = 1
_RESOURCE_REPORT_PAGE_SIZE_MAX = 500
_RESOURCE_REPORT_DEFAULT_PAGE_SIZE = 50
# Paging parameters the host renderer sends itself; a filter may not reuse them.
_RESOURCE_REPORT_PAGING_KEYS = frozenset({"limit", "offset"})
# Keys a write-capable surface uses; a report declares none of them.
_RESOURCE_REPORT_REFUSED_KEYS = ("item_path", "resources", "fields")
# status_document: one status object shown as label/value rows, plus a text
# document (typed or read from a file) the page PUTs to the module.
_STATUS_DOCUMENT_FIELD_TYPES = frozenset({"text", "datetime", "badge"})
_STATUS_DOCUMENT_DEFAULT_MAX_BYTES = 65_536
_STATUS_DOCUMENT_MAX_BYTES_LIMIT = 1_048_576
# File-picker hint such as ``.txt,.json,text/plain``: extensions and media types only.
_STATUS_DOCUMENT_ACCEPT = re.compile(r"^[A-Za-z0-9.+*/\-]+(\s*,\s*[A-Za-z0-9.+*/\-]+)*$")
_STATUS_DOCUMENT_REFUSED_KEYS = ("resources", "columns", "filters", "list_path", "item_path")
_REPORT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# An in-app page path such as ``/jobs/{job_id}``: never another origin, never the API.
_IN_APP_LINK_PATH = re.compile(r"^/[A-Za-z0-9_\-./~{}]*$")
_SAME_ORIGIN_API_PATH = re.compile(r"^/api/[A-Za-z0-9_\-/{}.]+$")
_API_PATH_TOKEN = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_CORE_DEFINITION_MARKER = "_core_definition"
_PAGE_REGISTRY_NAMES = ("settings_pages", "navigation_items", "registered_pages")
# Server-side only: which platform module registered a page. Never sent to the
# browser; operator diagnostics read it from the registries.
SOURCE_MODULE_FIELD = "source_module"
# Core entries without the Core marker that any registrant may replace under the
# same page key (a vendor build ships its own Extensions screen).
_REPLACEABLE_CORE_PAGE_KEYS = frozenset({"extensions"})


class PageRegistrySealedError(RuntimeError):
    """A page registration arrived after the composition root sealed the registries."""


# The page dictionaries the composition root sealed, by identity. Module views copy
# ``registries`` at the top level but share these inner dictionaries, so a module
# holding an old view is refused as well. Strong references keep ``id`` stable.
_SEALED_PAGE_REGISTRIES: dict[int, Any] = {}


def seal_page_registries(registries: MutableMapping[str, Any]) -> None:
    """Refuse every later page registration into ``registries`` (ADR 0082).

    Startup order: Core registers its own pages, the module loader runs every
    module's entrypoint, then the composition root calls this. Core registers no
    page after startup (extension pages are served by the extension runtime, not
    through this registry), so the seal covers every registration: an unscoped
    call made later from a hook, thread or callback can no longer claim Core's
    privileges. Sealing is idempotent.
    """
    for name in _PAGE_REGISTRY_NAMES:
        registry = registries.setdefault(name, {})
        _SEALED_PAGE_REGISTRIES[id(registry)] = registry


def page_registries_sealed(registries: Mapping[str, Any]) -> bool:
    """Whether any page dictionary of ``registries`` has been sealed."""
    for name in _PAGE_REGISTRY_NAMES:
        registry = registries.get(name)
        if registry is not None and _SEALED_PAGE_REGISTRIES.get(id(registry)) is registry:
            return True
    return False


def _refuse_sealed(registries: Mapping[str, Any], page_key: str) -> None:
    if page_registries_sealed(registries):
        raise PageRegistrySealedError(
            f"page {page_key!r} cannot be registered: page registration closed when "
            "startup finished; register pages from the module's register entrypoint"
        )


@dataclass(slots=True)
class PageRegistrationScope:
    """Who registers pages while Core's module loader runs an entrypoint.

    ``module_id`` is the identity under which Core loaded a prepared platform
    module, or ``None`` for an optional bootstrap module listed by environment,
    which has no verified identity. Outside any scope, and only until the
    registries are sealed (:func:`seal_page_registries`), the caller is Core.
    """

    module_id: str | None
    page_keys: list[str] = dataclass_field(default_factory=list)


_REGISTRATION_SCOPE: ContextVar[PageRegistrationScope | None] = ContextVar(
    "fs_page_registration_scope", default=None
)


@contextmanager
def page_registration_scope(*, module_id: str | None) -> Iterator[PageRegistrationScope]:
    """Bind page registrations inside the block to a loaded module (ADR 0082).

    Only Core's module loader opens a scope. Pages registered inside it are stamped
    with ``source_module`` and their host-rendered resources are confined to the
    module's API tree. The scope is reset on exit, also when the block raises.
    """
    scope = PageRegistrationScope(module_id=str(module_id or "").strip() or None)
    token = _REGISTRATION_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _REGISTRATION_SCOPE.reset(token)


def current_registration_module() -> str | None:
    """The id of the prepared platform module registering right now, else ``None``.

    ``None`` outside a :func:`page_registration_scope` and inside the scope of an
    environment-listed bootstrap module, which has no verified identity.
    """
    scope = _REGISTRATION_SCOPE.get()
    return scope.module_id if scope is not None else None


_CORE_SETTINGS_TITLE_KEYS = {
    "settings_home": "shell.nav.settings",
    "extensions": "shell.nav.extensions",
    "general": "registry.workflow_detail.settings.general",
    "security": "account.security.title",
    "retention": "settings.retention.title",
    "notifications": "settings.tab.notifications",
    "email": "settings.tab.email",
    "service_accounts": "react.settings.service_accounts",
    "model_providers": "settings.tab.model_providers",
    "secret_storage": "react.settings.secret_storage.title",
    "account": "shell.user_settings",
    "users": "react.settings.users_and_access",
}

_CORE_SETTINGS_PAGE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "page_key": "settings_home",
        "title": "Settings",
        "path": "/settings",
        "nav_section": "system",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": True,
        "settings_tab": False,
        "sort_order": 10,
        "nav_label": "Settings",
        "nav_icon": "settings",
        "current_pages": ("settings", "registry_index"),
    },
    {
        "page_key": "extensions",
        "title": "Extensions",
        "path": "/extensions",
        "nav_section": "system",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": False,
        "sidebar_nav": True,
        "settings_tab": False,
        "sort_order": 15,
        "nav_label": "Extensions",
        "nav_icon": "settings",
        "current_pages": ("extensions",),
        "loading_text": "Loading extensions...",
    },
    {
        "page_key": "general",
        "title": "General",
        "path": "/settings/general",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 1,
        "loading_text": "Loading general settings...",
    },
    {
        "page_key": "security",
        "title": "Security",
        "path": "/settings/security",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 20,
        "loading_text": "Loading security settings...",
    },
    {
        "page_key": "email",
        "title": "Email",
        "path": "/settings/email",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 55,
        "loading_text": "Loading email settings...",
    },
    {
        "page_key": "retention",
        "title": "Retention",
        "path": "/settings/retention",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 40,
        "loading_text": "Loading retention settings...",
    },
    {
        "page_key": "notifications",
        "title": "Notifications",
        "path": "/settings/notifications",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 50,
        "loading_text": "Loading notifications...",
    },
    {
        "page_key": "service_accounts",
        "title": "Service Accounts",
        "path": "/settings/service-accounts",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "manage_service_accounts",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 60,
        "loading_text": "Loading service accounts...",
    },
    {
        "page_key": "model_providers",
        "title": "Chat Models",
        "path": "/settings/providers",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 70,
        "loading_text": "Loading chat models...",
    },
    {
        # Where an account's secret values are kept (ADR 0066 amendment 2026-11-26).
        # Without a secret backend module the page says it is an add-on.
        "page_key": "secret_storage",
        "title": "Secret storage",
        "path": "/settings/secret-storage",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 75,
        "loading_text": "Loading secret storage...",
    },
    {
        "page_key": "account",
        "title": "User Settings",
        "path": "/settings/account",
        "nav_section": "account",
        "render_mode": "react_shell",
        "required_permission": "authenticated",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": True,
        "settings_tab": False,
        "sort_order": 10,
        "nav_label": "User Settings",
        "nav_icon": "account",
        "current_pages": ("account",),
    },
    {
        "page_key": "users",
        "title": "Users & Access",
        "path": "/settings/users",
        "nav_section": "account",
        "render_mode": "react_shell",
        "required_permission": "manage_users",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": True,
        "settings_tab": False,
        "sort_order": 20,
        "nav_label": "Users & Access",
        "nav_icon": "users",
        "current_pages": ("users",),
    },
)


def _normalize_text(value: Any, *, fallback: str = "") -> str:
    text = str(value or fallback).strip()
    return text or fallback


def _normalize_multi_text(
    values: Iterable[Any] | Any, *, fallback: tuple[str, ...]
) -> tuple[str, ...]:
    if isinstance(values, str):
        items = [values]
    elif values is None:
        items = []
    else:
        items = list(values)
    normalized = tuple(
        dict.fromkeys(_normalize_text(item).lower() for item in items if _normalize_text(item))
    )
    return normalized or fallback


def _auth_roles(auth_user: dict[str, Any] | None) -> set[str]:
    if not isinstance(auth_user, dict):
        return set()
    roles = auth_user.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    return {_normalize_text(role).lower() for role in roles if _normalize_text(role)}


def _auth_account_id(auth_user: Mapping[str, Any] | None) -> str:
    if not isinstance(auth_user, Mapping):
        return ""
    return _normalize_text(auth_user.get("account_id"))


def _required_features(value: Any) -> tuple[str, ...]:
    """The features a page's ``required_entitlement`` names, in lower case.

    One feature (a string) or a list of features, any one of which opens the page.
    """
    items = value if isinstance(value, list | tuple) else [value]
    features: list[str] = []
    for item in items:
        feature = _normalize_text(item).lower()
        if feature and feature not in features:
            features.append(feature)
    return tuple(features)


def _page_entitlement(value: Any, page_key: str) -> str | list[str]:
    """Validate ``required_entitlement``: a feature name, or a list meaning "any of"."""
    if value is None or isinstance(value, str):
        return _normalize_text(value)
    # An empty list would mean "any of no features": a page nobody could open, or,
    # read as "no requirement", a paid page open to everybody. Both are refused.
    if (
        not isinstance(value, list | tuple)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(
            f"page '{page_key}' declares required_entitlement {value!r}; use a feature "
            "name or a list of feature names, any one of which opens the page"
        )
    features: list[str] = []
    for item in value:
        feature = item.strip()
        if feature not in features:
            features.append(feature)
    return features


def _entitlement_visible(
    required_entitlement: Any,
    auth_user: Mapping[str, Any] | None,
    entitlement_service: Any = None,
) -> bool:
    """A page that needs a feature is visible only to an account entitled to it.

    ``required_entitlement`` is one feature or a list of features; with a list the
    page is visible when the account holds any of them. An anonymous request has no
    account and is never entitled.
    """
    features = _required_features(required_entitlement)
    if not features:
        return True
    account_id = _auth_account_id(auth_user)
    is_entitled = getattr(entitlement_service, "is_entitled", None)
    if not account_id or not callable(is_entitled):
        return False
    for feature in features:
        try:
            if is_entitled(account_id, feature):
                return True
        except Exception:
            continue
    return False


#: The ``required_permission`` names a page may declare: the names Core's
#: authorization service decides. Anything else would hide the page from everybody,
#: so registration refuses it.
PAGE_PERMISSIONS: frozenset[str] = frozenset(
    {
        "public",
        "always",
        "authenticated",
        "viewer",
        "operator",
        "view_settings",
        "manage_users",
        "can_manage_users",
        "manage_service_accounts",
        "can_manage_service_accounts",
        "manage_identity_integrations",
        "can_manage_identity_integrations",
        "can_view_private_settings_page",
        "view_private_settings_page",
        INSTALLATION_ADMIN_PERMISSION,
    }
)


def _page_permission(value: Any, page_key: str) -> str:
    permission = _normalize_text(value)
    if permission and permission.lower() not in PAGE_PERMISSIONS:
        raise ValueError(
            f"page '{page_key}' declares required_permission {permission!r}, which Core's "
            "authorization service does not know, so the page would be hidden from "
            "everybody; use one of "
            f"{', '.join(sorted(PAGE_PERMISSIONS))} (an administrator page uses "
            f"'view_settings', an installation-wide page '{INSTALLATION_ADMIN_PERMISSION}')"
        )
    return permission


def _permission_visible(
    required_permission: str,
    auth_user: dict[str, Any] | None,
    *,
    authorization_service: Any = None,
) -> bool:
    permission = _normalize_text(required_permission).lower()
    if not permission or permission in {"public", "always"}:
        return True
    if permission == "authenticated":
        return isinstance(auth_user, dict)

    if callable(getattr(authorization_service, "is_allowed", None)):
        try:
            return bool(authorization_service.is_allowed(permission, auth_user=auth_user))
        except Exception:
            return False

    decision_name = {
        "view_settings": "can_view_settings",
        "manage_users": "can_manage_users",
        "manage_service_accounts": "can_manage_service_accounts",
        "manage_identity_integrations": "can_manage_identity_integrations",
        "can_view_private_settings_page": "can_view_private_settings_page",
    }.get(permission)
    if decision_name:
        decision = getattr(authorization_service, decision_name, None)
        if callable(decision):
            try:
                return bool(decision(auth_user))
            except Exception:
                return False

    if auth_user is None:
        # Community mode with auth disabled keeps admin surfaces visible.
        return True

    roles = _auth_roles(auth_user)
    if "admin" in roles:
        return True
    if permission in {
        "view_settings",
        "manage_users",
        "manage_service_accounts",
        "manage_identity_integrations",
        "can_view_private_settings_page",
        INSTALLATION_ADMIN_PERMISSION,
    }:
        return False
    if permission == "operator":
        return "operator" in roles
    if permission == "viewer":
        return bool(roles)
    return False


def _page_visible(
    record: dict[str, Any],
    *,
    edition: str,
    auth_user: dict[str, Any] | None,
    authorization_service: Any = None,
    entitlement_service: Any = None,
) -> bool:
    visible_editions = _normalize_multi_text(
        record.get("edition_visibility"),
        fallback=_COMMUNITY_SAFE_EDITIONS,
    )
    normalized_edition = _normalize_text(edition, fallback="community").lower()
    if normalized_edition not in visible_editions:
        return False
    if not _entitlement_visible(
        record.get("required_entitlement"),
        auth_user,
        entitlement_service,
    ):
        return False
    return _permission_visible(
        _normalize_text(record.get("required_permission")),
        auth_user,
        authorization_service=authorization_service,
    )


def _same_origin_api_path(
    value: Any, *, label: str, allow_tokens: bool, api_prefix: str = "/api/"
) -> str:
    path = _normalize_text(value)
    if (
        not _SAME_ORIGIN_API_PATH.fullmatch(path)
        or path.startswith("//")
        or ".." in path
        or "://" in path
        or "?" in path
        or "#" in path
    ):
        raise ValueError(f"{label} must be a same-origin /api/ path, got {path!r}")
    if not path.startswith(api_prefix) or path == api_prefix:
        raise ValueError(f"{label} must live under {api_prefix}, got {path!r}")
    remainder = _API_PATH_TOKEN.sub("", path) if allow_tokens else path
    if "{" in remainder or "}" in remainder:
        raise ValueError(f"{label} may use braces only as {{name}} tokens, got {path!r}")
    return path


def _choice_option(value: Any, *, label: str) -> dict[str, str]:
    """One option of a choice field: ``{value, label, label_key?, description?}``.

    A bare string is its own value and label; a host renderer shows the label,
    never the value.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{label} has an empty option")
        return {"value": text, "label": text}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} options must be text or mappings")
    option_value = _normalize_text(value.get("value"))
    if not option_value:
        raise ValueError(f"{label} has an option without a value")
    option = {
        "value": option_value,
        "label": _normalize_text(value.get("label"), fallback=option_value),
    }
    for extra in ("label_key", "description", "description_key"):
        text = _normalize_text(value.get(extra))
        if text:
            option[extra] = text
    return option


def _resource_admin_choices(field: dict[str, Any], *, label: str, api_prefix: str) -> None:
    """Validate the options of a choice field in place."""
    field_type = field["type"]
    raw_options = field.get("options")
    options_path = _normalize_text(field.get("options_path"))
    if field_type not in _RESOURCE_ADMIN_CHOICE_TYPES:
        if raw_options or options_path:
            raise ValueError(f"{label} has options but is not a choice field")
        return
    if options_path:
        if field_type == "select":
            raise ValueError(f"{label} loads options_path: declare it as a reference")
        if raw_options:
            raise ValueError(f"{label} declares both options and options_path")
        field["options_path"] = _same_origin_api_path(
            options_path,
            label=f"{label} options_path",
            allow_tokens=False,
            api_prefix=api_prefix,
        )
        field.pop("options", None)
        return
    if field_type == "reference":
        raise ValueError(f"{label} requires options_path")
    if isinstance(raw_options, (str, bytes, Mapping)) or not isinstance(raw_options, Iterable):
        raw_options = []
    options = [_choice_option(item, label=label) for item in raw_options]
    if not options:
        raise ValueError(f"{label} requires options")
    values = [option["value"] for option in options]
    if len(set(values)) != len(values):
        raise ValueError(f"{label} repeats an option value")
    field["options"] = options


def _resource_admin_field(
    value: Any, *, resource_key: str, api_prefix: str = "/api/"
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(
            f"resource_admin resource {resource_key!r} has a field that is not a mapping"
        )
    field = dict(value)
    field_key = _normalize_text(field.get("key"))
    field_type = _normalize_text(field.get("type"), fallback="text")
    if not field_key:
        raise ValueError(f"resource_admin resource {resource_key!r} has a field without a key")
    if field_type not in _RESOURCE_ADMIN_FIELD_TYPES:
        raise ValueError(
            f"resource_admin field {resource_key}.{field_key} has unknown type {field_type!r}"
        )
    field["key"] = field_key
    field["type"] = field_type
    label = f"resource_admin field {resource_key}.{field_key}"
    _resource_admin_choices(field, label=label, api_prefix=api_prefix)
    for text_key in _RESOURCE_ADMIN_FIELD_TEXTS:
        if text_key in field and not isinstance(field[text_key], str):
            raise ValueError(f"{label} {text_key} must be text")
    visible_when = field.get("visible_when")
    if visible_when is not None:
        if not isinstance(visible_when, Mapping):
            raise ValueError(f"{label} visible_when must be a mapping")
        other = _normalize_text(visible_when.get("field"))
        contains = _normalize_text(visible_when.get("contains"))
        if not other or not contains or other == field_key:
            raise ValueError(f"{label} visible_when needs another field and contains")
        field["visible_when"] = {"field": other, "contains": contains}
    return field


_ROW_ACTION_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ROW_ACTION_KEYS = frozenset(
    {
        "key",
        "label",
        "label_key",
        "method",
        "path_template",
        "confirm",
        "confirm_key",
        "success_text",
        "success_text_key",
    }
)
_ROW_ACTION_TEXTS = ("label_key", "confirm", "confirm_key", "success_text", "success_text_key")


def _resource_admin_row_action(value: Any, *, resource_key: str, api_prefix: str) -> dict[str, str]:
    """One row action: a button next to Edit and Delete that POSTs to a row's path.

    ``path_template`` lies in the page's API tree (``api_prefix``, the module's own
    tree for a module) and carries exactly one ``{name}`` token as a whole path
    segment; the renderer fills it with the row's record id, so the id can never
    change which resource the request addresses.
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"resource_admin resource {resource_key!r} row_actions must be mappings")
    action_key = _normalize_text(value.get("key"))
    label = f"resource_admin row action {resource_key}.{action_key or '?'}"
    if not _ROW_ACTION_KEY.fullmatch(action_key):
        raise ValueError(f"{label} key must match {_ROW_ACTION_KEY.pattern}")
    unknown = sorted(set(value) - _ROW_ACTION_KEYS)
    if unknown:
        raise ValueError(f"{label} has unknown keys {unknown}")
    if not _normalize_text(value.get("label")):
        raise ValueError(f"{label} requires a label")
    method = _normalize_text(value.get("method"), fallback="POST").upper()
    if method != "POST":
        raise ValueError(f"{label} method must be POST, got {method!r}")
    path = _same_origin_api_path(
        value.get("path_template"),
        label=f"{label} path_template",
        allow_tokens=True,
        api_prefix=api_prefix,
    )
    tokens = _API_PATH_TOKEN.findall(path)
    if len(tokens) != 1 or tokens[0] not in path.split("/"):
        raise ValueError(
            f"{label} path_template must carry exactly one {{name}} token as a whole "
            f"path segment, got {path!r}"
        )
    action = {
        "key": action_key,
        "label": _normalize_text(value.get("label")),
        "method": method,
        "path_template": path,
    }
    for text_key in _ROW_ACTION_TEXTS:
        if text_key not in value or value[text_key] is None:
            continue
        if not isinstance(value[text_key], str):
            raise ValueError(f"{label} {text_key} must be text")
        text = value[text_key].strip()
        if text:
            action[text_key] = text
    return action


def _resource_admin_row_actions(
    value: Any, *, resource_key: str, api_prefix: str
) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ValueError(f"resource_admin resource {resource_key!r} row_actions must be a list")
    actions = [
        _resource_admin_row_action(item, resource_key=resource_key, api_prefix=api_prefix)
        for item in value
    ]
    keys = [action["key"] for action in actions]
    if len(set(keys)) != len(keys):
        raise ValueError(f"resource_admin resource {resource_key!r} repeats a row action key")
    return actions


def _resource_admin_resource(value: Any, *, api_prefix: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("resource_admin resources must be mappings")
    resource = dict(value)
    resource_key = _normalize_text(resource.get("key"))
    if not resource_key:
        raise ValueError("resource_admin resource requires a key")
    for required in ("title", "id_field"):
        if not _normalize_text(resource.get(required)):
            raise ValueError(f"resource_admin resource {resource_key!r} requires {required}")
    raw_fields = resource.get("fields")
    if isinstance(raw_fields, (str, bytes, Mapping)) or not isinstance(raw_fields, Iterable):
        raw_fields = []
    fields = [
        _resource_admin_field(item, resource_key=resource_key, api_prefix=api_prefix)
        for item in raw_fields
    ]
    if not fields:
        raise ValueError(f"resource_admin resource {resource_key!r} requires at least one field")
    field_keys = {field["key"] for field in fields}
    for field in fields:
        condition = field.get("visible_when")
        if condition and condition["field"] not in field_keys:
            raise ValueError(
                f"resource_admin field {resource_key}.{field['key']} visible_when names "
                f"an unknown field {condition['field']!r}"
            )
    resource["key"] = resource_key
    resource["fields"] = fields
    # Only an explicit ``false`` hides the delete action; anything else keeps it.
    resource["allow_delete"] = resource.get("allow_delete") is not False
    # Likewise only an explicit ``false`` hides the edit action.
    resource["allow_edit"] = resource.get("allow_edit") is not False
    # The browser calls list_path verbatim, so it carries no tokens; item_path
    # substitutes the record id into its {name} token.
    resource["list_path"] = _same_origin_api_path(
        resource.get("list_path"),
        label=f"resource_admin resource {resource_key!r} list_path",
        allow_tokens=False,
        api_prefix=api_prefix,
    )
    if _normalize_text(resource.get("item_path")):
        item_path = _same_origin_api_path(
            resource.get("item_path"),
            label=f"resource_admin resource {resource_key!r} item_path",
            allow_tokens=True,
            api_prefix=api_prefix,
        )
        # Exactly one {name} token, and it is the whole final segment, so the
        # substituted record id can never change which collection is addressed.
        tokens = _API_PATH_TOKEN.findall(item_path)
        if len(tokens) != 1 or item_path.rsplit("/", 1)[-1] != tokens[0]:
            raise ValueError(
                f"resource_admin resource {resource_key!r} item_path must end in exactly "
                f"one {{name}} token as its final segment, got {item_path!r}"
            )
        resource["item_path"] = item_path
    if resource.get("row_actions") is not None:
        resource["row_actions"] = _resource_admin_row_actions(
            resource["row_actions"], resource_key=resource_key, api_prefix=api_prefix
        )
    return resource


def _in_app_link_path(value: Any, *, label: str, allow_tokens: bool) -> str:
    """Validate a path inside the application frontend, such as ``/jobs/{job_id}``.

    A link a host-rendered page draws stays in the app: it starts with one slash,
    names no scheme or host, has no parent segments, query or fragment, and never
    points at the API. With ``allow_tokens`` braces are allowed only as
    ``{name}`` tokens, which the renderer fills from the row.
    """
    path = _normalize_text(value)
    if (
        not _IN_APP_LINK_PATH.fullmatch(path)
        or path.startswith(("//", "/api/"))
        or ".." in path
        or path == "/api"
    ):
        raise ValueError(f"{label} must be an in-app path such as /jobs/{{id}}, got {path!r}")
    remainder = _API_PATH_TOKEN.sub("", path) if allow_tokens else path
    if "{" in remainder or "}" in remainder:
        raise ValueError(f"{label} may use braces only as {{name}} tokens, got {path!r}")
    return path


def _report_key(value: Any, *, label: str) -> str:
    key = _normalize_text(value)
    if not _REPORT_KEY.fullmatch(key):
        raise ValueError(f"{label} must be an identifier, got {key!r}")
    return key


def _report_label(item: Mapping[str, Any], *, label: str) -> dict[str, str]:
    """The visible label and its optional UI catalogue key (localized on resolve)."""
    text = _normalize_text(item.get("label"))
    if not text:
        raise ValueError(f"{label} requires a label")
    result = {"label": text}
    label_key = _normalize_text(item.get("label_key"))
    if label_key:
        result["label_key"] = label_key
    return result


def _report_mappings(value: Any, *, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ValueError(f"{label} must be a list")
    items = list(value)
    if not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"{label} entries must be mappings")
    return items


def _resource_report_column(value: Mapping[str, Any]) -> dict[str, Any]:
    key = _report_key(value.get("key"), label="resource_report column key")
    column_type = _normalize_text(value.get("type"), fallback="text")
    if column_type not in _RESOURCE_REPORT_COLUMN_TYPES:
        raise ValueError(f"resource_report column {key!r} has unknown type {column_type!r}")
    column: dict[str, Any] = {
        "key": key,
        "type": column_type,
        **_report_label(value, label=f"resource_report column {key!r}"),
    }
    link_path = _normalize_text(value.get("link_path"))
    link_key = _normalize_text(value.get("link_key"))
    link_if = _normalize_text(value.get("link_if"))
    missing_label = _normalize_text(value.get("link_missing_label"))
    missing_label_key = _normalize_text(value.get("link_missing_label_key"))
    if column_type != "link":
        if link_path or link_key or link_if or missing_label or missing_label_key:
            raise ValueError(
                f"resource_report column {key!r} sets a link but its type is {column_type!r}"
            )
        return column
    if not link_path and not link_key:
        raise ValueError(f"resource_report link column {key!r} requires link_path or link_key")
    if link_path:
        column["link_path"] = _in_app_link_path(
            link_path, label=f"resource_report column {key!r} link_path", allow_tokens=True
        )
    if link_key:
        column["link_key"] = _report_key(link_key, label=f"resource_report column {key!r} link_key")
    # ``link_if`` names a row field: when it is falsy the row draws its label as
    # plain text (with ``link_missing_label`` beside it) instead of a link.
    if (missing_label or missing_label_key) and not link_if:
        raise ValueError(
            f"resource_report link column {key!r} sets link_missing_label without link_if"
        )
    if missing_label_key and not missing_label:
        raise ValueError(
            f"resource_report link column {key!r} sets link_missing_label_key without "
            "link_missing_label"
        )
    if link_if:
        column["link_if"] = _report_key(link_if, label=f"resource_report column {key!r} link_if")
    if missing_label:
        column["link_missing_label"] = missing_label
    if missing_label_key:
        column["link_missing_label_key"] = missing_label_key
    return column


def _resource_report_option(value: Any, *, filter_key: str) -> dict[str, str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"resource_report filter {filter_key!r} has an empty option")
        return {"value": text, "label": text}
    if not isinstance(value, Mapping):
        raise ValueError(f"resource_report filter {filter_key!r} options must be text or mappings")
    option_value = _normalize_text(value.get("value"))
    if not option_value:
        raise ValueError(f"resource_report filter {filter_key!r} has an option without a value")
    option = {
        "value": option_value,
        "label": _normalize_text(value.get("label"), fallback=option_value),
    }
    label_key = _normalize_text(value.get("label_key"))
    if label_key:
        option["label_key"] = label_key
    return option


def _resource_report_filter(
    value: Mapping[str, Any], *, api_prefix: str = "/api/"
) -> dict[str, Any]:
    key = _report_key(value.get("key"), label="resource_report filter key")
    if key in _RESOURCE_REPORT_PAGING_KEYS:
        raise ValueError(f"resource_report filter key {key!r} is reserved for paging")
    filter_type = _normalize_text(value.get("type"), fallback="text")
    if filter_type not in _RESOURCE_REPORT_FILTER_TYPES:
        raise ValueError(f"resource_report filter {key!r} has unknown type {filter_type!r}")
    report_filter: dict[str, Any] = {
        "key": key,
        "type": filter_type,
        **_report_label(value, label=f"resource_report filter {key!r}"),
    }
    raw_options = value.get("options")
    options_path = _normalize_text(value.get("options_path"))
    if filter_type != "select":
        if raw_options or options_path:
            raise ValueError(f"resource_report filter {key!r} has options but is not a select")
        return report_filter
    if options_path:
        # The choices are records of the module (workflows, people): the browser
        # loads ``[{value, label}]`` from this path instead of a typed id.
        if raw_options:
            raise ValueError(
                f"resource_report filter {key!r} declares both options and options_path"
            )
        report_filter["options_path"] = _same_origin_api_path(
            options_path,
            label=f"resource_report filter {key!r} options_path",
            allow_tokens=False,
            api_prefix=api_prefix,
        )
        return report_filter
    if isinstance(raw_options, (str, bytes, Mapping)) or not isinstance(raw_options, Iterable):
        raw_options = []
    options = [_resource_report_option(item, filter_key=key) for item in raw_options]
    if not options:
        raise ValueError(f"resource_report select filter {key!r} requires options")
    report_filter["options"] = options
    return report_filter


def _unique_keys(items: list[dict[str, Any]], *, label: str) -> None:
    keys = [item["key"] for item in items]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"resource_report {label} keys must be unique, repeated: {duplicates}")


def _resource_report_surface(surface: Mapping[str, Any], *, api_prefix: str) -> dict[str, Any]:
    """Validate a read-only report: one list path, columns, filters and paging."""
    for refused in _RESOURCE_REPORT_REFUSED_KEYS:
        if refused in surface:
            raise ValueError(f"resource_report control_surface is read-only; {refused} is refused")
    columns = [
        _resource_report_column(item)
        for item in _report_mappings(surface.get("columns"), label="resource_report columns")
    ]
    if not columns:
        raise ValueError("resource_report control_surface requires at least one column")
    _unique_keys(columns, label="column")
    filters = [
        _resource_report_filter(item, api_prefix=api_prefix)
        for item in _report_mappings(surface.get("filters"), label="resource_report filters")
    ]
    _unique_keys(filters, label="filter")
    page_size = surface.get("page_size", _RESOURCE_REPORT_DEFAULT_PAGE_SIZE)
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not _RESOURCE_REPORT_PAGE_SIZE_MIN <= page_size <= _RESOURCE_REPORT_PAGE_SIZE_MAX
    ):
        raise ValueError(
            "resource_report page_size must be an integer from "
            f"{_RESOURCE_REPORT_PAGE_SIZE_MIN} to {_RESOURCE_REPORT_PAGE_SIZE_MAX}, "
            f"got {page_size!r}"
        )
    report: dict[str, Any] = {
        "kind": "resource_report",
        # The browser adds the filter, limit and offset query itself.
        "list_path": _same_origin_api_path(
            surface.get("list_path"),
            label="resource_report list_path",
            allow_tokens=False,
            api_prefix=api_prefix,
        ),
        "items_key": _report_key(
            surface.get("items_key") or "items", label="resource_report items_key"
        ),
        "total_key": _report_key(
            surface.get("total_key") or "total", label="resource_report total_key"
        ),
        "columns": columns,
        "filters": filters,
        "page_size": page_size,
        "empty_text": _normalize_text(surface.get("empty_text")),
        "description": _normalize_text(surface.get("description")),
    }
    for text_key in ("empty_text_key", "description_key"):
        if _normalize_text(surface.get(text_key)):
            report[text_key] = _normalize_text(surface.get(text_key))
    return report


def _status_document_field(value: Mapping[str, Any]) -> dict[str, Any]:
    key = _report_key(value.get("key"), label="status_document field key")
    field_type = _normalize_text(value.get("type"), fallback="text")
    if field_type not in _STATUS_DOCUMENT_FIELD_TYPES:
        raise ValueError(f"status_document field {key!r} has unknown type {field_type!r}")
    return {
        "key": key,
        "type": field_type,
        **_report_label(value, label=f"status_document field {key!r}"),
    }


def _status_document_surface(surface: Mapping[str, Any], *, api_prefix: str) -> dict[str, Any]:
    """Validate a status page: GET one object, show chosen keys, PUT a text document."""
    for refused in _STATUS_DOCUMENT_REFUSED_KEYS:
        if refused in surface:
            raise ValueError(f"status_document control_surface does not take {refused}")
    fields = [
        _status_document_field(item)
        for item in _report_mappings(surface.get("fields"), label="status_document fields")
    ]
    if not fields:
        raise ValueError("status_document control_surface requires at least one field")
    keys = [item["key"] for item in fields]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"status_document field keys must be unique, repeated: {duplicates}")
    max_bytes = surface.get("max_bytes", _STATUS_DOCUMENT_DEFAULT_MAX_BYTES)
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= _STATUS_DOCUMENT_MAX_BYTES_LIMIT
    ):
        raise ValueError(
            "status_document max_bytes must be an integer from 1 to "
            f"{_STATUS_DOCUMENT_MAX_BYTES_LIMIT}, got {max_bytes!r}"
        )
    accept = _normalize_text(surface.get("accept"))
    if accept and not _STATUS_DOCUMENT_ACCEPT.fullmatch(accept):
        raise ValueError(
            "status_document accept must list file extensions or media types, "
            f"such as '.txt,.json', got {accept!r}"
        )
    document: dict[str, Any] = {
        "kind": "status_document",
        # Both paths are called verbatim: no tokens, no query.
        "status_path": _same_origin_api_path(
            surface.get("status_path"),
            label="status_document status_path",
            allow_tokens=False,
            api_prefix=api_prefix,
        ),
        "upload_path": _same_origin_api_path(
            surface.get("upload_path"),
            label="status_document upload_path",
            allow_tokens=False,
            api_prefix=api_prefix,
        ),
        "upload_field": _report_key(
            surface.get("upload_field") or "document", label="status_document upload_field"
        ),
        "fields": fields,
        "accept": accept,
        "max_bytes": max_bytes,
    }
    status_key = _normalize_text(surface.get("status_key"))
    if status_key:
        document["status_key"] = _report_key(status_key, label="status_document status_key")
    for text_key in ("description", "empty_text", "upload_label", "upload_description"):
        document[text_key] = _normalize_text(surface.get(text_key))
        catalogue_key = _normalize_text(surface.get(f"{text_key}_key"))
        if catalogue_key:
            document[f"{text_key}_key"] = catalogue_key
    return document


def _localized_status_document(surface: Mapping[str, Any], locale: str) -> dict[str, Any]:
    localized = dict(surface)
    for text_key in ("description", "empty_text", "upload_label", "upload_description"):
        catalogue_key = _normalize_text(surface.get(f"{text_key}_key"))
        if catalogue_key:
            localized[text_key] = translate_ui_text(
                catalogue_key, locale=locale, default=surface.get(text_key, "")
            )
    fields = []
    for field in surface.get("fields") or ():
        copy = dict(field)
        label_key = _normalize_text(field.get("label_key"))
        if label_key:
            copy["label"] = translate_ui_text(
                label_key, locale=locale, default=field.get("label", "")
            )
        fields.append(copy)
    localized["fields"] = fields
    return localized


def _localized_resource_admin(surface: Mapping[str, Any], locale: str) -> dict[str, Any]:
    """Translate the field labels, help texts and option labels that name a key."""

    def translated(item: Mapping[str, Any], *pairs: tuple[str, str]) -> dict[str, Any]:
        copy = dict(item)
        for text_key, catalogue_field in pairs:
            catalogue_key = _normalize_text(item.get(catalogue_field))
            if catalogue_key:
                copy[text_key] = translate_ui_text(
                    catalogue_key, locale=locale, default=item.get(text_key, "")
                )
        return copy

    localized = dict(surface)
    resources = []
    for resource in surface.get("resources") or ():
        resource_copy = dict(resource)
        fields = []
        for field in resource.get("fields") or ():
            copy = translated(field, ("label", "label_key"), ("help", "help_key"))
            if "options" in field:
                copy["options"] = [
                    translated(
                        option,
                        ("label", "label_key"),
                        ("description", "description_key"),
                    )
                    for option in field["options"]
                ]
            fields.append(copy)
        resource_copy["fields"] = fields
        if "row_actions" in resource:
            resource_copy["row_actions"] = [
                translated(
                    action,
                    ("label", "label_key"),
                    ("confirm", "confirm_key"),
                    ("success_text", "success_text_key"),
                )
                for action in resource["row_actions"]
            ]
        resources.append(resource_copy)
    localized["resources"] = resources
    return localized


def _localized_surface(surface: Any, locale: str) -> Any:
    """Translate a resource_report's labels through the UI catalogue, like page titles.

    A label with a ``label_key`` (and ``empty_text``/``description`` with their
    ``*_key``) is looked up in the user's locale; the declared text is the
    fallback. The registered surface is never changed.
    """
    if not isinstance(surface, Mapping) or locale == "en":
        return surface
    if surface.get("kind") == "status_document":
        return _localized_status_document(surface, locale)
    if surface.get("kind") == "resource_admin":
        return _localized_resource_admin(surface, locale)
    if surface.get("kind") != "resource_report":
        return surface

    def translated(item: Mapping[str, Any], field: str, key_field: str) -> dict[str, Any]:
        copy = dict(item)
        key = _normalize_text(item.get(key_field))
        if key:
            copy[field] = translate_ui_text(key, locale=locale, default=item.get(field, ""))
        return copy

    localized = translated(surface, "empty_text", "empty_text_key")
    localized["description"] = translated(surface, "description", "description_key")["description"]
    localized["columns"] = [
        translated(
            translated(column, "label", "label_key"),
            "link_missing_label",
            "link_missing_label_key",
        )
        for column in surface.get("columns") or ()
    ]
    filters = []
    for report_filter in surface.get("filters") or ():
        copy = translated(report_filter, "label", "label_key")
        if "options" in report_filter:
            copy["options"] = [
                translated(option, "label", "label_key") for option in report_filter["options"]
            ]
        filters.append(copy)
    localized["filters"] = filters
    return localized


def _normalize_control_surface(
    control_surface: Mapping[str, Any] | None,
    *,
    component_key: str,
    api_prefix: str = "/api/",
) -> dict[str, Any] | None:
    """Validate a declared control surface; host-renderer kinds are checked strictly.

    A page with a compiled ``component_key`` owns its surface and may use any
    kind. A page without one is rendered by the Core bundle, so its surface
    must be a host-renderer kind whose resources stay on same-origin API paths
    under ``api_prefix``: ``/api/`` for Core, the module's own tree for a module.
    """

    if not isinstance(control_surface, Mapping):
        return None
    surface = dict(control_surface)
    kind = _normalize_text(surface.get("kind"))
    if kind not in HOST_RENDERER_KINDS:
        if component_key:
            return surface
        raise ValueError(
            f"control_surface kind {kind!r} has no host renderer; "
            f"supported kinds: {sorted(HOST_RENDERER_KINDS)}"
        )
    if kind == "resource_report":
        return _resource_report_surface(surface, api_prefix=api_prefix)
    if kind == "status_document":
        return _status_document_surface(surface, api_prefix=api_prefix)
    raw_resources = surface.get("resources")
    if isinstance(raw_resources, (str, bytes, Mapping)) or not isinstance(raw_resources, Iterable):
        raw_resources = []
    resources = [_resource_admin_resource(item, api_prefix=api_prefix) for item in raw_resources]
    if not resources:
        raise ValueError("resource_admin control_surface requires at least one resource")
    surface["kind"] = kind
    surface["resources"] = resources
    return surface


def _has_host_renderer(record: Mapping[str, Any]) -> bool:
    surface = record.get("control_surface")
    return isinstance(surface, Mapping) and surface.get("kind") in HOST_RENDERER_KINDS


def _route_path(value: Any) -> str:
    path = _normalize_text(value)
    return path.rstrip("/") or "/"


def _owner_label(source_module: str | None) -> str:
    return f"platform module {source_module!r}" if source_module else "Core"


def _reject_page_collision(
    registries: MutableMapping[str, Any],
    *,
    page_key: str,
    path: str,
    source_module: str | None,
) -> None:
    """Refuse a registration that would replace a page another owner registered.

    Owners are Core (no ``source_module``) and each platform module. A page that
    Core marks as its own is never replaceable, not even by Core. Within one owner
    a later registration replaces the earlier one. The one exception is the
    unmarked Core ``extensions`` entry, which any owner may replace under the same
    page key.
    """

    target_path = _route_path(path)
    for registry_name in _PAGE_REGISTRY_NAMES:
        registry = registries.get(registry_name)
        if not isinstance(registry, Mapping):
            continue
        for record in registry.values():
            if not isinstance(record, Mapping):
                continue
            same_key = record.get("page_key") == page_key
            same_path = _route_path(record.get("path")) == target_path
            if not (same_key or same_path):
                continue
            if record.get(_CORE_DEFINITION_MARKER):
                if same_key:
                    raise ValueError(f"page key {page_key!r} is reserved by a Core page")
                raise ValueError(
                    f"page path {path!r} is reserved by Core page {record.get('page_key')!r}"
                )
            owner = _normalize_text(record.get(SOURCE_MODULE_FIELD)) or None
            if owner == source_module:
                continue
            if same_key and owner is None and page_key in _REPLACEABLE_CORE_PAGE_KEYS:
                continue
            what = f"page key {page_key!r}" if same_key else f"page path {path!r}"
            raise ValueError(
                f"{what} already belongs to {_owner_label(owner)} "
                f"(page {record.get('page_key')!r}); {_owner_label(source_module)} "
                "cannot replace it"
            )


def _build_page_record(
    *,
    key: str = "",
    page_key: str = "",
    label: str = "",
    title: str = "",
    path: str,
    component_key: str = "",
    icon: str = "",
    nav_section: str = "system",
    nav_order: int | None = None,
    render_mode: str = "react_shell",
    required_permission: str = "",
    required_entitlement: str | Iterable[str] = "",
    edition_visibility: Iterable[str] | str = _COMMUNITY_SAFE_EDITIONS,
    core_page: bool = False,
    sidebar_nav: bool = False,
    settings_tab: bool = False,
    sort_order: int = 100,
    nav_label: str = "",
    nav_icon: str = "",
    current_pages: Iterable[str] | None = None,
    loading_text: str = "",
    control_surface: Mapping[str, Any] | None = None,
    api_prefix: str = "/api/",
) -> dict[str, Any]:
    normalized_page_key = _normalize_text(key or page_key)
    normalized_label = _normalize_text(label or nav_label or title, fallback=normalized_page_key)
    normalized_icon = _normalize_text(icon or nav_icon, fallback="file")
    normalized_order = int(nav_order if nav_order is not None else sort_order)
    normalized_component_key = _normalize_text(component_key)
    record = {
        "key": normalized_page_key,
        "page_key": normalized_page_key,
        "label": normalized_label,
        "title": _normalize_text(title or label, fallback=normalized_label),
        "path": _normalize_text(path, fallback="/"),
        "component_key": normalized_component_key,
        "nav_section": _normalize_text(nav_section, fallback="system"),
        "render_mode": _normalize_text(render_mode, fallback="react_shell"),
        "required_permission": _page_permission(required_permission, normalized_page_key),
        "required_entitlement": _page_entitlement(required_entitlement, normalized_page_key),
        "edition_visibility": _normalize_multi_text(
            edition_visibility,
            fallback=_COMMUNITY_SAFE_EDITIONS,
        ),
        "core_page": bool(core_page),
        "sidebar_nav": bool(sidebar_nav),
        "settings_tab": bool(settings_tab),
        "sort_order": normalized_order,
        "nav_order": normalized_order,
        "nav_label": normalized_label,
        "nav_icon": normalized_icon,
        "icon": normalized_icon,
        "current_pages": list(
            _normalize_multi_text(
                current_pages or (normalized_page_key,),
                fallback=(normalized_page_key,),
            )
        ),
        "loading_text": _normalize_text(loading_text),
    }
    surface = _normalize_control_surface(
        control_surface, component_key=normalized_component_key, api_prefix=api_prefix
    )
    if surface is not None:
        record["control_surface"] = surface
    return record


def _store_page_record(registries: MutableMapping[str, Any], record: dict[str, Any]) -> None:
    page_key = record["page_key"]
    settings_registry = registries.setdefault("settings_pages", {})
    navigation_registry = registries.setdefault("navigation_items", {})
    page_registry = registries.setdefault("registered_pages", {})
    if record["settings_tab"] or str(record["path"]).startswith("/settings"):
        settings_registry[page_key] = record
    else:
        settings_registry.pop(page_key, None)

    if record["sidebar_nav"]:
        navigation_registry[page_key] = record
    else:
        navigation_registry.pop(page_key, None)

    # A page reaches the shell either through a compiled component or through a
    # host renderer selected by its control-surface kind.
    renderable = bool(record["component_key"]) or _has_host_renderer(record)
    if renderable and record["render_mode"] == "react_shell":
        page_registry[page_key] = record
    else:
        page_registry.pop(page_key, None)


def register_page(
    registries: MutableMapping[str, Any],
    *,
    key: str = "",
    page_key: str = "",
    label: str = "",
    title: str = "",
    path: str,
    component_key: str = "",
    icon: str = "",
    nav_section: str = "system",
    nav_order: int | None = None,
    render_mode: str = "react_shell",
    required_permission: str = "",
    required_entitlement: str | Iterable[str] = "",
    edition_visibility: Iterable[str] | str = _COMMUNITY_SAFE_EDITIONS,
    core_page: bool = False,
    sidebar_nav: bool = False,
    settings_tab: bool = False,
    sort_order: int = 100,
    nav_label: str = "",
    nav_icon: str = "",
    current_pages: Iterable[str] | None = None,
    loading_text: str = "",
    control_surface: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Register one internal React shell page/navigation record.

    Raises ``ValueError`` when the declared control surface is invalid, when
    the page key or path would replace a page another owner registered (Core or
    another platform module), or when a host-rendered page comes from neither
    Core nor a prepared platform module. Raises :class:`PageRegistrySealedError`
    once the composition root sealed the registries.

    Inside a prepared module's :func:`page_registration_scope` the record is
    stamped with that module's id as ``source_module`` (no argument can set it)
    and host-rendered resources must live under ``/api/modules/<module id>/``.
    Outside any scope the caller is Core, which is only possible before sealing.
    """

    _refuse_sealed(registries, _normalize_text(key or page_key))
    scope = _REGISTRATION_SCOPE.get()
    source_module = scope.module_id if scope is not None else None
    record = _build_page_record(
        key=key,
        page_key=page_key,
        label=label,
        title=title,
        path=path,
        component_key=component_key,
        icon=icon,
        nav_section=nav_section,
        nav_order=nav_order,
        render_mode=render_mode,
        required_permission=required_permission,
        required_entitlement=required_entitlement,
        edition_visibility=edition_visibility,
        core_page=core_page,
        sidebar_nav=sidebar_nav,
        settings_tab=settings_tab,
        sort_order=sort_order,
        nav_label=nav_label,
        nav_icon=nav_icon,
        current_pages=current_pages,
        loading_text=loading_text,
        control_surface=control_surface,
        api_prefix=module_api_prefix(source_module) if source_module else "/api/",
    )
    if scope is not None and source_module is None and _has_host_renderer(record):
        # ADR 0082, section 6: only Core and verified platform modules may use a
        # renderer that ships in the Core bundle.
        raise ValueError(
            f"page {record['page_key']!r} uses a host renderer; only Core and prepared "
            "platform modules may register host-rendered pages"
        )
    if scope is None or source_module is not None:
        # An environment-listed bootstrap module keeps its pre-ADR 0082 behaviour
        # and may replace any page; Core and platform modules may not.
        _reject_page_collision(
            registries,
            page_key=record["page_key"],
            path=record["path"],
            source_module=source_module,
        )
    if source_module:
        record[SOURCE_MODULE_FIELD] = source_module
    _store_page_record(registries, record)
    if scope is not None:
        scope.page_keys.append(record["page_key"])
    return dict(record)


def register_settings_page(
    registries: MutableMapping[str, Any],
    *,
    page_key: str,
    title: str,
    path: str,
    nav_section: str,
    render_mode: str,
    required_permission: str = "",
    required_entitlement: str | Iterable[str] = "",
    edition_visibility: Iterable[str] | str = _COMMUNITY_SAFE_EDITIONS,
    core_page: bool = False,
    sidebar_nav: bool = False,
    settings_tab: bool = False,
    sort_order: int = 100,
    nav_label: str = "",
    nav_icon: str = "",
    current_pages: Iterable[str] | None = None,
    loading_text: str = "",
    control_surface: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Register one internal settings or shell-navigation page."""

    return register_page(
        registries,
        page_key=page_key,
        title=title,
        path=path,
        nav_section=nav_section,
        render_mode=render_mode,
        required_permission=required_permission,
        required_entitlement=required_entitlement,
        edition_visibility=edition_visibility,
        core_page=core_page,
        sidebar_nav=sidebar_nav,
        settings_tab=settings_tab,
        sort_order=sort_order,
        nav_label=nav_label,
        nav_icon=nav_icon,
        current_pages=current_pages,
        loading_text=loading_text,
        control_surface=control_surface,
    )


def register_core_settings_pages(registries: MutableMapping[str, Any]) -> None:
    """Register the Community-owned settings and navigation entries.

    Definitions flagged ``core_page`` are reserved: a later registration with
    the same page key or path is refused. The Extensions entry is not a Core
    page and stays replaceable by a same-key registration.
    """

    _refuse_sealed(registries, "core settings pages")
    for definition in _CORE_SETTINGS_PAGE_DEFINITIONS:
        record = _build_page_record(**definition)
        record["_title_i18n_key"] = _CORE_SETTINGS_TITLE_KEYS[record["page_key"]]
        if record["core_page"]:
            record[_CORE_DEFINITION_MARKER] = True
        _store_page_record(registries, record)


def _localized_page_record(record: dict[str, Any], locale: str) -> dict[str, Any]:
    localized = dict(record)
    localized.pop(_CORE_DEFINITION_MARKER, None)
    localized.pop(SOURCE_MODULE_FIELD, None)
    title_key = localized.pop("_title_i18n_key", None)
    if "control_surface" in localized:
        localized["control_surface"] = _localized_surface(localized["control_surface"], locale)
    if locale == "en" or not title_key:
        return localized
    title = translate_ui_text(title_key, locale=locale, default=record.get("title", ""))
    for field in ("title", "label", "nav_label"):
        localized[field] = title
    return localized


def resolve_registered_settings_pages(
    registries: dict[str, Any],
    *,
    edition: str = "community",
    auth_user: dict[str, Any] | None = None,
    locale: str | None = None,
    include_core: bool = False,
    authorization_service: Any = None,
    entitlement_service: Any = None,
) -> list[dict[str, Any]]:
    """Resolve visible settings-hub page records for the current edition/user."""

    locale = normalize_ui_locale(locale) or resolve_ui_locale(auth_user=auth_user)
    rows = []
    for record in (registries.get("settings_pages") or {}).values():
        if not isinstance(record, dict) or not record.get("settings_tab"):
            continue
        if not include_core and record.get("core_page"):
            continue
        if not _page_visible(
            record,
            edition=edition,
            auth_user=auth_user,
            authorization_service=authorization_service,
            entitlement_service=entitlement_service,
        ):
            continue
        rows.append(_localized_page_record(record, locale))
    return sorted(
        rows, key=lambda item: (int(item.get("sort_order", 100)), item.get("page_key", ""))
    )


def resolve_registered_navigation_sections(
    registries: dict[str, Any],
    *,
    edition: str = "community",
    auth_user: dict[str, Any] | None = None,
    locale: str | None = None,
    include_core: bool = False,
    authorization_service: Any = None,
    entitlement_service: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve visible sidebar navigation items grouped by section."""

    locale = normalize_ui_locale(locale) or resolve_ui_locale(auth_user=auth_user)
    sections: dict[str, list[dict[str, Any]]] = {}
    for record in (registries.get("navigation_items") or {}).values():
        if not isinstance(record, dict):
            continue
        if not include_core and record.get("core_page"):
            continue
        if not _page_visible(
            record,
            edition=edition,
            auth_user=auth_user,
            authorization_service=authorization_service,
            entitlement_service=entitlement_service,
        ):
            continue
        section = _normalize_text(record.get("nav_section"), fallback="system")
        sections.setdefault(section, []).append(_localized_page_record(record, locale))

    for section in sections:
        sections[section].sort(
            key=lambda item: (int(item.get("sort_order", 100)), item.get("page_key", ""))
        )
    return sections


def resolve_registered_pages(
    registries: dict[str, Any],
    *,
    edition: str = "community",
    auth_user: dict[str, Any] | None = None,
    locale: str | None = None,
    include_core: bool = False,
    authorization_service: Any = None,
    entitlement_service: Any = None,
) -> list[dict[str, Any]]:
    """Resolve visible generic React shell page records for the current edition/user."""

    locale = normalize_ui_locale(locale) or resolve_ui_locale(auth_user=auth_user)
    rows = []
    for record in (registries.get("registered_pages") or {}).values():
        if not isinstance(record, dict):
            continue
        if not include_core and record.get("core_page"):
            continue
        if not _page_visible(
            record,
            edition=edition,
            auth_user=auth_user,
            authorization_service=authorization_service,
            entitlement_service=entitlement_service,
        ):
            continue
        rows.append(_localized_page_record(record, locale))
    return sorted(
        rows,
        key=lambda item: (
            int(item.get("nav_order", item.get("sort_order", 100))),
            item.get("page_key", ""),
        ),
    )


def resolve_registered_shell_paths(registries: dict[str, Any]) -> tuple[str, ...]:
    """Resolve explicitly registered React shell paths for direct browser navigation."""

    paths: list[str] = []
    for record in (registries.get("registered_pages") or {}).values():
        if not isinstance(record, dict):
            continue
        if _normalize_text(record.get("render_mode"), fallback="react_shell") != "react_shell":
            continue
        path = _normalize_text(record.get("path"))
        if path.startswith("/") and path not in paths:
            paths.append(path)
    return tuple(paths)


__all__ = [
    "HOST_RENDERER_KINDS",
    "PAGE_PERMISSIONS",
    "SOURCE_MODULE_FIELD",
    "PageRegistrationScope",
    "PageRegistrySealedError",
    "current_registration_module",
    "page_registration_scope",
    "page_registries_sealed",
    "register_core_settings_pages",
    "register_page",
    "register_settings_page",
    "resolve_registered_navigation_sections",
    "resolve_registered_pages",
    "resolve_registered_settings_pages",
    "resolve_registered_shell_paths",
    "seal_page_registries",
]
