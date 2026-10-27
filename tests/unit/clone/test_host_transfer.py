from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass
from core.infrastructure.clone.installation_gateway import DockerCloneVolumeTransfer
from core.infrastructure.clone.volume_metadata import inventory_volume_metadata


class _Runner:
    def __init__(self, *, fail_publication=False, fail_cleanup=False):
        self.commands = []
        self.inputs = []
        self.fail_publication = fail_publication
        self.fail_cleanup = fail_cleanup

    def __call__(self, command, **kwargs):
        argv = tuple(command)
        self.commands.append(argv)
        self.inputs.append(kwargs.get("input"))
        if argv[:2] == ("docker", "create"):
            return subprocess.CompletedProcess(argv, 0, stdout="owned-transfer\n", stderr="")
        failed = (self.fail_cleanup and argv[:2] == ("docker", "rm")) or (
            self.fail_publication and "core.infrastructure.clone.host_directory_publication" in argv
        )
        return subprocess.CompletedProcess(
            argv, int(failed), stdout="", stderr="private failure" if failed else ""
        )


def _source(tmp_path):
    source = tmp_path / "authenticated/extensions"
    source.mkdir(parents=True)
    (source / "marker").write_text("source")
    return source, inventory_volume_metadata(
        source, archive_root="extensions", data_class=CloneDataClass.EXTENSIONS
    )


@pytest.mark.parametrize("mode", ["publish", "validate", "verify"])
def test_transfer_binds_only_reviewed_external_root_and_cleans_owned_helper(tmp_path, mode):
    source, entries = _source(tmp_path)
    runner = _Runner()
    transfer = DockerCloneVolumeTransfer(
        project_name="owned-target",
        tool_image="ghcr.io/example/fs-cli@sha256:" + "a" * 64,
        runner=runner,
    )
    # External host path intentionally does not exist inside the caller container.
    target = Path("/host/external/custom/extensions")
    assert transfer.publish_host_filesystem(
        source,
        target,
        entries,
        plan_digest="b" * 64,
        owner=(1001, 1002),
        verify=mode == "verify",
        validate_only=mode == "validate",
    )
    create = runner.commands[0]
    mounts = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]
    suffix = "" if mode == "publish" else ",readonly"
    assert mounts == [
        "type=volume,destination=/capture,volume-nocopy",
        f"type=bind,source=/host/external/custom/extensions,destination=/target{suffix}",
    ]
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create
    assert create[create.index("--user") + 1] == "0:0"
    assert ("docker", "cp", str(source) + "/.", "owned-transfer:/capture/source") in runner.commands
    publication = next(
        command
        for command in runner.commands
        if "core.infrastructure.clone.host_directory_publication" in command
    )
    assert publication[publication.index("--uid") + 1] == "1001"
    assert publication[publication.index("--gid") + 1] == "1002"
    assert ("--verify" in publication) == (mode == "verify")
    assert ("--validate-only" in publication) == (mode == "validate")
    payload = next(value for value in runner.inputs if value)
    assert json.loads(payload)[0]["data_class"] == "extensions"
    assert runner.commands[-1] == ("docker", "rm", "--force", "--volumes", "owned-transfer")


@pytest.mark.parametrize(
    "target", ["/", "relative/path", "/root/../other", "/root,extra", "/root\nextra"]
)
def test_transfer_rejects_broad_or_unparseable_target_before_docker(tmp_path, target):
    source, entries = _source(tmp_path)
    runner = _Runner()
    transfer = DockerCloneVolumeTransfer(
        project_name="owned-target", tool_image="pinned", runner=runner
    )
    target_path = Path(target)
    owner = (os.geteuid(), os.getegid())
    with pytest.raises(ValueError):
        transfer.publish_host_filesystem(
            source, target_path, entries, plan_digest="b" * 64, owner=owner
        )
    assert runner.commands == []


def test_transfer_verification_failure_returns_false_and_removes_only_owned_helper(tmp_path):
    source, entries = _source(tmp_path)
    runner = _Runner(fail_publication=True)
    transfer = DockerCloneVolumeTransfer(
        project_name="owned-target", tool_image="pinned", runner=runner
    )
    assert not transfer.publish_host_filesystem(
        source,
        Path("/host/extensions"),
        entries,
        plan_digest="b" * 64,
        owner=(1001, 1002),
        verify=True,
    )
    assert runner.commands[-1] == ("docker", "rm", "--force", "--volumes", "owned-transfer")


def test_transfer_cleanup_failure_is_not_reported_as_successful_verification(tmp_path):
    source, entries = _source(tmp_path)
    runner = _Runner(fail_cleanup=True)
    transfer = DockerCloneVolumeTransfer(
        project_name="owned-target", tool_image="pinned", runner=runner
    )
    with pytest.raises(RuntimeError):
        transfer.publish_host_filesystem(
            source,
            Path("/host/extensions"),
            entries,
            plan_digest="b" * 64,
            owner=(1001, 1002),
            verify=True,
        )


def test_transfer_labels_project_and_operation_ownership(tmp_path):
    source, entries = _source(tmp_path)
    runner = _Runner()
    transfer = DockerCloneVolumeTransfer(
        project_name="owned-target", tool_image="pinned", runner=runner
    )
    transfer.publish_host_filesystem(
        source,
        Path("/host/extensions"),
        entries,
        plan_digest="b" * 64,
        owner=(1001, 1002),
        operation_id="clone-owned-operation",
    )
    create = runner.commands[0]
    labels = [create[index + 1] for index, value in enumerate(create) if value == "--label"]
    assert "com.flow-steward.clone.project=owned-target" in labels
    assert "com.flow-steward.clone.plan=" + "b" * 64 in labels
    assert "com.flow-steward.clone.operation=clone-owned-operation" in labels
