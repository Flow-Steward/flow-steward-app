"""Preserve upstream secret detection while retaining proven per-file passes."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from core.application.tooling.quality.file_scan_cache import FileScanCache


def _configuration(payload: dict) -> tuple:
    filters = payload.get("filters_used")
    plugins = payload.get("plugins_used")
    version = payload.get("version")
    if (
        not isinstance(filters, list)
        or not isinstance(plugins, list)
        or not isinstance(version, str)
    ):
        raise ValueError("scanner report has no complete configuration")
    # Only the temporary baseline filename is operational metadata. Every other
    # plugin/filter setting must equal the reviewed baseline configuration.
    filters = [
        item
        for item in filters
        if not (
            isinstance(item, dict)
            and item.get("path") == "detect_secrets.filters.common.is_baseline_file"
        )
    ]
    return version, plugins, filters


def _findings(report: str, baseline: Path, paths: set[str]) -> set[str]:
    payload = json.loads(report)
    expected = json.loads(baseline.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(expected, dict):
        raise ValueError("scanner report must be one complete JSON object")
    if set(payload) != {"version", "plugins_used", "filters_used", "results", "generated_at"}:
        raise ValueError("scanner report has unexpected or missing fields")
    if _configuration(payload) != _configuration(expected):
        raise ValueError("scanner report configuration does not match the reviewed baseline")
    results = payload["results"]
    if not isinstance(results, dict) or not isinstance(payload["generated_at"], str):
        raise ValueError("scanner findings are incomplete")
    for path, findings in results.items():
        if path not in paths or not isinstance(findings, list) or not findings:
            raise ValueError("scanner report contains an unscanned or invalid finding path")
        for finding in findings:
            if not isinstance(finding, dict) or not {
                "type",
                "filename",
                "hashed_secret",
                "is_verified",
                "line_number",
            } <= set(finding):
                raise ValueError("scanner finding is incomplete")
            if set(finding) - {
                "type",
                "filename",
                "hashed_secret",
                "is_verified",
                "line_number",
                "is_secret",
            }:
                raise ValueError("scanner finding has unknown fields")
            if (
                finding["filename"] != path
                or not isinstance(finding["type"], str)
                or not finding["type"]
                or not isinstance(finding["hashed_secret"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", finding["hashed_secret"])
                or type(finding["is_verified"]) is not bool
                or type(finding["line_number"]) is not int
                or finding["line_number"] < 1
            ):
                raise ValueError("scanner finding has invalid identity or status")
    # is_verified:false is still a finding, never proof of a clean file.
    return set(results)


def cache_scan_result(
    cache: FileScanCache,
    snapshot: dict,
    paths: Sequence[Path],
    exit_code: int,
    report: str,
    baseline: Path,
) -> int:
    pending = {path.as_posix() for path in paths}
    if exit_code == 0:
        findings = _findings(report, baseline, pending) if report.strip() else set()
        if findings:
            raise ValueError("successful scanner exit contradicts its findings")
    elif exit_code == 1:
        findings = _findings(report, baseline, pending)
        if not findings:
            raise ValueError("failed scanner has no complete findings report")
    else:
        raise ValueError("scanner did not complete a valid scan")
    return cache.record_snapshot(snapshot, pending - findings)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments.pop(0) != "hook":
        raise ValueError("expected the upstream hook command")
    from detect_secrets.core.log import log
    from detect_secrets.pre_commit_hook import main as upstream_main

    # The pinned hook otherwise suppresses IOError warnings. Expose warnings
    # without -v's per-file INFO stream, so incomplete scans cannot earn passes.
    # Do not alter plugins, verification, findings, baseline or exclusions.
    log.setLevel(logging.WARNING)
    return upstream_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
