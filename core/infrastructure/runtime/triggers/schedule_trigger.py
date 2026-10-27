"""ScheduleTrigger - creates TaskEnvelopes for scheduled workflow runs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from core.application.orchestration.task_envelope import TaskEnvelope

logger = logging.getLogger(__name__)


class ScheduleTrigger:
    """Creates generic TaskEnvelopes on a schedule."""

    def normalize(
        self,
        schedule_config: dict,
        project_id: str = "",
    ) -> TaskEnvelope:
        """Convert a schedule configuration to a TaskEnvelope."""
        title = schedule_config.get("title", "Scheduled workflow task")
        description = schedule_config.get("description", title)
        priority = schedule_config.get("priority", "low")

        now_dt = datetime.now(UTC)
        now = now_dt.strftime("%Y-%m-%d %H:%M UTC")

        return TaskEnvelope(
            entry_path="schedule",
            source_system="schedule",
            source_event_id=f"schedule-{now_dt.strftime('%Y%m%d-%H%M')}",
            source_object_id=f"schedule-{now_dt.strftime('%Y%m%d-%H%M')}",
            title=f"[SCHEDULED] {title}",
            description=f"{description}\n\n*Scheduled run at {now}*",
            priority=priority,
            project_id=project_id,
            requested_workflow_id=str(schedule_config.get("workflow_id") or "").strip(),
            inputs={
                "title": title,
                "description": description,
                "priority": priority,
            },
            files=[],
            metadata={
                "scheduled_at": now,
                "schedule_config": schedule_config,
            },
            entry_context={"schedule_config": schedule_config},
        )
