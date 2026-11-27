"""Internal settings-page and shell-navigation registration helpers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from core.application.auth.route_permissions import module_api_prefix
from core.application.reports.ui_page_core_definitions import (
    _COMMUNITY_SAFE_EDITIONS,
    _CORE_SETTINGS_PAGE_DEFINITIONS,
    _CORE_SETTINGS_TITLE_KEYS,
)
from core.application.reports.ui_page_surfaces import (
    HOST_RENDERER_KINDS,
    _localized_surface,
    _normalize_control_surface,
    _normalize_text,
)
from core.application.settings.platform_contracts import INSTALLATION_ADMIN_PERMISSION
from core.application.settings.ui_i18n import (
    normalize_ui_locale,
    resolve_ui_locale,
    translate_ui_text,
)

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
