"""Pure exact-ownership planning and verification for candidate cleanup."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.application.release.candidate_harness import HarnessFailure
from core.application.release.candidate_snapshot import FinalizedCandidateSnapshot

CANDIDATE_SHA_LABEL = "com.flow-steward.candidate.sha"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
OCI_REVISION_LABEL = "org.opencontainers.image.revision"


@dataclass(frozen=True)
class DockerResource:
    """Stable Docker resource identity and ownership labels."""

    identity: str
    name: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class DockerInventory:
    """One point-in-time inventory; resource types remain distinct."""

    containers: tuple[DockerResource, ...] = ()
    networks: tuple[DockerResource, ...] = ()
    volumes: tuple[DockerResource, ...] = ()
    images: tuple[DockerResource, ...] = ()


@dataclass(frozen=True)
class OwnedResourceSet:
    """Exact approved identities that may disappear during cleanup."""

    containers: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupPlan(OwnedResourceSet):
    """Ordered candidate removal plan."""

    release_directories: tuple[Path, ...] = ()


class DockerInventoryPort(Protocol):
    def inventory(self) -> DockerInventoryView: ...

    def remove_containers(self, identities: tuple[str, ...]) -> None: ...

    def remove_networks(self, identities: tuple[str, ...]) -> None: ...

    def remove_volumes(self, identities: tuple[str, ...]) -> None: ...

    def remove_images(self, identities: tuple[str, ...]) -> None: ...


class DockerResourceView(Protocol):
    @property
    def identity(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def labels(self) -> Mapping[str, str]: ...


class DockerInventoryView(Protocol):
    @property
    def containers(self) -> tuple[DockerResourceView, ...]: ...

    @property
    def networks(self) -> tuple[DockerResourceView, ...]: ...

    @property
    def volumes(self) -> tuple[DockerResourceView, ...]: ...

    @property
    def images(self) -> tuple[DockerResourceView, ...]: ...


def plan_cleanup(
    snapshot: FinalizedCandidateSnapshot,
    inventory: DockerInventoryView,
    *,
    preserve_sha: str | None,
    release_root: Path | None = None,
    external_evidence_verified: bool = False,
) -> CleanupPlan:
    """Select only recorded resources satisfying the full owner contract."""

    candidate_sha = snapshot.expected.candidate_commit
    if preserve_sha == candidate_sha:
        return CleanupPlan()
    project = snapshot.expected.compose_project
    recorded = snapshot.resource_identities
    return CleanupPlan(
        containers=_select_owned(
            inventory.containers,
            recorded.get("containers", ()),
            candidate_sha=candidate_sha,
            compose_project=project,
            require_compose=True,
            compose_working_dir=snapshot.compose_working_dir,
            compose_config_files=snapshot.compose_config_files,
        ),
        networks=_select_owned(
            inventory.networks,
            recorded.get("networks", ()),
            candidate_sha=candidate_sha,
            compose_project=project,
            require_compose=True,
            compose_working_dir=snapshot.compose_working_dir,
            compose_config_files=snapshot.compose_config_files,
        ),
        volumes=_select_owned(
            inventory.volumes,
            recorded.get("volumes", ()),
            candidate_sha=candidate_sha,
            compose_project=project,
            require_compose=True,
            compose_working_dir=snapshot.compose_working_dir,
            compose_config_files=snapshot.compose_config_files,
        ),
        images=_select_owned(
            inventory.images,
            recorded.get("images", ()),
            candidate_sha=candidate_sha,
            compose_project=project,
            require_compose=False,
            compose_working_dir=None,
            compose_config_files=None,
        ),
        release_directories=_release_directories(
            candidate_sha,
            release_root=release_root,
            external_evidence_verified=external_evidence_verified,
        ),
    )


def execute_cleanup(
    plan: CleanupPlan,
    *,
    gateway: DockerInventoryPort,
    remove_release_directory: Callable[[Path], None] | None = None,
) -> DockerInventoryView:
    """Remove approved resources in dependency order and verify the exact delta."""

    if plan.release_directories and remove_release_directory is None:
        raise HarnessFailure("candidate release directory removal is not configured")
    before = gateway.inventory()
    gateway.remove_containers(plan.containers)
    gateway.remove_networks(plan.networks)
    gateway.remove_volumes(plan.volumes)
    gateway.remove_images(plan.images)
    after = gateway.inventory()
    verify_inventory_delta(before, after, plan)
    for release_directory in plan.release_directories:
        assert remove_release_directory is not None
        remove_release_directory(release_directory)
        if release_directory.exists():
            raise HarnessFailure(
                f"candidate cleanup left release directory residue: {release_directory.name}"
            )
    return after


def verify_inventory_delta(
    before: DockerInventoryView,
    after: DockerInventoryView,
    approved: OwnedResourceSet,
) -> None:
    """Prove only approved resources disappeared and no approved residue remains."""

    unrelated_changes: list[str] = []
    residue: list[str] = []
    for kind in ("containers", "networks", "volumes", "images"):
        before_ids = {resource.identity for resource in getattr(before, kind)}
        after_ids = {resource.identity for resource in getattr(after, kind)}
        approved_ids = set(getattr(approved, kind))
        changed_outside_contract = (before_ids - after_ids) - approved_ids
        additions = after_ids - before_ids
        if changed_outside_contract or additions:
            unrelated_changes.extend(
                f"{kind}:{identity}" for identity in sorted(changed_outside_contract | additions)
            )
        residue.extend(
            f"{kind}:{identity}" for identity in sorted(approved_ids.intersection(after_ids))
        )
    if unrelated_changes:
        raise HarnessFailure(
            "unrelated Docker resources changed during cleanup: " + ", ".join(unrelated_changes)
        )
    if residue:
        raise HarnessFailure("candidate cleanup left owned residue: " + ", ".join(residue))


def _select_owned(
    resources: tuple[DockerResourceView, ...],
    recorded_identities: tuple[str, ...],
    *,
    candidate_sha: str,
    compose_project: str,
    require_compose: bool,
    compose_working_dir: str | None,
    compose_config_files: str | None,
) -> tuple[str, ...]:
    recorded = set(recorded_identities)
    selected: list[str] = []
    for resource in resources:
        if resource.identity not in recorded and resource.name not in recorded:
            continue
        if require_compose and resource.labels.get(COMPOSE_PROJECT_LABEL) != compose_project:
            continue
        if not _compose_origin_matches(
            resource,
            working_dir=compose_working_dir,
            config_files=compose_config_files,
        ):
            continue
        revision = resource.labels.get(CANDIDATE_SHA_LABEL)
        if not require_compose:
            revision = revision or resource.labels.get(OCI_REVISION_LABEL)
        if revision is not None and revision != candidate_sha:
            continue
        if not require_compose and revision != candidate_sha:
            continue
        selected.append(resource.identity)
    return tuple(sorted(selected))


def _compose_origin_matches(
    resource: DockerResourceView,
    *,
    working_dir: str | None,
    config_files: str | None,
) -> bool:
    observed_working_dir = resource.labels.get("com.docker.compose.project.working_dir")
    if working_dir is not None and observed_working_dir not in {None, working_dir}:
        return False
    observed_config_files = resource.labels.get("com.docker.compose.project.config_files")
    return config_files is None or observed_config_files in {None, config_files}


def _release_directories(
    candidate_sha: str,
    *,
    release_root: Path | None,
    external_evidence_verified: bool,
) -> tuple[Path, ...]:
    directories: list[Path] = []
    if release_root is None or not external_evidence_verified:
        return tuple(directories)
    resolved_root = release_root.resolve()
    candidate_directory = (resolved_root / candidate_sha).resolve()
    if candidate_directory.parent != resolved_root or candidate_directory.name != candidate_sha:
        raise HarnessFailure("candidate release directory is outside the exact release root")
    directories.append(candidate_directory)
    return tuple(directories)
