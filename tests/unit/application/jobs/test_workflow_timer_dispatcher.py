"""Due Wait timers become stored resume intents, then queue messages (T19, T20)."""

from __future__ import annotations

from core.application.jobs.workflow_resume_state import WorkflowResumeQueueRow
from core.application.jobs.workflow_timer_dispatcher import (
    WORKFLOW_TIMER_BATCH_LIMIT,
    dispatch_due_timers,
)


def _row(job_id: str, revision: int = 1) -> WorkflowResumeQueueRow:
    return WorkflowResumeQueueRow(
        job_id=job_id,
        project_id="project",
        task_id=f"task-{job_id}",
        resume_revision=revision,
        publish_token=f"token-{job_id}",
    )


class _Repository:
    def __init__(self, due: list[tuple[WorkflowResumeQueueRow, str]]) -> None:
        self.due = due
        self.claims: list[dict] = []
        self.published: list[tuple[str, int, str]] = []
        self.failed: list[tuple[str, int, str, str]] = []

    def claim_due_timers(self, *, now_epoch_ms: int, limit: int):
        self.claims.append({"now_epoch_ms": now_epoch_ms, "limit": limit})
        claimed, self.due = self.due[:limit], self.due[limit:]
        return claimed

    def mark_published(self, *, thread_id, resume_revision, publish_token):
        self.published.append((thread_id, resume_revision, publish_token))
        return True

    def mark_publish_failed(self, *, thread_id, resume_revision, publish_token, error):
        self.failed.append((thread_id, resume_revision, publish_token, error))
        return True


def test_each_due_timer_is_published_after_its_intent_is_claimed() -> None:
    repository = _Repository([(_row("a"), "thread-a"), (_row("b", 4), "thread-b")])
    sent: list[dict] = []

    result = dispatch_due_timers(repository, lambda **kw: sent.append(kw), now_epoch_ms=5000)

    assert result == {"claimed": 2, "published": 2, "failed": 0}
    assert repository.claims == [{"now_epoch_ms": 5000, "limit": WORKFLOW_TIMER_BATCH_LIMIT}]
    assert sent[1] == {
        "project_id": "project",
        "job_id": "b",
        "task_id": "task-b",
        "thread_id": "thread-b",
        "resume_revision": 4,
    }
    assert repository.published == [("thread-a", 1, "token-a"), ("thread-b", 4, "token-b")]


def test_a_failed_publish_leaves_the_stored_intent_for_the_republisher() -> None:
    repository = _Repository([(_row("a"), "thread-a"), (_row("b"), "thread-b")])

    def publish(**kwargs):
        if kwargs["job_id"] == "a":
            raise ConnectionError("broker down")

    result = dispatch_due_timers(repository, publish, now_epoch_ms=1)

    assert result == {"claimed": 2, "published": 1, "failed": 1}
    assert repository.failed == [("thread-a", 1, "token-a", "broker down")]
    assert repository.published == [("thread-b", 1, "token-b")]


def test_one_pass_claims_at_most_the_batch_limit() -> None:
    repository = _Repository([(_row(str(i)), f"t{i}") for i in range(150)])

    first = dispatch_due_timers(repository, lambda **kw: None, now_epoch_ms=1)
    second = dispatch_due_timers(repository, lambda **kw: None, now_epoch_ms=1)

    assert (first["claimed"], second["claimed"]) == (100, 50)


def test_nothing_due_publishes_nothing() -> None:
    repository = _Repository([])
    result = dispatch_due_timers(repository, lambda **kw: None, now_epoch_ms=1)
    assert result == {"claimed": 0, "published": 0, "failed": 0}


def test_finished_children_wake_their_parents_through_the_same_publication() -> None:
    from core.application.jobs.workflow_timer_dispatcher import dispatch_finished_children

    class _ChildRepository(_Repository):
        def claim_finished_children(self, *, limit: int):
            self.claims.append({"limit": limit})
            claimed, self.due = self.due[:limit], self.due[limit:]
            return claimed

    repository = _ChildRepository([(_row("parent-1", 2), "thread-p")])
    sent: list[dict] = []

    result = dispatch_finished_children(repository, lambda **kw: sent.append(kw))

    assert result == {"claimed": 1, "published": 1, "failed": 0}
    assert sent[0]["job_id"] == "parent-1"
    assert repository.published == [("thread-p", 2, "token-parent-1")]
