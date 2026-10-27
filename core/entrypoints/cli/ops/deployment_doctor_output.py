from __future__ import annotations

from typing import Any


def render_human(payload: dict[str, Any]) -> str:
    lines = [
        "Flow Steward Deployment Doctor",
        f"Repository: {payload.get('repo_root')}",
        f"Environment: {payload.get('env_file')}",
        "",
    ]
    for check in payload.get("checks", []):
        status = str(check.get("status") or "unknown").upper()
        lines.append(f"[{status}] {check.get('category')}.{check.get('id')} - {check.get('title')}")
        detail = str(check.get("detail") or "").strip()
        remediation = str(check.get("remediation") or "").strip()
        if detail:
            lines.append(f"  {detail}")
        if remediation:
            lines.append(f"  fix: {remediation}")
    summary_obj = payload.get("summary")
    summary: dict[str, Any] = summary_obj if isinstance(summary_obj, dict) else {}
    lines.extend(
        [
            "",
            (
                "Summary: "
                f"{int(summary.get('pass') or 0)} passed, "
                f"{int(summary.get('warn') or 0)} warned, "
                f"{int(summary.get('fail') or 0)} failed."
            ),
        ]
    )
    return "\n".join(lines)
