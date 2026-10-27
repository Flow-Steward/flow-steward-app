"""The housekeeping doctor --fix repairs (D3): each check from both sides, and the reads
behind them decided by the same code the installer's own repair uses."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from core.application.doctor import repairs
from core.application.doctor.contracts import DoctorFinding
from core.application.doctor.repairs import RetentionFacts
from core.application.installer import InstallConfig
from core.infrastructure.installer import release_retention
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_GIB = 1024**3


@dataclass
class _Probe:
    profile: str = "scalable"
    facts: RetentionFacts = field(
        default_factory=lambda: RetentionFacts(free_bytes=20 * _GIB, minimum_free_bytes=4 * _GIB)
    )
    renames: list[tuple[str, str, str]] = field(default_factory=list)
    mismatch: str = ""

    def retention(self) -> RetentionFacts:
        return self.facts

    def temporary_names(self) -> list[tuple[str, str, str]]:
        return self.renames

    def cli_runtime_mismatch(self) -> str:
        return self.mismatch


def _diagnose(check_id: str, probe: _Probe) -> DoctorFinding:
    (check,) = [check for check in repairs.repair_checks() if check.check_id == check_id]
    assert check.repair is not None
    return check.diagnose(probe)


def test_releases_beyond_the_retention_are_a_repair() -> None:
    facts = RetentionFacts(
        releases=("a" * 40,),
        ambiguous_images=("flow-steward-app:candidate-aaaaaaaaaaaa",),
        kept=("b" * 40, "c" * 40),
        free_bytes=20 * _GIB,
        minimum_free_bytes=4 * _GIB,
    )

    finding = _diagnose("release_retention", _Probe(facts=facts))

    assert finding.status == "warn"
    assert finding.repairable
    assert "1 releases (kept: bbbbbbbbbbbb, cccccccccccc)" in finding.cause
    assert "1 images retained: exclusive installation ownership is unproven" in finding.cause
    assert finding.next_step == "./flow-steward doctor --fix release_retention"


def test_low_disk_with_releases_to_remove_fails_and_without_them_is_a_hint() -> None:
    low = {"free_bytes": 2 * _GIB, "minimum_free_bytes": 4 * _GIB}

    prunable = _diagnose(
        "release_retention", _Probe(facts=RetentionFacts(archives=("a" * 40,), **low))
    )
    nothing = _diagnose("release_retention", _Probe(facts=RetentionFacts(**low)))

    assert (prunable.status, prunable.repairable) == ("fail", True)
    assert (nothing.status, nothing.repairable) == ("fail", False)
    assert "2.0 GiB free, an update needs 4.0 GiB" in nothing.cause


def test_releases_within_the_retention_pass() -> None:
    assert _diagnose("release_retention", _Probe()).status == "ok"


def test_temporary_container_names_are_a_repair_and_own_names_pass() -> None:
    renamed = _Probe(renames=[("a1b2c3d4e5f6" + "0" * 52, "fs-ext-fs-app-1", "")])

    finding = _diagnose("container_names", renamed)

    assert finding.repairable
    assert "a1b2c3d4e5f6 should be fs-ext-fs-app-1" in finding.cause
    assert _diagnose("container_names", _Probe()).status == "ok"


def test_a_stale_fs_cli_is_a_repair_on_scalable_only() -> None:
    (check,) = [c for c in repairs.repair_checks() if c.check_id == "stale_cli_runtime"]

    finding = check.diagnose(
        _Probe(mismatch="the fs-cli image is at aaaa, the installation at bbbb")
    )

    assert check.profiles == frozenset({"scalable"})  # Compact's fs-cli is a one-shot
    assert finding.repairable
    assert "aaaa" in finding.cause
    assert check.diagnose(_Probe()).status == "ok"


# -- the reads: the installer's own decisions -------------------------------------------------


class _Process:
    def __init__(self, answers: dict[tuple[str, ...], str]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def run(self, command, **_kwargs):
        self.commands.append(list(command))
        for prefix, out in self.answers.items():
            if tuple(command[: len(prefix)]) == prefix:
                return ProcessOutcome(command=tuple(command), returncode=0, stdout=out)
        return ProcessOutcome(command=tuple(command), returncode=0, stdout="")


def _gateway(
    root: Path, answers: dict[tuple[str, ...], str], project: str = "fs-ext"
) -> DockerInstallerGateway:
    (root / "infra/docker").mkdir(parents=True, exist_ok=True)
    return DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward", lifecycle_state_root=root),
        process=_Process(answers),
        environment={"FS_COMPOSE_PROJECT_NAME": project},
    )


def _releases(root: Path, *revisions: str) -> Path:
    release_root = root / ".flow-steward/release"
    for age, revision in enumerate(revisions):
        path = release_root / revision
        path.mkdir(parents=True)
        (path / "candidate-images.tar").write_bytes(b"x" * 10)
        os.utime(path, (1_000_000 - age, 1_000_000 - age))  # the first is the newest
    return release_root


def test_what_doctor_reports_is_what_prune_then_removes(tmp_path: Path) -> None:
    current, previous, older = "c" * 40, "b" * 40, "a" * 40
    _releases(tmp_path, current, previous, older)
    tags = f"flow-steward-app:candidate-{older[:12]}\nflow-steward-app:candidate-{current[:12]}\n"
    gateway = _gateway(tmp_path, {tuple(release_retention.CANDIDATE_IMAGE_LISTING): tags})
    config = InstallConfig(environment="production", deployment_profile="scalable")

    plan = gateway.superseded_releases(config, current=current)

    assert plan.releases == ()  # a shared image keeps its associated record
    assert plan.ambiguous_images == (f"flow-steward-app:candidate-{older[:12]}",)
    assert set(plan.archives) == {current, previous, older}
    assert (tmp_path / ".flow-steward/release" / older).is_dir()  # a read removes nothing

    summary = gateway.prune_superseded_releases(
        config, current=current, previous="", log=gateway.state_store.open_log("doctor-test")
    )

    assert "Removed image archives" in summary
    assert "ownership" in summary
    assert (tmp_path / ".flow-steward/release" / older).is_dir()
    remaining = gateway.superseded_releases(config, current=current)
    assert remaining.kept == plan.kept
    assert remaining.ambiguous_images == plan.ambiguous_images
    assert not remaining.releases
    assert not remaining.archives


def test_compact_has_no_release_retention(tmp_path: Path) -> None:
    _releases(tmp_path, "a" * 40, "b" * 40, "c" * 40)
    gateway = _gateway(tmp_path, {})

    plan = gateway.superseded_releases(
        InstallConfig(environment="production", deployment_profile="compact"), current=""
    )

    assert not plan.any


# A Docker container ID (hex), not a secret.
_RUNNING_UNDER_TEMPORARY_NAME = "5f3e2d1c0b9a"  # pragma: allowlist secret


def test_the_fs_acc_state_is_found_the_runtime_running_under_a_temporary_name(
    tmp_path: Path,
) -> None:
    """Live on fs-acc (2026-09-25, D3a): the runtime ran as
    "ad9cfacdf781_fs-acc-compact-runtime-1", the canonical container was gone, and
    container_names said every container had its own name."""
    listing = (
        "5f3e2d1c0b9a\tad9cfacdf781_fs-acc-compact-runtime-1\trunning\n"
        "ffffffffffff\tfs-acc-pg-fs-1\trunning\n"
    )
    gateway = _gateway(tmp_path, {("docker", "ps", "--all"): listing}, project="fs-acc")

    repairs_found = gateway.temporary_container_names(InstallConfig(environment="production"))

    assert [(r.container_id, r.name, r.replaced_id) for r in repairs_found] == [
        (_RUNNING_UNDER_TEMPORARY_NAME, "fs-acc-compact-runtime-1", "")
    ]
    assert "label=com.docker.compose.project=fs-acc" in gateway.process.commands[0]


def test_canonical_names_pass(tmp_path: Path) -> None:
    listing = "5f3e2d1c0b9a\tfs-ext-fs-app-1\trunning\n"
    gateway = _gateway(tmp_path, {("docker", "ps", "--all"): listing})

    assert gateway.temporary_container_names(InstallConfig(environment="production")) == ()


_OLD_IMAGE = "sha256:" + "0" * 64
_NEW_IMAGE = "sha256:" + "1" * 64


def _update_gateway(tmp_path: Path, container_images: str) -> DockerInstallerGateway:
    (tmp_path / ".env.prod").write_text(f"FS_COMPACT_IMAGE={_NEW_IMAGE}\n", encoding="utf-8")
    return _gateway(
        tmp_path,
        {
            ("docker", "ps", "-a"): "2d589e15b668\n9c7b7a7eead0\n",
            ("docker", "container", "inspect"): container_images,
        },
        project="fs-acc",
    )


_COMPACT = InstallConfig(
    environment="production", deployment_profile="compact", data_placement="compact_volume"
)


def test_an_update_killed_after_recreate_is_the_recorded_image_not_being_the_runtimes(
    tmp_path: Path,
) -> None:
    """The fs-acc state (F1): the old runtime exited under its name, the new one created."""
    gateway = _update_gateway(tmp_path, f"{_OLD_IMAGE}\n{_NEW_IMAGE}\n")

    assert gateway.interrupted_update(_COMPACT) == (
        "compact-runtime is not on the image .env.prod records for it"
    )


def test_a_runtime_on_its_recorded_image_is_no_interrupted_update(tmp_path: Path) -> None:
    gateway = _update_gateway(tmp_path, f"{_NEW_IMAGE}\n{_NEW_IMAGE}\n")

    assert gateway.interrupted_update(_COMPACT) == ""


def test_the_volume_is_never_created_by_reading_it(tmp_path: Path) -> None:
    import pytest

    (tmp_path / ".env.prod").write_text(f"FS_COMPACT_IMAGE={_NEW_IMAGE}\n", encoding="utf-8")
    gateway = _gateway(tmp_path, {}, project="fs-acc")
    gateway.process.run = lambda command, **_kw: ProcessOutcome(  # every read fails
        command=tuple(command), returncode=1, stdout=""
    )

    with pytest.raises(OSError, match="fs-acc_fs-compact-data does not exist"):
        gateway.read_compact_volume(_COMPACT, "print(1)")
