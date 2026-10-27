"""The housekeeping `./flow-steward doctor --fix` repairs (the doctor task, D3).

Each repair is one the installer or a lifecycle operation already makes -- release
retention after an update, the W4 rename after a recreate, the fs-cli refresh before a
command -- run here on its own, after the check has found it needed. Idempotent, so an
interrupted --fix leaves what the next doctor names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.application.doctor.contracts import DoctorCheck, DoctorFinding

_GIB = 1024**3


@dataclass(frozen=True)
class RetentionFacts:
    releases: tuple[str, ...] = ()  # release directories beyond the retention
    archives: tuple[str, ...] = ()  # image archives left in kept releases
    ambiguous_images: tuple[str, ...] = ()  # not safe to delete automatically
    kept: tuple[str, ...] = ()
    free_bytes: int = 0
    minimum_free_bytes: int = 0  # what an update needs


class RepairProbe(Protocol):
    profile: str

    def retention(self) -> RetentionFacts: ...

    def prune_releases(self) -> str: ...

    # (container id, the name it should have, a stopped replaced container holding it)
    def temporary_names(self) -> list[tuple[str, str, str]]: ...

    def restore_names(self) -> str: ...

    def cli_runtime_mismatch(self) -> str: ...  # "" when fs-cli is the expected one

    def refresh_cli_runtime(self) -> str: ...

    def orphan_installers(self) -> list[tuple[str, str]]: ...  # (container id, its host pid)

    def installation_lock_held(self) -> bool: ...  # by another run: an orphan still working

    def stop_orphan_installers(self) -> str: ...


def _fix(check_id: str) -> str:
    return f"./flow-steward doctor --fix {check_id}"


def _gib(value: int) -> str:
    return f"{value / _GIB:.1f} GiB"


def _release_retention(probe: RepairProbe) -> DoctorFinding:
    facts = probe.retention()
    superseded = [
        f"{len(values)} {noun}"
        for values, noun in (
            (facts.releases, "releases"),
            (facts.archives, "image archives"),
        )
        if values
    ]
    low = facts.free_bytes < facts.minimum_free_bytes
    space = f"{_gib(facts.free_bytes)} free, an update needs {_gib(facts.minimum_free_bytes)}"
    ambiguity = (
        f" {len(facts.ambiguous_images)} images retained: exclusive installation ownership "
        "is unproven; another installation may need them for rollback."
        if facts.ambiguous_images
        else ""
    )
    if superseded:
        kept = ", ".join(revision[:12] for revision in facts.kept)
        return DoctorFinding(
            "release_retention",
            "fail" if low else "warn",
            cause=f"Beyond the retention: {' and '.join(superseded)} (kept: {kept}); {space}."
            + ambiguity,
            next_step=_fix("release_retention"),
            repairable=True,
        )
    if facts.ambiguous_images:
        return DoctorFinding(
            "release_retention",
            "fail" if low else "warn",
            cause=space + "." + ambiguity,
            next_step="Inspect image use and rollback records of all installations before "
            "manual cleanup; doctor --fix does not delete images with unproven ownership.",
        )
    if low:
        return DoctorFinding(
            "release_retention",
            "fail",
            cause=f"Only {space}, and no release is beyond the retention.",
            next_step="Free disk space on this host (docker system df shows what Docker holds).",
        )
    return DoctorFinding(
        "release_retention", "ok", cause=f"Releases are within the retention; {space}."
    )


def _container_names(probe: RepairProbe) -> DoctorFinding:
    renames = probe.temporary_names()
    if not renames:
        return DoctorFinding("container_names", "ok", cause="Every container has its own name.")
    words = "; ".join(
        f"{container_id[:12]} should be {name}"
        + (f" (the stopped {replaced[:12]} it replaced still holds the name)" if replaced else "")
        for container_id, name, replaced in renames
    )
    return DoctorFinding(
        "container_names",
        "fail",
        cause=f"A killed recreate left temporary names: {words}. "
        f"`docker exec {renames[0][1]}` answers No such container.",
        next_step=_fix("container_names"),
        repairable=True,
    )


def _stale_cli_runtime(probe: RepairProbe) -> DoctorFinding:
    mismatch = probe.cli_runtime_mismatch()
    if not mismatch:
        return DoctorFinding("stale_cli_runtime", "ok", cause="fs-cli is this installation's.")
    return DoctorFinding(
        "stale_cli_runtime",
        "fail",
        cause=f"The retained fs-cli is stale: {mismatch}.",
        next_step=_fix("stale_cli_runtime"),
        repairable=True,
    )


def _orphan_installer(probe: RepairProbe) -> DoctorFinding:
    orphans = probe.orphan_installers()
    if not orphans:
        return DoctorFinding(
            "orphan_installer", "ok", cause="No installer container runs without its ./install.sh."
        )
    words = ", ".join(
        container_id[:12] + (f" (its ./install.sh, process {pid}, is gone)" if pid else "")
        for container_id, pid in orphans
    )
    if probe.installation_lock_held():
        # P3 (live on fs-acc): its ./install.sh was killed, but it went on with the update
        # and finished it; --fix could only have refused.
        return DoctorFinding(
            "orphan_installer",
            "warn",
            cause=f"Installer containers run with no ./install.sh behind them: {words}. "
            "One is still performing the operation (it holds the installation's lock).",
            next_step="Rerun ./flow-steward doctor after it has finished.",
        )
    return DoctorFinding(
        "orphan_installer",
        "fail",
        cause=f"Installer containers run with no ./install.sh behind them: {words}. One can "
        "hold the installation's lock, or wait for input that will never come.",
        next_step=_fix("orphan_installer"),
        repairable=True,
    )


def repair_checks() -> tuple[DoctorCheck, ...]:
    return (
        DoctorCheck(
            "release_retention",
            "Release retention and disk",
            _release_retention,
            lambda probe, _finding: probe.prune_releases(),
        ),
        DoctorCheck(
            "container_names",
            "Container names",
            _container_names,
            lambda probe, _finding: probe.restore_names(),
        ),
        DoctorCheck(
            "stale_cli_runtime",
            "Retained fs-cli",
            _stale_cli_runtime,
            lambda probe, _finding: probe.refresh_cli_runtime(),
            profiles=frozenset({"scalable"}),
        ),
        DoctorCheck(
            "orphan_installer",
            "Installer containers",
            _orphan_installer,
            lambda probe, _finding: probe.stop_orphan_installers(),
        ),
    )
