"""Tool-round classification constants for agent runtime helpers."""

from __future__ import annotations

# ── Token budget constants ────────────────────────────────────────────────────
# Each tool result is truncated to this many characters before being appended
# to the message history. Prevents runaway growth when a read tool returns a
# large payload that would otherwise be re-sent on every
# subsequent round.
# 6 000 chars ≈ 1 500 tokens — enough for meaningful context.
_MAX_TOOL_RESULT_CHARS = 6_000

# ── Research vs action tool classification ────────────────────────────────────
# Research-only rounds (all tools in the round are in this set) do NOT count
# toward max_tool_rounds — only action rounds do.  This lets agents research
# as much as needed while bounding the number of write/execute operations.
# Safety: total rounds (research + action) are hard-capped at 3x max_tool_rounds.
_RESEARCH_TOOLS: frozenset[str] = frozenset(
    {
        # Platform reads
        "project_list",
        "project_jobs",
    }
)


# After this many ReAct rounds the oldest round (AI response + its tool results)
# is dropped from history.  Keeps the sliding window at ≤ this many rounds so
# prompt tokens stay bounded regardless of how many tools were called.
_MAX_HISTORY_ROUNDS = 6
