"""Accept named, expiring npm advisories in dev-only packages; refuse everything else.

``npm audit`` has no ignore list. When an advisory has no patched release and the
package is used only to build and test the frontend, the owner can accept it for
a limited time in ``infra/tooling/quality/npm-audit-exceptions.json``. This module
decides whether a failing ``npm audit --json`` report contains anything else.

Standalone on purpose: the audit runs from a temporary frontend workspace and
the wrapper calls this file by path, without the product tree on ``sys.path``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

BLOCKING_SEVERITIES = frozenset({"high", "critical"})


def _advisory_id(advisory: dict[str, Any]) -> str:
    return str(advisory.get("url") or "").rstrip("/").rsplit("/", 1)[-1]


def _dev_only(lock: dict[str, Any], nodes: list[Any]) -> bool:
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict) or not nodes:
        return False
    return all(
        isinstance(packages.get(str(node)), dict) and packages[str(node)].get("dev") is True
        for node in nodes
    )


def unaccepted_findings(
    report: dict[str, Any],
    lock: dict[str, Any],
    exceptions: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """Return one line per blocking advisory that no valid exception covers."""
    vulnerabilities = report.get("vulnerabilities") if isinstance(report, dict) else None
    if not isinstance(vulnerabilities, dict):
        return ["npm audit report has no vulnerabilities map"]
    accepted: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in exceptions:
        try:
            expires = date.fromisoformat(str(entry["expires"]))
        except (KeyError, ValueError):
            continue
        if expires >= today and entry.get("dev_only") is True:
            accepted[(str(entry.get("advisory")), str(entry.get("package")))] = entry
    findings: list[str] = []
    for name, vulnerability in sorted(vulnerabilities.items()):
        if not isinstance(vulnerability, dict):
            findings.append(f"{name}: unreadable entry")
            continue
        for via in vulnerability.get("via") or []:
            if not isinstance(via, dict):
                continue  # A package name: its own entry carries the advisory.
            if str(via.get("severity")) not in BLOCKING_SEVERITIES:
                continue
            package = str(via.get("name") or name)
            advisory = _advisory_id(via)
            owner = vulnerabilities.get(package)
            nodes = owner.get("nodes") if isinstance(owner, dict) else None
            if (advisory, package) not in accepted:
                findings.append(f"{package}: {advisory or 'advisory without id'} is not accepted")
            elif not _dev_only(lock, list(nodes or [])):
                findings.append(f"{package}: {advisory} is accepted only for dev-only installs")
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--exceptions", required=True, type=Path)
    parser.add_argument("--today", default="")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else datetime.now(UTC).date()
    try:
        report = json.loads(sys.stdin.read())
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
        exceptions = json.loads(args.exceptions.read_text(encoding="utf-8")).get("exceptions", [])
    except (OSError, ValueError, AttributeError) as exc:
        print(f"[lint] npm audit exceptions could not be evaluated: {exc}", file=sys.stderr)
        return 1
    findings = unaccepted_findings(report, lock, list(exceptions or []), today)
    if findings:
        for line in findings:
            print(f"[lint] npm audit: {line}", file=sys.stderr)
        return 1
    print(
        "[lint] npm audit: only accepted dev-only advisories remain (see npm-audit-exceptions.json)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
