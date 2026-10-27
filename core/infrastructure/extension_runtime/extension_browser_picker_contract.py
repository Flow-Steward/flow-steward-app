"""Validate browser picker authority, canonical URLs and JSON/UTF-16 budgets."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

from .extension_runtime_validation import _as_dict, _as_list

_EXTERNAL_PICKER_DATA_KEYS = {
    "allowed_connect_origins",
    "allowed_frame_origins",
    "allowed_script_origins",
    "bootstrap_action_id",
    "bridge_global",
    "busy_label",
    "button_label",
    "error_message",
    "options",
    "scripts",
    "selection",
    "target_component_id",
}
_EXTERNAL_PICKER_SELECTION_KEYS = {"fields", "maximum"}
_EXTERNAL_PICKER_FIELD_KEYS = {"max_length", "required", "source", "target"}
_EXTERNAL_PICKER_SCRIPT_KEYS = ({"src"}, {"asset_ref"})
_EXTERNAL_PICKER_ASSET_RE = re.compile(r"assets/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.js")
_EXTERNAL_PICKER_FIELD_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")
_EXTERNAL_PICKER_BRIDGE_RE = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*){0,4}"
)
_EXTERNAL_PICKER_ACTION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CANONICAL_HTTPS_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_PERCENT_ENCODED_DOT_RE = re.compile(r"%2e", re.IGNORECASE)
_WHATWG_RAW_URL_CHARACTERS = frozenset("\"'<>^`{}")
_ECMASCRIPT_TRIM_CHARACTERS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _normalize_utf16(value: str) -> tuple[str, int] | None:
    normalized: list[str] = []
    units = 0
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(value):
                return None
            low = ord(value[index + 1])
            if not 0xDC00 <= low <= 0xDFFF:
                return None
            normalized.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)))
            units += 2
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            return None
        normalized.append(value[index])
        units += 2 if codepoint > 0xFFFF else 1
        index += 1
    return "".join(normalized), units


def _utf16_length(value: str) -> int | None:
    normalized = _normalize_utf16(value)
    return normalized[1] if normalized is not None else None


def _browser_trim(value: str) -> str:
    return value.strip(_ECMASCRIPT_TRIM_CHARACTERS)


def _json_string_budget(value: str) -> int | None:
    normalized = _normalize_utf16(value)
    if normalized is None:
        return None
    value = normalized[0]
    budget = 2
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
            budget += 2
        elif codepoint < 0x20:
            budget += 6
        else:
            budget += 2 if codepoint > 0xFFFF else 1
    return budget


_INVALID_JSON_UNICODE = object()


def _normalize_json_unicode(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    if depth > 32:
        return _INVALID_JSON_UNICODE
    if isinstance(value, str):
        normalized = _normalize_utf16(value)
        return normalized[0] if normalized is not None else _INVALID_JSON_UNICODE
    if not isinstance(value, (dict, list)):
        return value
    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return _INVALID_JSON_UNICODE
    seen.add(identity)
    try:
        if isinstance(value, list):
            result: list[Any] = []
            for item in value:
                normalized_item = _normalize_json_unicode(item, depth=depth + 1, seen=seen)
                if normalized_item is _INVALID_JSON_UNICODE:
                    return _INVALID_JSON_UNICODE
                result.append(normalized_item)
            return result
        result_dict: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = _normalize_json_unicode(key, depth=depth + 1, seen=seen)
            normalized_item = _normalize_json_unicode(item, depth=depth + 1, seen=seen)
            if (
                normalized_key is _INVALID_JSON_UNICODE
                or normalized_item is _INVALID_JSON_UNICODE
                or normalized_key in result_dict
            ):
                return _INVALID_JSON_UNICODE
            result_dict[normalized_key] = normalized_item
        return result_dict
    finally:
        seen.remove(identity)


def _json_structural_budget(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> int | None:
    if depth > 32:
        return None
    if value is None:
        return 4
    if isinstance(value, bool):
        return 4 if value else 5
    if isinstance(value, str):
        return _json_string_budget(value)
    if isinstance(value, int):
        return 32 if -(2**53 - 1) <= value <= 2**53 - 1 else None
    if isinstance(value, float):
        if not math.isfinite(value) or (
            value.is_integer() and not -(2**53 - 1) <= value <= 2**53 - 1
        ):
            return None
        return 32
    if not isinstance(value, (dict, list)):
        return None
    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return None
    seen.add(identity)
    try:
        rows: list[int] = []
        if isinstance(value, list):
            for item in value:
                item_budget = _json_structural_budget(item, depth=depth + 1, seen=seen)
                if item_budget is None:
                    return None
                rows.append(item_budget)
        else:
            for key, item in value.items():
                if not isinstance(key, str):
                    return None
                key_budget = _json_string_budget(key)
                item_budget = _json_structural_budget(item, depth=depth + 1, seen=seen)
                if key_budget is None or item_budget is None:
                    return None
                rows.append(key_budget + 1 + item_budget)
        return 2 + sum(rows) + max(0, len(rows) - 1)
    finally:
        seen.remove(identity)


def _canonical_url_parts(value: Any) -> tuple[Any, str] | tuple[None, str]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or not value.isascii()
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
        or "\\" in value
    ):
        return None, ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host
            or _CANONICAL_HTTPS_HOST_RE.fullmatch(host) is None
            or host != host.lower()
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None, ""
        canonical_netloc = host if port in (None, 443) else f"{host}:{port}"
        if port == 443 or parsed.netloc != canonical_netloc:
            return None, ""
        return parsed, f"https://{canonical_netloc}"
    except ValueError:
        return None, ""


def _https_origin(value: Any) -> str:
    parsed, origin = _canonical_url_parts(value)
    if parsed is None or parsed.path or parsed.query or parsed.fragment or value != origin:
        return ""
    return origin


def _canonical_script_origin(value: Any) -> str:
    parsed, origin = _canonical_url_parts(value)
    if (
        parsed is None
        or parsed.geturl() != value
        or not parsed.path.startswith("/")
        or any(character in _WHATWG_RAW_URL_CHARACTERS for character in value)
        or any(
            _PERCENT_ENCODED_DOT_RE.sub(".", segment) in {".", ".."}
            for segment in parsed.path.split("/")
        )
    ):
        return ""
    return origin


def _safe_browser_bootstrap_action_ids(descriptor: dict[str, Any]) -> set[str]:
    effect_ids = {
        str(row.get("operation_id") or "").strip()
        for row in _as_list(descriptor.get("external_effects"))
        if isinstance(row, dict)
    }
    safe_ids: set[str] = set()
    for action_payload in _as_list(descriptor.get("action_definitions")):
        action = _as_dict(action_payload)
        action_id = str(action.get("action_id") or "").strip()
        if (
            action_id
            and str(_as_dict(action.get("handler")).get("mode") or "").strip().lower()
            == "extension"
            and action.get("mutates_platform") is False
            and action.get("workflow_visible") is False
            and action.get("parameters") == []
            and action_id not in effect_ids
        ):
            safe_ids.add(action_id)
    return safe_ids


def _validate_external_resource_picker(
    *,
    component: dict[str, Any],
    component_index: dict[str, dict[str, Any]],
    action_ids: set[str],
) -> bool:
    data = component.get("data")
    if not isinstance(data, dict) or set(data) - _EXTERNAL_PICKER_DATA_KEYS:
        return False

    def bounded_optional_text(key: str, maximum: int) -> bool:
        value = data.get(key)
        if value is None:
            return True
        normalized = _normalize_utf16(value) if isinstance(value, str) else None
        if normalized is None or normalized[1] > maximum or not _browser_trim(normalized[0]):
            return False
        data[key] = normalized[0]
        return True

    button_label = data.get("button_label")
    bootstrap_action_id = data.get("bootstrap_action_id")
    bridge_global = data.get("bridge_global")
    target_component_id = data.get("target_component_id")
    normalized_button_label = (
        _normalize_utf16(button_label) if isinstance(button_label, str) else None
    )
    if (
        not isinstance(button_label, str)
        or normalized_button_label is None
        or not _browser_trim(normalized_button_label[0])
        or normalized_button_label[1] > 120
        or not bounded_optional_text("busy_label", 120)
        or not bounded_optional_text("error_message", 500)
        or not isinstance(bootstrap_action_id, str)
        or _EXTERNAL_PICKER_ACTION_RE.fullmatch(bootstrap_action_id) is None
        or bootstrap_action_id not in action_ids
        or not isinstance(bridge_global, str)
        or _EXTERNAL_PICKER_BRIDGE_RE.fullmatch(bridge_global) is None
        or not isinstance(target_component_id, str)
        or _EXTERNAL_PICKER_FIELD_ID_RE.fullmatch(target_component_id) is None
    ):
        return False
    data["button_label"] = normalized_button_label[0]

    target_component = _as_dict(component_index.get(target_component_id))
    if str(target_component.get("type") or "").strip() != "form":
        return False
    target_fields = {
        str(
            field.get("field_id") or field.get("id") or field.get("path") or field.get("name") or ""
        ).strip()
        for field in _as_list(_as_dict(target_component.get("ui_schema")).get("fields"))
        if isinstance(field, dict)
    }

    def declared_origins(key: str, *, required: bool) -> list[str] | None:
        origins = data.get(key)
        if origins is None and not required:
            return []
        if not isinstance(origins, list) or not 1 <= len(origins) <= 4:
            return None
        normalized = [_https_origin(origin) for origin in origins]
        if not all(normalized) or len(set(normalized)) != len(normalized):
            return None
        data[key] = normalized
        return normalized

    normalized_origins = declared_origins("allowed_script_origins", required=True)
    if normalized_origins is None:
        return False
    if (
        declared_origins("allowed_connect_origins", required=False) is None
        or declared_origins("allowed_frame_origins", required=False) is None
    ):
        return False

    scripts = data.get("scripts")
    if not isinstance(scripts, list) or not 1 <= len(scripts) <= 4:
        return False
    script_sources: list[str] = []
    for script in scripts:
        if not isinstance(script, dict) or set(script) not in _EXTERNAL_PICKER_SCRIPT_KEYS:
            return False
        asset_ref = script.get("asset_ref")
        if asset_ref is not None:
            if (
                not isinstance(asset_ref, str)
                or _EXTERNAL_PICKER_ASSET_RE.fullmatch(asset_ref) is None
            ):
                return False
            script_sources.append(f"asset:{asset_ref}")
            continue
        source = script.get("src")
        if not isinstance(source, str) or not source or len(source) > 2048:
            return False
        origin = _canonical_script_origin(source)
        if not origin or origin not in normalized_origins:
            return False
        script_sources.append(source)
    if len(set(script_sources)) != len(script_sources):
        return False

    selection = data.get("selection")
    if (
        not isinstance(selection, dict)
        or set(selection) != _EXTERNAL_PICKER_SELECTION_KEYS
        or isinstance(selection.get("maximum"), bool)
        or selection.get("maximum") != 1
    ):
        return False
    fields = selection.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 32:
        return False
    targets: set[str] = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - _EXTERNAL_PICKER_FIELD_KEYS:
            return False
        source = field.get("source")
        target = field.get("target")
        required = field.get("required")
        max_length = field.get("max_length")
        if (
            not isinstance(source, str)
            or _EXTERNAL_PICKER_FIELD_ID_RE.fullmatch(source) is None
            or not isinstance(target, str)
            or _EXTERNAL_PICKER_FIELD_ID_RE.fullmatch(target) is None
            or target in targets
            or target not in target_fields
            or (required is not None and not isinstance(required, bool))
            or isinstance(max_length, bool)
            or not isinstance(max_length, int)
            or not 1 <= max_length <= 4096
        ):
            return False
        targets.add(target)

    options = data.get("options")
    if options is not None:
        if not isinstance(options, dict):
            return False
        normalized_options = _normalize_json_unicode(options)
        if normalized_options is _INVALID_JSON_UNICODE or not isinstance(normalized_options, dict):
            return False
        options_budget = _json_structural_budget(normalized_options)
        if options_budget is None or options_budget > 32_768:
            return False
        data["options"] = normalized_options
    return True
