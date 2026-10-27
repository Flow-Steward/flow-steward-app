"""Narrow action-audit persistence capability for responsibility-owned stores."""

from __future__ import annotations

from typing import Any

from core.infrastructure.db import action_log_store


class PlatformLoggerActionAuditMixin:
    conn: Any

    def log_action(
        self,
        project_id,
        task_id,
        task_summary,
        agent,
        action_type,
        status=None,
        iteration_no=None,
        cycle_no=None,
        duration=None,
        payload=None,
        result=None,
        error=None,
        meta=None,
        lifecycle_id=None,
        job_id=None,
        tokens_prompt=None,
        tokens_completion=None,
        tokens_total=None,
        cost_usd=None,
        model_used=None,
    ):
        return action_log_store.log_action(
            conn=self.conn,
            project_id=project_id,
            job_id=job_id,
            task_id=task_id,
            task_summary=task_summary,
            lifecycle_id=lifecycle_id,
            agent=agent,
            action_type=action_type,
            status=status,
            iteration_no=iteration_no,
            cycle_no=cycle_no,
            duration=duration,
            payload=payload,
            result=result,
            error=error,
            meta=meta,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_total=tokens_total,
            cost_usd=cost_usd,
            model_used=model_used,
        )


__all__ = ["PlatformLoggerActionAuditMixin"]
