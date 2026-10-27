"""A worker accepts task messages and workflow resumes, nothing else (§8.5)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.infrastructure.runtime.worker import worker_task_execution as task_exec


@pytest.fixture
def services(monkeypatch):
    monkeypatch.setattr(task_exec, "_runtime_config", dict)
    monkeypatch.setattr(task_exec, "_build_platform_logger", lambda _config: object())
    monkeypatch.setattr(task_exec, "_notification_store_provider", lambda _config: None)
    task = MagicMock()
    task.execute.return_value = {"action": "ack", "reason": "task"}
    resume = MagicMock()
    resume.execute.return_value = {"action": "ack", "reason": "resume"}
    with (
        patch.object(task_exec, "_task_service", return_value=task),
        patch.object(task_exec, "_workflow_resume_service", return_value=resume),
    ):
        yield task, resume


def test_a_workflow_resume_goes_to_the_resume_service(services) -> None:
    task, _resume = services
    result = task_exec.execute_task({"message_type": "workflow_resume", "job_id": "j"})
    assert result["reason"] == "resume"
    task.execute.assert_not_called()


def test_a_task_message_has_no_type_and_goes_to_the_task_service(services) -> None:
    _task, resume = services
    result = task_exec.execute_task({"job_id": "j", "task_id": "t"})
    assert result["reason"] == "task"
    resume.execute.assert_not_called()


@pytest.mark.parametrize("message_type", ["approval_resume", "something_else"])
def test_any_other_message_type_is_dead_lettered_without_running(services, message_type) -> None:
    task, resume = services

    result = task_exec.execute_task({"message_type": message_type, "job_id": "j"})

    assert result == {"action": "dead_letter", "reason": "unsupported_message_type"}
    task.execute.assert_not_called()
    resume.execute.assert_not_called()
