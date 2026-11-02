"""Agent file reads require a named binding and a trusted execution owner."""

import hashlib

import pytest

from core.application.orchestration.agent_file_runtime import (
    AgentFileAccessError,
    AgentFileRuntime,
)


class ArtifactPlane:
    def __init__(self, body=b"", *, owner=None, content_type="text/markdown", expires_at=None):
        self.body = body
        self.meta = {
            "artifact_id": "art_one",
            "object_key": "artifacts/art_one/input.md",
            "persistence_status": "available",
            "filename": "input.md",
            "content_type": content_type,
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "owner_scope": owner or {"account_id": "a", "project_id": "p", "job_id": "j"},
            "expires_at": expires_at,
        }

    def get_artifact(self, artifact_id):
        return self.meta if artifact_id == "art_one" else None

    def read_artifact_bytes(self, artifact_id, *, max_size_bytes):
        assert artifact_id == "art_one"
        if len(self.body) > max_size_bytes:
            raise ValueError("too large")
        return self.body


def context(plane, *, source="state.outputs.document", state=None):
    runtime = AgentFileRuntime(plane=plane, account_id="a", project_id="p", job_id="j")
    return runtime.bind_files(
        [{"name": "document", "source": source}],
        state or {"outputs": {"document": "artifact:art_one"}},
    )


def test_bound_markdown_read_is_paginated_without_silent_truncation():
    content = "A" * 5000 + "λ end"
    files = context(ArtifactPlane(content.encode()))
    assert files.list_files() == [
        {
            "name": "document",
            "filename": "input.md",
            "content_type": "text/markdown",
            "size_bytes": len(content.encode()),
            "text_readable": True,
        }
    ]
    first = files.read_text("document", 0, 900)
    second = files.read_text("document", first["next_offset"], 8192)
    assert first == {"text": content[:900], "next_offset": 900, "eof": False}
    assert second == {"text": content[900:], "next_offset": len(content), "eof": True}


@pytest.mark.parametrize("field", ["account_id", "project_id", "job_id"])
def test_binding_rejects_foreign_owner(field):
    owner = {"account_id": "a", "project_id": "p", "job_id": "j"}
    owner[field] = "foreign"
    with pytest.raises(AgentFileAccessError):
        context(ArtifactPlane(b"hello", owner=owner))


def test_literal_or_unknown_file_cannot_authorize_read():
    with pytest.raises(AgentFileAccessError):
        context(ArtifactPlane(b"hello"), source="literal:artifact:art_one")
    files = context(ArtifactPlane(b"hello"))
    with pytest.raises(AgentFileAccessError):
        files.read_text("art_one", 0, 100)


def test_read_refuses_changed_body_and_invalid_utf8():
    plane = ArtifactPlane(b"hello")
    files = context(plane)
    plane.body = b"other"
    with pytest.raises(AgentFileAccessError):
        files.read_text("document", 0, 100)
    with pytest.raises(AgentFileAccessError):
        context(ArtifactPlane(b"\xff")).read_text("document", 0, 100)


def test_read_refuses_expired_and_excessive_requests():
    with pytest.raises(AgentFileAccessError):
        context(ArtifactPlane(b"hello", expires_at=1))
    files = context(ArtifactPlane(b"hello"))
    with pytest.raises(AgentFileAccessError):
        files.read_text("document", -1, 100)
    with pytest.raises(AgentFileAccessError):
        files.read_text("document", 0, 100000000)


def test_binding_rejects_duplicate_names_and_missing_trusted_scope():
    plane = ArtifactPlane(b"hello")
    runtime = AgentFileRuntime(plane=plane, account_id="a", project_id="p", job_id="j")
    binding = {"name": "document", "source": "state.outputs.document"}
    with pytest.raises(AgentFileAccessError):
        runtime.bind_files([binding, binding], {"outputs": {"document": "artifact:art_one"}})
    with pytest.raises(AgentFileAccessError):
        AgentFileRuntime(plane=plane, account_id="", project_id="p", job_id="j").bind_files(
            [binding], {"outputs": {"document": "artifact:art_one"}}
        )


def test_media_read_requires_explicit_bound_and_text_reader_rejects_binary_mime():
    files = context(ArtifactPlane(b"binary", content_type="image/png"))
    with pytest.raises(AgentFileAccessError):
        files.read_text("document", 0, 10)
    assert files.read_bytes("document", max_size_bytes=6) == b"binary"
    with pytest.raises(AgentFileAccessError):
        files.read_bytes("document", max_size_bytes=5)


def test_worker_scope_uses_durable_job_and_project_and_resets_nested_runtime():
    from core.application.orchestration.agent_file_runtime import (
        bind_agent_file_runtime,
        current_agent_file_runtime,
        runtime_from_durable_job,
    )

    class Store:
        def get_project(self, project_id):
            assert project_id == "p"
            return {"id": "p", "account_id": "a"}

    plane = ArtifactPlane(b"hello")
    runtime = runtime_from_durable_job(
        store=Store(), job={"id": "j", "project_id": "p"}, job_id="j", plane_provider=lambda: plane
    )
    assert runtime is not None
    assert (
        runtime_from_durable_job(
            store=Store(),
            job={"id": "other", "project_id": "p"},
            job_id="j",
            plane_provider=lambda: plane,
        )
        is None
    )
    assert (
        runtime_from_durable_job(
            store=Store(),
            job={"id": "j", "project_id": "p", "account_id": "other"},
            job_id="j",
            plane_provider=lambda: plane,
        )
        is None
    )
    with bind_agent_file_runtime(runtime):
        assert current_agent_file_runtime() is runtime
        with bind_agent_file_runtime(None):
            assert current_agent_file_runtime() is None
        assert current_agent_file_runtime() is runtime
    assert current_agent_file_runtime() is None


def test_authoring_rejects_literal_selector_and_discovers_valid_file_source():
    from core.application.orchestration.workflow_state_selector_contract import (
        discover_step_state_selectors,
        validate_step_state_selectors,
        validate_workflow_state_selectors,
    )

    step = {
        "step_id": "read",
        "step_kind": "agent",
        "override": {
            "agent": {"file_bindings": [{"name": "document", "source": "literal:artifact:art_one"}]}
        },
    }
    assert validate_step_state_selectors(step)
    assert validate_workflow_state_selectors({"steps": [step]})
    step["override"]["agent"]["file_bindings"][0]["source"] = "state.outputs.document"
    assert not validate_step_state_selectors(step)
    assert discover_step_state_selectors(step)[0].selector == "state.outputs.document"


def test_metadata_must_be_complete_and_match_handle():
    plane = ArtifactPlane(b"hello")
    plane.meta["persistence_status"] = "preparing"
    with pytest.raises(AgentFileAccessError):
        context(plane)
    plane.meta["persistence_status"] = "available"
    plane.meta["artifact_id"] = "art_other"
    with pytest.raises(AgentFileAccessError):
        context(plane)


def test_file_count_and_total_bytes_are_bounded(monkeypatch):
    import core.application.orchestration.agent_file_runtime as runtime_module

    plane = ArtifactPlane(b"hello")
    runtime = AgentFileRuntime(plane=plane, account_id="a", project_id="p", job_id="j")
    monkeypatch.setattr(runtime_module, "_MAX_TOTAL_FILE_BYTES", 4)
    with pytest.raises(AgentFileAccessError):
        runtime.bind_files(
            [{"name": "document", "source": "state.file"}], {"file": "artifact:art_one"}
        )
    with pytest.raises(AgentFileAccessError):
        runtime.bind_files(
            [{"name": f"f{i}", "source": "state.file"} for i in range(11)],
            {"file": "artifact:art_one"},
        )


def test_text_budget_is_enforced(monkeypatch):
    import core.application.orchestration.agent_file_runtime as runtime_module

    files = context(ArtifactPlane(b"hello"))
    monkeypatch.setattr(runtime_module, "_MAX_TOTAL_TEXT_CHARS", 6)
    assert files.read_text("document", 0, 4)["text"] == "hell"
    with pytest.raises(AgentFileAccessError):
        files.read_text("document", 0, 4)


def test_adopted_webhook_attachment_uses_verified_project_account():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.agent_file_runtime import runtime_from_durable_job

    plane = ArtifactDatasetPlane()
    handle, _ = plane.create_artifact(
        owner_scope={"entry_path": "webhook", "entry_key": "hook"},
        run_id="webhook",
        step_id="intake",
        filename="notes.md",
        content_type="text/markdown",
        body=b"adopted attachment",
    )
    plane.bind_replay_owner(artifact_id=handle.artifact_id, project_id="p", job_id="j")

    class DurableStore:
        def get_project(self, project_id):
            assert project_id == "p"
            return {"id": "p", "account_id": "a"}

    runtime = runtime_from_durable_job(
        store=DurableStore(),
        job={"id": "j", "project_id": "p"},
        job_id="j",
        plane_provider=lambda: plane,
    )
    assert runtime is not None
    files = runtime.bind_files(
        [{"name": "notes", "source": "state.inputs.notes"}],
        {"inputs": {"notes": f"artifact:{handle.artifact_id}"}},
    )
    assert files.read_text("notes", 0, 100)["text"] == "adopted attachment"


def test_explicit_foreign_account_remains_denied_for_same_job():
    plane = ArtifactPlane(
        b"hello", owner={"account_id": "foreign", "project_id": "p", "job_id": "j"}
    )
    with pytest.raises(AgentFileAccessError):
        context(plane)


def test_invocation_runtime_reaches_langgraph_thread_and_forked_agent():
    import multiprocessing
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from core.application.orchestration.agent_file_runtime import (
        bind_agent_file_runtime,
        current_agent_file_runtime,
    )
    from core.application.orchestration.agents.base import AgentInput, AgentOutput
    from core.application.orchestration.graph.nodes import _execute_agent_with_timeout

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("worker uses fork on supported Linux runtime")
    plane = ArtifactPlane(b"hello")
    runtime = AgentFileRuntime(plane=plane, account_id="a", project_id="p", job_id="j")

    class State(TypedDict):
        read: str

    class Agent:
        def execute(self, agent_input):
            text = agent_input.file_context.read_text("document", 0, 10)["text"]
            return AgentOutput(status="pass", artifacts={}, summary=text)

    def read_node(state):
        bound = current_agent_file_runtime()
        assert bound is not None
        files = bound.bind_files(
            [{"name": "document", "source": "state.file"}], {"file": "artifact:art_one"}
        )
        result = _execute_agent_with_timeout(
            Agent(),
            AgentInput(task_context={}, step_context={}, agent_config={}, file_context=files),
            timeout_seconds=5,
            agent_id="reader",
            state={},
        )
        return {"read": result.summary}

    builder = StateGraph(State)
    builder.add_node("reader", read_node)
    builder.add_edge(START, "reader")
    builder.add_edge("reader", END)
    graph = builder.compile()
    with bind_agent_file_runtime(runtime):
        result = graph.invoke({"read": ""})
    assert result["read"] == "hello"
    assert current_agent_file_runtime() is None
