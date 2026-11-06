"""Rules shared by definitions and snapshots that JSON Schema cannot express."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from core.application.workflow_documents.report_formats import (
    currency_known,
    fraction_digits_problem,
    locale_known,
    time_zone_known,
)
from core.application.workflow_documents.report_issues import Issue, issue
from core.application.workflow_documents.report_tree import iter_formats

SUPPORTED_VERSIONS = (1,)


def serialized_size(value: Any) -> int | None:
    """UTF-8 bytes of compact, non-ASCII-preserving JSON; ``None`` if not JSON.

    Schema v1 section 6 measures sizes this way; the repository's
    ``canonical_json_bytes`` escapes non-ASCII and would overcount.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return len(text.encode("utf-8"))


def size_issue(value: Any, limit: int, code: str, label: str) -> Issue | None:
    size = serialized_size(value)
    if size is None:
        return issue("type_invalid", f"The {label} is not plain JSON with finite numbers")
    if size > limit:
        return issue(code, f"The {label} is {size} bytes; the limit is {limit}")
    return None


def map_strings(value: Any, transform: Callable[[str], str]) -> Any:
    """A copy of ``value`` with ``transform`` applied to every string value.

    Keys are left alone: in a snapshot every key is schema vocabulary, and row
    data lives in arrays, so only values can carry report data.
    """
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, Mapping):
        return {key: map_strings(item, transform) for key, item in value.items()}
    if isinstance(value, list):
        return [map_strings(item, transform) for item in value]
    return value


def version_issue(document: Mapping[str, Any]) -> Issue | None:
    version = document.get("schema_version")
    if isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        return issue(
            "schema_version_unsupported",
            "Only report schema_version 1 is supported",
            ("schema_version",),
        )
    return None


def locale_issues(document: Mapping[str, Any]) -> list[Issue]:
    problems: list[Issue] = []
    if not locale_known(str(document.get("locale"))):
        problems.append(issue("locale_invalid", "The locale is not known", ("locale",)))
    if not time_zone_known(document.get("time_zone")):
        problems.append(issue("time_zone_invalid", "The time zone is not known", ("time_zone",)))
    return problems


def format_issues(document: Mapping[str, Any]) -> list[Issue]:
    """Fraction digit order and ISO 4217 codes for every format in the document."""
    problems: list[Issue] = []
    for fmt, path in iter_formats(document):
        problem = fraction_digits_problem(fmt)
        if problem:
            problems.append(issue("fraction_digits_invalid", problem, path))
        if fmt.get("type") == "currency" and not currency_known(fmt.get("currency")):
            problems.append(
                issue("currency_invalid", "Not a known ISO 4217 currency", (*path, "currency"))
            )
    return problems


__all__ = [
    "SUPPORTED_VERSIONS",
    "format_issues",
    "locale_issues",
    "map_strings",
    "serialized_size",
    "size_issue",
    "version_issue",
]
