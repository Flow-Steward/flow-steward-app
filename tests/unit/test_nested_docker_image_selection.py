from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import uuid
from functools import partial
from pathlib import Path

import pytest

from tests.integration import test_candidate_harness_disposable_docker as candidate
from tests.integration import test_clone_volume_disposable as clone

IMAGE_ID = "sha256:" + "a" * 64
COMMITTED_ID = "sha256:" + "b" * 64


def _record_docker(monkeypatch, module):
    calls = []

    def run(*command, check=True):
        calls.append(command)
        output = ""
        if command[:3] == ("docker", "image", "inspect"):
            output = IMAGE_ID
        elif command[:2] == ("docker", "create"):
            output = "owned-container"
        elif command[:2] == ("docker", "commit"):
            output = COMMITTED_ID
        return subprocess.CompletedProcess(command, 0, output + "\n", "")

    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setenv("FS_TEST_AUDIT_IMAGE_ID", IMAGE_ID)
    return calls


@pytest.fixture
def clone_docker(monkeypatch, tmp_path: Path):
    source = tmp_path / "candidate-core"
    source.mkdir()
    (source / "candidate.py").write_text("CANDIDATE = True\n")
    derived = tmp_path / "derived-core"
    derived.mkdir()
    (derived / "stale-baked.py").write_text("STALE = True\n")
    calls = []
    state = {
        "container": False,
        "tag": False,
        "corrupt_copy": False,
        "bad_commit": False,
        "copy_failure": False,
        "commit_failure": False,
        "mutate_source": False,
    }
    clear_command = ""

    def run(*command, check=True):
        nonlocal clear_command
        calls.append(command)
        output = ""
        if command[:3] == ("docker", "image", "inspect"):
            if command[-1] in (IMAGE_ID, COMMITTED_ID):
                output = command[-1]
            else:
                return subprocess.CompletedProcess(command, 1, "[]", "No such image")
        elif command[:2] == ("docker", "create"):
            clear_command = command[-1]
            state["container"] = True
            output = "owned-container"
        elif command[:2] == ("docker", "start"):
            # Execute the actual disposable clearing command against a synthetic
            # container filesystem, so an overlay leaves its stale baked file.
            subprocess.run(
                ["sh", "-c", clear_command.replace("/app/core", shlex.quote(str(derived)))],
                check=True,
            )
        elif command[:2] == ("docker", "cp"):
            shutil.copytree(source, derived, dirs_exist_ok=True)
            if state["copy_failure"]:
                raise subprocess.CalledProcessError(23, command, stderr="copy failed")
            if state["mutate_source"]:
                (source / "candidate.py").write_text("CHANGED = True\n")
            if state["corrupt_copy"]:
                (derived / "candidate.py").write_text("WRONG = True\n")
        elif command[:2] == ("docker", "commit"):
            state["tag"] = True
            if state["commit_failure"]:
                raise subprocess.CalledProcessError(24, command, stderr="commit failed")
            output = "clone:latest" if state["bad_commit"] else COMMITTED_ID
        elif command[:2] == ("docker", "run"):
            # Execute the emitted real manifest program with this synthetic
            # filesystem standing in for the disposable image's /app/core.
            script = command[-1].replace('Path("/app/core")', f"Path({str(derived)!r})")
            output = subprocess.check_output([sys.executable, "-c", script], text=True)
        elif command[:2] == ("docker", "rm"):
            state["container"] = False
        elif command[:3] == ("docker", "image", "rm"):
            state["tag"] = False
        elif command[:2] == ("docker", "inspect"):
            return subprocess.CompletedProcess(
                command,
                int(not state["container"]),
                "",
                "No such container" if not state["container"] else "",
            )
        else:
            pytest.fail(f"Unexpected Docker boundary: {command}")
        return subprocess.CompletedProcess(command, 0, output + "\n", "")

    monkeypatch.setattr(clone, "_run", run)
    monkeypatch.setattr(clone, "_CANDIDATE_CORE", source)
    monkeypatch.setenv("FS_TEST_AUDIT_IMAGE_ID", IMAGE_ID)
    return calls, state, derived


def test_populate_uses_runner_image_identity(monkeypatch) -> None:
    calls = _record_docker(monkeypatch, clone)

    clone._populate("owned-volume", "printf synthetic > /volume/value")

    run = next(command for command in calls if command[:2] == ("docker", "run"))
    assert run[run.index("sh") + 1] == IMAGE_ID
    assert "--pull=never" in run


def test_clone_image_replaces_core_before_copy_and_yields_commit_id(clone_docker) -> None:
    calls, state, derived = clone_docker
    fixture = clone.clone_tool_image.__wrapped__()

    try:
        selected = next(fixture)
        create = next(command for command in calls if command[:2] == ("docker", "create"))
        assert "rm -rf /app/core" in create[-1]
        assert create[create.index("sh") + 1] == IMAGE_ID
        assert selected == COMMITTED_ID
        start_at = next(i for i, command in enumerate(calls) if command[:2] == ("docker", "start"))
        copy_at = next(i for i, command in enumerate(calls) if command[:2] == ("docker", "cp"))
        commit_at = next(
            i for i, command in enumerate(calls) if command[:2] == ("docker", "commit")
        )
        assert start_at < copy_at < commit_at
    finally:
        fixture.close()
    assert ("docker", "rm", "--force", "owned-container") in calls
    removed = [command for command in calls if command[:3] == ("docker", "image", "rm")]
    assert removed, "derived image teardown must run"
    assert not (derived / "stale-baked.py").exists()
    assert state["container"] is False
    assert state["tag"] is False


def test_candidate_container_uses_runner_image_before_cleanup(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("FS_TEST_AUDIT_IMAGE_ID", IMAGE_ID)
    token = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    monkeypatch.setattr(candidate.uuid, "uuid4", lambda: token)

    class StopAfterCreate(Exception):
        pass

    def run(*command, check=True):
        calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            assert command == ("docker", "image", "inspect", "--format", "{{.Id}}", IMAGE_ID)
            return subprocess.CompletedProcess(command, 0, IMAGE_ID + "\n", "")
        if command[:3] == ("docker", "network", "create"):
            assert command == (
                "docker",
                "network",
                "create",
                "--label",
                "com.docker.compose.project=fs-cleanup-test-cccccccccc-a",
                "--label",
                "com.flow-steward.candidate.sha=" + "a" * 40,
                "fs-cleanup-test-cccccccccc-a-network",
            ), "network creation must reject container/image-only flags"
            return subprocess.CompletedProcess(command, 0, "owned-network\n", "")
        if command[:2] == ("docker", "create"):
            raise StopAfterCreate
        if command[:3] == ("docker", "network", "rm"):
            assert command == ("docker", "network", "rm", "owned-network")
            return subprocess.CompletedProcess(command, 0, "owned-network\n", "")
        if command[:3] == ("docker", "network", "inspect"):
            assert command == ("docker", "network", "inspect", "owned-network")
            return subprocess.CompletedProcess(command, 1, "", "No such network")
        pytest.fail(f"Unexpected Docker boundary: {command}")

    monkeypatch.setattr(candidate, "_run", run)
    with pytest.raises(StopAfterCreate):
        candidate.test_cleanup_removes_only_one_exact_disposable_candidate()

    create = next(command for command in calls if command[:2] == ("docker", "create"))
    assert create[-1] == IMAGE_ID
    assert "--pull=never" in create


@pytest.mark.parametrize(
    "value", (None, "", " ", "audit:latest", "sha256:abcd", "sha256:" + "A" * 64)
)
@pytest.mark.parametrize("consumer", ("populate", "volume", "candidate"))
def test_invalid_identity_fails_before_docker_mutation(monkeypatch, value, consumer) -> None:
    if value is None:
        monkeypatch.delenv("FS_TEST_AUDIT_IMAGE_ID", raising=False)
    else:
        monkeypatch.setenv("FS_TEST_AUDIT_IMAGE_ID", value)

    def forbidden(*command, check=True):
        pytest.fail(f"invalid image identity reached Docker: {command}")

    monkeypatch.setattr(clone, "_run", forbidden)
    monkeypatch.setattr(candidate, "_run", forbidden)
    if consumer == "populate":
        consume = partial(clone._populate, "owned-volume", "true")
    elif consumer == "volume":
        consume = partial(clone._volume, "owned-volume", "owned-project")
    else:
        consume = candidate.test_cleanup_removes_only_one_exact_disposable_candidate
    with pytest.raises(RuntimeError, match="FS_TEST_AUDIT_IMAGE_ID"):
        consume()


@pytest.mark.parametrize("fault", ("corrupt_copy", "bad_commit", "mutate_source"))
def test_clone_image_refuses_unproven_content_and_cleans_construction(clone_docker, fault) -> None:
    _, state, _ = clone_docker
    state[fault] = True
    fixture = clone.clone_tool_image.__wrapped__()

    with pytest.raises(RuntimeError, match=r"core differs|immutable clone image ID"):
        next(fixture)

    assert state["container"] is False
    assert state["tag"] is False


@pytest.mark.parametrize("module", (clone, candidate))
@pytest.mark.parametrize("fault", ("unavailable", "different_id"))
def test_selected_local_image_is_verified_before_mutation(monkeypatch, module, fault) -> None:
    monkeypatch.setenv("FS_TEST_AUDIT_IMAGE_ID", IMAGE_ID)
    calls = []

    def run(*command, check=True):
        calls.append(command)
        assert command[:3] == ("docker", "image", "inspect"), "mutation preceded image preflight"
        if fault == "unavailable":
            raise subprocess.CalledProcessError(1, command, stderr="No such image")
        return subprocess.CompletedProcess(command, 0, COMMITTED_ID + "\n", "")

    monkeypatch.setattr(module, "_run", run)
    if module is clone:
        consume = partial(clone._volume, "owned-volume", "owned-project")
    else:
        consume = candidate.test_cleanup_removes_only_one_exact_disposable_candidate
    with pytest.raises(RuntimeError, match="FS_TEST_AUDIT_IMAGE_ID"):
        consume()
    assert len(calls) == 1
    assert calls[0][-1] == IMAGE_ID


def test_core_manifest_covers_absence_modes_bytes_and_symlinks(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir(mode=0o755)
    file = core / "code.py"
    file.write_bytes(b"candidate\n")
    file.chmod(0o640)
    (core / "alias").symlink_to("code.py")

    before = clone._core_manifest(core)
    assert [row["path"] for row in before] == [".", "alias", "code.py"]
    assert before[1]["kind"] == "symlink"
    assert before[1]["target"] == "code.py"
    assert before[2]["mode"] == 0o640
    assert before[2]["size"] == 10
    file.chmod(0o600)
    assert clone._core_manifest(core) != before
    file.chmod(0o640)
    file.write_bytes(b"different\n")
    assert clone._core_manifest(core) != before
    file.write_bytes(b"candidate\n")
    (core / "stale.py").write_bytes(b"stale")
    assert clone._core_manifest(core) != before


def test_clone_cleanup_attempts_every_owned_resource_and_reports_failure(monkeypatch) -> None:
    calls = []

    def run(*command, check=True):
        calls.append(command)
        if command[-1] == "first":
            return subprocess.CompletedProcess(
                command, 1 if "rm" in command else 0, "", "permission denied"
            )
        return subprocess.CompletedProcess(
            command,
            0 if "rm" in command else 1,
            "",
            "No such volume" if "inspect" in command else "",
        )

    monkeypatch.setattr(clone, "_run", run)
    with pytest.raises(RuntimeError, match="Owned Docker cleanup failed"):
        clone._cleanup_owned(("volume", "first"), ("volume", "second"))
    assert ("docker", "volume", "rm", "--force", "second") in calls


@pytest.mark.parametrize("fault", ("copy_failure", "commit_failure"))
def test_clone_image_construction_failure_cleans_every_created_resource(
    clone_docker, fault
) -> None:
    _, state, _ = clone_docker
    state[fault] = True
    fixture = clone.clone_tool_image.__wrapped__()

    with pytest.raises(subprocess.CalledProcessError):
        next(fixture)

    assert state["container"] is False
    assert state["tag"] is False


def test_candidate_cleanup_attempts_other_resources_after_native_failure(monkeypatch) -> None:
    calls = []

    def run(*command, check=True):
        calls.append(command)
        if command[-1] == "container-id":
            return subprocess.CompletedProcess(
                command, 1 if "rm" in command else 0, "", "permission denied"
            )
        return subprocess.CompletedProcess(
            command,
            0 if "rm" in command else 1,
            "",
            "network not found" if "inspect" in command else "",
        )

    monkeypatch.setattr(candidate, "_run", run)
    with pytest.raises(RuntimeError, match="Owned Docker cleanup failed"):
        candidate._cleanup_owned(["container-id"], ["network-id"])
    assert ("docker", "network", "rm", "network-id") in calls
