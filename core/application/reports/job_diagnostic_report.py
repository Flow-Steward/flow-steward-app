"""On-demand, redacted diagnostic report projection for one job."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.security.prompt_safety import (
    redact_sensitive_structure,
    redact_structure_for_persistence,
    redact_text_for_persistence,
)
from core.infrastructure.security.secret_detection import redact_detected_secrets

_MAX_SECURITY_SCAN_CHARS = 512_000


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _artifact_manifest(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for step in steps:
        for key in ("input_artifacts", "output_artifacts"):
            for artifact in _rows(step.get(key)):
                artifact_id = str(artifact.get("artifact_id") or "").strip()
                if not artifact_id or artifact_id in seen:
                    continue
                seen.add(artifact_id)
                manifest.append(artifact)
    return manifest


def _redact_canonical_pattern_secrets(value: Any) -> Any:
    """Redact decoded strings without risking another invalid JSON replacement."""
    if isinstance(value, dict):
        return {
            redact_detected_secrets(str(key)): _redact_canonical_pattern_secrets(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_canonical_pattern_secrets(nested) for nested in value]
    return redact_detected_secrets(value) if isinstance(value, str) else value


def _scan_serialized_value(value: Any, *, project_id: str, job_id: str) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) > _MAX_SECURITY_SCAN_CHARS:
        if isinstance(value, dict):
            return {
                key: _scan_serialized_value(nested, project_id=project_id, job_id=job_id)
                for key, nested in value.items()
            }
        if isinstance(value, list):
            return [
                _scan_serialized_value(nested, project_id=project_id, job_id=job_id)
                for nested in value
            ]

    scanned = redact_text_for_persistence(
        encoded,
        project_id=project_id,
        job_id=job_id,
        context_type="job_diagnostic_report",
        preserve_raw_on_block=True,
    )
    scanned = redact_detected_secrets(scanned)
    try:
        return json.loads(scanned)
    except (TypeError, ValueError):
        # A custom security replacement must never make the downloadable JSON
        # invalid. Keep the same wire-normalized values while applying the
        # leaf-wise fallback: database scalars must not escape as Python objects.
        fallback = redact_structure_for_persistence(
            json.loads(encoded),
            project_id=project_id,
            job_id=job_id,
            context_type="job_diagnostic_report",
        )
        # The failed serialized replacement is discarded, not its canonical
        # credential-pattern policy. Apply it to decoded keys and values too.
        return _redact_canonical_pattern_secrets(fallback)


def _fully_redacted(value: dict[str, Any], *, project_id: str, job_id: str) -> dict[str, Any]:
    structurally_redacted = redact_sensitive_structure(value)
    decoded = _scan_serialized_value(
        structurally_redacted,
        project_id=project_id,
        job_id=job_id,
    )

    return decoded if isinstance(decoded, dict) else {}


def build_job_diagnostic_report(
    *,
    job_id: str,
    project_id: str,
    job: dict[str, Any],
    detail_contract: dict[str, Any],
    messages: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    runtime_journal: list[dict[str, Any]],
    causal_chain: dict[str, Any],
    decisions: list[dict[str, Any]],
    cost_breakdown: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete persisted diagnostic bundle without binary artifact bodies."""

    normalized_contract = dict(detail_contract or {})
    steps = _rows(
        normalized_contract.get("execution_steps")
        or (normalized_contract.get("sections") or {}).get("execution_steps")
    )
    report = {
        "schema_version": "flow-steward.job-diagnostic-report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "job": dict(job or {}),
        "summary": normalized_contract,
        "steps": steps,
        "messages": _rows(messages),
        "logs": _rows(logs),
        "events": _rows(events),
        "errors": _rows(errors),
        "runtime_journal": _rows(runtime_journal),
        "causal_chain": dict(causal_chain or {}),
        "decisions": _rows(decisions),
        "cost_breakdown": dict(cost_breakdown or {}),
        "artifact_manifest": _artifact_manifest(steps),
    }
    return _fully_redacted(report, project_id=project_id, job_id=job_id)


__all__ = ["build_job_diagnostic_report"]
