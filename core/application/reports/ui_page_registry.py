"""Internal settings-page and shell-navigation registration helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

from core.application.settings.ui_i18n import (
    normalize_ui_locale,
    resolve_ui_locale,
    translate_ui_text,
)

_COMMUNITY_SAFE_EDITIONS = ("community", "private")

_CORE_SETTINGS_TITLE_KEYS = {
    "settings_home": "shell.nav.settings",
    "extensions": "shell.nav.extensions",
    "general": "registry.workflow_detail.settings.general",
    "security": "account.security.title",
    "retention": "settings.retention.title",
    "notifications": "settings.tab.notifications",
    "service_accounts": "react.settings.service_accounts",
    "model_providers": "settings.tab.model_providers",
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
        "title": "Model Providers",
        "path": "/settings/providers",
        "nav_section": "settings_hub",
        "render_mode": "react_shell",
        "required_permission": "view_settings",
        "edition_visibility": _COMMUNITY_SAFE_EDITIONS,
        "core_page": True,
        "sidebar_nav": False,
        "settings_tab": True,
        "sort_order": 70,
        "loading_text": "Loading model providers...",
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


def _entitlement_visible(required_entitlement: str, entitlement_service: Any = None) -> bool:
    feature = _normalize_text(required_entitlement).lower()
    if not feature:
        return True
    is_enabled = getattr(entitlement_service, "is_enabled", None)
    if callable(is_enabled):
        try:
            return bool(is_enabled(feature))
        except Exception:
            return False
    return False


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
        _normalize_text(record.get("required_entitlement")),
        entitlement_service,
    ):
        return False
    return _permission_visible(
        _normalize_text(record.get("required_permission")),
        auth_user,
        authorization_service=authorization_service,
    )


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
    required_entitlement: str = "",
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
    """Register one internal React shell page/navigation record."""

    normalized_page_key = _normalize_text(key or page_key)
    normalized_label = _normalize_text(label or nav_label or title, fallback=normalized_page_key)
    normalized_icon = _normalize_text(icon or nav_icon, fallback="file")
    normalized_order = int(nav_order if nav_order is not None else sort_order)
    record = {
        "key": normalized_page_key,
        "page_key": normalized_page_key,
        "label": normalized_label,
        "title": _normalize_text(title or label, fallback=normalized_label),
        "path": _normalize_text(path, fallback="/"),
        "component_key": _normalize_text(component_key),
        "nav_section": _normalize_text(nav_section, fallback="system"),
        "render_mode": _normalize_text(render_mode, fallback="react_shell"),
        "required_permission": _normalize_text(required_permission),
        "required_entitlement": _normalize_text(required_entitlement),
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
    if isinstance(control_surface, Mapping):
        record["control_surface"] = dict(control_surface)

    settings_registry = registries.setdefault("settings_pages", {})
    navigation_registry = registries.setdefault("navigation_items", {})
    page_registry = registries.setdefault("registered_pages", {})
    if record["settings_tab"] or str(record["path"]).startswith("/settings"):
        settings_registry[normalized_page_key] = record
    else:
        settings_registry.pop(normalized_page_key, None)

    if record["sidebar_nav"]:
        navigation_registry[normalized_page_key] = record
    else:
        navigation_registry.pop(normalized_page_key, None)

    if record["component_key"] and record["render_mode"] == "react_shell":
        page_registry[normalized_page_key] = record
    else:
        page_registry.pop(normalized_page_key, None)

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
    required_entitlement: str = "",
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
    """Register the Community-owned settings and navigation entries."""

    for definition in _CORE_SETTINGS_PAGE_DEFINITIONS:
        register_settings_page(registries, **definition)
        page_key = definition["page_key"]
        for registry_name in ("settings_pages", "navigation_items", "registered_pages"):
            record = registries[registry_name].get(page_key)
            if record is not None:
                record["_title_i18n_key"] = _CORE_SETTINGS_TITLE_KEYS[page_key]


def _localized_page_record(record: dict[str, Any], locale: str) -> dict[str, Any]:
    localized = dict(record)
    title_key = localized.pop("_title_i18n_key", None)
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
    "register_core_settings_pages",
    "register_page",
    "register_settings_page",
    "resolve_registered_navigation_sections",
    "resolve_registered_pages",
    "resolve_registered_settings_pages",
    "resolve_registered_shell_paths",
]
