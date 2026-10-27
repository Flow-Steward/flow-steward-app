"""Pure, secret-safe release candidate snapshot calculation."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.application.release.candidate_requirements import AggregatedRequirements

SNAPSHOT_SCHEMA_VERSION = "flow-steward-candidate-snapshot-v1"
HARNESS_VERSION = "phase-1"


@dataclass(frozen=True)
class SnapshotInputs:
    """Original sources and explicit non-secret inputs for one candidate."""

    candidate_commit: str
    compose_project: str
    source_env: Path
    explicit_values: Mapping[str, str] = field(default_factory=dict)
    plain_values: Mapping[str, str] = field(default_factory=dict)
    source_paths: Mapping[str, Path] = field(default_factory=dict)
    build_configuration: Mapping[str, str] = field(default_factory=dict)
    image_inputs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedCandidateSnapshot:
    """Deterministic expected state with protected values represented by HMAC."""

    candidate_commit: str
    compose_project: str
    source_hashes: Mapping[str, str]
    input_presence: Mapping[str, str]
    protected_inputs: Mapping[str, str]
    plain_inputs: Mapping[str, str]
    requirements_digest: str
    image_fingerprints: Mapping[str, str]
    fingerprint: str
    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "harness_version": self.harness_version,
            "candidate_commit": self.candidate_commit,
            "compose_project": self.compose_project,
            "source_hashes": dict(self.source_hashes),
            "input_presence": dict(self.input_presence),
            "protected_inputs": dict(self.protected_inputs),
            "plain_inputs": dict(self.plain_inputs),
            "requirements_digest": self.requirements_digest,
            "image_fingerprints": dict(self.image_fingerprints),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> ExpectedCandidateSnapshot:
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("candidate snapshot schema is invalid")
        return cls(
            schema_version=str(payload["schema_version"]),
            harness_version=str(payload["harness_version"]),
            candidate_commit=str(payload["candidate_commit"]),
            compose_project=str(payload["compose_project"]),
            source_hashes=_string_mapping(payload["source_hashes"]),
            input_presence=_string_mapping(payload["input_presence"]),
            protected_inputs=_string_mapping(payload["protected_inputs"]),
            plain_inputs=_string_mapping(payload["plain_inputs"]),
            requirements_digest=str(payload["requirements_digest"]),
            image_fingerprints=_string_mapping(payload["image_fingerprints"]),
            fingerprint=str(payload["fingerprint"]),
        )


@dataclass(frozen=True)
class FinalizedCandidateSnapshot:
    """Expected snapshot paired with immutable runtime identities."""

    expected: ExpectedCandidateSnapshot
    image_digests: Mapping[str, str]
    resource_identities: Mapping[str, tuple[str, ...]]
    compose_working_dir: str | None = None
    compose_config_files: str | None = None
    extension_digest: str | None = None
    provenance_checksum: str | None = None


@dataclass(frozen=True)
class SnapshotComparison:
    """Stable snapshot check-only verdict."""

    matches: bool
    reasons: tuple[str, ...]


def protected_identity(key: bytes, name: str, value: str) -> str:
    """Return a candidate-keyed identity without retaining the protected value."""

    digest = hmac.new(key, f"{name}\0{value}".encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def calculate_expected_snapshot(
    inputs: SnapshotInputs,
    requirements: AggregatedRequirements,
    *,
    key: bytes,
    environment: Mapping[str, str],
    source_hashes: Mapping[str, str],
) -> ExpectedCandidateSnapshot:
    """Calculate expected state only from current original sources and overrides."""

    values = {**environment, **inputs.explicit_values}
    protected_names = tuple(
        sorted(
            (set(requirements.required_env) | set(requirements.optional_env))
            - set(inputs.plain_values)
        )
    )
    protected = {
        name: protected_identity(key, name, values[name])
        for name in protected_names
        if name in values
    }
    presence = {
        name: _presence(values.get(name))
        for name in sorted(set(requirements.required_env) | set(requirements.optional_env))
    }
    plain = {
        **dict(sorted(inputs.plain_values.items())),
        **{f"build:{name}": value for name, value in sorted(inputs.build_configuration.items())},
    }
    requirements_digest = _digest(_requirements_payload(requirements))
    image_fingerprints = {
        image: _digest(
            {
                "inputs": {
                    name: plain.get(
                        name,
                        protected.get(
                            name,
                            source_hashes.get(name, presence.get(name, "missing")),
                        ),
                    )
                    for name in names
                },
                "requirements_digest": requirements_digest,
            }
        )
        for image, names in sorted(inputs.image_inputs.items())
    }
    normalized_source_hashes = dict(sorted(source_hashes.items()))
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "candidate_commit": inputs.candidate_commit,
        "compose_project": inputs.compose_project,
        "source_hashes": normalized_source_hashes,
        "input_presence": presence,
        "protected_inputs": protected,
        "plain_inputs": plain,
        "requirements_digest": requirements_digest,
        "image_fingerprints": image_fingerprints,
    }
    return ExpectedCandidateSnapshot(
        candidate_commit=inputs.candidate_commit,
        compose_project=inputs.compose_project,
        source_hashes=normalized_source_hashes,
        input_presence=presence,
        protected_inputs=protected,
        plain_inputs=plain,
        requirements_digest=requirements_digest,
        image_fingerprints=image_fingerprints,
        fingerprint=_digest(payload),
    )


def compare_finalized_snapshot(
    stored: ExpectedCandidateSnapshot | FinalizedCandidateSnapshot,
    expected: ExpectedCandidateSnapshot,
) -> SnapshotComparison:
    """Compare fresh expected state with the materialized or finalized snapshot."""

    baseline = stored.expected if isinstance(stored, FinalizedCandidateSnapshot) else stored
    reasons: list[str] = []
    _mapping_change_reasons(
        reasons,
        baseline.protected_inputs,
        expected.protected_inputs,
        label="protected input",
    )
    _mapping_change_reasons(
        reasons,
        baseline.plain_inputs,
        expected.plain_inputs,
        label="plain input",
    )
    _mapping_change_reasons(
        reasons,
        baseline.input_presence,
        expected.input_presence,
        label="input presence",
    )
    if baseline.candidate_commit != expected.candidate_commit:
        reasons.append("candidate commit changed")
    if baseline.compose_project != expected.compose_project:
        reasons.append("compose project changed")
    if baseline.requirements_digest != expected.requirements_digest:
        reasons.append("consumer requirements changed")
    if baseline.image_fingerprints != expected.image_fingerprints:
        reasons.append("image inputs changed")
    if not reasons and baseline.source_hashes != expected.source_hashes:
        reasons.append("candidate source changed")
    if not reasons and baseline.fingerprint != expected.fingerprint:
        reasons.append("candidate fingerprint changed")
    return SnapshotComparison(matches=not reasons, reasons=tuple(reasons))


def _mapping_change_reasons(
    reasons: list[str],
    previous: Mapping[str, str],
    current: Mapping[str, str],
    *,
    label: str,
) -> None:
    for name in sorted(set(previous) | set(current)):
        if previous.get(name) != current.get(name):
            reasons.append(f"{label} changed: {name}")


def _presence(value: str | None) -> str:
    if value is None:
        return "missing"
    return "empty" if value == "" else "present"


def _requirements_payload(requirements: AggregatedRequirements) -> dict[str, object]:
    return {
        "check_ids": requirements.check_ids,
        "groups": requirements.groups,
        "required_env": requirements.required_env,
        "optional_env": requirements.optional_env,
        "services": requirements.services,
        "scopes": requirements.scopes,
        "mutable_resources": requirements.mutable_resources,
        "cleanup_obligations": requirements.cleanup_obligations,
    }


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("candidate snapshot mapping is invalid")
    return dict(value)
