"""Reusable helpers for job cost aggregation queries."""

from __future__ import annotations

from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def get_job_cost_summary(*, conn, job_id: str, logger) -> dict:
    """Return aggregated token and cost totals for a job."""
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(tokens_prompt), 0)     AS tokens_prompt,
                    COALESCE(SUM(tokens_completion), 0) AS tokens_completion,
                    COALESCE(SUM(tokens_total), 0)      AS tokens_total,
                    COALESCE(SUM(cost_usd), 0)          AS cost_usd,
                    COALESCE(SUM(
                        COALESCE(tokens_total, tokens_prompt, 0)
                        + CASE WHEN tokens_total IS NULL THEN COALESCE(tokens_completion, 0) ELSE 0 END
                    ) FILTER (
                        WHERE cost_usd IS NULL
                          AND COALESCE(tokens_total, tokens_prompt, tokens_completion, 0) > 0
                    ), 0)                               AS unknown_cli_tokens,
                    COUNT(*) FILTER (
                        WHERE cost_usd IS NOT NULL
                           OR model_used IS NOT NULL
                           OR COALESCE(tokens_total, tokens_prompt, tokens_completion, 0) > 0
                    ) AS llm_calls,
                    (
                        SELECT ranked.agent_key
                        FROM (
                            SELECT
                                NULLIF(BTRIM(agent), '') AS agent_key,
                                COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS agent_cost,
                                MAX(timestamp) AS last_seen
                            FROM platform_action_log
                            WHERE job_id = %s
                            GROUP BY NULLIF(BTRIM(agent), '')
                        ) AS ranked
                        WHERE ranked.agent_key IS NOT NULL
                        ORDER BY ranked.agent_cost DESC, ranked.last_seen DESC
                        LIMIT 1
                    ) AS dominant_agent
                FROM platform_action_log
                WHERE job_id = %s
                """,
                (job_id, job_id),
            )
            return dict(cur.fetchone() or {})
    except Exception as exc:
        logger.debug("get_job_cost_summary failed (non-fatal): %s", exc)
        return {}


def get_jobs_cost_summary(*, conn, job_ids: list[str], logger) -> dict[str, dict]:
    """Return aggregated token and cost totals for many jobs in one query."""
    try:
        normalized_ids: list[str] = []
        for raw in job_ids or []:
            job_id = str(raw or "").strip()
            if not job_id:
                continue
            normalized_ids.append(job_id)
        normalized_ids = list(dict.fromkeys(normalized_ids))
        if not normalized_ids:
            return {}

        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH totals AS (
                    SELECT
                        job_id,
                        COALESCE(SUM(tokens_prompt), 0)     AS tokens_prompt,
                        COALESCE(SUM(tokens_completion), 0) AS tokens_completion,
                        COALESCE(SUM(tokens_total), 0)      AS tokens_total,
                        COALESCE(SUM(cost_usd), 0)          AS cost_usd,
                        COALESCE(SUM(
                            COALESCE(tokens_total, tokens_prompt, 0)
                            + CASE WHEN tokens_total IS NULL THEN COALESCE(tokens_completion, 0) ELSE 0 END
                        ) FILTER (
                            WHERE cost_usd IS NULL
                              AND COALESCE(tokens_total, tokens_prompt, tokens_completion, 0) > 0
                        ), 0)                               AS unknown_cli_tokens,
                        COUNT(*) FILTER (
                            WHERE cost_usd IS NOT NULL
                               OR model_used IS NOT NULL
                               OR COALESCE(tokens_total, tokens_prompt, tokens_completion, 0) > 0
                        ) AS llm_calls
                    FROM platform_action_log
                    WHERE job_id = ANY(%s)
                    GROUP BY job_id
                ),
                ranked_agents AS (
                    SELECT
                        ranked.job_id,
                        ranked.agent_key AS dominant_agent,
                        ROW_NUMBER() OVER (
                            PARTITION BY ranked.job_id
                            ORDER BY ranked.agent_cost DESC, ranked.last_seen DESC
                        ) AS rn
                    FROM (
                        SELECT
                            job_id,
                            NULLIF(BTRIM(agent), '') AS agent_key,
                            COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS agent_cost,
                            MAX(timestamp) AS last_seen
                        FROM platform_action_log
                        WHERE job_id = ANY(%s)
                        GROUP BY job_id, NULLIF(BTRIM(agent), '')
                    ) AS ranked
                    WHERE ranked.agent_key IS NOT NULL
                )
                SELECT
                    t.job_id,
                    t.tokens_prompt,
                    t.tokens_completion,
                    t.tokens_total,
                    t.cost_usd,
                    t.unknown_cli_tokens,
                    t.llm_calls,
                    ra.dominant_agent
                FROM totals t
                LEFT JOIN ranked_agents ra
                    ON ra.job_id = t.job_id
                   AND ra.rn = 1
                """,
                (normalized_ids, normalized_ids),
            )
            rows = [dict(row) for row in cur.fetchall()]

        by_job: dict[str, dict] = {}
        for row in rows:
            job_id = str(row.get("job_id") or "").strip()
            if not job_id:
                continue
            by_job[job_id] = row

        zero = {
            "tokens_prompt": 0,
            "tokens_completion": 0,
            "tokens_total": 0,
            "cost_usd": 0.0,
            "unknown_cli_tokens": 0,
            "llm_calls": 0,
            "dominant_agent": None,
        }
        for job_id in normalized_ids:
            by_job.setdefault(job_id, dict(zero))
        return by_job
    except Exception as exc:
        logger.debug("get_jobs_cost_summary failed (non-fatal): %s", exc)
        return {}
