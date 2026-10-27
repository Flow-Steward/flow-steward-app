"""An open job stream ends when its session or its project access does (CWE-613).

The job WebSocket checked the session and the project membership once, before
accepting, and then streamed logs, events and messages until the job finished.
A stream opened before a logout, a revoked or expired session, a disabled user
or a removed membership kept receiving new data.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.entrypoints.ws import jobs as ws_jobs


class _Store:
    def __init__(self, polls_until_done: int = 1_000) -> None:
        self.log_reads = 0
        self.job_reads = 0
        self.polls_until_done = polls_until_done

    def get_job(self, job_id: str) -> dict[str, Any]:
        self.job_reads += 1
        status = "completed" if self.job_reads > self.polls_until_done else "running"
        return {"id": job_id, "project_id": "project-1", "status": status}

    def get_project(self, project_id: str) -> dict[str, Any]:
        return {"id": project_id, "account_id": "acc_default"}

    def get_logs(self, job_id: str, since_id: str = "") -> list:
        self.log_reads += 1
        return []

    def get_events(self, job_id: str, since_id: str = "") -> list:
        return []

    def get_agent_messages(self, job_id: str, since_id: str = "") -> list:
        return []

    def get_job_cost_summary(self, job_id: str) -> dict:
        return {}


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: list[Any],
    access: list[bool],
    store: _Store,
) -> TestClient:
    """A stream whose session lookups and access checks answer in turn."""

    async def _authenticate(_websocket: Any) -> dict[str, Any]:
        answer = sessions.pop(0) if len(sessions) > 1 else sessions[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _denied(_bl: Any, _principal: Any, _job: dict[str, Any]) -> bool:
        return not (access.pop(0) if len(access) > 1 else access[0])

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ws_jobs, "REVALIDATE_SECONDS", 0.0)
    monkeypatch.setattr(ws_jobs, "_project_access_denied", _denied)
    monkeypatch.setattr(ws_jobs.asyncio, "sleep", _no_wait)
    app = FastAPI()
    app.include_router(
        ws_jobs.create_jobs_websocket_router(
            ws_authenticate=_authenticate,
            get_logger_only=lambda: store,
            serialize_log_row=lambda row, _job_id: row,
            serialize_event_row=lambda row: row,
            serialize_agent_message_row=lambda row: row,
            ws_terminal={"completed", "failed"},
            ws_get_approval_payload=lambda _job: None,
            approval_fallback_payload=lambda _job: {},
            build_runtime_payload=lambda _job, _bl: {},
            logger=type("L", (), {"warning": staticmethod(lambda *a, **k: None)})(),
        )
    )
    return TestClient(app)


def _close_code(client: TestClient) -> int:
    with (
        client.websocket_connect("/ws/jobs/job-1") as websocket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        while True:
            websocket.receive_json()
    return closed.value.code


def test_a_revoked_session_closes_the_open_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    user = {"user_id": "u1", "account_id": "acc_default"}
    client = _client(
        monkeypatch,
        sessions=[user, PermissionError("Invalid or expired session")],
        access=[True],
        store=store,
    )

    assert _close_code(client) == 4401


def test_a_removed_membership_closes_the_open_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    user = {"user_id": "u1", "account_id": "acc_default"}
    client = _client(monkeypatch, sessions=[user], access=[True, False], store=store)

    assert _close_code(client) == 4403


def test_a_live_session_keeps_its_stream_to_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(polls_until_done=4)
    user = {"user_id": "u1", "account_id": "acc_default"}
    client = _client(monkeypatch, sessions=[user], access=[True], store=store)

    frames = []
    with client.websocket_connect("/ws/jobs/job-1") as websocket:
        while not frames or frames[-1]["type"] != "done":
            frames.append(websocket.receive_json())
    # Checked again on every poll here (REVALIDATE_SECONDS = 0) and still open.
    assert store.log_reads >= 3
    assert frames[-1] == {"type": "done", "data": {"status": "completed"}}


def test_access_is_checked_before_the_stream_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    user = {"user_id": "u1", "account_id": "acc_default"}
    client = _client(monkeypatch, sessions=[user], access=[False], store=store)

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws/jobs/job-1") as websocket,
    ):
        websocket.receive_json()
    assert refused.value.code == 4403
    assert store.log_reads == 0


def test_approval_history_frame_omits_private_retry_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_entry = {
        "approval_id": "gate-42",
        "stage": "Release review",
        "question": "Retry this publication?",
        "approved": False,
        "action": "reject",
        "comment": "Retry the same publication after checking the destination.",
        "actor": "release-operator",
        "actor_type": "user",
        "timestamp": "2026-10-18T19:00:00+00:00",
        "retry_ordinal": 1,
    }
    private_context = {
        "external_effect_resolution_ref": {
            "command_id": "private-command",
            "effect_id": "private-effect",
            "fenced_version": 4,
            "approval_id": "gate-42",
        },
        "source_ref": {"step_execution_id": "private-execution"},
        "claim_version": 3,
        "pinned_definition": {"digest": "private-definition-pin"},
    }
    stored_entry = {**public_entry, **private_context}

    class HistoryStore(_Store):
        def get_job(self, job_id: str) -> dict[str, Any]:
            return {**super().get_job(job_id), "meta": {"approvals": [stored_entry]}}

    store = HistoryStore(polls_until_done=2)
    user = {"user_id": "u1", "account_id": "acc_default"}
    client = _client(monkeypatch, sessions=[user], access=[True], store=store)

    frames = []
    with client.websocket_connect("/ws/jobs/job-1") as websocket:
        for _ in range(20):
            frames.append(websocket.receive_json())
            if frames[-1]["type"] == "done":
                break

    history_frames = [frame for frame in frames if frame["type"] == "approval_history"]
    assert history_frames == [{"type": "approval_history", "data": [public_entry]}]
    assert frames[-1] == {"type": "done", "data": {"status": "completed"}}
    assert stored_entry == {**public_entry, **private_context}
