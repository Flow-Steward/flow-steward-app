from __future__ import annotations

from pathlib import Path

import pytest

from core.application.release.candidate_harness import (
    HarnessFailure,
    InputFailure,
)
from core.application.release.candidate_preflight import run_static_preflight
from core.application.release.candidate_requirements import AggregatedRequirements
from core.application.release.candidate_snapshot import (
    ExpectedCandidateSnapshot,
    SnapshotComparison,
)
from core.infrastructure.release_candidate.host_gateway import (
    EndpointAllocator,
    EndpointUnavailableError,
)


class RecordingHost:
    def __init__(self) -> None:
        self.docker_calls = 0

    def current_commit(self) -> str:
        return "a" * 40

    def tracked_tree_clean(self) -> bool:
        return True

    def docker_storage_readable(self) -> bool:
        self.docker_calls += 1
        return True


def _requirements(*required_env: str) -> AggregatedRequirements:
    return AggregatedRequirements(
        check_ids=("A01",),
        groups=("A",),
        required_env=tuple(required_env),
        optional_env=(),
        services=("fs-app",),
        scopes=(),
        mutable_resources=(),
        cleanup_obligations=(),
    )


def _snapshot(*, token_presence: str = "present") -> ExpectedCandidateSnapshot:
    return ExpectedCandidateSnapshot(
        candidate_commit="a" * 40,
        compose_project="fs-candidate-aaaaaaaaaaaa",
        source_hashes={"environment": "sha256:" + "1" * 64},
        input_presence={"FS_SMOKE_BEARER_TOKEN": token_presence},
        protected_inputs={},
        plain_inputs={"APP_PORT": "18099"},
        requirements_digest="sha256:" + "2" * 64,
        image_fingerprints={},
        fingerprint="sha256:" + "3" * 64,
    )


def test_missing_required_input_stops_before_docker_inspection() -> None:
    host = RecordingHost()

    _raises_input_68_1 = _snapshot(token_presence="missing")
    _raises_input_68_2 = _requirements("FS_SMOKE_BEARER_TOKEN")
    _raises_input_68_3 = SnapshotComparison(matches=True, reasons=())
    with pytest.raises(InputFailure, match="FS_SMOKE_BEARER_TOKEN"):
        run_static_preflight(
            _raises_input_68_1,
            requirements=_raises_input_68_2,
            host=host,
            snapshot_comparison=_raises_input_68_3,
        )

    assert host.docker_calls == 0


def test_two_candidate_allocations_never_share_a_port(tmp_path: Path) -> None:
    allocator = EndpointAllocator(tmp_path, bind_probe=lambda _port: True, first_port=18000)

    first = allocator.allocate("a" * 40, count=3)
    second = allocator.allocate("b" * 40, count=3)

    assert set(first).isdisjoint(second)
    assert first == (18000, 18001, 18002)
    assert second == (18003, 18004, 18005)


def test_stale_snapshot_fails_after_static_host_checks() -> None:
    host = RecordingHost()

    _raises_input_93_1 = _snapshot()
    _raises_input_93_2 = _requirements()
    _raises_input_93_3 = SnapshotComparison(
        matches=False, reasons=("protected input changed: TOKEN",)
    )
    with pytest.raises(HarnessFailure, match="protected input changed: TOKEN"):
        run_static_preflight(
            _raises_input_93_1,
            requirements=_raises_input_93_2,
            host=host,
            snapshot_comparison=_raises_input_93_3,
        )

    assert host.docker_calls == 1


def test_explicit_port_owned_by_another_candidate_is_rejected(tmp_path: Path) -> None:
    allocator = EndpointAllocator(tmp_path, bind_probe=lambda _port: True)
    allocator.allocate("a" * 40, count=1, explicit_ports=(19000,))

    with pytest.raises(EndpointUnavailableError, match="owned by another candidate"):
        allocator.allocate("b" * 40, count=1, explicit_ports=(19000,))


def test_endpoint_release_removes_only_the_exact_candidate_claim(tmp_path: Path) -> None:
    allocator = EndpointAllocator(tmp_path, bind_probe=lambda _port: True)
    first = allocator.allocate("a" * 40, count=1)
    second = allocator.allocate("b" * 40, count=1)

    allocator.release("a" * 40)

    assert allocator.allocate("b" * 40, count=1) == second
    assert allocator.allocate("c" * 40, count=1) == first
