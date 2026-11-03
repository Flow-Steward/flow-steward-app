"""Diagnostic hosted proof of input-scoped evidence reuse; never a release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from core.application.tooling.quality.check_registry import (
    CheckDefinition,
    CheckRegistry,
    load_check_registry,
)
from core.application.tooling.quality.evidence import EvidenceStore
from core.application.tooling.quality.fingerprint import (
    FingerprintContext,
    committed_candidate_sha,
    fingerprint_check,
)
from core.application.tooling.quality.verify import (
    CheckRunner,
    SubprocessCheckRunner,
    VerificationRequest,
    verify,
)

CHECK_ID = "quality-reuse-smoke"
REGRESSION = (
    "tests/unit/tooling/quality/test_evidence.py"
    "::test_input_scope_never_overrides_a_fingerprint_mismatch"
)


def smoke_definition() -> CheckDefinition:
    """Bound one pure existing regression and its canonical wrapper inputs."""
    return CheckDefinition(
        id=CHECK_ID,
        owner_tier=1,
        reuse_scope="inputs",
        command=(
            "env",
            # This pure policy test needs no product/bootstrap conftest. Make
            # that isolation explicit so its complete input graph stays bounded.
            "PYTEST_ADDOPTS=--noconftest",
            "bash",
            "core/entrypoints/cli/scripts/lint.sh",
            "--inside",
            "--checks",
            "targeted-tests",
            "--test-paths",
            REGRESSION,
            "--evidence-mode",
            "off",
        ),
        inputs=(
            ".github/workflows/remote-quality.yml",
            ".github/actions/prepare-quality/action.yml",
            "core/application/tooling/quality/**",
            "core/infrastructure/config/**",
            "core/entrypoints/cli/scripts/**",
            "infra/docker/**",
            "infra/tooling/**",
            "requirements*.txt",
            "requirements*.lock",
            "tests/__init__.py",
            "tests/unit/tooling/quality/test_evidence.py",
            "pyproject.toml",
            "pytest.ini",
            ".env.example",
            "assets/**",
        ),
    )


class _ObservedRunner:
    def __init__(self, runner: CheckRunner, *, require_reuse: bool) -> None:
        self.runner = runner
        self.require_reuse = require_reuse
        self.calls = 0

    def run(self, check: CheckDefinition, *, timeout_seconds: int) -> int:
        self.calls += 1
        if self.require_reuse:
            print("Consumer cache miss: refusing to execute an unexpected regression.", flush=True)
            return 2
        return self.runner.run(check, timeout_seconds=timeout_seconds)


def run_smoke(
    root: Path,
    *,
    commit_sha: str,
    run_id: str,
    source_sha: str = "",
    source_run_id: str = "",
    previous_report: Path | None = None,
    runner: CheckRunner | None = None,
) -> dict:
    """Execute a producer or prove a consumer reused an intact imported record."""
    candidate = committed_candidate_sha(root, commit_sha)
    context = FingerprintContext(root=root, source="commit", commit_sha=candidate)
    check = smoke_definition()
    registry = load_check_registry(root / "infra/tooling/quality/check-registry.json")
    frontend = next(item for item in registry.checks if item.id == "frontend-tests")
    frontend_fingerprint = fingerprint_check(frontend, context)
    assets = hashlib.sha256()
    for path, content in context.iter_matching_content(frontend.inputs):
        assets.update(str(path).encode() + b"\0" + hashlib.sha256(content).digest())
    assets_digest = assets.hexdigest()
    source = None
    if source_sha:
        if previous_report is None or not source_run_id:
            raise ValueError("Consumer requires its validated source report and run identity")
        source = json.loads(previous_report.read_text())
        if (
            source.get("diagnostic_only") is not True
            or source.get("full_gate") is not False
            or source.get("check_id") != CHECK_ID
            or source.get("candidate_sha") != source_sha
            or source.get("run_id") != source_run_id
            or source.get("status") != "passed"
        ):
            raise ValueError("Imported smoke report does not match the validated official source")
        if source.get("frontend_fingerprint") != frontend_fingerprint:
            raise ValueError(
                "Real frontend-tests fingerprint differs between producer and consumer"
            )
        if source.get("frontend_assets_digest") != assets_digest:
            raise ValueError("Frontend asset content differs between producer and consumer")
    elif source_run_id or previous_report is not None:
        raise ValueError("Source report requires a validated source SHA")

    observed = _ObservedRunner(
        runner or SubprocessCheckRunner(root), require_reuse=source is not None
    )
    summary = verify(
        VerificationRequest(
            tier=1,
            evidence_mode="reuse",
            fingerprint_source="commit",
            trust="trusted_ci",
            commit_sha=candidate,
            timeout_seconds=600,
            repository_root=root,
            evidence_root=root / "logs/quality/evidence",
        ),
        registry=CheckRegistry(schema_version=1, checks=(check,)),
        runner=observed,
    )
    fingerprint = fingerprint_check(check, context)
    record = EvidenceStore(root / "logs/quality/evidence").read(CHECK_ID, fingerprint)
    expected_calls = 0 if source is not None else 1
    expected_origin = source.get("evidence_commit_sha") if source is not None else candidate
    passed = (
        summary.status == "passed"
        and observed.calls == expected_calls
        and summary.executed == (() if source is not None else (CHECK_ID,))
        and summary.reused == ((CHECK_ID,) if source is not None else ())
        and record is not None
        and record.commit_sha == expected_origin
    )
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "full_gate": False,
        "official_suite_pass_claimed": False,
        "frontend_suite_executed": False,
        "check_id": CHECK_ID,
        "candidate_sha": candidate,
        "run_id": run_id,
        "source_sha": source_sha,
        "source_run_id": source_run_id,
        "selected_regression": REGRESSION,
        "pytest_isolation": "--noconftest (pure evidence-policy regression)",
        "status": "passed" if passed else "failed",
        "verification": asdict(summary),
        "runner_calls": observed.calls,
        "fingerprint": fingerprint,
        "evidence_commit_sha": record.commit_sha if record is not None else None,
        "frontend_fingerprint": frontend_fingerprint,
        "frontend_assets_digest": assets_digest,
        "frontend_fingerprint_matches_source": source is not None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("/app"))
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--previous-report", type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("FS_AUDIT_ISOLATED") != "1" or os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Hosted reuse smoke must run inside the canonical isolated fs-audit container")
    report = run_smoke(
        args.repository_root,
        commit_sha=args.commit_sha,
        run_id=args.run_id,
        source_sha=args.source_sha,
        source_run_id=args.source_run_id,
        previous_report=args.previous_report,
    )
    destination = args.repository_root / "logs/quality/hosted/reuse-smoke.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2) + "\n"
    destination.write_text(payload)
    print(payload, end="", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
