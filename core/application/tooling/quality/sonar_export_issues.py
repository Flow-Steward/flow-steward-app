#!/usr/bin/env python3
# ruff: noqa: UP017
"""Export SonarQube issues to machine-readable files (JSON/CSV/summary)."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
from collections import Counter

# This script intentionally uses `datetime.timezone.utc` instead of the
# `datetime.UTC` alias (added in Python 3.11). The audit host runs Python 3.9
# and cannot import the 3.11 alias. See R-13 notes in
# the local Sonar quality-gate policy.
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export SonarQube issues via /api/issues/search to JSON and CSV."
    )
    parser.add_argument(
        "--host-url",
        default=(
            _runtime_env.get("SONAR_EXPORT_HOST_URL")
            or _runtime_env.get("SONAR_WAIT_URL")
            or _runtime_env.get("SONAR_HOST_URL")
            or "http://localhost:9000"
        ),
        help="SonarQube base URL accessible from this machine.",
    )
    parser.add_argument(
        "--project-key",
        default=_runtime_env.get("SONAR_PROJECT_KEY", "flow-steward-project"),
        help="SonarQube project key.",
    )
    parser.add_argument(
        "--statuses",
        default=_runtime_env.get("SONAR_ISSUES_STATUSES", "OPEN,CONFIRMED,REOPENED"),
        help="Comma-separated issue statuses filter. Use empty string for all.",
    )
    parser.add_argument(
        "--severities",
        default=_runtime_env.get("SONAR_ISSUES_SEVERITIES", ""),
        help="Comma-separated severities filter (optional).",
    )
    parser.add_argument(
        "--types",
        default=_runtime_env.get("SONAR_ISSUES_TYPES", ""),
        help="Comma-separated types filter (BUG,VULNERABILITY,CODE_SMELL).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=int(_runtime_env.get("SONAR_ISSUES_PAGE_SIZE", "500")),
        help="Sonar API page size (max 500).",
    )
    parser.add_argument(
        "--output-dir",
        default=_runtime_env.get("SONAR_EXPORT_DIR", "logs/quality/sonar"),
        help="Directory where export files are written.",
    )
    return parser.parse_args()


def get_token(explicit_token: str = "") -> str:
    token = str(explicit_token or "").strip() or (_runtime_env.get("SONAR_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "[sonar-export] SONAR_TOKEN is not set. Add it to .env or export it before running."
        )
    return token


def fetch_json(host_url: str, token: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    parsed_host = urlparse(host_url)
    if parsed_host.scheme not in {"http", "https"}:
        raise SystemExit(
            f"[sonar-export] Unsupported URL scheme for --host-url: {parsed_host.scheme!r}"
        )
    query = urlencode(params, doseq=True)
    url = f"{host_url.rstrip('/')}{path}?{query}"
    basic = base64.b64encode(f"{token}:".encode()).decode("ascii")
    req = Request(url, headers={"Authorization": f"Basic {basic}"})
    try:
        with urlopen(req, timeout=60) as resp:  # nosec B310
            payload = resp.read()
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unavailable>"
        raise SystemExit(
            f"[sonar-export] HTTP {exc.code} from SonarQube API.\nURL: {url}\nBody: {body[:1200]}"
        ) from exc
    except URLError as exc:
        raise SystemExit(f"[sonar-export] Failed to reach SonarQube: {exc}") from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[sonar-export] Invalid JSON from SonarQube API at {url}: {exc}") from exc


def fetch_all_issues(
    host_url: str,
    token: str,
    project_key: str,
    statuses: str,
    severities: str,
    types: str,
    page_size: int,
) -> list[dict[str, Any]]:
    page_size = max(1, min(page_size, 500))
    page = 1
    issues: list[dict[str, Any]] = []
    total = None

    while True:
        params: dict[str, Any] = {
            "componentKeys": project_key,
            "ps": page_size,
            "p": page,
        }
        if statuses.strip():
            params["statuses"] = statuses.strip()
        if severities.strip():
            params["severities"] = severities.strip()
        if types.strip():
            params["types"] = types.strip()

        payload = fetch_json(host_url, token, "/api/issues/search", params)
        chunk = payload.get("issues") or []
        issues.extend(chunk)

        total = int(payload.get("total", len(issues)))
        if len(issues) >= total or not chunk:
            break
        page += 1

    return issues


def issue_path(component: str) -> str:
    if ":" not in component:
        return component
    return component.split(":", 1)[1]


def flatten_issues(
    host_url: str, project_key: str, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = host_url.rstrip("/")
    for issue in issues:
        key = str(issue.get("key") or "")
        component = str(issue.get("component") or "")
        row = {
            "key": key,
            "project_key": project_key,
            "severity": issue.get("severity"),
            "type": issue.get("type"),
            "status": issue.get("status"),
            "resolution": issue.get("resolution"),
            "rule": issue.get("rule"),
            "component": component,
            "path": issue_path(component),
            "line": issue.get("line"),
            "message": issue.get("message"),
            "effort": issue.get("effort"),
            "debt": issue.get("debt"),
            "author": issue.get("author"),
            "assignee": issue.get("assignee"),
            "creation_date": issue.get("creationDate"),
            "update_date": issue.get("updateDate"),
            "tags": issue.get("tags") or [],
            "url": f"{base}/project/issues?id={project_key}&issues={key}&open={key}" if key else "",
        }
        rows.append(row)
    return rows


def build_summary(flat_rows: list[dict[str, Any]]) -> dict[str, Any]:
    sev = Counter(str(r.get("severity") or "UNKNOWN") for r in flat_rows)
    typ = Counter(str(r.get("type") or "UNKNOWN") for r in flat_rows)
    st = Counter(str(r.get("status") or "UNKNOWN") for r in flat_rows)
    top_files = Counter(str(r.get("path") or "") for r in flat_rows if r.get("path"))
    top_rules = Counter(str(r.get("rule") or "") for r in flat_rows if r.get("rule"))

    return {
        "total_issues": len(flat_rows),
        "by_severity": dict(sev.most_common()),
        "by_type": dict(typ.most_common()),
        "by_status": dict(st.most_common()),
        "top_files": dict(top_files.most_common(25)),
        "top_rules": dict(top_rules.most_common(25)),
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "key",
        "project_key",
        "severity",
        "type",
        "status",
        "resolution",
        "rule",
        "component",
        "path",
        "line",
        "message",
        "effort",
        "debt",
        "author",
        "assignee",
        "creation_date",
        "update_date",
        "tags",
        "url",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            clean = dict(row)
            clean["tags"] = ",".join(clean.get("tags") or [])
            writer.writerow(clean)


def export_sonar_issues(
    *,
    host_url: str,
    project_key: str,
    statuses: str,
    severities: str,
    types: str,
    page_size: int,
    output_dir: str,
    sonar_token: str,
) -> int:
    """Export Sonar issues from a typed operation boundary."""
    token = get_token(sonar_token)
    host_url = str(host_url).strip()
    project_key = str(project_key).strip()
    if not host_url or not project_key:
        raise SystemExit("[sonar-export] --host-url and --project-key must be non-empty.")

    issues = fetch_all_issues(
        host_url=host_url,
        token=token,
        project_key=project_key,
        statuses=str(statuses or ""),
        severities=str(severities or ""),
        types=str(types or ""),
        page_size=int(page_size),
    )
    flat_rows = flatten_issues(host_url, project_key, issues)
    summary = build_summary(flat_rows)

    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    raw_path = out_dir / f"sonar_issues_raw_{timestamp}.json"
    flat_json_path = out_dir / f"sonar_issues_flat_{timestamp}.json"
    flat_csv_path = out_dir / f"sonar_issues_flat_{timestamp}.csv"
    summary_path = out_dir / f"sonar_issues_summary_{timestamp}.json"

    raw_payload = {
        "exported_at_utc": timestamp,
        "host_url": host_url,
        "project_key": project_key,
        "filters": {
            "statuses": statuses,
            "severities": severities,
            "types": types,
            "page_size": page_size,
        },
        "total_issues": len(issues),
        "issues": issues,
    }

    write_json(raw_path, raw_payload)
    write_json(flat_json_path, flat_rows)
    write_csv(flat_csv_path, flat_rows)
    write_json(summary_path, summary)

    print(f"[sonar-export] Export complete. Total issues: {len(issues)}")
    print(f"[sonar-export] Raw JSON:     {raw_path}")
    print(f"[sonar-export] Flat JSON:    {flat_json_path}")
    print(f"[sonar-export] Flat CSV:     {flat_csv_path}")
    print(f"[sonar-export] Summary JSON: {summary_path}")
    return 0


def main() -> int:
    args = parse_args()
    return export_sonar_issues(
        host_url=str(args.host_url),
        project_key=str(args.project_key),
        statuses=str(args.statuses or ""),
        severities=str(args.severities or ""),
        types=str(args.types or ""),
        page_size=int(args.page_size),
        output_dir=str(args.output_dir),
        sonar_token="",
    )


if __name__ == "__main__":
    sys.exit(main())
