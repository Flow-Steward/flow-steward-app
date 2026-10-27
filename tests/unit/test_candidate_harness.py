from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.application.release.candidate_harness import (
    CandidateHarness,
    CandidateRunEvidence,
    CandidateRunIdentity,
    FailureClass,
    HarnessFailure,
    InfrastructureFailure,
    InputFailure,
    PhaseEvidence,
    PhaseStatus,
    ProductFailure,
    phase,
)
from core.application.release.candidate_store import CandidateEvidenceStore


class RecordingEvidenceStore:
    def __init__(self) -> None:
        self.writes = []

    def write(self, evidence: object) -> None:
        self.writes.append(evidence)


def _identity() -> CandidateRunIdentity:
    return CandidateRunIdentity(
        candidate_commit="a" * 40,
        fingerprint="sha256:" + "b" * 64,
        compose_project="fs-candidate-aaaaaaaaaaaa",
        run_id="run-001",
        image_digests={"fs-app": "sha256:" + "c" * 64},
    )


def _clock(*values: float):
    iterator = iter(values)
    return lambda: next(iterator)


def test_first_failed_phase_stops_dependent_phases() -> None:
    calls: list[str] = []

    def fail_preflight() -> None:
        raise ProductFailure("bad wiring")

    result = CandidateHarness(identity=_identity(), clock=_clock(1.0, 1.5, 10.0, 12.5)).run(
        (
            phase("snapshot", lambda: calls.append("snapshot")),
            phase("preflight", fail_preflight, check_id="A01"),
            phase("build", lambda: calls.append("build")),
        )
    )

    assert calls == ["snapshot"]
    assert result.phases[-1].failure_class is FailureClass.PRODUCT
    assert result.phases[-1].duration_seconds == 2.5
    assert result.phases[-1].check_id == "A01"


def test_started_phase_is_persisted_as_incomplete_before_execution() -> None:
    store = RecordingEvidenceStore()

    def interrupt() -> None:
        raise KeyboardInterrupt

    _raises_callable_74_1 = CandidateHarness(identity=_identity(), store=store).run
    _raises_input_74_2 = (phase("preflight", interrupt),)
    with pytest.raises(KeyboardInterrupt):
        _raises_callable_74_1(_raises_input_74_2)

    assert store.writes[0].phases[-1].status is PhaseStatus.INCOMPLETE
    assert store.writes[-1].phases[-1].status is PhaseStatus.INCOMPLETE
    assert store.writes[-1].phases[-1].failure_class is FailureClass.INCOMPLETE


@pytest.mark.parametrize(
    ("failure_type", "expected_class"),
    (
        (InputFailure, FailureClass.INPUT),
        (HarnessFailure, FailureClass.HARNESS),
        (InfrastructureFailure, FailureClass.INFRASTRUCTURE),
        (ProductFailure, FailureClass.PRODUCT),
    ),
)
def test_typed_failures_keep_their_operator_classification(
    failure_type: type[RuntimeError],
    expected_class: FailureClass,
) -> None:
    def fail() -> None:
        raise failure_type("safe reason")

    result = CandidateHarness(identity=_identity()).run((phase("preflight", fail),))

    assert result.phases[-1].status is PhaseStatus.FAILED
    assert result.phases[-1].failure_class is expected_class
    assert result.phases[-1].reason == "safe reason"


def test_external_evidence_manifest_and_log_remain_checksummed(tmp_path: Path) -> None:
    source_log = tmp_path / "preflight.log"
    source_log.write_text("safe diagnostic\n", encoding="utf-8")
    evidence = CandidateHarness(identity=_identity()).run(
        (phase("preflight", lambda: (source_log,)),)
    )

    run_dir = CandidateEvidenceStore(tmp_path / "logs/release/evidence").write(evidence)

    manifest = run_dir / "manifest.json"
    sidecar = run_dir / "manifest.sha256"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    retained_log = run_dir / payload["artifacts"][0]["path"]
    assert retained_log.read_text(encoding="utf-8") == "safe diagnostic\n"
    assert (
        payload["artifacts"][0]["sha256"] == hashlib.sha256(retained_log.read_bytes()).hexdigest()
    )
    assert (
        sidecar.read_text(encoding="utf-8").strip()
        == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )


def test_failed_phase_retains_checksummed_diagnostic_log(tmp_path: Path) -> None:
    source_log = tmp_path / "release-smoke.log"
    source_log.write_text("72 PASS / 1 FAIL\n", encoding="utf-8")
    store = CandidateEvidenceStore(tmp_path / "logs/release/evidence")

    def fail() -> None:
        raise ProductFailure("release smoke failed", log_paths=(source_log,))

    evidence = CandidateHarness(identity=_identity(), store=store).run(
        (phase("release-profile-1", fail, check_id="release"),)
    )

    assert evidence.phases[0].status is PhaseStatus.FAILED
    assert evidence.phases[0].log_paths == (source_log,)
    assert store.has_verified_evidence(_identity().candidate_commit, run_id="run-001") is True


def test_interrupted_phase_retains_checksummed_diagnostic_log(tmp_path: Path) -> None:
    source_log = tmp_path / "release-smoke.log"
    source_log.write_text("interrupted after D08\n", encoding="utf-8")
    store = CandidateEvidenceStore(tmp_path / "logs/release/evidence")

    class Interrupted(KeyboardInterrupt):
        log_paths = (source_log,)

    def interrupt() -> None:
        raise Interrupted

    _raises_callable_156_1 = CandidateHarness(identity=_identity(), store=store).run
    _raises_input_156_2 = (phase("release-profile-1", interrupt, check_id="release"),)
    with pytest.raises(Interrupted):
        _raises_callable_156_1(_raises_input_156_2)

    assert store.has_verified_evidence(_identity().candidate_commit, run_id="run-001") is True
    manifest = (
        tmp_path
        / "logs/release/evidence"
        / _identity().candidate_commit
        / "run-001"
        / "manifest.json"
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["phases"][0]["status"] == "incomplete"
    assert payload["artifacts"][0]["phase"] == "release-profile-1"


def test_evidence_verification_is_bound_to_exact_run_and_fails_closed_on_tamper(
    tmp_path: Path,
) -> None:
    source_log = tmp_path / "safe.log"
    source_log.write_text("safe profile passed\n", encoding="utf-8")
    store = CandidateEvidenceStore(tmp_path / "logs/release/evidence")
    evidence = CandidateHarness(identity=_identity()).run(
        (phase("safe-profile", lambda: source_log),)
    )
    run_dir = store.write(evidence)

    assert store.has_verified_evidence(_identity().candidate_commit, run_id="run-001") is True
    assert store.has_verified_evidence(_identity().candidate_commit, run_id="other-run") is False

    retained_log = next((run_dir / "artifacts").iterdir())
    retained_log.write_text("tampered\n", encoding="utf-8")
    assert store.has_verified_evidence(_identity().candidate_commit, run_id="run-001") is False


def test_evidence_verification_rejects_manifest_with_phase_still_running(
    tmp_path: Path,
) -> None:
    source_log = tmp_path / "release-1.log"
    source_log.write_text("release profile 1 passed\n", encoding="utf-8")
    evidence = CandidateRunEvidence(
        identity=_identity(),
        phases=(
            PhaseEvidence(
                name="release-profile-1",
                status=PhaseStatus.PASSED,
                duration_seconds=1.0,
                failure_class=None,
                reason=None,
                check_id="release",
                log_paths=(source_log,),
            ),
            PhaseEvidence(
                name="release-profile-2",
                status=PhaseStatus.INCOMPLETE,
                duration_seconds=None,
                failure_class=FailureClass.INCOMPLETE,
                reason=None,
                check_id="release",
            ),
        ),
    )
    store = CandidateEvidenceStore(tmp_path / "logs/release/evidence")
    store.write(evidence)

    assert store.has_verified_evidence(_identity().candidate_commit, run_id="run-001") is False
