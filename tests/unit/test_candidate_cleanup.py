from __future__ import annotations

import pytest

from core.application.release.candidate_cleanup import (
    DockerInventory,
    DockerResource,
    OwnedResourceSet,
    plan_cleanup,
    verify_inventory_delta,
)
from core.application.release.candidate_harness import HarnessFailure
from core.application.release.candidate_snapshot import (
    ExpectedCandidateSnapshot,
    FinalizedCandidateSnapshot,
)

SHA = "a" * 40
PROJECT = "fs-candidate-aaaaaaaaaaaa"
SHA_LABEL = "com.flow-steward.candidate.sha"
PROJECT_LABEL = "com.docker.compose.project"


def _resource(
    identity: str,
    *,
    sha: str = SHA,
    project: str = PROJECT,
) -> DockerResource:
    return DockerResource(
        identity=identity,
        name=identity,
        labels={SHA_LABEL: sha, PROJECT_LABEL: project},
    )


def _inventory(*containers: DockerResource) -> DockerInventory:
    return DockerInventory(containers=containers)


def _finalized(*container_ids: str) -> FinalizedCandidateSnapshot:
    expected = ExpectedCandidateSnapshot(
        candidate_commit=SHA,
        compose_project=PROJECT,
        source_hashes={},
        input_presence={},
        protected_inputs={},
        plain_inputs={},
        requirements_digest="sha256:" + "1" * 64,
        image_fingerprints={},
        fingerprint="sha256:" + "2" * 64,
    )
    return FinalizedCandidateSnapshot(
        expected=expected,
        image_digests={},
        resource_identities={"containers": container_ids},
    )


def test_cleanup_selects_only_resources_matching_full_owner_contract() -> None:
    inventory = _inventory(
        _resource("candidate-app"),
        _resource("wrong-sha", sha="b" * 40),
        _resource("wrong-project", project="other"),
    )

    plan = plan_cleanup(
        _finalized("candidate-app", "wrong-sha", "wrong-project"),
        inventory,
        preserve_sha=None,
    )

    assert plan.containers == ("candidate-app",)


def test_cleanup_resolves_recorded_compose_name_to_runtime_container_id() -> None:
    container_name = f"{PROJECT}-fs-cli-1"
    inventory = DockerInventory(
        containers=(
            DockerResource(
                identity="sha256:runtime-container-id",
                name=container_name,
                labels={PROJECT_LABEL: PROJECT},
            ),
        )
    )

    plan = plan_cleanup(
        _finalized(container_name),
        inventory,
        preserve_sha=None,
    )

    assert plan.containers == ("sha256:runtime-container-id",)


def test_after_inventory_may_change_only_by_approved_owned_set() -> None:
    candidate = _resource("candidate-app")
    unrelated = _resource("dev-db", sha="b" * 40, project="dev")
    before = _inventory(candidate, unrelated)
    after = _inventory(unrelated)

    verify_inventory_delta(
        before,
        after,
        OwnedResourceSet(containers=("candidate-app",)),
    )


def test_cleanup_delta_rejects_unrelated_resource_removal() -> None:
    candidate = _resource("candidate-app")
    unrelated = _resource("dev-db", sha="b" * 40, project="dev")

    _raises_input_114_1 = _inventory(candidate, unrelated)
    _raises_input_114_2 = _inventory()
    _raises_input_114_3 = OwnedResourceSet(containers=("candidate-app",))
    with pytest.raises(HarnessFailure, match="unrelated Docker resources changed"):
        verify_inventory_delta(_raises_input_114_1, _raises_input_114_2, _raises_input_114_3)


def test_release_directory_requires_verified_external_evidence(tmp_path) -> None:
    unverified = plan_cleanup(
        _finalized(),
        _inventory(),
        preserve_sha=None,
        release_root=tmp_path,
        external_evidence_verified=False,
    )
    verified = plan_cleanup(
        _finalized(),
        _inventory(),
        preserve_sha=None,
        release_root=tmp_path,
        external_evidence_verified=True,
    )

    assert unverified.release_directories == ()
    assert verified.release_directories == (tmp_path / SHA,)


def test_cleanup_selects_exact_candidate_image_tag_not_shared_digest() -> None:
    candidate_tag = "flow-steward:candidate-aaaaaaaaaaaa"
    shared_tag = "flow-steward:latest"
    labels = {"org.opencontainers.image.revision": SHA}
    inventory = DockerInventory(
        images=(
            DockerResource(candidate_tag, candidate_tag, labels),
            DockerResource(shared_tag, shared_tag, labels),
        )
    )
    finalized = _finalized()
    finalized = FinalizedCandidateSnapshot(
        expected=finalized.expected,
        image_digests={},
        resource_identities={"images": (candidate_tag,)},
    )

    plan = plan_cleanup(finalized, inventory, preserve_sha=None)

    assert plan.images == (candidate_tag,)
