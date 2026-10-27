"""Activity pages keep the sentinel out of either cursor direction."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceForbiddenError
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext


def _service(rows, *, status="in_progress"):
    store = MagicMock()
    store.get_job.return_value = {"id": "job", "project_id": "project", "status": status}
    store.get_project.return_value = {"id": "project", "account_id": DEFAULT_ACCOUNT_ID}
    store.get_logs.return_value = rows
    store.get_agent_messages.return_value = rows
    return JobQueryService(
        get_logger=lambda: store,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "running",
        build_runtime_payload_for_api=AsyncMock(return_value={}),
        serialize_log_row=lambda row, job_id: row,
        serialize_agent_message_row=lambda row: row,
    ), store


@pytest.mark.parametrize("kind", ["logs", "messages"])
@pytest.mark.parametrize("limit", [1, 2])
def test_forward_page_retains_first_rows_and_excludes_sentinel(kind, limit):
    ids = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    service, store = _service([{"id": value} for value in ids[: limit + 1]])
    payload = getattr(service, f"list_{kind}")(
        "job",
        since_id="previous",
        limit=limit,
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
    ).dump()
    assert [row["id"] for row in payload[kind]] == ids[:limit]
    assert payload["last_id"] == ids[limit - 1]
    assert payload["has_more"] is True
    if kind == "logs":
        assert payload["next_before_id"] == ""
    read = store.get_logs if kind == "logs" else store.get_agent_messages
    assert read.call_args.kwargs["since_id"] == "previous"
    assert read.call_args.kwargs["newest"] is False


@pytest.mark.parametrize("cursor", [{}, {"newest": True}, {"before_id": "later"}])
def test_terminal_backward_page_exposes_oldest_returned_cursor(cursor):
    service, _ = _service(
        [{"id": "sentinel"}, {"id": "older"}, {"id": "newer"}], status="completed"
    )
    payload = service.list_logs("job", limit=2, **cursor).dump()
    assert [row["id"] for row in payload["logs"]] == ["older", "newer"]
    assert payload["last_id"] == "newer"
    assert payload["next_before_id"] == "older"
    assert payload["has_more"] is True


def test_terminal_explicit_since_remains_forward():
    service, store = _service(
        [{"id": "first"}, {"id": "second"}, {"id": "sentinel"}], status="completed"
    )
    payload = service.list_logs("job", since_id="previous", limit=2).dump()
    assert [row["id"] for row in payload["logs"]] == ["first", "second"]
    assert payload["next_before_id"] == ""
    assert store.get_logs.call_args.kwargs["newest"] is False


@pytest.mark.parametrize("rows", [[], [{"id": "only"}]])
def test_backward_end_does_not_offer_another_page(rows):
    service, _ = _service(rows, status="completed")
    payload = service.list_logs("job", limit=1).dump()
    assert payload["has_more"] is False
    assert payload["next_before_id"] == ""


@pytest.mark.parametrize("kind", ["logs", "messages"])
def test_foreign_account_is_denied_before_reading_activity(kind):
    service, store = _service([])
    list_activity = getattr(service, f"list_{kind}")
    foreign_scope = ScopeContext(account_id="foreign")
    with pytest.raises(ServiceForbiddenError):
        list_activity("job", scope=foreign_scope)
    store.get_logs.assert_not_called()
    store.get_agent_messages.assert_not_called()


def test_messages_forward_continuation_does_not_skip_or_repeat():
    service, store = _service([])
    store.get_agent_messages.side_effect = [
        [{"id": "first"}, {"id": "second"}, {"id": "third"}],
        [{"id": "third"}],
    ]
    first = service.list_messages("job", limit=2).dump()
    second = service.list_messages("job", since_id=first["last_id"], limit=2).dump()
    assert [row["id"] for row in first["messages"] + second["messages"]] == [
        "first",
        "second",
        "third",
    ]
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert store.get_agent_messages.call_args.kwargs["since_id"] == "second"
