from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.application.reports.job_result_bundle import build_job_result_bundle


def _view(**payload):
    return SimpleNamespace(dump=lambda: payload)


class _Jobs:
    async def get_job_status(self, job_id, *, scope, include_workflow_result=False):
        return _view(id=job_id, status="completed", runtime={"last_response_excerpt": "done"})

    def list_messages(self, job_id, *, since_id, before_id, limit, scope):
        return _view(messages=[{"summary": "message", "artifacts": {"files": [{"id": "a"}]}}])

    def list_logs(self, job_id, *, since_id, before_id, newest, limit, scope):
        return _view(logs=[])

    def list_events(self, job_id, *, since_id, limit, scope):
        return _view(events=[])


def test_job_result_bundle_preserves_agent_handoff_contract() -> None:
    result = asyncio.run(
        build_job_result_bundle(
            job_query_service=_Jobs(),
            job_id="job-1",
            scope=object(),
            include_messages_limit=10,
            include_logs_limit=5,
        )
    )

    assert result["job"] == {
        "id": "job-1",
        "status": "completed",
        "current_step_id": None,
        "current_group_id": None,
        "current_agent": None,
    }
    assert result["completed"] is True
    assert result["result_summary"] == "done"
    assert result["artifacts"] == [{"id": "a"}]
    assert "job-1" in result["handoff_prompt"]
