"""Field values and declared field shapes of the public workflow contract.

``coerce_contract_value`` is the one value check every run path shares: UI,
API, MCP, webhook, schedule and parent-workflow inputs, configured defaults and
variable outputs all go through it.

A field may declare a shape (ADR 0075, item 6):

- ``items``: the element of an ``array``. ``type`` is any field type except
  ``array`` and ``secret``; ``options`` belong to a ``select`` element,
  ``format`` to a text element and ``properties`` to an ``object`` element.
  Several files are ``type: array`` with ``items.type: file``; a multi-select
  is ``items.type: select`` with ``items.options``.
- ``properties``: the sub-fields of an ``object``, in the same field model.
  Nesting stops at ``MAX_FIELD_DEPTH`` levels, the top-level field included,
  so a form stays a form: a group inside a group inside a field.
- ``min_items`` / ``max_items``: bounds on the length of an ``array``.
- ``format``: ``date``, ``date-time``, ``time``, ``email`` or ``uri`` for
  ``string`` and ``paragraph``.

Every key is optional and is checked only where it is declared. A field
without them validates exactly as before, so ``array`` and ``object`` still
accept a JSON string and released workflows keep their behaviour.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, time
from typing import Any
from urllib.parse import urlsplit

from .workflow_contract import CANONICAL_FIELD_TYPES, WorkflowContractField, json_object

FIELD_STRING_FORMATS: tuple[str, ...] = ("date", "date-time", "time", "email", "uri")
#: Field types a ``format`` applies to.
FORMAT_FIELD_TYPES: frozenset[str] = frozenset({"string", "paragraph"})
#: Element types of an ``array``. An array of arrays has no form to fill in,
#: and a secret is a single credential, never one of a list.
ITEM_FIELD_TYPES: frozenset[str] = frozenset(
    {"string", "paragraph", "number", "boolean", "select", "object", "file"}
)
#: The top-level field is depth 1; each ``properties`` level adds one.
MAX_FIELD_DEPTH = 3

_FORMAT_DESCRIPTIONS: dict[str, str] = {
    "date": "a date (YYYY-MM-DD)",
    "date-time": "a date and time (YYYY-MM-DDTHH:MM[:SS] with an optional timezone)",
    "time": "a time (HH:MM[:SS])",
    "email": "an email address",
    "uri": "a URI with a scheme",
}
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_PATTERN = re.compile(r"\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:[Zz]|[+-]\d{2}:\d{2})?")
_URI_SCHEME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")

Coercion = tuple[bool, Any, str]


def is_empty_contract_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def is_missing_contract_value(value: Any, field: WorkflowContractField) -> bool:
    """Whether a value counts as not supplied: an empty array or object is supplied."""
    if value is None:
        return True
    if field.type in {"object", "array"}:
        return False
    return is_empty_contract_value(value)


def is_item_bound(value: Any) -> bool:
    """Whether ``min_items`` / ``max_items`` holds a usable bound."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --- Definition issues -------------------------------------------------------


def _issue(code: str, message: str, path: str) -> dict[str, Any]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def field_shape_issues(
    field: WorkflowContractField, *, path: str, depth: int = 1
) -> list[dict[str, Any]]:
    """Problems with the shape keys a field declares; none when it declares none."""
    label = field.name or path
    return [
        *_format_issues(field, path=path, label=label),
        *_item_bound_issues(field, path=path, label=label),
        *_items_issues(field, path=path, label=label, depth=depth),
        *_properties_issues(field, path=path, label=label, depth=depth),
    ]


def _format_issues(field: WorkflowContractField, *, path: str, label: str) -> list[dict]:
    if not field.format:
        return []
    if field.format not in FIELD_STRING_FORMATS:
        return [
            _issue(
                "workflow.interface.format_unknown",
                f"Field '{label}' has unknown format '{field.format}'; use one of: "
                + ", ".join(FIELD_STRING_FORMATS),
                f"{path}.format",
            )
        ]
    if field.type not in FORMAT_FIELD_TYPES:
        return [
            _issue(
                "workflow.interface.format_type_mismatch",
                f"Field '{label}' declares a format but has type '{field.type}'; "
                "a format applies to string and paragraph",
                f"{path}.format",
            )
        ]
    return []


def _item_bound_issues(field: WorkflowContractField, *, path: str, label: str) -> list[dict]:
    issues: list[dict[str, Any]] = []
    for key in ("min_items", "max_items"):
        value = getattr(field, key)
        if value is None:
            continue
        if not is_item_bound(value):
            issues.append(
                _issue(
                    "workflow.interface.item_bound_invalid",
                    f"{key} of field '{label}' must be a whole number of zero or more",
                    f"{path}.{key}",
                )
            )
        elif field.type != "array":
            issues.append(
                _issue(
                    "workflow.interface.item_bound_not_array",
                    f"Field '{label}' declares {key} but is not an array",
                    f"{path}.{key}",
                )
            )
    if (
        is_item_bound(field.min_items)
        and is_item_bound(field.max_items)
        and field.min_items > field.max_items
    ):
        issues.append(
            _issue(
                "workflow.interface.item_bounds_inverted",
                f"min_items of field '{label}' is greater than its max_items",
                f"{path}.min_items",
            )
        )
    return issues


def _element_type_issues(
    field: WorkflowContractField, *, path: str, label: str, allowed: frozenset[str]
) -> list[dict]:
    """A nested field's type: never a secret, and one the position allows."""
    if field.is_secret:
        return [
            _issue(
                "workflow.interface.nested_secret_forbidden",
                f"Field '{label}' cannot be a secret: a secret is a top-level input",
                f"{path}.type",
            )
        ]
    if field.type not in allowed:
        return [
            _issue(
                "workflow.interface.invalid_type",
                f"Field '{label}' has unsupported type '{field.type}'",
                f"{path}.type",
            )
        ]
    if field.type == "select" and not field.options:
        return [
            _issue(
                "workflow.interface.select_options_missing",
                f"Select field '{label}' must declare options",
                f"{path}.options",
            )
        ]
    return []


def _items_issues(field: WorkflowContractField, *, path: str, label: str, depth: int) -> list[dict]:
    if field.items is None:
        return []
    items_path = f"{path}.items"
    if field.type != "array":
        return [
            _issue(
                "workflow.interface.items_not_array",
                f"Field '{label}' declares items but is not an array",
                items_path,
            )
        ]
    item = field.item_field()
    if item is None:
        return [
            _issue(
                "workflow.interface.items_invalid",
                f"items of field '{label}' must be an object that describes the element",
                items_path,
            )
        ]
    issues = _element_type_issues(item, path=items_path, label=item.name, allowed=ITEM_FIELD_TYPES)
    if issues:
        return issues
    return field_shape_issues(item, path=items_path, depth=depth)


def _properties_issues(
    field: WorkflowContractField, *, path: str, label: str, depth: int
) -> list[dict]:
    if field.properties is None:
        return []
    properties_path = f"{path}.properties"
    if field.type != "object":
        return [
            _issue(
                "workflow.interface.properties_not_object",
                f"Field '{label}' declares properties but is not an object",
                properties_path,
            )
        ]
    rows = field.property_fields()
    if rows is None:
        return [
            _issue(
                "workflow.interface.properties_invalid",
                f"properties of field '{label}' must be a list of fields",
                properties_path,
            )
        ]
    if rows and depth >= MAX_FIELD_DEPTH:
        return [
            _issue(
                "workflow.interface.nesting_too_deep",
                f"Field '{label}' nests properties deeper than {MAX_FIELD_DEPTH} levels",
                properties_path,
            )
        ]
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        row_path = f"{properties_path}[{index}]"
        row_issues = _property_name_issues(row, path=row_path, parent=label, seen=seen)
        if not row_issues:
            row_label = f"{label}.{row.name}"
            row_issues = _element_type_issues(
                row, path=row_path, label=row_label, allowed=CANONICAL_FIELD_TYPES
            ) or field_shape_issues(
                row.model_copy(update={"name": row_label}), path=row_path, depth=depth + 1
            )
        issues.extend(row_issues)
    return issues


def _property_name_issues(
    row: WorkflowContractField, *, path: str, parent: str, seen: set[str]
) -> list[dict]:
    name = row.name
    if not name:
        return [
            _issue(
                "workflow.interface.name_required",
                f"A property of field '{parent}' has no name",
                f"{path}.name",
            )
        ]
    if any(ch.isspace() for ch in name):
        return [
            _issue(
                "workflow.interface.name_whitespace",
                f"Property '{name}' of field '{parent}' cannot contain whitespace",
                f"{path}.name",
            )
        ]
    if name in seen:
        return [
            _issue(
                "workflow.interface.duplicate_name",
                f"Duplicate property '{name}' in field '{parent}'",
                f"{path}.name",
            )
        ]
    seen.add(name)
    return []


# --- Values ------------------------------------------------------------------


def coerce_contract_value(value: Any, field: WorkflowContractField) -> Coercion:
    """Check and normalize one value of *field*: its type, then its declared shape."""
    ok, coerced, message = _coerce_type(value, field)
    if not ok or not field.declares_shape:
        return ok, coerced, message
    if field.type in FORMAT_FIELD_TYPES:
        return _checked_format(coerced, field)
    if field.type == "array":
        return _checked_array(coerced, field)
    if field.type == "object":
        return _checked_object(coerced, field)
    return True, coerced, ""


def _coerce_type(value: Any, field: WorkflowContractField) -> Coercion:
    field_type = field.type
    name = field.name
    if field_type in {"string", "paragraph", "select"}:
        if isinstance(value, (dict, list, tuple, set)):
            return False, None, f"Workflow field '{name}' must be text"
        text = str(value if value is not None else "").strip()
        if field_type == "select" and field.options and text not in field.options:
            return (
                False,
                None,
                f"Workflow field '{name}' must be one of: {', '.join(field.options)}",
            )
        return True, text, ""
    if field_type == "secret":
        return _coerce_secret(value, name)
    if field_type == "number":
        return _coerce_number(value, name)
    if field_type == "boolean":
        if isinstance(value, bool):
            return True, value, ""
        if isinstance(value, (int, float)):
            return True, bool(value), ""
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True, True, ""
        if text in {"0", "false", "no", "off"}:
            return True, False, ""
        return False, None, f"Workflow field '{name}' must be boolean"
    if field_type == "object":
        obj = json_object(value)
        if obj:
            return True, obj, ""
        if value == {}:
            return True, {}, ""
        return False, None, f"Workflow field '{name}' must be an object"
    if field_type == "array":
        return _coerce_array(value, name)
    if field_type == "file":
        return (
            (not is_empty_contract_value(value)),
            value,
            f"Workflow field '{name}' must be a file",
        )
    return False, None, f"Workflow field '{name}' has unsupported type '{field_type}'"


def _coerce_secret(value: Any, name: str) -> Coercion:
    # A caller may supply a secret for a field with no configured default.
    # It is held for the run the way a configured one is — as a reference,
    # never as plaintext — so nothing between here and the tool boundary has
    # the value to print, persist or checkpoint.
    if isinstance(value, (dict, list, tuple, set)):
        return False, None, f"Workflow field '{name}' must be a secret value"
    text = str(value if value is not None else "")
    if not text.strip():
        return False, None, f"Workflow field '{name}' must be a secret value"
    from core.infrastructure.security.secret_codec import encrypt_secret
    from core.infrastructure.security.secret_reference import SecretReference

    return True, SecretReference(name=name, ciphertext=encrypt_secret(text)).to_wire(), ""


def _coerce_number(value: Any, name: str) -> Coercion:
    if isinstance(value, bool):
        return False, None, f"Workflow field '{name}' must be a number"
    if isinstance(value, (int, float)):
        return (
            (True, value, "")
            if math.isfinite(float(value))
            else (False, None, f"Workflow field '{name}' must be a finite number")
        )
    text = str(value or "").strip()
    try:
        parsed = float(text)
    except ValueError:
        return False, None, f"Workflow field '{name}' must be a number"
    if not math.isfinite(parsed):
        return False, None, f"Workflow field '{name}' must be a finite number"
    return True, int(parsed) if parsed.is_integer() else parsed, ""


def _coerce_array(value: Any, name: str) -> Coercion:
    if isinstance(value, list):
        return True, value, ""
    if isinstance(value, (tuple, set)):
        return True, list(value), ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return False, None, f"Workflow field '{name}' must be an array"
        if isinstance(parsed, list):
            return True, parsed, ""
    return False, None, f"Workflow field '{name}' must be an array"


def _checked_format(text: str, field: WorkflowContractField) -> Coercion:
    check = _FORMAT_CHECKS.get(field.format)
    # An empty optional value has no format to check; required is checked before.
    if check is None or not text or check(text):
        return True, text, ""
    return (
        False,
        None,
        f"Workflow field '{field.name}' must be {_FORMAT_DESCRIPTIONS[field.format]}",
    )


def _checked_array(values: list[Any], field: WorkflowContractField) -> Coercion:
    name = field.name
    if is_item_bound(field.min_items) and len(values) < field.min_items:
        return False, None, f"Workflow field '{name}' must have at least {field.min_items} item(s)"
    if is_item_bound(field.max_items) and len(values) > field.max_items:
        return False, None, f"Workflow field '{name}' must have at most {field.max_items} item(s)"
    item = field.item_field()
    if item is None or item.is_secret or item.type not in ITEM_FIELD_TYPES:
        return True, values, ""
    elements: list[Any] = []
    for index, value in enumerate(values):
        ok, coerced, message = coerce_contract_value(
            value, item.model_copy(update={"name": f"{name}[{index}]"})
        )
        if not ok:
            return False, None, message
        elements.append(coerced)
    return True, elements, ""


def _checked_object(value: dict[str, Any], field: WorkflowContractField) -> Coercion:
    rows = field.property_fields()
    if not rows:
        return True, value, ""
    result = dict(value)
    for row in rows:
        if not row.name or row.is_secret:
            continue
        nested = row.model_copy(update={"name": f"{field.name}.{row.name}"})
        if row.name not in value or is_missing_contract_value(value.get(row.name), nested):
            if row.required:
                return False, None, f"Workflow field '{nested.name}' is required"
            continue
        ok, coerced, message = coerce_contract_value(value[row.name], nested)
        if not ok:
            return False, None, message
        result[row.name] = coerced
    return True, result, ""


def _is_date(text: str) -> bool:
    if not _DATE_PATTERN.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _is_time(text: str) -> bool:
    if not _TIME_PATTERN.fullmatch(text):
        return False
    normalized = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    try:
        time.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _is_date_time(text: str) -> bool:
    return len(text) > 11 and text[10] in "Tt " and _is_date(text[:10]) and _is_time(text[11:])


def _is_email(text: str) -> bool:
    if any(ch.isspace() for ch in text) or text.count("@") != 1:
        return False
    local, domain = text.split("@")
    labels = domain.split(".")
    return bool(local) and len(labels) > 1 and all(labels)


def _is_uri(text: str) -> bool:
    if any(ch.isspace() for ch in text):
        return False
    scheme, separator, rest = text.partition(":")
    if not separator or not rest or not _URI_SCHEME_PATTERN.fullmatch(scheme):
        return False
    if scheme.lower() in {"http", "https"}:
        try:
            return bool(urlsplit(text).netloc)
        except ValueError:
            # A malformed authority ("http://[::1") is not a URI; it must stay a
            # field error, never escape the request's 422 path.
            return False
    return True


_FORMAT_CHECKS = {
    "date": _is_date,
    "date-time": _is_date_time,
    "time": _is_time,
    "email": _is_email,
    "uri": _is_uri,
}
