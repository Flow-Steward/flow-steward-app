"""UI localization helpers backed by gettext catalogs."""

from __future__ import annotations

import gettext
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_UI_LOCALE = "en"
UI_LOCALE_COOKIE = "ui-locale"
UI_GETTEXT_DOMAIN = "ui"
LOCALES_ROOT = Path(__file__).resolve().parents[3] / "locales"
LOCALE_META_FILENAME = "meta.json"


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _locale_catalog_path(locale_code: str) -> Path:
    return LOCALES_ROOT / locale_code / "LC_MESSAGES" / f"{UI_GETTEXT_DOMAIN}.mo"


def _locale_meta_path(locale_code: str) -> Path:
    return LOCALES_ROOT / locale_code / LOCALE_META_FILENAME


def _load_locale_meta(locale_code: str) -> dict[str, str]:
    meta_path = _locale_meta_path(locale_code)
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "code": str(payload.get("code") or locale_code).strip().lower(),
        "label": str(payload.get("label") or locale_code).strip(),
        "native_label": str(
            payload.get("native_label") or payload.get("label") or locale_code
        ).strip(),
    }


def _locale_codes_from_filesystem() -> list[str]:
    codes: set[str] = set()
    if not LOCALES_ROOT.exists():
        return [DEFAULT_UI_LOCALE]
    for entry in LOCALES_ROOT.iterdir():
        if not entry.is_dir():
            continue
        code = str(entry.name or "").strip().lower()
        if not code:
            continue
        if _locale_catalog_path(code).exists():
            codes.add(code)
    if DEFAULT_UI_LOCALE not in codes:
        codes.add(DEFAULT_UI_LOCALE)
    return sorted(codes)


@lru_cache(maxsize=1)
def _supported_locale_entries_cached() -> tuple[dict[str, str], ...]:
    codes = _locale_codes_from_filesystem()
    entries: list[dict[str, str]] = []
    for code in codes:
        payload = _load_locale_meta(code)
        entries.append(
            {
                "code": code,
                "label": payload.get("label") or code,
                "native_label": payload.get("native_label") or payload.get("label") or code,
            }
        )
    entries.sort(key=lambda row: (0 if row["code"] == DEFAULT_UI_LOCALE else 1, row["code"]))
    return tuple(entries)


def supported_ui_locales() -> list[dict[str, str]]:
    return [dict(entry) for entry in _supported_locale_entries_cached()]


def _supported_locale_codes() -> set[str]:
    return {entry["code"] for entry in _supported_locale_entries_cached()}


def normalize_ui_locale(value: Any) -> str:
    text = str(value or "").strip().replace("_", "-").lower()
    if not text:
        return ""
    primary = text.split(",", 1)[0].split(";", 1)[0].strip()
    if not primary:
        return ""
    code = primary.split("-", 1)[0]
    return code if code in _supported_locale_codes() else ""


def _accept_language_candidates(header_value: Any) -> list[str]:
    entries: list[tuple[float, str]] = []
    for part in str(header_value or "").split(","):
        chunk = str(part or "").strip()
        if not chunk:
            continue
        language, _, params_text = chunk.partition(";")
        quality = 1.0
        for param in params_text.split(";"):
            key, _, value = str(param or "").strip().partition("=")
            if key != "q":
                continue
            try:
                quality = float(value)
            except (TypeError, ValueError):
                quality = 0.0
        entries.append((quality, language))
    entries.sort(key=lambda item: item[0], reverse=True)
    return [entry[1] for entry in entries if entry[0] > 0]


def resolve_ui_locale(
    request: Any | None = None,
    *,
    auth_user: Mapping[str, Any] | None = None,
    default: str = DEFAULT_UI_LOCALE,
) -> str:
    request_state = getattr(request, "state", None)
    state_auth_user = getattr(request_state, "auth_user", None)
    resolved_auth_user = auth_user if isinstance(auth_user, Mapping) else None
    if resolved_auth_user is None and isinstance(state_auth_user, Mapping):
        resolved_auth_user = state_auth_user

    candidates: list[Any] = []
    query_candidates: list[Any] = []
    cookie_candidates: list[Any] = []
    header_candidates: list[str] = []
    if request is not None:
        query_params = getattr(request, "query_params", None)
        cookies = getattr(request, "cookies", None)
        headers = getattr(request, "headers", None)
        if query_params is not None:
            query_candidates.extend([query_params.get("lang"), query_params.get("locale")])
        if cookies is not None:
            cookie_candidates.extend(
                [
                    cookies.get(UI_LOCALE_COOKIE),
                ]
            )
        if headers is not None:
            accept_language = headers.get("accept-language") if headers is not None else ""
            header_candidates.extend(_accept_language_candidates(accept_language))

    candidates.extend(query_candidates)
    if resolved_auth_user is not None:
        candidates.extend(
            [
                resolved_auth_user.get("locale"),
                resolved_auth_user.get("language"),
                resolved_auth_user.get("preferred_locale"),
                resolved_auth_user.get("preferred_language"),
            ]
        )
    candidates.extend(cookie_candidates)
    candidates.extend(header_candidates)

    for candidate in candidates:
        normalized = normalize_ui_locale(candidate)
        if normalized:
            return normalized
    return normalize_ui_locale(default) or DEFAULT_UI_LOCALE


@lru_cache(maxsize=64)
def _gettext_translation(locale: str) -> gettext.NullTranslations:
    language = normalize_ui_locale(locale) or DEFAULT_UI_LOCALE
    return gettext.translation(
        UI_GETTEXT_DOMAIN,
        localedir=str(LOCALES_ROOT),
        languages=[language],
        fallback=True,
    )


@lru_cache(maxsize=64)
def _catalog_messages(locale: str) -> dict[str, str]:
    translation = _gettext_translation(locale)
    catalog = getattr(translation, "_catalog", {})
    if not isinstance(catalog, dict):
        return {}
    messages: dict[str, str] = {}
    for raw_key, raw_value in catalog.items():
        if not isinstance(raw_key, str) or raw_key == "":
            continue
        if isinstance(raw_value, str):
            messages[raw_key] = raw_value
    return messages


def ui_messages_for_locale(locale: Any) -> dict[str, str]:
    normalized = normalize_ui_locale(locale) or DEFAULT_UI_LOCALE
    default_messages = _catalog_messages(DEFAULT_UI_LOCALE)
    if normalized == DEFAULT_UI_LOCALE:
        return dict(default_messages)
    merged = dict(default_messages)
    merged.update(_catalog_messages(normalized))
    return merged


def translate_ui_text(
    key: str,
    *,
    locale: Any | None = None,
    request: Any | None = None,
    auth_user: Mapping[str, Any] | None = None,
    default: str = "",
    params: Mapping[str, Any] | None = None,
) -> str:
    normalized_locale = normalize_ui_locale(locale)
    if not normalized_locale:
        normalized_locale = resolve_ui_locale(request, auth_user=auth_user)
    messages = ui_messages_for_locale(normalized_locale)
    template = messages.get(str(key or "").strip())
    if template is None:
        template = default if default != "" else str(key or "")
    if not params:
        return str(template)
    try:
        return str(template).format_map(_SafeFormatDict(params))
    except Exception:
        return str(template)


def build_client_i18n_payload(
    request: Any | None = None,
    *,
    auth_user: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    locale = resolve_ui_locale(request, auth_user=auth_user)
    return {
        "locale": locale,
        "fallback_locale": DEFAULT_UI_LOCALE,
        "supported_locales": supported_ui_locales(),
        "messages": ui_messages_for_locale(locale),
    }


def reset_ui_i18n_cache() -> None:
    """Clear locale/catalog caches; useful for tests and local tooling."""
    _supported_locale_entries_cached.cache_clear()
    _gettext_translation.cache_clear()
    _catalog_messages.cache_clear()
