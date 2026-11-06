"""Issue records and path helpers shared by the report validators and compiler.

Every problem is a plain dict in the repository's issue shape
(``code``, ``message``, ``path``, ``severity``). Paths are relative to the
document; callers add the prefix of the place that reported them (schema v1
section 7).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

Issue = dict[str, str]
PathPart = str | int


def join_path(parts: Iterable[PathPart]) -> str:
    """Render ``["components", 3, "value"]`` as ``components[3].value``."""
    text = ""
    for part in parts:
        if isinstance(part, int):
            text += f"[{part}]"
        elif text:
            text += f".{part}"
        else:
            text = str(part)
    return text


def issue(code: str, message: str, path: Sequence[PathPart] | str = "") -> Issue:
    """One error-severity report issue; ``code`` is given without the ``report.`` prefix."""
    rendered = path if isinstance(path, str) else join_path(path)
    return {
        "code": f"report.{code}",
        "message": message,
        "path": rendered,
        "severity": "error",
    }


def prefix_issues(issues: Iterable[Issue], prefix: str) -> list[Issue]:
    """Return copies of ``issues`` whose paths start with ``prefix``."""
    stem = prefix.rstrip(".")
    prefixed: list[Issue] = []
    for item in issues:
        path = item.get("path") or ""
        if not stem:
            joined = path
        elif not path:
            joined = stem
        elif path.startswith("["):
            joined = f"{stem}{path}"
        else:
            joined = f"{stem}.{path}"
        prefixed.append({**item, "path": joined})
    return prefixed


class ReportError(ValueError):
    """A report definition, its inputs or a snapshot broke the v1 contract."""

    def __init__(self, issues: Sequence[Issue]) -> None:
        self.issues: list[Issue] = list(issues)
        first = self.issues[0]["message"] if self.issues else "invalid report"
        super().__init__(first)


def describe_type(value: Any) -> str:
    """Name a JSON value the way report messages do."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


__all__ = [
    "Issue",
    "PathPart",
    "ReportError",
    "describe_type",
    "issue",
    "join_path",
    "prefix_issues",
]
