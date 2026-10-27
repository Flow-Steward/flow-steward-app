"""Aggregate quality evidence timing and reuse metrics."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from core.application.tooling.quality.evidence import EvidenceStore

MetricStatus = Literal["executed", "reused", "failed", "infrastructure_failed"]


@dataclass(frozen=True)
class MetricSample:
    status: MetricStatus
    duration_seconds: float
    invalidation_reason: str = ""
    intentional_repeat: bool = False


@dataclass(frozen=True)
class QualityMetrics:
    executed_seconds: float
    saved_seconds: float
    reuse_rate: float
    repeated_check_percentage: float
    time_to_first_failure_seconds: float | None
    invalidation_reasons: dict[str, int]


def aggregate(samples: list[MetricSample]) -> QualityMetrics:
    """Aggregate ordered samples without conflating execution and reuse time."""
    total = len(samples)
    executed_seconds = sum(
        sample.duration_seconds for sample in samples if sample.status != "reused"
    )
    saved_seconds = sum(sample.duration_seconds for sample in samples if sample.status == "reused")
    first_failure = next(
        (
            sample.duration_seconds
            for sample in samples
            if sample.status in {"failed", "infrastructure_failed"}
        ),
        None,
    )
    reasons = Counter(
        sample.invalidation_reason for sample in samples if sample.invalidation_reason
    )
    return QualityMetrics(
        executed_seconds=executed_seconds,
        saved_seconds=saved_seconds,
        reuse_rate=(sum(sample.status == "reused" for sample in samples) / total if total else 0),
        repeated_check_percentage=(
            100 * sum(sample.intentional_repeat for sample in samples) / total if total else 0
        ),
        time_to_first_failure_seconds=first_failure,
        invalidation_reasons=dict(sorted(reasons.items())),
    )


def samples_from_evidence(evidence_root: Path) -> list[MetricSample]:
    """Read valid checksummed evidence records below one evidence root."""
    store = EvidenceStore(evidence_root)
    samples: list[MetricSample] = []
    for path in sorted(evidence_root.glob("*/*/evidence.json")):
        check_id = path.parents[1].name
        fingerprint = path.parent.name
        record = store.read(check_id, fingerprint)
        if record is None:
            continue
        status: MetricStatus
        if record.status == "passed":
            status = "executed"
        elif record.status == "infrastructure_failed":
            status = "infrastructure_failed"
        else:
            status = "failed"
        samples.append(MetricSample(status=status, duration_seconds=record.duration_seconds))
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize quality evidence metrics.")
    parser.add_argument("--evidence-root", default="logs/quality/evidence")
    parser.add_argument("--fail-repeated-over", type=float, default=100.0)
    args = parser.parse_args(argv)
    report = aggregate(samples_from_evidence(Path(args.evidence_root)))
    print(json.dumps(asdict(report), sort_keys=True))
    return 1 if report.repeated_check_percentage > args.fail_repeated_over else 0


if __name__ == "__main__":
    raise SystemExit(main())
