#!/usr/bin/env python3
"""SEC-001: Naming guardrail — reject the retired token marker in new artifacts.

Checks new or renamed files, SQL entity names, and Python route paths for the
retired token marker.

Usage
-----
    # Against a base branch (CI / PR mode):
    flow-steward quality check-naming --base origin/main

    # Against the previous commit (pre-commit / push mode):
    flow-steward quality check-naming --base HEAD~1

    # Dry-run on an explicit file list (for unit tests):
    flow-steward quality check-naming --files db/schema/099_retired_foo.sql security/bar.py

Exit code 0 = pass, 1 = violations found.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# No allowlist remains: all newly introduced paths/entities/routes must
# be free of the retired token marker.
# ---------------------------------------------------------------------------
ALLOWLISTED_PATH_PREFIXES: tuple[str, ...] = ()

_RETIRED_PRODUCT_TOKEN = "retiredcodename"
TOKEN_RE = re.compile(re.escape(_RETIRED_PRODUCT_TOKEN), re.IGNORECASE)

# SQL entity creation patterns
SQL_CREATE_RE = re.compile(
    r"CREATE\s+(?:TABLE|INDEX|UNIQUE\s+INDEX|TYPE|SEQUENCE|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(\S+)",
    re.IGNORECASE,
)

# Python route path patterns for retired route segments.
PY_ROUTE_RE = re.compile(
    r"""@\w+\.(?:get|post|put|patch|delete|head|options|websocket)\s*\(\s*["']([^"']+)["']""",
)


def _is_allowlisted(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in ALLOWLISTED_PATH_PREFIXES)


def _check_filename(path: str) -> list[str]:
    """Return violations if the filename itself contains the retired token marker."""
    basename = Path(path).name
    if TOKEN_RE.search(basename):
        return [f"  {path}: filename contains the retired token marker"]
    return []


def _check_sql_entities(path: str, content: str) -> list[str]:
    """Check SQL CREATE statements for the retired token marker in entity names."""
    violations: list[str] = []
    for m in SQL_CREATE_RE.finditer(content):
        entity = m.group(1).strip('"').strip("'").lower()
        if TOKEN_RE.search(entity):
            violations.append(f"  {path}: SQL entity '{entity}' contains the retired token marker")
    return violations


def _check_py_routes(path: str, content: str) -> list[str]:
    """Check Python route decorator paths for the retired token marker."""
    violations: list[str] = []
    for m in PY_ROUTE_RE.finditer(content):
        route = m.group(1)
        if TOKEN_RE.search(route):
            violations.append(f"  {path}: route path '{route}' contains the retired token marker")
    return violations


def _rev_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def get_changed_files(base: str) -> list[str]:
    """Return list of added/copied/renamed files relative to *base*.

    Robust to a shallow CI clone that does not contain the PR base SHA: best-effort fetch
    the base, then fall back to resolvable refs. If none resolve, warn and return [] rather
    than crash the whole gate on a base-resolution infra problem (the CI checkout should
    use fetch-depth: 0 so the base is present and this fallback is not exercised).
    """
    if not _rev_exists(base):
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", base], capture_output=True, text=True
        )
    if not _rev_exists(base):
        base = next((alt for alt in ("origin/main", "main", "HEAD~1") if _rev_exists(alt)), "")
        if not base:
            print(
                "check_naming: base ref unavailable (shallow clone?); skipping diff-based check",
                file=sys.stderr,
            )
            return []
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACR", base],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"check_naming: `git diff {base}` failed ({result.stderr.strip()[:120]}); skipping",
            file=sys.stderr,
        )
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def check_files(files: list[str], repo_root: Path | None = None) -> list[str]:
    """Run all checks on *files*.  Returns list of violation strings."""
    violations: list[str] = []
    for path in files:
        if _is_allowlisted(path):
            continue
        violations.extend(_check_filename(path))

        # Read file content for deeper checks
        full = (repo_root / path) if repo_root else Path(path)
        if not full.is_file():
            continue
        try:
            content = full.read_text(errors="replace")
        except OSError:
            continue

        if path.endswith(".sql"):
            violations.extend(_check_sql_entities(path, content))
        if path.endswith(".py"):
            violations.extend(_check_py_routes(path, content))

    return violations


def run_check_naming(*, base: str = "", files: list[str] | None = None) -> int:
    """Check naming guardrails from a typed operation boundary."""
    if files is not None:
        check_targets = files
    elif base:
        check_targets = get_changed_files(base)
    else:
        raise ValueError("Provide base or files")

    if not check_targets:
        print("SEC-001 Naming Guardrail PASSED — 0 file(s) checked, 0 violations.")
        return 0

    repo_root: Path | None = None
    try:
        resolved_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        resolved_root = ""
    if resolved_root:
        repo_root = Path(resolved_root)

    violations = check_files(check_targets, repo_root=repo_root)

    if violations:
        print("SEC-001 Naming Guardrail FAILED — retired product token found in new artifacts:\n")
        print("\n".join(violations))
        print(
            f"\n{len(violations)} violation(s) found. New artifacts must not contain the retired product token."
        )
        return 1

    print(f"SEC-001 Naming Guardrail PASSED — {len(check_targets)} file(s) checked, 0 violations.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check for forbidden retired-token naming in new artifacts"
    )
    parser.add_argument("--base", help="Git ref to diff against (e.g. origin/main)")
    parser.add_argument("--files", nargs="*", help="Explicit file list (for testing)")
    args = parser.parse_args(argv)

    if args.files is not None:
        return run_check_naming(files=list(args.files))
    if args.base:
        files = get_changed_files(args.base)
    else:
        parser.error("Provide --base or --files")
        return 1
    return run_check_naming(files=files)


if __name__ == "__main__":
    sys.exit(main())
