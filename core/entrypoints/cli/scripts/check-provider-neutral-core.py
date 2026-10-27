#!/usr/bin/env python3
"""Fail when extension-owned provider identities leak outside their owner roots."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

POLICY_NAME = "oauth-boundary-policy.json"
SCANNED_ROOTS = (
    Path("assets/frontend"),
    Path("core"),
    Path("db"),
    Path("extensions"),
    Path("infra"),
    Path("scripts"),
    Path("tests"),
)
SCANNED_FILES = (Path("docs/architecture/adr/0023-extension-owned-oauth-boundary.md"),)
# Static, user-editable MCP template data is deliberately the one provider-named
# surface outside an extension owner. Only the catalog JSON and its exact
# frontend contract test are data; the loader and all other files stay scanned.
SANCTIONED_FRONTEND_TEMPLATE_PATHS = frozenset(
    {
        Path("assets/frontend/src/pages/registry/mcp/mcp-templates.json"),
        Path("assets/frontend/src/pages/registry/mcp/mcp-templates.test.ts"),
    }
)

_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
_IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dist",
    "node_modules",
}


class BoundaryPattern(NamedTuple):
    owner_root: Path
    rule: str
    regex: re.Pattern[str]
    needles: tuple[str, ...]


class Finding:
    __slots__ = ("kind", "line", "path", "rule")

    def __init__(self, *, kind: str, path: Path, line: int | None, rule: str) -> None:
        self.kind = kind
        self.path = path
        self.line = line
        self.rule = rule

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line is not None else str(self.path)
        return f"{location}: {self.kind} violates provider-neutral OAuth boundary ({self.rule})"


def _relative(root: Path, path: Path) -> Path:
    try:
        return path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ValueError(f"scan path is outside repository root: {path}") from exc


def _policy_rows(value: Any, *, path: Path) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"invalid OAuth boundary policy: {path}")
    rows = value.get("patterns")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"invalid OAuth boundary policy patterns: {path}")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"needles", "rule", "regex"}:
            raise ValueError(f"invalid OAuth boundary policy row: {path}")
        rule = row.get("rule")
        pattern = row.get("regex")
        needles = row.get("needles")
        if (
            not isinstance(rule, str)
            or not rule.strip()
            or not isinstance(pattern, str)
            or not isinstance(needles, list)
            or not needles
            or any(not isinstance(needle, str) or not needle for needle in needles)
        ):
            raise ValueError(f"invalid OAuth boundary policy row: {path}")
        normalized.append({"rule": rule.strip(), "regex": pattern, "needles": needles})
    return normalized


def load_boundary_patterns(root: Path) -> list[BoundaryPattern]:
    root = root.resolve()
    patterns: list[BoundaryPattern] = []
    for policy_path in sorted((root / "extensions").glob(f"*/tests/{POLICY_NAME}")):
        owner_root = policy_path.parents[1].resolve().relative_to(root)
        value = json.loads(policy_path.read_text(encoding="utf-8"))
        for row in _policy_rows(value, path=policy_path):
            patterns.append(
                BoundaryPattern(
                    owner_root=owner_root,
                    rule=row["rule"],
                    regex=re.compile(row["regex"], re.IGNORECASE),
                    needles=tuple(needle.lower() for needle in row["needles"]),
                )
            )
    return patterns


def _match(value: str, patterns: Iterable[BoundaryPattern]) -> str | None:
    lowered = value.lower()
    for pattern in patterns:
        if any(needle in lowered for needle in pattern.needles) and pattern.regex.search(value):
            return pattern.rule
    return None


def scan_paths(
    root: Path,
    paths: Iterable[Path],
    patterns: Iterable[BoundaryPattern] | None = None,
) -> list[Finding]:
    root = root.resolve()
    active_patterns = list(patterns) if patterns is not None else load_boundary_patterns(root)
    findings: list[Finding] = []
    for path in sorted({candidate.absolute() for candidate in paths}):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = _relative(root, path)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if relative in SANCTIONED_FRONTEND_TEMPLATE_PATHS:
            continue
        file_patterns = [
            pattern
            for pattern in active_patterns
            if relative != pattern.owner_root and pattern.owner_root not in relative.parents
        ]
        path_rule = _match(relative.as_posix(), file_patterns)
        if path_rule is not None:
            findings.append(Finding(kind="path", path=relative, line=None, rule=path_rule))
        if path.is_symlink():
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            content_rule = _match(line, file_patterns)
            if content_rule is not None:
                findings.append(
                    Finding(kind="content", path=relative, line=line_number, rule=content_rule)
                )
    return findings


def scan_repository(root: Path) -> list[Finding]:
    root = root.resolve()
    paths: list[Path] = [root / path for path in SCANNED_FILES]
    for relative_root in SCANNED_ROOTS:
        bases = (
            sorted(path for path in (root / relative_root).iterdir() if path.is_dir())
            if relative_root == Path("extensions")
            else [root / relative_root]
        )
        for base in bases:
            for directory, names, files in os.walk(base, followlinks=False):
                names[:] = [name for name in names if name not in _IGNORED_PARTS]
                paths.extend(Path(directory) / name for name in files)
    return scan_paths(root, paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    args = parser.parse_args()
    findings = scan_repository(args.root)
    if findings:
        print("Provider-neutral OAuth boundary violations:")
        for finding in findings:
            print(f"- {finding.render()}")
        return 1
    print("Provider-neutral OAuth boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
