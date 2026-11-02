"""Audit every installed scanner dependency, with verified upstream code identity."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path

from core.application.tooling.quality.semgrep_distribution import (
    DEPENDENCY_PATCH,
    LOCAL_VERSION,
    UPSTREAM_DIGESTS,
    UPSTREAM_VERSION,
)


def _name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None:
        raise ValueError("invalid installed package name")
    return re.sub(r"[-_.]+", "-", value).lower()


def verified_inventory(receipt_path: Path) -> dict[str, str]:
    """Bind advisory lookup to unchanged upstream code and actual installed packages."""
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        receipt.get("source_sha256") not in UPSTREAM_DIGESTS
        or receipt.get("upstream_version") != UPSTREAM_VERSION
        or receipt.get("local_version") != LOCAL_VERSION
        or not isinstance(receipt.get("files"), dict)
        or not receipt["files"]
    ):
        raise ValueError("invalid scanner provenance")
    scanner = importlib.metadata.distribution("semgrep")
    if scanner.version != LOCAL_VERSION:
        raise ValueError("unexpected installed scanner identity")
    for relative, expected in receipt["files"].items():
        path = Path(str(scanner.locate_file(relative)))
        if Path(relative).is_absolute() or ".." in Path(relative).parts or path.is_symlink():
            raise ValueError("unsafe installed scanner path")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError("installed scanner provenance mismatch")
    inventory: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = _name(distribution.metadata["Name"])
        version = distribution.version
        if name in inventory or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]*", version) is None:
            raise ValueError("invalid or duplicate installed package identity")
        inventory[name] = version
    if inventory.get("semgrep") != LOCAL_VERSION:
        raise ValueError("scanner missing from installed inventory")
    for requirement in DEPENDENCY_PATCH.values():
        name, version = requirement.split("==")
        if inventory.get(_name(name)) != version:
            raise ValueError(f"unexpected scanner dependency: {name}")
    # Only packaging metadata changed. Query the original, published code version
    # explicitly so a local version absent from PyPI cannot silently skip Semgrep.
    inventory["semgrep"] = UPSTREAM_VERSION
    return dict(sorted(inventory.items()))


def validate_report(
    report_path: Path, inventory: dict[str, str], *, audit_status: int | None = None
) -> None:
    """Reject skips and incomplete reports before any acceptance evaluation."""
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    dependencies = payload.get("dependencies") if isinstance(payload, dict) else None
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("missing scanner audit inventory")
    if payload.get("errors") or payload.get("error"):
        raise ValueError("scanner audit service error")
    actual: dict[str, str] = {}
    has_findings = False
    for dependency in dependencies:
        if not isinstance(dependency, dict) or dependency.get("skip_reason"):
            raise ValueError("scanner audit skipped a package")
        name = _name(str(dependency.get("name") or ""))
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns")
        if name in actual or not isinstance(version, str) or not isinstance(vulnerabilities, list):
            raise ValueError("malformed scanner audit dependency")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or not vulnerability.get("id"):
                raise ValueError("malformed scanner advisory")
        has_findings = has_findings or bool(vulnerabilities)
        actual[name] = version
    if audit_status is not None and audit_status != int(has_findings):
        raise ValueError("scanner audit process status does not match its report")
    if actual != inventory:
        raise ValueError("scanner audit does not match the installed inventory")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inventory", "validate"])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--audit-status", type=int)
    args = parser.parse_args()
    if args.command == "inventory":
        inventory = verified_inventory(args.source)
        args.target.write_text(
            "".join(f"{name}=={version}\n" for name, version in inventory.items()), encoding="utf-8"
        )
    else:
        inventory = {}
        for line in args.source.read_text().splitlines():
            key, value = line.split("==", 1)
            inventory[key] = value
        validate_report(args.target, inventory, audit_status=args.audit_status)


if __name__ == "__main__":
    main()
