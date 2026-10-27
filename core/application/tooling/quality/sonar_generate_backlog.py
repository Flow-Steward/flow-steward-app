#!/usr/bin/env python3
"""Generate a developer backlog Markdown file from SonarQube CSV export."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

SEVERITY_ORDER = {
    "BLOCKER": 0,
    "CRITICAL": 1,
    "MAJOR": 2,
    "MINOR": 3,
    "INFO": 4,
    "UNKNOWN": 5,
}


@dataclass(frozen=True)
class Issue:
    key: str
    severity: str
    issue_type: str
    rule: str
    path: str
    line: str
    message: str
    effort: str
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate developer backlog from SonarQube flat CSV export."
    )
    parser.add_argument(
        "--input-csv",
        default=_runtime_env.get("SONAR_BACKLOG_INPUT_CSV", "").strip(),
        help="Path to sonar_issues_flat_*.csv. If omitted, the latest file from --input-dir is used.",
    )
    parser.add_argument(
        "--input-dir",
        default=_runtime_env.get("SONAR_EXPORT_DIR", "logs/quality/sonar"),
        help="Directory containing sonar_issues_flat_*.csv exports.",
    )
    parser.add_argument(
        "--output",
        default=_runtime_env.get("SONAR_BACKLOG_PATH", "logs/quality/sonar/sonar-dev-backlog.md"),
        help="Output Markdown file path.",
    )
    parser.add_argument(
        "--top-rules",
        type=int,
        default=int(_runtime_env.get("SONAR_BACKLOG_TOP_RULES", "15")),
        help="Top rules to include in grouped remediation table.",
    )
    parser.add_argument(
        "--top-files",
        type=int,
        default=int(_runtime_env.get("SONAR_BACKLOG_TOP_FILES", "20")),
        help="Top hotspot files to include.",
    )
    parser.add_argument(
        "--max-p0",
        type=int,
        default=int(_runtime_env.get("SONAR_BACKLOG_MAX_P0", "60")),
        help="Maximum number of P0 issues listed in detail.",
    )
    parser.add_argument(
        "--max-p1",
        type=int,
        default=int(_runtime_env.get("SONAR_BACKLOG_MAX_P1", "40")),
        help="Maximum number of P1 issues listed in detail.",
    )
    return parser.parse_args()


def find_latest_csv(directory: Path) -> Path:
    candidates = sorted(
        directory.glob("sonar_issues_flat_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"[sonar-backlog] No sonar_issues_flat_*.csv files found in: {directory}")
    return candidates[0]


def read_issues(csv_path: Path) -> list[Issue]:
    issues: list[Issue] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            issues.append(
                Issue(
                    key=str(row.get("key") or "").strip(),
                    severity=str(row.get("severity") or "UNKNOWN").strip().upper() or "UNKNOWN",
                    issue_type=str(row.get("type") or "UNKNOWN").strip().upper() or "UNKNOWN",
                    rule=str(row.get("rule") or "").strip(),
                    path=str(row.get("path") or "").strip(),
                    line=str(row.get("line") or "").strip(),
                    message=str(row.get("message") or "").strip(),
                    effort=str(row.get("effort") or "").strip(),
                    url=str(row.get("url") or "").strip(),
                )
            )
    return issues


def priority_for(issue: Issue) -> str:
    if issue.issue_type == "VULNERABILITY":
        return "P0"
    if issue.issue_type == "BUG" and issue.severity in {"BLOCKER", "CRITICAL"}:
        return "P0"
    if issue.severity == "BLOCKER":
        return "P0"
    if issue.issue_type == "BUG":
        return "P1"
    if issue.severity == "CRITICAL":
        return "P1"
    if issue.severity == "MAJOR":
        return "P2"
    return "P3"


def sort_issues(issues: list[Issue]) -> list[Issue]:
    return sorted(
        issues,
        key=lambda issue: (
            SEVERITY_ORDER.get(issue.severity, SEVERITY_ORDER["UNKNOWN"]),
            issue.path,
            _safe_int(issue.line),
            issue.rule,
            issue.key,
        ),
    )


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 10**9


def parse_effort_minutes(value: str) -> int:
    if not value:
        return 0
    total = 0
    for amount, unit in _iter_effort_tokens(value):
        n = int(amount)
        if unit.startswith("min"):
            total += n
        elif unit.startswith("h"):
            total += n * 60
        elif unit.startswith("d"):
            total += n * 60 * 8
        elif unit.startswith("w"):
            total += n * 60 * 8 * 5
    return total


def _iter_effort_tokens(value: str) -> list[tuple[str, str]]:
    text = str(value or "").lower()
    tokens: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(text):
        if not text[cursor].isdigit():
            cursor += 1
            continue

        amount_start = cursor
        while cursor < len(text) and text[cursor].isdigit():
            cursor += 1
        amount = text[amount_start:cursor]

        while cursor < len(text) and text[cursor].isspace():
            cursor += 1

        unit_start = cursor
        while cursor < len(text) and text[cursor].isalpha():
            cursor += 1
        unit = text[unit_start:cursor]
        if unit:
            tokens.append((amount, unit))

    return tokens


def escape_md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def short_message(message: str, limit: int = 96) -> str:
    clean = " ".join(message.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def top_rule_suggestions(rule: str) -> str:
    suggestions = {
        "python:S3776": "Split high-complexity functions into smaller, testable helpers.",
        "python:S1192": "Extract duplicated string literals into named constants.",
        "python:S107": "Reduce function argument count via config objects/dataclasses.",
        "Web:S6853": "Fix HTML form labeling/association and semantics for accessibility.",
        "python:S1172": "Remove unused parameters or refactor call sites.",
        "javascript:S3776": "Reduce JS cognitive complexity by isolating conditional branches.",
    }
    return suggestions.get(rule, "Apply the Sonar rule fix pattern and add focused tests.")


def inline_code_list(items: list[str], *, fallback: str) -> str:
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        return fallback
    escaped = [item.replace("`", "\\`") for item in cleaned]
    return ", ".join(f"`{item}`" for item in escaped)


def format_counter(counter: Counter[str]) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{k}={v}" for k, v in counter.most_common())


def render_markdown(
    *,
    csv_path: Path,
    output_path: Path,
    issues: list[Issue],
    top_rules_limit: int,
    top_files_limit: int,
    max_p0: int,
    max_p1: int,
) -> str:
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    sorted_issues = sort_issues(issues)

    by_priority: dict[str, list[Issue]] = defaultdict(list)
    by_severity = Counter()
    by_type = Counter()
    by_file = Counter()
    by_rule = Counter()
    effort_by_priority = Counter()

    for issue in sorted_issues:
        p = priority_for(issue)
        by_priority[p].append(issue)
        by_severity[issue.severity] += 1
        by_type[issue.issue_type] += 1
        by_file[issue.path] += 1
        by_rule[issue.rule] += 1
        effort_by_priority[p] += parse_effort_minutes(issue.effort)

    file_severity: dict[str, Counter[str]] = defaultdict(Counter)
    for issue in sorted_issues:
        file_severity[issue.path][issue.severity] += 1

    lines: list[str] = []
    lines.append("# SonarQube Developer Backlog")
    lines.append("")
    lines.append(f"- Generated (UTC): `{generated_at}`")
    lines.append(f"- Source CSV: `{csv_path}`")
    lines.append(f"- Output: `{output_path}`")
    lines.append("")
    lines.append("## Snapshot")
    lines.append("")
    lines.append(f"- Total issues: **{len(sorted_issues)}**")
    lines.append("- Severity: " + format_counter(by_severity))
    lines.append("- Type: " + format_counter(by_type))
    lines.append(
        "- Priority buckets: "
        + ", ".join(f"{p}={len(by_priority.get(p, []))}" for p in ("P0", "P1", "P2", "P3"))
    )
    lines.append(
        "- Estimated effort (hours): "
        + ", ".join(
            f"{p}={effort_by_priority.get(p, 0) / 60:.1f}h" for p in ("P0", "P1", "P2", "P3")
        )
    )
    lines.append("")

    lines.append("## Immediate Queue (P0)")
    lines.append("")
    p0_issues = by_priority.get("P0", [])
    if not p0_issues:
        lines.append("No P0 issues found.")
    else:
        lines.append("| Severity | Type | File:Line | Rule | Issue |")
        lines.append("|---|---|---|---|---|")
        for issue in p0_issues[:max_p0]:
            loc = issue.path or "-"
            if issue.line:
                loc = f"{loc}:{issue.line}"
            lines.append(
                "| "
                + " | ".join(
                    [
                        escape_md(issue.severity),
                        escape_md(issue.issue_type),
                        escape_md(loc),
                        escape_md(issue.rule or "-"),
                        escape_md(short_message(issue.message)),
                    ]
                )
                + " |"
            )
        remaining = len(p0_issues) - max_p0
        if remaining > 0:
            lines.append("")
            lines.append(f"_... and {remaining} more P0 issues in source CSV._")
    lines.append("")

    lines.append("## Important Queue (P1)")
    lines.append("")
    p1_issues = by_priority.get("P1", [])
    if not p1_issues:
        lines.append("No P1 issues found.")
    else:
        lines.append("| Severity | Type | File:Line | Rule | Issue |")
        lines.append("|---|---|---|---|---|")
        for issue in p1_issues[:max_p1]:
            loc = issue.path or "-"
            if issue.line:
                loc = f"{loc}:{issue.line}"
            lines.append(
                "| "
                + " | ".join(
                    [
                        escape_md(issue.severity),
                        escape_md(issue.issue_type),
                        escape_md(loc),
                        escape_md(issue.rule or "-"),
                        escape_md(short_message(issue.message)),
                    ]
                )
                + " |"
            )
        remaining = len(p1_issues) - max_p1
        if remaining > 0:
            lines.append("")
            lines.append(f"_... and {remaining} more P1 issues in source CSV._")
    lines.append("")

    lines.append("## Rule Buckets (For Sprint Planning)")
    lines.append("")
    lines.append("| Rule | Count | Suggested action |")
    lines.append("|---|---:|---|")
    if by_rule:
        for rule, count in by_rule.most_common(top_rules_limit):
            lines.append(
                f"| {escape_md(rule or '-')} | {count} | {escape_md(top_rule_suggestions(rule))} |"
            )
    else:
        lines.append("| - | 0 | No open rule violations in this snapshot. |")
    lines.append("")

    lines.append("## Hotspot Files")
    lines.append("")
    lines.append("| File | Total | Blocker | Critical | Major |")
    lines.append("|---|---:|---:|---:|---:|")
    if by_file:
        for path, count in by_file.most_common(top_files_limit):
            sev = file_severity[path]
            lines.append(
                f"| {escape_md(path)} | {count} | {sev.get('BLOCKER', 0)} | {sev.get('CRITICAL', 0)} | {sev.get('MAJOR', 0)} |"
            )
    else:
        lines.append("| - | 0 | 0 | 0 | 0 |")
    lines.append("")

    top_hotspot_files = [path for path, _ in by_file.most_common(3)]
    top_rule_ids = [rule for rule, _ in by_rule.most_common(4) if rule]
    hotspot_hint = inline_code_list(
        top_hotspot_files, fallback="highest-density files from this report"
    )
    rule_hint = inline_code_list(top_rule_ids, fallback="top recurring rules from this report")

    lines.append("## Suggested Execution Steps")
    lines.append("")
    if p0_issues:
        lines.append(
            "1. Close all P0 issues (security + blocker defects) and rerun `./core/entrypoints/cli/scripts/lint.sh --sonar-full`."
        )
    else:
        lines.append(
            "1. P0 queue is empty; keep it empty by blocking any new blocker/vulnerability regressions."
        )
    if p1_issues:
        lines.append(f"2. Tackle P1 issues in high-density files first ({hotspot_hint}).")
    else:
        lines.append(
            "2. P1 queue is empty; keep it empty while addressing remaining P2/P3 cleanup."
        )
    if by_rule:
        lines.append(f"3. Run a rule-focused cleanup sprint for top rules ({rule_hint}).")
    else:
        lines.append("3. No open rule buckets remain; keep `--sonar-js` green in routine checks.")
    lines.append(
        "4. After each merge batch, run `./core/entrypoints/cli/scripts/lint.sh --sonar-issues` and `./core/entrypoints/cli/scripts/lint.sh --sonar-backlog`."
    )
    lines.append(
        "5. Before release, run `./core/entrypoints/cli/scripts/lint.sh --sonar-full` and keep CI strict on new P0/P1 plus critical/blocker growth."
    )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def generate_sonar_backlog(
    *,
    input_csv: str,
    input_dir: str,
    output: str,
    top_rules: int,
    top_files: int,
    max_p0: int,
    max_p1: int,
) -> int:
    """Generate a Sonar backlog from a typed operation boundary."""
    input_csv_arg = str(input_csv or "").strip()
    input_dir_path = Path(str(input_dir)).resolve()
    output_path = Path(str(output)).resolve()

    csv_path = Path(input_csv_arg).resolve() if input_csv_arg else find_latest_csv(input_dir_path)
    if not csv_path.exists():
        raise SystemExit(f"[sonar-backlog] Input CSV not found: {csv_path}")

    issues = read_issues(csv_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_markdown(
        csv_path=csv_path,
        output_path=output_path,
        issues=issues,
        top_rules_limit=max(1, int(top_rules)),
        top_files_limit=max(1, int(top_files)),
        max_p0=max(1, int(max_p0)),
        max_p1=max(1, int(max_p1)),
    )
    output_path.write_text(content, encoding="utf-8")

    print(f"[sonar-backlog] Source CSV: {csv_path}")
    print(f"[sonar-backlog] Issues parsed: {len(issues)}")
    print(f"[sonar-backlog] Backlog written: {output_path}")
    return 0


def main() -> int:
    args = parse_args()
    return generate_sonar_backlog(
        input_csv=str(args.input_csv or ""),
        input_dir=str(args.input_dir),
        output=str(args.output),
        top_rules=int(args.top_rules),
        top_files=int(args.top_files),
        max_p0=int(args.max_p0),
        max_p1=int(args.max_p1),
    )


if __name__ == "__main__":
    raise SystemExit(main())
