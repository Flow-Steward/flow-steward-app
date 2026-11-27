"""Control-surface validation and localization for registered UI pages.

A page may declare a ``control_surface`` that the Core frontend bundle renders
itself (``resource_admin``, ``resource_report`` and ``status_document``). This
module validates those declarations at registration time, keeps every resource
on a same-origin API path under the registrant's prefix, and translates the
surface's labels through the UI catalogue when a page is resolved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from core.application.settings.ui_i18n import translate_ui_text

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


def _normalize_text(value: Any, *, fallback: str = "") -> str:
    text = str(value or fallback).strip()
    return text or fallback


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
