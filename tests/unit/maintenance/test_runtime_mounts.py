"""Runtime marker mount parity for every process that claims new work."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUNTIME_MOUNT = (
    "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime:/run/flow-steward-runtime:ro"
)
WRITABLE_RUNTIME_MOUNT = (
    "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime:/run/flow-steward-runtime:rw"
)


def _service_block(source: str, service: str) -> str:
    match = re.search(rf"^  {re.escape(service)}:\n", source, flags=re.MULTILINE)
    assert match is not None, f"missing service {service}"
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", source[match.end() :])
    end = match.end() + following.start() if following else len(source)
    return source[match.start() : end]


@pytest.mark.parametrize(
    "compose_path",
    ["infra/docker/docker-compose.yaml", "infra/docker/docker-compose.prod.yaml"],
)
def test_claiming_processes_share_read_only_runtime_mount(compose_path: str) -> None:
    source = (ROOT / compose_path).read_text(encoding="utf-8")

    for service in ("fs-app", "fs-worker", "control-plane"):
        assert RUNTIME_MOUNT in _service_block(source, service), (
            f"{compose_path}: {service} cannot observe maintenance state"
        )

    mcp_block = _service_block(source, "fs-mcp")
    assert RUNTIME_MOUNT not in mcp_block
    assert WRITABLE_RUNTIME_MOUNT not in mcp_block


@pytest.mark.parametrize(
    "compose_path",
    ["infra/docker/docker-compose.yaml", "infra/docker/docker-compose.prod.yaml"],
)
def test_retained_cli_owns_the_only_writable_runtime_mount(compose_path: str) -> None:
    source = (ROOT / compose_path).read_text(encoding="utf-8")

    cli_block = _service_block(source, "fs-cli")

    assert WRITABLE_RUNTIME_MOUNT in cli_block
    assert RUNTIME_MOUNT not in cli_block
