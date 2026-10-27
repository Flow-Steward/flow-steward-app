"""AC17-R158: an update keeps the current and previous release, not every release.

Measured by the auditor on a live Scalable: .flow-steward/release/ held 48 GB after one
working day -- sixteen revisions, each with a 3.3 GB candidate-images.tar -- and the
matching flow-steward-*:candidate-<sha> image sets piled up in Docker the same way.
Nothing ever removed either; the host would have filled in two days.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from core.application.installer import InstallConfig
from core.infrastructure.installer import release_retention
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_OLDEST, _OLD, _PREVIOUS, _CURRENT = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)
_LISTING = ("docker", "image", "ls", "--filter", "reference=flow-steward-*:candidate-*")


class _Process:
    def __init__(self, tags: list[str], refused: set[str] | None = None) -> None:
        self.tags = tags
        self.refused = refused or set()
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        key = tuple(command)
        self.commands.append(key)
        if key[: len(_LISTING)] == _LISTING:
            return ProcessOutcome(command=key, returncode=0, stdout="\n".join(self.tags) + "\n")
        if key[:3] == ("docker", "image", "rm") and key[-1] in self.refused:
            return ProcessOutcome(command=key, returncode=1, stderr="image is being used")
        return ProcessOutcome(command=key, returncode=0)

    def removed(self) -> list[str]:
        return [key[-1] for key in self.commands if key[:3] == ("docker", "image", "rm")]


def _release(root: Path, revision: str, *, age: int, archive_bytes: int = 4096) -> Path:
    release = root / ".flow-steward/release" / revision
    release.mkdir(parents=True)
    (release / "candidate.env").write_text(f"FS_BUILD_SHA={revision}\n", encoding="utf-8")
    (release / "candidate-images.tar").write_bytes(b"\0" * archive_bytes)
    stamp = 1_700_000_000 + age
    os.utime(release, (stamp, stamp))
    return release


def _gateway(root: Path, process: _Process, **environment: str) -> DockerInstallerGateway:
    (root / "infra/docker").mkdir(parents=True, exist_ok=True)
    return DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward"),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment=environment,
        sleep=lambda _seconds: None,
    )


def _four_releases(root: Path) -> None:
    for age, revision in enumerate((_OLDEST, _OLD, _PREVIOUS, _CURRENT)):
        _release(root, revision, age=age)


def _tags(*revisions: str) -> list[str]:
    return [
        f"flow-steward-{role}:candidate-{revision[:12]}"
        for revision in revisions
        for role in ("app", "worker")
    ]


def _prune(gateway: DockerInstallerGateway, root: Path, **kwargs) -> str:
    log = gateway.state_store.open_log(f"retention-{uuid4().hex}")
    try:
        return gateway.prune_superseded_releases(
            InstallConfig(environment="production"),
            current=kwargs.get("current", _CURRENT),
            previous=kwargs.get("previous", _PREVIOUS),
            log=log,
        )
    finally:
        log.close()


def test_an_update_removes_archives_but_retains_shared_images_and_records(
    tmp_path: Path,
) -> None:
    _four_releases(tmp_path)
    process = _Process(_tags(_OLDEST, _OLD, _PREVIOUS, _CURRENT))

    summary = _prune(_gateway(tmp_path, process), tmp_path)

    releases = tmp_path / ".flow-steward/release"
    assert sorted(path.name for path in releases.iterdir()) == [_OLDEST, _OLD, _PREVIOUS, _CURRENT]
    assert process.removed() == []
    # AC17-R159: nothing reads an archive -- a rollback records the kept revision's own
    # images from its record, which therefore stays
    for revision in (_OLDEST, _OLD, _PREVIOUS, _CURRENT):
        assert not (releases / revision / "candidate-images.tar").exists()
        assert (releases / revision / "candidate.env").is_file()
    assert "Removed image archives" in summary
    assert "ownership is unproven" in summary


def test_the_retention_setting_keeps_more_but_never_fewer_than_two(tmp_path: Path) -> None:
    _four_releases(tmp_path)
    process = _Process([])

    _prune(_gateway(tmp_path, process, FS_INSTALLER_RELEASE_RETENTION="3"), tmp_path)
    kept = sorted(path.name for path in (tmp_path / ".flow-steward/release").iterdir())
    assert kept == [_OLD, _PREVIOUS, _CURRENT]

    _prune(_gateway(tmp_path, process, FS_INSTALLER_RELEASE_RETENTION="1"), tmp_path)
    kept = sorted(path.name for path in (tmp_path / ".flow-steward/release").iterdir())
    assert kept == [_PREVIOUS, _CURRENT]


def test_a_release_the_installation_still_references_is_kept(tmp_path: Path) -> None:
    _four_releases(tmp_path)
    (tmp_path / ".env.prod").write_text(
        # written under the host's path, read by an installer that may see another
        f"FS_EXTENSIONS_DIR=/srv/host/fs/.flow-steward/release/{_OLDEST}/candidate-extensions\n",
        encoding="utf-8",
    )

    _prune(_gateway(tmp_path, _Process([])), tmp_path)

    kept = sorted(path.name for path in (tmp_path / ".flow-steward/release").iterdir())
    assert kept == [_OLDEST, _PREVIOUS, _CURRENT]


def test_an_image_docker_refuses_to_remove_is_reported_not_forced(tmp_path: Path) -> None:
    _four_releases(tmp_path)
    in_use = _tags(_OLD)[0]
    process = _Process(_tags(_OLD), refused={in_use})

    summary = _prune(_gateway(tmp_path, process), tmp_path)

    assert all("-f" not in command and "--force" not in command for command in process.commands)
    assert in_use in summary


def test_repeated_cleanup_does_not_turn_ambiguous_ownership_into_permission(tmp_path: Path) -> None:
    _four_releases(tmp_path)
    in_use = _tags(_OLD)[0]
    process = _Process([in_use], refused={in_use})
    gateway = _gateway(tmp_path, process)

    _prune(gateway, tmp_path)

    release = tmp_path / ".flow-steward/release" / _OLD
    assert (release / "candidate.env").is_file()
    process.refused.clear()
    process.commands.clear()
    summary = _prune(gateway, tmp_path)

    assert process.removed() == []
    assert (release / "candidate.env").is_file()
    assert "ownership" in summary


def test_interrupted_archive_cleanup_is_resumable_without_deleting_shared_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_releases(tmp_path)
    root = tmp_path / ".flow-steward/release"
    tag = _tags(_OLD)[0]

    original_unlink = Path.unlink

    def interrupted(path: Path, *args, **kwargs) -> None:
        if path.parent.name == _OLD:
            raise KeyboardInterrupt
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)

    with pytest.raises(KeyboardInterrupt):
        release_retention.prune(
            root,
            current=_CURRENT,
            protected=[_PREVIOUS],
            retention=2,
            candidate_tags=[tag],
        )

    assert (root / _OLD / "candidate.env").is_file()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    report = release_retention.prune(
        root,
        current=_CURRENT,
        protected=[_PREVIOUS],
        retention=2,
        candidate_tags=[tag],
    )

    assert report.ambiguous_images == [tag]
    assert (root / _OLD / "candidate.env").is_file()
    assert not any(root.glob("*/candidate-images.tar"))


def test_with_no_record_and_no_log_the_newest_other_release_is_kept(
    tmp_path: Path,
) -> None:
    """The last resort only: nothing says what the installation replaced."""
    _four_releases(tmp_path)

    _prune(_gateway(tmp_path, _Process([])), tmp_path, previous="")

    kept = sorted(path.name for path in (tmp_path / ".flow-steward/release").iterdir())
    assert kept == [_PREVIOUS, _CURRENT]


def test_compact_retention_never_touches_scalable_releases_or_candidates(tmp_path: Path) -> None:
    """Compact keeps no release directories; its retention is over its own runtime
    images (AC17-R178, test_compact_image_retention.py) -- it was nothing at all before,
    which this test pinned as `process.commands == []`."""
    _four_releases(tmp_path)
    process = _Process(_tags(_OLDEST))
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("retention")

    summary = gateway.prune_superseded_releases(
        InstallConfig(environment="production", deployment_profile="compact"),
        current=_CURRENT,
        previous=_PREVIOUS,
        log=log,
    )
    log.close()

    assert summary == ""  # this installation built no Compact image here
    assert sorted(path.name for path in (tmp_path / ".flow-steward/release").iterdir()) == sorted(
        (_CURRENT, _PREVIOUS, _OLD, _OLDEST)
    )
    assert not any("candidate" in " ".join(command) for command in process.commands)
    assert not any(command[:3] == ("docker", "image", "rm") for command in process.commands)


def test_the_running_revisions_images_survive_a_missing_release_directory(
    tmp_path: Path,
) -> None:
    _release(tmp_path, _OLDEST, age=0)
    process = _Process(_tags(_OLDEST, _CURRENT))

    _prune(_gateway(tmp_path, process), tmp_path)  # no directory for _CURRENT or _PREVIOUS

    assert process.removed() == []
    assert (tmp_path / ".flow-steward/release" / _OLDEST / "candidate.env").is_file()


def test_the_previous_release_comes_from_the_installations_record(tmp_path: Path) -> None:
    """AC17-R159: a build made by hand is the newest directory, and it is not a rollback."""
    _four_releases(tmp_path)  # _CURRENT is the newest by mtime, then _PREVIOUS
    (tmp_path / ".flow-steward/installed-releases.json").write_text(
        json.dumps({"current": _CURRENT, "previous": _OLDEST}), encoding="utf-8"
    )

    _prune(_gateway(tmp_path, _Process([])), tmp_path, previous="")

    kept = sorted(path.name for path in (tmp_path / ".flow-steward/release").iterdir())
    assert kept == [_OLDEST, _CURRENT]


def test_an_installation_older_than_the_record_finds_its_previous_in_its_logs(
    tmp_path: Path,
) -> None:
    _four_releases(tmp_path)
    logs = tmp_path / ".flow-steward/logs"
    logs.mkdir(parents=True)
    (logs / "install-20260923T120000Z-a.log").write_text(
        f"... Updated from {_OLD} to {_CURRENT}. The schema check passed ...\n", encoding="utf-8"
    )

    _prune(_gateway(tmp_path, _Process([])), tmp_path, previous="")

    kept = sorted(path.name for path in (tmp_path / ".flow-steward/release").iterdir())
    assert kept == [_OLD, _CURRENT]


def test_a_run_that_only_removed_archives_says_so(tmp_path: Path) -> None:
    """R159 acceptance: "Removed 0 superseded releases and 0 candidate images, freeing 3.1 GB"."""
    for age, revision in enumerate((_PREVIOUS, _CURRENT)):
        _release(tmp_path, revision, age=age)

    summary = _prune(_gateway(tmp_path, _Process([])), tmp_path)

    assert summary.startswith("Removed image archives, freeing")
    assert "0 superseded" not in summary


def test_another_installations_candidate_images_are_left_alone(tmp_path: Path) -> None:
    """C2 (External review): the listing is host-wide, and a second Scalable installation
    on the host lost the rollback images this one did not keep."""
    _four_releases(tmp_path)
    foreign = "9" * 40  # built by another installation: no release directory here
    process = _Process(_tags(_OLDEST, _OLD, _PREVIOUS, _CURRENT, foreign))

    _prune(_gateway(tmp_path, process), tmp_path)

    assert process.removed() == []
    assert not set(process.removed()) & set(_tags(foreign))


def test_shared_revision_tags_are_not_exclusively_owned_by_a_local_release(tmp_path: Path) -> None:
    installation_a = tmp_path / "a"
    installation_b = tmp_path / "b"
    _four_releases(installation_a)
    rollback_b = _release(installation_b, _OLD, age=0)
    shared = _tags(_OLD)
    process = _Process(shared)

    summary = _prune(_gateway(installation_a, process), installation_a)

    assert process.removed() == []
    assert (rollback_b / "candidate.env").is_file()
    assert (installation_a / ".flow-steward/release" / _OLD / "candidate.env").is_file()
    assert "ownership" in summary


def test_failed_image_listing_retains_records_and_reports_nonfatal_housekeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_releases(tmp_path)
    process = _Process([])
    original = process.run

    def failed_listing(command, **kwargs):
        if tuple(command)[: len(_LISTING)] == _LISTING:
            return ProcessOutcome(command=tuple(command), returncode=1, stderr="daemon unavailable")
        return original(command, **kwargs)

    monkeypatch.setattr(process, "run", failed_listing)

    summary = _prune(_gateway(tmp_path, process), tmp_path)

    assert "not all removed" in summary
    assert process.removed() == []
    for revision in (_OLDEST, _OLD, _PREVIOUS, _CURRENT):
        assert (tmp_path / ".flow-steward/release" / revision / "candidate.env").is_file()
