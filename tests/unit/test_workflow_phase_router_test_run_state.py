from __future__ import annotations

from types import SimpleNamespace


def test_router_combines_checkpoint_result_with_durable_job_status(monkeypatch):
    from core.entrypoints.admin import workflows_definition_router as router

    calls: dict[str, object] = {}

    def project(thread_id: str, *, durable_state_reader):
        calls["thread_id"] = thread_id
        calls["durable"] = durable_state_reader(thread_id)
        return {
            "status": "completed",
            "result": {
                "http_request": {
                    "request": {
                        "method": "POST",
                        "url": "https://example.test/orders",
                    },
                    "status_code": 201,
                }
            },
        }

    monkeypatch.setattr(router, "get_workflow_test_run_state", project)
    monkeypatch.setattr(
        router,
        "_workflow_test_run_query_service",
        SimpleNamespace(
            get_public_state=lambda thread_id: {
                "status": "completed",
                "thread_id": thread_id,
            }
        ),
    )

    payload = router._durable_test_run_state("thread-http-1")

    assert calls == {
        "thread_id": "thread-http-1",
        "durable": {"status": "completed", "thread_id": "thread-http-1"},
    }
    assert payload["result"]["http_request"]["request"]["method"] == "POST"
