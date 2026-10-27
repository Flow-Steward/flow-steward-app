#!/usr/bin/env python3
"""Fail a release run on unexpected candidate-owned error log lines."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

REQUIRED_SERVICES = ("fs-app", "fs-worker", "fs-mcp", "control-plane")
ERROR_SEVERITY = re.compile(r"\bERROR\b")
SUSPICIOUS_DETAIL = re.compile(
    r"(?:foreign[ -]key|ForeignKeyViolation|\bFK\b|protocol error|requeue|"
    r"Traceback \(most recent call last\))",
    re.IGNORECASE,
)


def _run_docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _candidate_services(compose_project: str) -> dict[str, str]:
    result = _run_docker(
        "ps",
        "--filter",
        f"label=com.docker.compose.project={compose_project}",
        "--filter",
        "status=running",
        "--format",
        '{{.Names}}|{{.Label "com.docker.compose.service"}}',
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "candidate container inventory failed")
    by_service: dict[str, list[str]] = {service: [] for service in REQUIRED_SERVICES}
    for raw_line in result.stdout.splitlines():
        container, separator, service = raw_line.partition("|")
        if separator and service in by_service and container:
            by_service[service].append(container)
    invalid = {
        service: containers for service, containers in by_service.items() if len(containers) != 1
    }
    if invalid:
        detail = ", ".join(
            f"{service}={containers or ['missing']}" for service, containers in invalid.items()
        )
        raise RuntimeError(f"candidate log service inventory is not singular: {detail}")
    return {service: containers[0] for service, containers in by_service.items()}


def _load_allowlist(path: Path) -> dict[str, set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, dict):
        raise RuntimeError("candidate log allowlist must contain a services object")
    unknown = sorted(set(services) - set(REQUIRED_SERVICES))
    if unknown:
        raise RuntimeError(f"candidate log allowlist has unknown services: {unknown}")
    normalized: dict[str, set[str]] = {}
    for service in REQUIRED_SERVICES:
        lines = services.get(service, [])
        if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
            raise RuntimeError(f"candidate log allowlist for {service} must be a string array")
        normalized[service] = {line.rstrip("\r\n") for line in lines}
    return normalized


def _unexpected_lines(
    *,
    service: str,
    container: str,
    since: str,
    until: str,
    allowlist: dict[str, set[str]],
) -> list[str]:
    result = _run_docker("logs", "--since", since, "--until", until, container)
    if result.returncode != 0:
        raise RuntimeError(
            f"candidate log read failed for {service}/{container}: {result.stderr.strip()[:500]}"
        )
    findings: list[str] = []
    for raw_line in (result.stdout + result.stderr).splitlines():
        line = raw_line.rstrip("\r\n")
        if (
            ERROR_SEVERITY.search(line) or SUSPICIOUS_DETAIL.search(line)
        ) and line not in allowlist[service]:
            findings.append(line)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--allowlist", required=True, type=Path)
    args = parser.parse_args()

    try:
        allowlist = _load_allowlist(args.allowlist)
        services = _candidate_services(args.compose_project)
        findings: list[tuple[str, str]] = []
        for service in REQUIRED_SERVICES:
            for line in _unexpected_lines(
                service=service,
                container=services[service],
                since=args.since,
                until=args.until,
                allowlist=allowlist,
            ):
                findings.append((service, line))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"candidate_log_window=failed reason={exc}")
        return 1

    if findings:
        for service, line in findings:
            print(f"candidate_log_error service={service} line={line}")
        print(
            "candidate_log_window=failed "
            f"since={args.since} until={args.until} unexpected={len(findings)}"
        )
        return 1
    print(
        "candidate_log_window=ok "
        f"since={args.since} until={args.until} services={len(REQUIRED_SERVICES)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
