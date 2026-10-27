from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests.support.disposable_installer import DisposableInstallerProject

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def disposable_project(tmp_path: Path) -> DisposableInstallerProject:
    project = DisposableInstallerProject.create(
        work_root=tmp_path,
        source_root=ROOT,
    )
    try:
        yield project
    finally:
        if not project.cleaned:
            project.cleanup()


def _cli_result(result: Any) -> dict[str, Any]:
    assert result.returncode == 0, result.combined_output
    payload = json.loads(result.stdout)
    assert payload["schema"] == "flow-steward.cli.v1"
    return dict(payload["result"])


def _queue_total(project: DisposableInstallerProject) -> int:
    observation = project.ready_queue_observation()
    return observation["ready"] + observation["unacknowledged"]


def _wait_until(predicate: Any, *, timeout_seconds: float = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError("maintenance acceptance condition did not converge")


def test_disposable_maintenance_closes_ingress_workers_and_reopens_explicitly(
    disposable_project: DisposableInstallerProject,
) -> None:
    install = disposable_project.install(
        environment="development",
        renderer="non_interactive",
        storage="bundled",
    )
    assert install.returncode == 0, install.combined_output
    baseline_queue_depth = _queue_total(disposable_project)

    enabled = _cli_result(
        disposable_project.cli(
            "maintenance",
            "enable",
            "--non-interactive",
            "--confirm",
            "--json",
        )
    )
    assert enabled["status"] in {"enabled", "already_enabled"}
    assert disposable_project.runtime_maintenance_observations() == {
        "fs-worker": True,
        "control-plane": True,
    }

    status = disposable_project.public_json("GET", "/api/system/maintenance")
    assert status.status == 200
    assert status.payload["status"] == "enabled"
    assert "reason" not in status.payload

    blocked_api = disposable_project.public_json("POST", "/api/projects", payload={})
    assert blocked_api.status == 503
    assert blocked_api.payload["error"]["code"] == "maintenance_mode_enabled"
    assert blocked_api.headers["Retry-After"] == "60"
    blocked_callback = disposable_project.public_json(
        "GET",
        "/webhook/extensions/missing/callbacks/missing/token",
    )
    assert blocked_callback.status == 503
    assert blocked_callback.payload["error"]["code"] == "maintenance_mode_enabled"
    assert disposable_project.public_json("GET", "/health").status == 200

    disposable_project.publish_ready_maintenance_probe()
    _wait_until(lambda: _queue_total(disposable_project) == baseline_queue_depth + 1)

    disabled = _cli_result(
        disposable_project.cli(
            "maintenance",
            "disable",
            "--non-interactive",
            "--confirm",
            "--json",
        )
    )
    assert disabled["status"] == "disabled"
    assert disposable_project.runtime_maintenance_observations() == {
        "fs-worker": False,
        "control-plane": False,
    }
    _wait_until(lambda: _queue_total(disposable_project) == baseline_queue_depth)
    assert disposable_project.public_json("POST", "/api/projects", payload={}).status != 503

    disposable_project.cleanup()
    assert disposable_project.cleanup_proof() == {
        "containers": [],
        "networks": [],
        "volumes": [],
        "images": [],
        "paths": [],
    }
