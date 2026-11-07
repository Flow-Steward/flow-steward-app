"""Token usage breakdown projection helpers for report UI payloads."""

from __future__ import annotations

from typing import Any

from .ui_projection_common import _format_token_count, _humanize_identifier, _normalize_rows


def _usage_row_total(row: dict[str, Any]) -> int:
    total = row.get("tokens_total")
    if total is not None:
        return int(total)
    prompt, completion = row.get("tokens_prompt"), row.get("tokens_completion")
    if prompt is not None and completion is not None:
        return int(prompt) + int(completion)
    return 0


def _build_usage_breakdown(usage_breakdown: dict[str, Any]) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    for row in _normalize_rows(usage_breakdown.get("breakdown")):
        tokens = _usage_row_total(row)
        agent = str(row.get("agent") or "unknown")
        prepared.append(
            {
                "agent": agent,
                "agent_label": _humanize_identifier(agent) or agent,
                "model": str(row.get("model_used") or "Unspecified"),
                "model_used": row.get("model_used"),
                "calls": int(row.get("calls") or 0),
                "tokens": tokens,
                "tokens_display": _format_token_count(tokens),
            }
        )
    prepared.sort(key=lambda row: row["tokens"], reverse=True)
    total_tokens = sum(row["tokens"] for row in prepared)
    for row in prepared:
        row["share_pct"] = round(row["tokens"] / total_tokens * 100) if total_tokens else 0
    return {
        "grouping": "agent",
        "rows": prepared,
        "total_tokens": total_tokens,
        "total_tokens_display": _format_token_count(total_tokens),
        "has_data": bool(prepared),
    }
