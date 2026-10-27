from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from core.application.tooling.contracts import ToolRunResult
from core.application.tooling.testing.service import TestingToolingService as LiveTestingService


class _InlineRunner:
    def run_callable(self, **kwargs: Any) -> ToolRunResult:
        exit_code = int(kwargs["target"]() or 0)
        return ToolRunResult(
            operation=str(kwargs["operation"]),
            status="completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
            command=[str(kwargs["operation"])],
        )

    def run_process(self, **_kwargs: Any) -> ToolRunResult:
        raise AssertionError("Live runner integration must stay on the callable boundary")


class _Registry:
    def __init__(self, runner: Any | None = None) -> None:
        self.runtime = SimpleNamespace(
            testing_tooling_service=LiveTestingService(runner=runner or _InlineRunner())
        )

    async def invoke(self, key: str, value: object, **kwargs: Any) -> object:
        from core.entrypoints.cli.platform.service_bindings.testing import testing_cutover_smoke

        assert key == "testing.cutover-smoke"
        return await testing_cutover_smoke(self.runtime, value, **kwargs)

    async def choices(self, _key: str) -> tuple[object, ...]:
        return ()


@pytest.mark.parametrize("isolated_process", [False, True], ids=["inline", "packaged-process"])
def test_cutover_cli_verifies_bearer_session_before_mutation(
    isolated_process: bool, tmp_path
) -> None:
    calls: list[tuple[str, str, str]] = []
    job_reads = 0

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            nonlocal job_reads
            auth = str(self.headers.get("Authorization") or "")
            calls.append(("GET", self.path, auth))
            if auth != "Bearer service-token":
                self._send(401, {"detail": "unauthorized"})
            elif self.path == "/api/auth/me":
                self._send(200, {"id": "service-account-1"})
            elif self.path == "/api/projects/project-1/workflows":
                self._send(
                    200,
                    {
                        "operator_default_workflow_id": "wf.current",
                        "project_workflows": [
                            {
                                "workflow_id": "wf.current",
                                "enabled": True,
                                "availability_status": "available",
                                "new_runs_blocked": False,
                            }
                        ],
                    },
                )
            elif self.path == "/api/projects/project-1/workflows/wf.current/contract":
                self._send(200, {"interface": {"input_schema": {"properties": {}}}})
            elif self.path == "/api/jobs/job-1":
                job_reads += 1
                if job_reads == 1:
                    self._send(503, {"error": "resume state is temporarily unavailable"})
                    return
                if job_reads == 2:
                    self._send(409, {"error": "resume state revision is changing"})
                    return
                self._send(200, {"status": "completed", "thread_id": "thread-1"})
            elif self.path == "/api/jobs/job-1/messages?limit=2000":
                self._send(
                    200,
                    {
                        "messages": [
                            {"status": "completed", "from_agent": "planner"},
                            {"status": "completed", "from_agent": "writer"},
                            {"status": "completed", "from_agent": "checker"},
                        ]
                    },
                )
            else:
                self._send(404, {"detail": "not found"})

        def do_POST(self) -> None:
            auth = str(self.headers.get("Authorization") or "")
            calls.append(("POST", self.path, auth))
            if auth != "Bearer service-token":
                self._send(401, {"detail": "unauthorized"})
            elif self.path == "/api/workflow-runs":
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body.get("workflow_id") != "wf.current":
                    self._send(400, {"error": "workflow_id is required"})
                    return
                self._send(202, {"job_id": "job-1"})
            else:
                self._send(404, {"detail": "not found"})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from core.entrypoints.cli.platform.click_adapter import build_click_app
        from core.infrastructure.tooling.process_runner import OSProcessRunner

        base_url = f"http://127.0.0.1:{server.server_port}"
        runner = (
            OSProcessRunner(working_directory=tmp_path) if isolated_process else _InlineRunner()
        )
        result = CliRunner().invoke(
            build_click_app(_Registry(runner), SimpleNamespace()),
            [
                "testing",
                "cutover-smoke",
                "--project-id",
                "project-1",
                "--base-url",
                base_url,
                "--bearer-token-stdin",
                "--confirm",
                "--plain",
            ],
            input="service-token\n",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.exit_code == 0, result.output
    assert [(method, path) for method, path, _ in calls[:4]] == [
        ("GET", "/api/auth/me"),
        ("GET", "/api/projects/project-1/workflows"),
        ("GET", "/api/projects/project-1/workflows/wf.current/contract"),
        ("POST", "/api/workflow-runs"),
    ]
    assert all(auth == "Bearer service-token" for _, _, auth in calls)
    assert "service-token" not in result.output
