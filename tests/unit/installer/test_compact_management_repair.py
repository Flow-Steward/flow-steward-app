"""Installed Compact repairs management tooling without replacing its runtime."""

from __future__ import annotations

import json

import pytest

from core.application.installer import ImageCurrency
from core.application.installer.errors import InstallerFailure
from core.infrastructure.installer.process import ProcessOutcome
from tests.unit.installer.test_compact_management_image import (
    _COMPACT_IMAGE,
    _CONFIG,
    _prepare,
    _source_compact,
)
from tests.unit.installer.test_installer_gateway import (
    _CLI_IMAGE_ID,
    _FULL_REVISION,
    _image_inspection,
    _stage_official_release,
)
from tests.unit.installer.test_installer_service import _installed_gateway, _installed_service


def _running_compact(gateway, process):
    listed = (
        "docker",
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={gateway._project_identity()}",
        "--filter",
        "label=com.docker.compose.service=compact-runtime",
        "--filter",
        "label=com.docker.compose.oneoff=False",
    )
    inspected = ("docker", "container", "inspect", "--format", "{{.Image}}", "compact-id")
    process.outcomes[listed] = ProcessOutcome(command=listed, returncode=0, stdout="compact-id\n")
    process.outcomes[inspected] = ProcessOutcome(
        command=inspected, returncode=0, stdout=_COMPACT_IMAGE + "\n"
    )


def _ensure(gateway):
    log = gateway.state_store.open_log("compact-management-repair")
    try:
        gateway.ensure_cli_runtime(_CONFIG, log=log)
    finally:
        log.close()


@pytest.mark.parametrize("selection", ("absent", "lost_owned", "stale_owned"))
def test_installed_source_compact_repairs_management_image_without_runtime_changes(
    tmp_path, monkeypatch, selection
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _running_compact(gateway, process)
    if selection != "absent":
        _prepare(gateway)
    if selection == "lost_owned":
        inspected = ("docker", "image", "inspect", _CLI_IMAGE_ID)
        process.queued_outcomes[inspected] = [
            ProcessOutcome(command=inspected, returncode=1),
            process.outcomes[inspected],
        ]
    if selection == "stale_owned":
        record = gateway.state_store.root / "compact-cli-image.json"
        owned = json.loads(record.read_text(encoding="utf-8"))
        owned["revision"] = "9" * 40
        gateway.state_store.write_owner_only_text(record, json.dumps(owned))
        gateway.state_store.update_env_values(
            tmp_path / ".env.prod", {"FS_CLI_IMAGE_REVISION": "9" * 40}
        )
    process.commands.clear()

    _ensure(gateway)

    values = gateway.state_store.read_env_values(tmp_path / ".env.prod")
    assert values["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    assert values["FS_CLI_IMAGE_REVISION"] == values["FS_BUILD_SHA"] == _FULL_REVISION
    builds = [
        command for command in process.commands if command[:3] == ("docker", "buildx", "build")
    ]
    assert len(builds) == 1
    assert builds[0][builds[0].index("--target") + 1] == "cli"
    assert values["FS_COMPACT_IMAGE"] == _COMPACT_IMAGE
    assert not any("up" in command or "pull" in command for command in process.commands)
    assert process.commands[-1][-1] == "core.entrypoints.cli.ops.candidate_runtime_preflight"


def test_source_compact_missing_explicit_management_pin_fails_without_build(tmp_path, monkeypatch):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    missing = "sha256:" + "9" * 64
    gateway._environment["FS_CLI_IMAGE"] = missing
    inspected = ("docker", "image", "inspect", "--format", "{{.Id}}", missing)
    process.outcomes[inspected] = ProcessOutcome(command=inspected, returncode=1)

    with pytest.raises(InstallerFailure, match="not present locally"):
        _ensure(gateway)

    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)


def test_already_installed_service_repairs_missing_compact_management_image(tmp_path, monkeypatch):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _running_compact(gateway, process)
    operations = _installed_gateway(
        ImageCurrency(source_revision=_FULL_REVISION, running_revision=_FULL_REVISION)
    )
    operations.active_config = _CONFIG
    operations.ensure_cli_runtime = gateway.ensure_cli_runtime
    service = _installed_service(tmp_path, operations)

    result = service.install(_CONFIG)

    assert result.status == "already_installed"
    assert (
        gateway.state_store.read_env_values(tmp_path / ".env.prod")["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    )
    assert "prepare_core" not in operations.calls
    assert not any("up" in command or "pull" in command for command in process.commands)


@pytest.mark.parametrize("different_selection", (False, True))
def test_source_compact_stale_explicit_management_pin_fails_full_source_validation(
    tmp_path, monkeypatch, different_selection
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    _running_compact(gateway, process)
    selected = "sha256:" + "9" * 64 if different_selection else _CLI_IMAGE_ID
    gateway._environment["FS_CLI_IMAGE"] = selected
    if not different_selection:
        gateway.state_store.update_env_values(
            tmp_path / ".env.prod", {"FS_CLI_IMAGE_REVISION": "9" * 40}
        )
    inspected = ("docker", "image", "inspect", selected)
    process.outcomes[inspected] = ProcessOutcome(
        command=inspected,
        returncode=0,
        stdout=_image_inspection(
            selected,
            "9" * 40,
            entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
        ),
    )
    process.commands.clear()

    with pytest.raises(InstallerFailure, match="source image provenance"):
        _ensure(gateway)

    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)
    assert (
        gateway.state_store.read_env_values(tmp_path / ".env.prod")["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    )


def test_source_compact_current_explicit_management_pin_does_not_replace_durable_selection(
    tmp_path, monkeypatch
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    _running_compact(gateway, process)
    selected = "sha256:" + "9" * 64
    gateway._environment["FS_CLI_IMAGE"] = selected
    inspected = ("docker", "image", "inspect", selected)
    process.outcomes[inspected] = ProcessOutcome(
        command=inspected,
        returncode=0,
        stdout=_image_inspection(
            selected,
            entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
        ),
    )
    process.commands.clear()

    _ensure(gateway)

    assert inspected in process.commands
    assert (
        gateway.state_store.read_env_values(tmp_path / ".env.prod")["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    )
    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)


def test_source_compact_mutable_explicit_management_tag_is_refused(tmp_path, monkeypatch):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    _running_compact(gateway, process)
    selected = "flow-steward-cli:operator-tag"
    gateway._environment["FS_CLI_IMAGE"] = selected
    inspected = ("docker", "image", "inspect", selected)
    process.outcomes[inspected] = process.outcomes[("docker", "image", "inspect", _CLI_IMAGE_ID)]
    process.commands.clear()

    with pytest.raises(InstallerFailure) as failure:
        _ensure(gateway)

    assert failure.value.code == "core_image_preparation_failed"
    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)
    assert not any(
        command[-1] == "core.entrypoints.cli.ops.candidate_runtime_preflight"
        for command in process.commands
    )


def test_source_compact_management_verification_keeps_registry_runtime_index_identity(
    tmp_path, monkeypatch
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    _running_compact(gateway, process)
    index_digest = "sha256:" + "9" * 64
    gateway.state_store.update_env_values(
        tmp_path / ".env.prod",
        {
            "FS_COMPACT_IMAGE_REGISTRY_REF": f"ghcr.io/example/compact@{index_digest}",
            "FS_COMPACT_IMAGE_INDEX_DIGEST": index_digest,
        },
    )
    process.commands.clear()

    _ensure(gateway)

    assert (
        gateway.state_store.read_env_values(tmp_path / ".env.prod")["FS_COMPACT_IMAGE_INDEX_DIGEST"]
        == index_digest
    )
    assert not any(command[:3] == ("docker", "image", "save") for command in process.commands)
    assert not any("up" in command or "pull" in command for command in process.commands)


def test_official_compact_cli_verification_never_builds_source_management_image(
    tmp_path, monkeypatch
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _stage_official_release(gateway, "compact")

    _ensure(gateway)

    assert len(process.commands) == 1
    assert process.commands[0][-1] == "core.entrypoints.cli.ops.candidate_runtime_preflight"
