"""0.9.25: the release upgrade's migration container can log, and its errors are shown.

`release upgrade` applies the target release's migrations with
`docker compose run ... fs-app -m core.entrypoints.cli.click_app database upgrade --json`
(upgrade_gateway.py). Without the launcher's -e flags the command found no writable
operation-log root, stopped with operation_log_unavailable, and the upgrade reported
Compose's "Container ... Created" lines as the migration problem. Every Scalable
upgrade that had to apply a migration stopped there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.application.lifecycle.stack_service import StackGatewayError
from core.infrastructure.lifecycle.docker_cli_errors import cli_error_detail
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
from tests.unit.lifecycle.test_stack_service import _target

ROOT = Path(__file__).resolve().parents[3]
#: source (which may hold `${VAR:-default}`), container path, optional mode.
_MOUNT = re.compile(r"(?P<source>(?:\$\{[^}]*\}|[^:$])+):(?P<target>/[^:]*)(?::(?P<mode>\w+))?")
PROD_COMPOSE = ROOT / "infra/docker/docker-compose.prod.yaml"

_COMPOSE_STDERR = (
    " Container fs-demo-fs-app-run-7ea89bd889f7 Creating \n"
    " Container fs-demo-fs-app-run-7ea89bd889f7 Created\n"
)
_CLI_ERROR = {
    "schema": "flow-steward.cli.v1",
    "ok": False,
    "command": "database.upgrade",
    "error": {
        "code": "operation_log_unavailable",
        "message": "Cannot write the operation log in '.flow-steward/logs'",
        "hint": "Make the state directory writable for the user running this command.",
    },
}


class _Process:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def run(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        del command, kwargs
        return self.result


def test_fs_app_writes_cli_operation_logs_to_its_writable_logs_mount() -> None:
    service = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))["services"]["fs-app"]
    root = service["environment"]["FS_CLI_OPERATION_LOG_ROOT"]
    mounts = [_MOUNT.fullmatch(str(volume)) for volume in service["volumes"]]
    assert all(mounts), service["volumes"]
    writable = [m["target"] for m in mounts if m and m["mode"] != "ro"]

    assert root == "/app/logs/cli"
    assert any(Path(root).is_relative_to(target) for target in writable), writable


def test_a_failed_container_cli_reports_its_own_error_not_compose_progress(
    tmp_path: Path,
) -> None:
    gateway = DockerLifecycleGateway(
        project_root=tmp_path,
        process=_Process(4, json.dumps(_CLI_ERROR), _COMPOSE_STDERR),
    )

    with pytest.raises(StackGatewayError) as raised:
        gateway._run_compose(
            _target(),
            ("run", "--rm", "fs-app", "database", "upgrade", "--json"),
            action="database_upgrade",
            timeout_seconds=30,
            environment={},
            separate_stderr=True,
        )

    assert raised.value.detail.startswith("operation_log_unavailable: Cannot write")
    assert "Container" not in raised.value.detail


def test_a_failure_without_a_cli_error_keeps_the_compose_output(tmp_path: Path) -> None:
    gateway = DockerLifecycleGateway(
        project_root=tmp_path,
        process=_Process(1, "", "no such service: fs-app\n"),
    )

    with pytest.raises(StackGatewayError) as raised:
        gateway._run_compose(
            _target(),
            ("run", "--rm", "fs-app"),
            action="database_upgrade",
            timeout_seconds=30,
            environment={},
            separate_stderr=True,
        )

    assert raised.value.detail == "no such service: fs-app"


@pytest.mark.parametrize(
    "stdout",
    (
        "",
        "not json",
        "[1, 2]",
        json.dumps({"ok": True, "result": {}}),
        json.dumps({"ok": False, "error": "flat"}),
    ),
)
def test_only_a_cli_error_envelope_yields_a_detail(stdout: str) -> None:
    assert cli_error_detail(stdout) == ""


def test_cli_error_detail_names_code_message_and_hint() -> None:
    assert cli_error_detail(json.dumps(_CLI_ERROR)) == (
        "operation_log_unavailable: Cannot write the operation log in '.flow-steward/logs' "
        "Make the state directory writable for the user running this command."
    )
