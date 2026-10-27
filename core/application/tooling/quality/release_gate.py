#!/usr/bin/env python3
"""Release-only provenance and quality-registry validation."""

from __future__ import annotations

from pathlib import Path

from core.application.tooling.quality.check_registry import CheckRegistry, load_check_registry
from core.application.tooling.quality.evidence import (
    EvidenceExpectation,
    EvidencePolicy,
    EvidenceStore,
    shared_evidence_root,
)
from core.application.tooling.quality.fingerprint import (
    FingerprintContext,
    committed_candidate_sha,
    fingerprint_check,
)

_DEFAULT_EVIDENCE_ROOT = Path("logs/quality/evidence")


_REQUIRED_TIERS = (2, 3)


def _quality_evidence_failures(
    evidence_root: Path,
    registry: CheckRegistry,
    context: FingerprintContext,
    shared_root: Path | None = None,
) -> list[str]:
    """Require exact final-candidate evidence for every Tier 2 and Tier 3 owner."""
    store = EvidenceStore(evidence_root, shared_root=shared_root)
    policy = EvidencePolicy.local()
    problems: list[str] = []
    for check in registry.checks:
        if check.owner_tier not in _REQUIRED_TIERS:
            continue
        fingerprint = fingerprint_check(check, context)
        record = store.read(check.id, fingerprint)
        if record is None:
            problems.append(
                f"{check.id}: no retained Tier {check.owner_tier} evidence for this fingerprint"
            )
            continue
        decision = policy.evaluate(
            record,
            EvidenceExpectation(
                check_id=check.id,
                owner_tier=check.owner_tier,
                fingerprint=fingerprint,
                commit_sha=context.commit_sha,
                reuse_scope=check.reuse_scope,
            ),
        )
        if not decision.reusable:
            problems.append(f"{check.id}: {decision.reason}")
        elif record.command != check.command:
            problems.append(f"{check.id}: command mismatch")
    return problems


def run_release_gate(
    *,
    repository_root: Path | None = None,
    expected_sha: str | None = None,
    evidence_root: Path | None = None,
    shared_root: Path | None = None,
) -> int:
    """Validate the clean candidate against retained green Tier 2 and Tier 3."""
    root = (repository_root or Path.cwd()).resolve()
    try:
        candidate_sha = committed_candidate_sha(root, expected_sha or "")
        registry = load_check_registry(root / "infra/tooling/quality/check-registry.json")
        context = FingerprintContext(root=root, source="commit", commit_sha=candidate_sha)
        problems = _quality_evidence_failures(
            evidence_root if evidence_root is not None else root / _DEFAULT_EVIDENCE_ROOT,
            registry,
            context,
            shared_root,
        )
    except (OSError, ValueError) as exc:
        print(f"Release gate failed: {exc}")
        return 1
    counts = {
        tier: sum(check.owner_tier == tier for check in registry.checks) for tier in _REQUIRED_TIERS
    }
    for tier, count in counts.items():
        if not count:
            problems.append(f"quality registry has no Tier {tier} owner checks")
    if problems:
        print(f"Release gate failed: no complete green Tier 2 + Tier 3 for {candidate_sha}.")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        "Release gate passed: candidate provenance, "
        f"{len(registry.checks)} quality ownership records; "
        f"{counts[2]} green Tier 2 and {counts[3]} green Tier 3 results "
        f"for {candidate_sha}."
    )
    return 0


def main() -> int:
    return run_release_gate(shared_root=shared_evidence_root())


if __name__ == "__main__":
    raise SystemExit(main())
