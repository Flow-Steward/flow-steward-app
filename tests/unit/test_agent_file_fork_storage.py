"""The worker reopens its artifact client in the forked agent process."""

import multiprocessing
import os

import pytest

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
from core.application.orchestration.agent_file_runtime import AgentFileRuntime
from core.application.orchestration.agents.base import AgentInput, AgentOutput
from core.application.orchestration.graph.nodes import _execute_agent_with_timeout
from core.infrastructure.runtime import artifact_storage_factory
from core.infrastructure.runtime.worker import worker_execution_adapters as worker


def test_worker_artifact_provider_reopens_process_bound_storage_after_agent_fork(
    tmp_path, monkeypatch
):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("worker uses fork on supported Linux runtime")

    class ProcessBoundStorage(LocalFilesystemStorageAdapter):
        def __init__(self):
            super().__init__(root_dir=str(tmp_path))
            self.creator_pid = os.getpid()

        def get(self, key):
            if os.getpid() != self.creator_pid:
                raise RuntimeError("storage client inherited across fork")
            return super().get(key)

    def build_plane(**_kwargs):
        return ArtifactDatasetPlane(storage=ProcessBoundStorage())

    monkeypatch.setattr(artifact_storage_factory, "build_artifact_storage", build_plane)
    cache = getattr(worker, "_artifact_plane_for_process", worker._artifact_plane)
    cache.cache_clear()
    try:
        plane = worker._artifact_plane()
        assert worker._artifact_plane() is plane
        handle, _ = plane.create_artifact(
            owner_scope={"account_id": "a", "project_id": "p", "job_id": "j"},
            run_id="j",
            step_id="produce",
            filename="notes.md",
            content_type="text/markdown",
            body=b"file through fork",
        )
        runtime = AgentFileRuntime(
            plane=worker._artifact_plane, account_id="a", project_id="p", job_id="j"
        )
        files = runtime.bind_files(
            [{"name": "notes", "source": "state.outputs.notes"}],
            {"outputs": {"notes": f"artifact:{handle.artifact_id}"}},
        )

        class Agent:
            def execute(self, input):
                return AgentOutput(
                    status="pass",
                    artifacts={},
                    summary=input.file_context.read_text("notes", 0, 100)["text"],
                )

        result = _execute_agent_with_timeout(
            Agent(),
            AgentInput(task_context={}, step_context={}, agent_config={}, file_context=files),
            timeout_seconds=5,
            agent_id="reader",
            state={},
        )
        assert result.summary == "file through fork"
    finally:
        cache.cache_clear()
