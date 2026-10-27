"""Cost breakdown projection helpers for report UI payloads."""

from __future__ import annotations

from typing import Any

from .ui_projection_common import (
    _format_cost_knownness_display,
    _format_currency,
    _format_token_count,
    _humanize_identifier,
    _normalize_rows,
)


def _build_cost_breakdown(cost_breakdown: dict[str, Any]) -> dict[str, Any]:
    rows = _normalize_rows(cost_breakdown.get("breakdown"))
    prepared: list[dict[str, Any]] = []
    total_cost = 0.0
    total_tokens = 0
    unknown_cli_tokens = 0
    for row in rows:
        row_unknown_tokens = int(row.get("unknown_cli_tokens") or 0)
        row_knownness = str(row.get("cost_knownness") or "").strip().lower()
        try:
            cost_usd = float(row.get("known_cost_usd") or row.get("cost_usd") or 0.0)
        except Exception:
            cost_usd = 0.0
        prompt_tokens = int(row.get("tokens_prompt") or 0)
        completion_tokens = int(row.get("tokens_completion") or 0)
        token_total = int(row.get("tokens_total") or 0) or (prompt_tokens + completion_tokens)
        total_cost += cost_usd
        total_tokens += token_total
        unknown_cli_tokens += row_unknown_tokens
        cost_unknown = row_knownness == "unknown" or (
            row_unknown_tokens > 0 and row.get("cost_usd") is None
        )
        prepared.append(
            {
                "agent": str(row.get("agent") or "").strip(),
                "agent_label": _humanize_identifier(row.get("agent")) or "Unknown agent",
                "model": str(row.get("model_used") or "").strip() or "Model not recorded",
                "calls": int(row.get("calls") or 0),
                "tokens": token_total,
                "tokens_display": _format_token_count(token_total),
                "cost_usd": None if cost_unknown else cost_usd,
                "known_cost_usd": cost_usd,
                "unknown_cli_tokens": row_unknown_tokens,
                "cost_knownness": "unknown" if cost_unknown else "known",
                "cost_display": "Unknown" if cost_unknown else _format_currency(cost_usd),
                "share_pct": 0,
            }
        )
    prepared.sort(key=lambda row: (row.get("known_cost_usd") or 0.0, row["tokens"]), reverse=True)
    if total_cost > 0:
        for row in prepared:
            row["share_pct"] = round(((row.get("known_cost_usd") or 0.0) / total_cost) * 100)
    if unknown_cli_tokens > 0 and total_cost > 0:
        cost_knownness = "partial"
    elif unknown_cli_tokens > 0:
        cost_knownness = "unknown"
    else:
        cost_knownness = "known"
    return {
        "grouping": "agent",
        "rows": prepared,
        "top_spenders": prepared[:3],
        "total_cost_usd": total_cost,
        "known_cost_usd": total_cost,
        "unknown_cli_tokens": unknown_cli_tokens,
        "cost_knownness": cost_knownness,
        "total_cost_display": _format_cost_knownness_display(
            total_cost, cost_knownness, unknown_cli_tokens
        ),
        "total_tokens": total_tokens,
        "total_tokens_display": _format_token_count(total_tokens),
        "has_data": bool(prepared),
    }
