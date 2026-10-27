#!/usr/bin/env python3
"""Lint tests/CI_EXCLUDES.txt to enforce the R-12 ownership contract.

Every non-blank, non-comment line in the exclude file must either:
  1. Sit inside a ROOT_CAUSE block that has OWNER + TICKET + REMOVE_BY
     metadata, OR
  2. Be immediately preceded by a comment line of the form
     ``# root_cause: <ID>`` that points at a known ROOT_CAUSE block.

Additionally:
  * Every ROOT_CAUSE block MUST carry OWNER, TICKET, and REMOVE_BY.
  * REMOVE_BY must be a YYYY-MM-DD date and must NOT be in the past.
  * Every test nodeid must reference a file that exists in the repo.
  * The exclude file must parse without ambiguity (trailing whitespace,
    duplicate entries, or malformed nodeids all fail).

Exit codes:
  0  OK
  1  lint errors found
  2  CLI / environment error

Run:
  flow-steward quality check-ci-excludes
  flow-steward quality check-ci-excludes --path tests/CI_EXCLUDES.txt
"""

from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import sys
from dataclasses import dataclass, field

ROOT_CAUSE_HEADER_RE = re.compile(r"^#\s*ROOT_CAUSE:\s*([\w.-]+)\b", re.ASCII)
METADATA_LINE_RE = re.compile(r"^#\s*+(OWNER|TICKET|REMOVE_BY):\s*+([^\r\n]*+)$")
INLINE_CAUSE_RE = re.compile(r"^#\s*root_cause:\s*([\w.-]+)\b", re.IGNORECASE | re.ASCII)


def _is_pytest_node_id(value: str) -> bool:
    """Return whether *value* names a Python test file or one of its nodes."""
    if not value or value.startswith("#") or any(character.isspace() for character in value):
        return False
    path, separator, node = value.partition("::")
    return path.endswith(".py") and (not separator or bool(node))


@dataclass
class RootCause:
    name: str
    line_no: int
    owner: str | None = None
    ticket: str | None = None
    remove_by: str | None = None
    remove_by_date: _dt.date | None = None
    entries: list[str] = field(default_factory=list)


def parse(path: pathlib.Path) -> tuple[list[RootCause], list[str]]:
    """Return (root_causes_in_order, errors). Never raises."""
    if not path.is_file():
        return [], [f"exclude file not found: {path}"]

    errors: list[str] = []
    causes: list[RootCause] = []
    current: RootCause | None = None
    seen_entries: set[str] = set()
    inline_cause: str | None = None

    for line_no, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.rstrip()
        if line != raw:
            errors.append(f"{path}:{line_no}: trailing whitespace")

        if not line.strip():
            inline_cause = None
            continue

        if line.startswith("#"):
            m = ROOT_CAUSE_HEADER_RE.match(line)
            if m:
                current = RootCause(name=m.group(1), line_no=line_no)
                causes.append(current)
                inline_cause = None
                continue

            m = METADATA_LINE_RE.match(line)
            if m and current is not None:
                key, value = m.group(1), m.group(2).strip()
                if key == "OWNER":
                    current.owner = value
                elif key == "TICKET":
                    current.ticket = value
                elif key == "REMOVE_BY":
                    current.remove_by = value
                continue

            m = INLINE_CAUSE_RE.match(line)
            if m:
                inline_cause = m.group(1)
                continue

            # End of header block when we hit a separator comment
            if line.startswith(("# ===", "# ---")):
                # still inside the same ROOT_CAUSE block — don't reset
                inline_cause = None
            continue

        # Not a comment, not blank → must be a test nodeid
        if not _is_pytest_node_id(line):
            errors.append(f"{path}:{line_no}: malformed test nodeid: {line!r}")
            continue

        if line in seen_entries:
            errors.append(f"{path}:{line_no}: duplicate entry: {line}")
            continue
        seen_entries.add(line)

        cause_name = inline_cause
        if cause_name is None and current is not None:
            cause_name = current.name

        if cause_name is None:
            errors.append(
                f"{path}:{line_no}: entry has no ROOT_CAUSE context "
                f"(missing header or '# root_cause: ID' comment): {line}"
            )
            continue

        # Attach to the matching cause
        matched = next((c for c in causes if c.name == cause_name), None)
        if matched is None:
            errors.append(
                f"{path}:{line_no}: entry references unknown ROOT_CAUSE={cause_name}: {line}"
            )
            continue
        matched.entries.append(line)

    return causes, errors


def _validate_metadata(cause: RootCause, path: pathlib.Path) -> list[str]:
    errors: list[str] = []
    header = f"{path}:{cause.line_no}: ROOT_CAUSE {cause.name}"
    if not cause.owner:
        errors.append(f"{header}: missing OWNER")
    if not cause.ticket:
        errors.append(f"{header}: missing TICKET")
    if not cause.remove_by:
        errors.append(f"{header}: missing REMOVE_BY")
        return errors
    try:
        cause.remove_by_date = _dt.datetime.strptime(cause.remove_by, "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"{header}: REMOVE_BY is not YYYY-MM-DD: {cause.remove_by!r}")
        return errors
    today = _dt.date.today()
    if cause.remove_by_date < today:
        errors.append(
            f"{header}: REMOVE_BY={cause.remove_by} has passed (today={today.isoformat()})"
        )
    if not cause.entries:
        errors.append(f"{header}: has no excluded tests — remove the header")
    return errors


def _validate_entries(
    causes: list[RootCause], path: pathlib.Path, repo_root: pathlib.Path
) -> list[str]:
    errors: list[str] = []
    for cause in causes:
        for entry in cause.entries:
            test_path = entry.split("::", 1)[0]
            if not (repo_root / test_path).is_file():
                errors.append(f"{path}: {cause.name} references missing file: {test_path}")
    return errors


def run_check_ci_excludes(path: str = "") -> int:
    """Validate CI_EXCLUDES metadata from a typed operation boundary."""
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    exclude_path = pathlib.Path(path).resolve() if path else repo_root / "tests" / "CI_EXCLUDES.txt"

    causes, errors = parse(exclude_path)
    for cause in causes:
        errors.extend(_validate_metadata(cause, exclude_path))
    errors.extend(_validate_entries(causes, exclude_path, repo_root))

    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        print(
            f"\n[check_ci_excludes] FAIL — {len(errors)} issue(s) in {exclude_path}",
            file=sys.stderr,
        )
        return 1

    total_entries = sum(len(c.entries) for c in causes)
    print(
        f"[check_ci_excludes] OK — {len(causes)} ROOT_CAUSE group(s), "
        f"{total_entries} excluded test(s), all metadata valid"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint tests/CI_EXCLUDES.txt")
    parser.add_argument(
        "--path",
        default=None,
        help="path to the exclude file (default: tests/CI_EXCLUDES.txt at repo root)",
    )
    args = parser.parse_args(argv)
    return run_check_ci_excludes(str(args.path or ""))


if __name__ == "__main__":
    raise SystemExit(main())
