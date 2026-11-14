from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.application.tooling.quality import backend_runtime
from core.application.tooling.quality.check_registry import load_check_registry
from core.application.tooling.quality.evidence import EvidenceStore
from core.application.tooling.quality.fingerprint import FingerprintContext, fingerprint_check
from core.application.tooling.quality.release_gate import _quality_evidence_failures
from core.application.tooling.quality.verify import VerificationRequest, verify

ROOT = Path(__file__).resolve().parents[4]


def _backend_registry():
    registry = load_check_registry(ROOT / "infra/tooling/quality/check-registry.json")
    # Keep the real check definitions and prerequisite owners. Unrelated
    # frontend runtime probes are outside this regression.
    return registry.model_copy(
        update={
            "checks": tuple(
                check
                for check in registry.checks
                if check.id in {"backend-tests", "backend-frontend-contracts", "ruff", "mypy"}
            )
        }
    )


class RecordingRunner:
    def __init__(self, results: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self.results = results or {}

    def run(self, check, *, timeout_seconds: int, extra_env=None) -> int:
        self.calls.append(check.id)
        return self.results.get(check.id, 0)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=root, text=True).strip()


def _commit(root: Path, path: str, content: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "candidate")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Quality fixture")
    _git(root, "config", "user.email", "quality@example.invalid")
    monkeypatch.setenv("FS_TEST_AUDIT_IMAGE_ID", "sha256:" + "a" * 64)
    monkeypatch.setenv("FS_TEST_BACKEND_SERVICES_ID", "sha256:" + "b" * 64)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "development")
    monkeypatch.setenv("FS_AUDIT_ISOLATED", "1")
    monkeypatch.setenv("PYTHONPATH", "/app")
    # Model the canonical verifier, independently of the surrounding suite's
    # xdist scratch directories and pytest-cov subprocess configuration. These
    # checks use RecordingRunner, so they never write artifacts or spawn pytest.
    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT_DIR", "/run/flow-steward-audit-artifacts")
    monkeypatch.setenv("FS_ARTIFACT_ROOT", "/run/flow-steward-audit-ops")
    monkeypatch.setattr(backend_runtime, "_runtime_receipt_identity", lambda: "c" * 64)
    for key in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
        "COV_CORE_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
    sha = _commit(root, "core/value.py", "VALUE = 1\n")
    external_names = sorted(
        key
        for key, value in os.environ.items()
        if value
        and (
            key in backend_runtime._EXTERNAL_INPUT_NAMES
            or (
                key not in backend_runtime._OPERATIONAL_NAMES
                and key not in backend_runtime._NON_PATH_CONFIGURATION_NAMES
                and key != "PATH"
                and backend_runtime._CONFIGURATION_PATH_NAME.search(key.upper())
                and value not in backend_runtime._CANONICAL_CONFIGURATION_PATHS.get(key, ())
            )
        )
    )
    assert "backend_candidate" not in backend_runtime.backend_runtime_identity(sha), external_names
    return root, sha


@pytest.mark.parametrize(
    "changed_path,reuses_backend",
    [
        ("assets/frontend/src/view.tsx", True),
        ("assets/frontend/src/view.test.tsx", True),
        ("assets/platform-renderer.js", True),
        ("core/value.py", False),
        ("tests/fixtures/input.json", False),
        ("db/schema/schema.sql", False),
        ("locales/en/messages.po", False),
        ("infra/docker/docker-compose.yaml", False),
        ("extensions/example/extension.yaml", False),
        ("pyproject.toml", False),
    ],
)
def test_real_backend_registry_reuses_only_identical_inputs_across_sha(
    tmp_path: Path, candidate, changed_path: str, reuses_backend: bool
) -> None:
    root, producer_sha = candidate
    registry = _backend_registry()
    shared = tmp_path / "shared"
    request = VerificationRequest(
        tier=3,
        evidence_mode="reuse",
        repository_root=root,
        commit_sha=producer_sha,
        evidence_root=tmp_path / "producer",
        shared_evidence_root=shared,
    )
    assert verify(request, registry=registry, runner=RecordingRunner()).status == "passed"
    consumer_sha = _commit(root, changed_path, "changed\n")
    assert consumer_sha != producer_sha
    consumer_request = request.model_copy(
        update={"commit_sha": consumer_sha, "evidence_root": tmp_path / "consumer"}
    )
    runner = RecordingRunner()
    result = verify(consumer_request, registry=registry, runner=runner)
    assert result.status == "passed"
    assert ("backend-tests" in result.reused) == reuses_backend
    assert ("backend-tests" in runner.calls) != reuses_backend
    assert "backend-frontend-contracts" in runner.calls
    if reuses_backend:
        check = next(check for check in registry.checks if check.id == "backend-tests")
        fingerprint = fingerprint_check(
            check, FingerprintContext(root=root, source="commit", commit_sha=consumer_sha)
        )
        record = EvidenceStore(consumer_request.evidence_root).read(check.id, fingerprint)
        assert record is not None and record.commit_sha == producer_sha
        assert not (root / "logs/coverage").exists()


def test_failed_frontend_contracts_cannot_be_hidden_by_a_reused_backend(
    tmp_path: Path, candidate
) -> None:
    root, sha = candidate
    registry = _backend_registry()
    request = VerificationRequest(
        tier=3,
        evidence_mode="reuse",
        repository_root=root,
        commit_sha=sha,
        evidence_root=tmp_path / "evidence",
    )
    verify(request, registry=registry, runner=RecordingRunner())
    sha = _commit(root, "assets/frontend/src/view.tsx", "broken contract\n")
    runner = RecordingRunner({"backend-frontend-contracts": 1})
    result = verify(
        request.model_copy(update={"commit_sha": sha}), registry=registry, runner=runner
    )
    assert "backend-tests" in result.reused
    assert result.status == "failed"
    assert "backend-frontend-contracts" in runner.calls


def test_final_reader_accepts_reused_backend_without_launcher_evidence_mode(
    tmp_path: Path, candidate, monkeypatch
) -> None:
    root, sha = candidate
    registry = _backend_registry()
    request = VerificationRequest(
        tier=3,
        evidence_mode="reuse",
        repository_root=root,
        commit_sha=sha,
        evidence_root=tmp_path / "evidence",
    )
    monkeypatch.setenv("FS_QUALITY_EVIDENCE_MODE", "reuse")
    verify(request, registry=registry, runner=RecordingRunner())
    sha = _commit(root, "assets/frontend/src/view.tsx", "frontend change\n")
    result = verify(
        request.model_copy(update={"commit_sha": sha}), registry=registry, runner=RecordingRunner()
    )
    assert "backend-tests" in result.reused
    monkeypatch.delenv("FS_QUALITY_EVIDENCE_MODE")
    # This is a policy regression with simulated check executions, not a claim
    # that the actual repository's full suites have passed.
    backend_checks = registry.model_copy(
        update={
            "checks": tuple(
                check
                for check in registry.checks
                if check.id in {"backend-tests", "backend-frontend-contracts"}
            )
        }
    )
    assert (
        _quality_evidence_failures(
            request.evidence_root,
            backend_checks,
            FingerprintContext(root=root, source="commit", commit_sha=sha),
        )
        == []
    )
