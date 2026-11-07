"""Token-only usage queries against fresh and legacy-shaped audit tables."""

import logging

import pytest

from core.infrastructure.db.action_log_store import get_job_usage_breakdown
from core.infrastructure.db.job_usage_store import get_job_usage_summary, get_jobs_usage_summary
from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter


@pytest.mark.parametrize("legacy_money_column", [False, True])
@pytest.mark.parametrize(
    ("reported_total", "completion", "expected_writer_total"),
    [(None, 5, 15), (0, 5, 0), (None, None, 0)],
)
def test_usage_queries_preserve_cached_and_missing_total_tokens(
    pg_conn, legacy_money_column, reported_total, completion, expected_writer_total
):
    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname.startswith("flow_steward_audit")
    db = ConnectionAdapter(pg_conn)
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE platform_action_log ("
                "job_id text, agent text, action_type text, model_used text, "
                "timestamp timestamptz DEFAULT now(), tokens_prompt integer, "
                "tokens_completion integer, tokens_total integer)"
            )
            if legacy_money_column:
                cur.execute("ALTER TABLE platform_action_log ADD COLUMN cost_usd numeric")
            cur.execute(
                "INSERT INTO platform_action_log "
                "(job_id, agent, action_type, model_used, tokens_prompt, tokens_completion, tokens_total) "
                "VALUES ('job', 'writer', 'llm_call', 'custom-model', 10, %s, %s), "
                "('job', 'reviewer', 'llm_call', 'cached-model', 20, 5, 31), "
                "('job', 'writer', 'phase_complete', NULL, NULL, NULL, NULL)",
                (completion, reported_total),
            )
        logger = logging.getLogger(__name__)
        summary = get_job_usage_summary(conn=db, job_id="job", logger=logger)
        assert summary == {
            "tokens_prompt": 30,
            "tokens_completion": 5 + (completion or 0),
            "tokens_total": 31 + expected_writer_total,
            "llm_calls": 2,
            "dominant_agent": "reviewer",
        }
        bulk = get_jobs_usage_summary(conn=db, job_ids=["job"], logger=logger)
        assert bulk["job"] == {"job_id": "job", **summary}
        rows = get_job_usage_breakdown(conn=db, job_id="job", logger=logger)
        assert [(row["agent"], row["tokens_total"], row["calls"]) for row in rows] == [
            ("reviewer", 31, 1),
            ("writer", expected_writer_total, 1),
        ]
        assert all("cost_usd" not in row for row in rows)
    finally:
        pg_conn.rollback()
