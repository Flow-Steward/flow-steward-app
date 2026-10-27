"""An extension's own translations: its ``locales/<code>.yaml`` dictionaries.

Each file is one flat YAML mapping of a stable key to a string, for example
``page.setup.title: Nustatymai``. Text is resolved as: a non-empty string in the
user's language, then a non-empty ``en`` string, then the declaration's own
text. A dictionary never replaces an id, a path or a value; it only changes what
the user reads.

Authoring files are YAML only: ``locales/*.json`` is reported by the bundle's
YAML-only check and never read as a dictionary here. A dictionary that is not
valid YAML, is not a mapping, or holds a nested or non-string value is an
error with its file path; a missing dictionary or a missing key is not.

The keys a manifest's pages use are derived here with the same identity rules
the browser applies (``extension-locale.ts``), so the validator, the compiled
manifest and the pages agree on which keys exist.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_LOCALE = "en"
LOCALES_DIRNAME = "locales"
LOCALE_FILE_SUFFIXES = (".yaml", ".yml")
_LOCALE_CODE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _issue(path: str, message: str, code: str) -> dict[str, str]:
    return {"path": path, "message": message, "code": code}


def _parse_dictionary(path: Path, rel: str, errors: list[dict[str, str]]) -> dict[str, str] | None:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        errors.append(_issue(rel, f"Not a valid UTF-8 YAML file: {reason}", "invalid_locale_file"))
        return None
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        errors.append(
            _issue(rel, "A locale file must be a flat mapping of key: text", "invalid_locale_file")
        )
        return None
    dictionary: dict[str, str] = {}
    valid = True
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(
                _issue(rel, f"Key {key!r} must be a non-empty string", "invalid_locale_key")
            )
            valid = False
            continue
        if value is None:
            # `key:` with nothing after it is an empty translation: it falls back.
            dictionary[key] = ""
        elif isinstance(value, str):
            dictionary[key] = value
        else:
            kind = "nested" if isinstance(value, (Mapping, list)) else type(value).__name__
            errors.append(
                _issue(
                    f"{rel}.{key}",
                    f"Value of '{key}' must be text, not {kind}; keys are flat (a.b.c: text)",
                    "invalid_locale_value",
                )
            )
            valid = False
    return dictionary if valid else None


def load_locale_bundles(
    bundle_root: str | Path | None,
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """``(bundles, errors, warnings)`` for the dictionaries under ``locales/``."""
    if bundle_root is None:
        return {}, [], []
    locales_dir = Path(bundle_root) / LOCALES_DIRNAME
    if not locales_dir.is_dir():
        return {}, [], []
    bundles: dict[str, dict[str, str]] = {}
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for child in sorted(locales_dir.iterdir()):
        if not child.is_file() or child.suffix.lower() not in LOCALE_FILE_SUFFIXES:
            continue
        rel = f"{LOCALES_DIRNAME}/{child.name}"
        code = child.stem.lower()
        if not _LOCALE_CODE.match(code):
            errors.append(
                _issue(
                    rel,
                    f"'{child.stem}' is not a language code such as en or lt",
                    "invalid_locale_code",
                )
            )
            continue
        if code in bundles:
            errors.append(
                _issue(rel, f"Language '{code}' has more than one file", "duplicate_locale_file")
            )
            continue
        dictionary = _parse_dictionary(child, rel, errors)
        if dictionary is not None:
            bundles[code] = dictionary
    if bundles and DEFAULT_LOCALE not in bundles:
        warnings.append(
            _issue(
                f"{LOCALES_DIRNAME}/",
                "locales/ directory exists but en.yaml is missing",
                "locale_missing_default",
            )
        )
    return bundles, errors, warnings


def resolve_locale_text(
    bundles: Mapping[str, Mapping[str, str]] | None,
    locale: str,
    key: str,
    fallback: str,
) -> str:
    """The user's language, then English, then the declaration's own text."""
    if not bundles or not key:
        return fallback
    for code in (_text(locale).lower(), DEFAULT_LOCALE):
        value = (bundles.get(code) or {}).get(key) if code else None
        if isinstance(value, str) and value.strip():
            return value
    return fallback


# --- keys the pages use ------------------------------------------------------------


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _add_if(keys: set[str], source: Mapping[str, Any], field: str, key: str) -> None:
    if _text(source.get(field)):
        keys.add(key)


def _field_keys(field: Mapping[str, Any], prefix: str, keys: set[str]) -> None:
    _add_if(keys, field, "label", f"{prefix}.label")
    if _text(field.get("description")) or _text(field.get("help")):
        keys.add(f"{prefix}.help")
    _add_if(keys, field, "placeholder", f"{prefix}.placeholder")
    props = _obj(field.get("props"))
    for options in (field.get("options"), props.get("options")):
        for option in _rows(options):
            value = _text(option.get("value") or option.get("id") or option.get("key"))
            if value and (_text(option.get("label")) or _text(option.get("title"))):
                keys.add(f"{prefix}.option.{value}.label")
    for item in _rows(props.get("fields")):
        item_id = _text(
            item.get("path") or item.get("name") or item.get("field_id") or item.get("id")
        )
        if item_id:
            _field_keys(item, f"{prefix}.field.{item_id}", keys)


def _fields_keys(fields: Any, component_prefix: str, keys: set[str]) -> None:
    for field in _rows(fields):
        field_id = _text(
            field.get("field_id") or field.get("id") or field.get("path") or field.get("label")
        )
        if field_id:
            _field_keys(field, f"{component_prefix}.field.{field_id}", keys)


def _component_keys(component: Mapping[str, Any], keys: set[str]) -> None:
    component_id = _text(
        component.get("component_id") or component.get("id") or component.get("key")
    )
    if not component_id:
        return
    prefix = f"component.{component_id}"
    _add_if(keys, component, "title", f"{prefix}.title")
    _add_if(keys, component, "description", f"{prefix}.description")
    _add_if(keys, component, "content", f"{prefix}.content")
    _fields_keys(component.get("fields"), prefix, keys)
    ui_schema = _obj(component.get("ui_schema"))
    _add_if(keys, ui_schema, "description", f"{prefix}.description")
    _fields_keys(ui_schema.get("fields"), prefix, keys)
    for section in _rows(ui_schema.get("sections")):
        section_id = _text(section.get("section_id"))
        if section_id:
            _add_if(keys, section, "title", f"{prefix}.section.{section_id}.title")
            _add_if(keys, section, "description", f"{prefix}.section.{section_id}.description")
    for action_key, action in _obj(ui_schema.get("actions")).items():
        if action_key in {"save_label", "test_label"}:
            if _text(action):
                keys.add(f"{prefix}.{action_key}")
        elif isinstance(action, dict) and (
            _text(action.get("label")) or _text(action.get("title"))
        ):
            keys.add(f"{prefix}.action.{action_key}.label")
    for column in _rows(component.get("columns")):
        column_id = _text(column.get("key") or column.get("label"))
        if column_id:
            _add_if(keys, column, "label", f"{prefix}.column.{column_id}.label")
    for action in _rows(component.get("row_actions")):
        action_id = _text(action.get("action_id") or action.get("id") or action.get("query_id"))
        if action_id and (_text(action.get("label")) or _text(action.get("title"))):
            keys.add(f"{prefix}.row_action.{action_id}.label")
    empty_state = _obj(component.get("empty_state"))
    _add_if(keys, empty_state, "title", f"{prefix}.empty_state")
    _add_if(keys, empty_state, "description", f"{prefix}.empty_state_description")
    _add_if(keys, _obj(component.get("data")), "account_label", f"{prefix}.account_label")


def ui_manifest_locale_keys(ui_manifest: Mapping[str, Any] | None) -> set[str]:
    """Every key a resolved UI manifest's readable text can be translated by."""
    keys: set[str] = set()
    for page in _rows(_obj(ui_manifest).get("pages")):
        page_id = _text(
            page.get("page_id") or page.get("id") or page.get("key") or page.get("title")
        )
        if page_id:
            _add_if(keys, page, "title", f"page.{page_id}.title")
            _add_if(keys, page, "description", f"page.{page_id}.description")
        for component in _rows(page.get("components")):
            _component_keys(component, keys)
    return keys


__all__ = [
    "DEFAULT_LOCALE",
    "load_locale_bundles",
    "resolve_locale_text",
    "ui_manifest_locale_keys",
]
