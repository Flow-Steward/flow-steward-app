"""The failure vocabulary a client reads must mean something.

RS5-012 / CLAUDE-P2-failed-step: a failed run reported code="failed" — the job
status restated as a diagnosis — while the actionable cause sat in the same
job's diagnostics. Two separate causes are pinned here.

1. No status substitution. An absent error code is left absent so the public
   projection's own fallback ladder runs (job_failed, cancelled) instead of
   being displaced by a tautology.
2. One total translation between two vocabularies. Call sites name WHERE a run
   failed; consumers read a small canonical set of WHAT KIND it was. Every
   call-site kind must land on a canonical value - `cancellation` and `queue`
   previously had no entry and were silently flattened to `unknown`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.application.jobs.terminal_persistence import _durable_category
from core.infrastructure.db.platform_logger_common import _normalize_error_category
from tests.support.source_index import parse_file

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

CANONICAL = {
    "transient",
    "contract",
    "tool",
    "validation",
    "provider",
    "human_blocked",
    "unknown",
}


def _call_site_categories() -> set[str]:
    """Every literal `category=` passed to complete_failure across the codebase."""
    found: set[str] = set()
    for path in (REPO_ROOT / "core").rglob("*.py"):
        try:
            parsed = parse_file(path)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in parsed.nodes_of(ast.Call):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "complete_failure":
                continue
            for keyword in node.keywords:
                if keyword.arg != "category":
                    continue
                found.update(_possible_values(keyword.value))
    return found


def _possible_values(node: ast.AST) -> set[str]:
    """Values the expression can actually evaluate to.

    Deliberately not ast.walk: a conditional like
    `"timeout" if status == "timed_out" else "execution"` contains
    "timed_out" in its TEST, which is never a category. Walking every
    constant collected it and demanded a mapping for a kind nothing passes.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _possible_values(node.body) | _possible_values(node.orelse)
    if isinstance(node, ast.BoolOp):
        return {value for operand in node.values for value in _possible_values(operand)}
    return set()


def test_the_call_site_vocabulary_is_discovered_not_assumed() -> None:
    categories = _call_site_categories()
    assert categories, "no complete_failure category literals found - the scan is broken"
    # The kinds that were silently flattened before this fix.
    assert {"cancellation", "queue"} <= categories


@pytest.mark.parametrize("retryable", [True, False])
def test_every_call_site_category_lands_on_a_canonical_value(retryable: bool) -> None:
    for category in sorted(_call_site_categories()):
        durable = _durable_category(category, retryable=retryable)
        assert durable in CANONICAL, (
            f"{category!r} translates to {durable!r}, which is not canonical"
        )
        assert _normalize_error_category(durable) == durable, (
            f"{category!r} -> {durable!r} does not survive normalisation unchanged, "
            "so the durable record would disagree with the translation"
        )


def test_queue_failures_split_by_retryability() -> None:
    """`queue` cannot take one mapping and must not be forced into one.

    An unreadable dispatch envelope is a contract failure raised non-retryable;
    a failed publish is infrastructure raised retryable.
    """
    assert _durable_category("queue", retryable=True) == "transient"
    assert _durable_category("queue", retryable=False) == "contract"


def test_an_operator_cancellation_is_not_reported_as_a_malfunction() -> None:
    assert _durable_category("cancellation", retryable=False) == "human_blocked"


def test_an_unknown_kind_is_not_invented_into_something_specific() -> None:
    assert _normalize_error_category(_durable_category("something_new", retryable=False)) == (
        "unknown"
    )


# Deliberately not unit-tested here: that `complete_failure` writes no error_code
# when none was given, and that a cancellation reaches the durable record as
# human_blocked. Both need the terminal write path, whose guards query the
# database; faking it convincingly enough to reach log_job_error means building a
# mock that answers several different queries, and a mock that elaborate proves
# the mock. This epic has already been burned once by exactly that shape - a
# green suite over a MagicMock while production failed open.
# Those two are proven on packaged candidates instead, before and after, with the
# measurement recorded on RS5-012.


def test_the_mcp_summary_projects_the_keys_the_failure_summary_emits() -> None:
    """A field allowlist has to match the payload it filters.

    RS5-012: MCP job_status reported a failed job with failure_summary {} while
    REST carried the cause, because the allowlist named code/message/reason/
    phase/agent - keys build_job_failure_summary never emits. The one
    overlapping key, retryable, is dropped by the projector when falsy, so a
    non-retryable failure projected to nothing at all.
    """
    from core.application.jobs.failure_summary import build_job_failure_summary
    from core.application.management.job_public_projection import _FAILURE_FIELDS

    produced = build_job_failure_summary(
        job={
            "status": "failed",
            "last_error": "Missing or invalid required inputs for skill 'draft_outline'",
        },
        runtime={},
    )

    assert produced.get("present") is True, "the fixture must be a real failure"
    selected = set(_FAILURE_FIELDS) & set(produced)
    assert selected >= {"kind", "summary", "detail"}, (
        f"the allowlist {sorted(_FAILURE_FIELDS)} does not select the reason-bearing "
        f"keys of {sorted(produced)}"
    )


def test_a_non_retryable_failure_still_projects_a_reason_to_mcp() -> None:
    from core.application.jobs.failure_summary import build_job_failure_summary
    from core.application.management.job_public_projection import (
        job_public_summary_projection,
    )

    projected = job_public_summary_projection(
        {
            "id": "job-1",
            "status": "failed",
            "failure_summary": build_job_failure_summary(
                job={"status": "failed", "last_error": "Missing required input 'story_spec'"},
                runtime={},
            ),
        }
    )

    summary = projected.get("failure_summary") or {}
    assert summary, "a failed job projected no failure information at all"
    assert any("story_spec" in str(value) for value in summary.values()), (
        f"the reason did not survive the projection: {summary}"
    )
