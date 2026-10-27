from __future__ import annotations

import json
import os
import re
import subprocess
import uuid

from core.application.release.candidate_cleanup import execute_cleanup, plan_cleanup
from core.application.release.candidate_snapshot import (
    ExpectedCandidateSnapshot,
    FinalizedCandidateSnapshot,
)
from core.infrastructure.release_candidate.docker_gateway import DockerCliGateway


def _run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=check)


def _tool_image() -> str:
    image = os.environ.get("FS_TEST_AUDIT_IMAGE_ID", "")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise RuntimeError(
            "FS_TEST_AUDIT_IMAGE_ID requires a full immutable ID from canonical lint.sh"
        )
    try:
        observed = _run("docker", "image", "inspect", "--format", "{{.Id}}", image)
    except subprocess.CalledProcessError as error:
        raise RuntimeError("FS_TEST_AUDIT_IMAGE_ID is unavailable locally") from error
    if observed.stdout.strip() != image:
        raise RuntimeError("FS_TEST_AUDIT_IMAGE_ID does not match the local immutable image")
    return image


def _cleanup_owned(containers: list[str], networks: list[str]) -> None:
    failures = []
    for kind, identities in (("container", containers), ("network", networks)):
        for identity in identities:
            prefix = ("docker",) if kind == "container" else ("docker", "network")
            flags = ("--force",) if kind == "container" else ()
            removed = _run(*prefix, "rm", *flags, identity, check=False)
            absent = _run(*prefix, "inspect", identity, check=False)
            print(
                json.dumps(
                    {
                        "cleanup_kind": kind,
                        "identity": identity,
                        "remove_exit": removed.returncode,
                        "remove_stderr": removed.stderr,
                        "absence_exit": absent.returncode,
                        "absence_stderr": absent.stderr,
                    }
                )
            )
            removed_missing = any(
                word in removed.stderr.lower() for word in ("no such", "not found")
            )
            absent_missing = any(word in absent.stderr.lower() for word in ("no such", "not found"))
            if (removed.returncode and not removed_missing) or not (
                absent.returncode and absent_missing
            ):
                failures.append(f"{kind} {identity}")
    if failures:
        raise RuntimeError("Owned Docker cleanup failed: " + ", ".join(failures))


def _finalized(
    *,
    sha: str,
    project: str,
    container_id: str,
    network_id: str,
) -> FinalizedCandidateSnapshot:
    expected = ExpectedCandidateSnapshot(
        candidate_commit=sha,
        compose_project=project,
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
        resource_identities={
            "containers": (container_id,),
            "networks": (network_id,),
        },
    )


def test_cleanup_removes_only_one_exact_disposable_candidate() -> None:
    image = _tool_image()
    token = uuid.uuid4().hex
    first_sha = "a" * 40
    second_sha = "b" * 40
    first_project = f"fs-cleanup-test-{token[:10]}-a"
    second_project = f"fs-cleanup-test-{token[:10]}-b"
    created_containers: list[str] = []
    created_networks: list[str] = []
    try:
        for project, sha in ((first_project, first_sha), (second_project, second_sha)):
            network_id = _run(
                "docker",
                "network",
                "create",
                "--label",
                f"com.docker.compose.project={project}",
                "--label",
                f"com.flow-steward.candidate.sha={sha}",
                f"{project}-network",
            ).stdout.strip()
            created_networks.append(network_id)
            container_id = _run(
                "docker",
                "create",
                "--pull=never",
                "--label",
                f"com.docker.compose.project={project}",
                "--label",
                f"com.flow-steward.candidate.sha={sha}",
                "--network",
                network_id,
                "--entrypoint",
                "true",
                image,
            ).stdout.strip()
            created_containers.append(container_id)

        gateway = DockerCliGateway()
        before = gateway.inventory()
        plan = plan_cleanup(
            _finalized(
                sha=first_sha,
                project=first_project,
                container_id=created_containers[0],
                network_id=created_networks[0],
            ),
            before,
            preserve_sha=None,
        )

        execute_cleanup(plan, gateway=gateway)

        assert _run("docker", "inspect", created_containers[0], check=False).returncode != 0
        assert (
            _run("docker", "network", "inspect", created_networks[0], check=False).returncode != 0
        )
        assert _run("docker", "inspect", created_containers[1], check=False).returncode == 0
        assert (
            _run("docker", "network", "inspect", created_networks[1], check=False).returncode == 0
        )
    finally:
        _cleanup_owned(created_containers, created_networks)
