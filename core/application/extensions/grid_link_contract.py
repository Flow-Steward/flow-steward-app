"""Compile-time rules for grid links and grid filters on extension pages.

A grid column may link each cell, and a grid may make each row clickable, to
one of two same-app targets:

* another page of the same extension, with ``p.<name>`` page parameters filled
  from the row: ``{page: run_detail, params: {run_id: "{row.run_id}"}}``;
* an in-app path the row provides: ``{path_field: job_path}`` (the field holds
  for example ``/jobs/42``).

A grid may also declare filters above the table, each bound to one
``p.<name>`` page parameter: a select whose options are static or come from
an ordinary query, or a text box. Changing a filter changes the page address,
and the page's queries load again with the new ``params``.

The host builds every URL; an extension never supplies a scheme or a host.
These rules refuse a declaration the host could not honour: an unknown page,
a row field the grid does not declare, an unknown query, a malformed name.
"""

from __future__ import annotations

import re
from typing import Any

from core.application.extensions.query_connection_contract import query_connection_type_ids
from core.infrastructure.extension_runtime.extension_runtime_validation import (
    _as_dict,
    _as_list,
    _issue,
)

PAGE_PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROW_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
ROW_PLACEHOLDER = re.compile(r"\{row\.([^{}]*)\}")
PAGE_PARAMETER_MAX_COUNT = 20
GRID_FILTER_MAX_COUNT = 10
GRID_FILTER_MAX_OPTIONS = 200
GRID_FILTER_TYPES = frozenset({"select", "text"})
_LINK_KEYS = frozenset({"page", "params", "path_field"})
_FILTER_KEYS = frozenset({"param", "label", "type", "options", "options_query", "placeholder"})
_OPTIONS_QUERY_KEYS = frozenset({"query_id", "value_field", "label_field"})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class _Checker:
    def __init__(
        self,
        *,
        issues: list[dict],
        manifest_path: str,
        page_ids: set[str],
        queries: dict[str, dict[str, Any]],
    ) -> None:
        self.issues = issues
        self.manifest_path = manifest_path
        self.page_ids = page_ids
        self.queries = queries

    def error(self, code: str, message: str, field_path: str) -> None:
        self.issues.append(
            _issue(
                severity="error",
                code=code,
                message=message,
                file_path=self.manifest_path,
                field_path=field_path,
            )
        )

    def row_field(self, name: Any, fields: set[str], path: str) -> None:
        field = _text(name)
        if not ROW_FIELD.fullmatch(field):
            self.error("grid_link_field_invalid", f"'{name}' is not a row field name", path)
        elif field not in fields:
            self.error(
                "grid_link_field_unknown",
                f"Row field '{field}' is neither a column key nor listed in row_fields",
                path,
            )

    def link(self, raw: Any, *, fields: set[str], path: str) -> None:
        link = raw if isinstance(raw, dict) else None
        if link is None or not link or set(link) - _LINK_KEYS:
            self.error(
                "grid_link_invalid",
                "A grid link is {page, params} or {path_field}",
                path,
            )
            return
        page = _text(link.get("page"))
        has_path = "path_field" in link
        if bool(page) == has_path or ("params" in link and not page):
            self.error(
                "grid_link_invalid",
                "A grid link names either another page of this extension (page, params) "
                "or a row field that holds an in-app path (path_field)",
                path,
            )
            return
        if has_path:
            self.row_field(link.get("path_field"), fields, f"{path}.path_field")
            return
        if page not in self.page_ids:
            self.error(
                "grid_link_page_unknown",
                f"Page '{page}' is not a page of this extension",
                f"{path}.page",
            )
        self.params(link.get("params", {}), fields=fields, path=f"{path}.params")

    def params(self, raw: Any, *, fields: set[str], path: str) -> None:
        if not isinstance(raw, dict) or len(raw) > PAGE_PARAMETER_MAX_COUNT:
            self.error(
                "grid_link_params_invalid",
                f"Link params are an object of at most {PAGE_PARAMETER_MAX_COUNT} page parameters",
                path,
            )
            return
        for name, value in raw.items():
            if not PAGE_PARAMETER_NAME.fullmatch(str(name)) or not isinstance(value, str):
                self.error(
                    "grid_link_params_invalid",
                    f"Page parameter '{name}' needs a lowercase identifier name and a text value",
                    f"{path}.{name}",
                )
                continue
            stripped = ROW_PLACEHOLDER.sub("", value)
            if "{" in stripped or "}" in stripped:
                self.error(
                    "grid_link_params_invalid",
                    f"Page parameter '{name}' may hold text and {{row.<field>}} placeholders only",
                    f"{path}.{name}",
                )
            for field in ROW_PLACEHOLDER.findall(value):
                self.row_field(field, fields, f"{path}.{name}")

    def filters(self, raw: Any, *, path: str) -> None:
        if not isinstance(raw, list) or len(raw) > GRID_FILTER_MAX_COUNT:
            self.error(
                "grid_filter_invalid",
                f"Grid filters are a list of at most {GRID_FILTER_MAX_COUNT}",
                path,
            )
            return
        seen: set[str] = set()
        for idx, item in enumerate(raw):
            self.filter(item, seen=seen, path=f"{path}[{idx}]")

    def filter(self, raw: Any, *, seen: set[str], path: str) -> None:
        item = raw if isinstance(raw, dict) else {}
        unknown = sorted(set(item) - _FILTER_KEYS)
        param = _text(item.get("param"))
        kind = _text(item.get("type")) or "select"
        if not item or unknown or not PAGE_PARAMETER_NAME.fullmatch(param):
            self.error(
                "grid_filter_invalid",
                "A grid filter is {param, label, type, options | options_query, placeholder} "
                "with a lowercase identifier param",
                path,
            )
            return
        if param in seen:
            self.error(
                "grid_filter_param_duplicate",
                f"Page parameter '{param}' has two filters",
                f"{path}.param",
            )
        seen.add(param)
        if kind not in GRID_FILTER_TYPES:
            self.error("grid_filter_invalid", "A grid filter is a select or a text box", path)
            return
        has_options = "options" in item
        has_query = "options_query" in item
        if kind == "text":
            if has_options or has_query:
                self.error("grid_filter_invalid", "A text filter has no options", path)
            return
        if has_options == has_query:
            self.error(
                "grid_filter_invalid",
                "A select filter takes its options from options or from options_query",
                path,
            )
            return
        if has_options:
            self.static_options(item.get("options"), path=f"{path}.options")
        else:
            self.options_query(item.get("options_query"), path=f"{path}.options_query")

    def static_options(self, raw: Any, *, path: str) -> None:
        options = raw if isinstance(raw, list) else []
        valid = 0 < len(options) <= GRID_FILTER_MAX_OPTIONS and all(
            isinstance(option, dict)
            and set(option) <= {"value", "label"}
            and isinstance(option.get("value"), str)
            and _text(option.get("label"))
            for option in options
        )
        if not valid:
            self.error(
                "grid_filter_invalid",
                f"Filter options are 1..{GRID_FILTER_MAX_OPTIONS} objects {{value, label}}",
                path,
            )

    def options_query(self, raw: Any, *, path: str) -> None:
        source = raw if isinstance(raw, dict) else {}
        query_id = _text(source.get("query_id"))
        if (
            not source
            or set(source) - _OPTIONS_QUERY_KEYS
            or not query_id
            or not ROW_FIELD.fullmatch(_text(source.get("value_field")))
            or ("label_field" in source and not ROW_FIELD.fullmatch(_text(source["label_field"])))
        ):
            self.error(
                "grid_filter_invalid",
                "options_query is {query_id, value_field, label_field}",
                path,
            )
            return
        if query_id not in self.queries:
            self.error(
                "grid_filter_query_unknown",
                f"Query '{query_id}' is not a query of this extension",
                f"{path}.query_id",
            )
        elif query_connection_type_ids(self.queries[query_id]):
            self.error(
                "grid_filter_query_connection",
                f"Query '{query_id}' runs with a connection; filter options come from an "
                "ordinary query",
                f"{path}.query_id",
            )

    def grid(self, component: dict[str, Any], *, path: str) -> None:
        columns = [_as_dict(column) for column in _as_list(component.get("columns"))]
        fields = {_text(column.get("key")) for column in columns if _text(column.get("key"))}
        row_fields = component.get("row_fields", [])
        if not isinstance(row_fields, list) or any(
            not ROW_FIELD.fullmatch(_text(field)) for field in row_fields
        ):
            self.error(
                "grid_link_field_invalid",
                "row_fields lists the row field names the query returns beside the columns",
                f"{path}.row_fields",
            )
        else:
            fields |= {_text(field) for field in row_fields}
        for idx, column in enumerate(columns):
            if "link" in column:
                if _text(column.get("type")).lower() != "link":
                    self.error(
                        "grid_link_invalid",
                        "A column with a link has type: link",
                        f"{path}.columns[{idx}].type",
                    )
                self.link(column.get("link"), fields=fields, path=f"{path}.columns[{idx}].link")
        if "row_link" in component:
            self.link(component.get("row_link"), fields=fields, path=f"{path}.row_link")
        if "filters" in component:
            self.filters(component.get("filters"), path=f"{path}.filters")

    def walk(self, value: Any, *, path: str) -> None:
        if isinstance(value, list):
            for idx, item in enumerate(value):
                self.walk(item, path=f"{path}[{idx}]")
            return
        if not isinstance(value, dict):
            return
        if _text(value.get("type")).lower() == "grid":
            self.grid(value, path=path)
        if "components" in value:
            self.walk(value.get("components"), path=f"{path}.components")


def validate_grid_link_contract(
    compiled_manifest: dict[str, Any], *, issues: list[dict], manifest_path: str
) -> None:
    """Refuse a release whose grid links or grid filters the host could not honour."""
    pages = [
        _as_dict(page)
        for page in _as_list(_as_dict(compiled_manifest.get("ui_manifest")).get("pages"))
    ]
    queries = {
        _text(_as_dict(row).get("query_id")): _as_dict(row)
        for row in _as_list(compiled_manifest.get("query_definitions"))
        if _text(_as_dict(row).get("query_id"))
    }
    checker = _Checker(
        issues=issues,
        manifest_path=manifest_path,
        page_ids={_text(page.get("page_id")) for page in pages if _text(page.get("page_id"))},
        queries=queries,
    )
    for idx, page in enumerate(pages):
        checker.walk(page.get("components"), path=f"ui.pages[{idx}].components")


__all__ = ["validate_grid_link_contract"]
