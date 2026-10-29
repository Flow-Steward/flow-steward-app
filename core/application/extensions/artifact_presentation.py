"""Terminal-only display claims for host-prepared extension artifact outputs."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MIME_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/pdf",
        "application/json",
        "application/zip",
        "text/plain",
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "application/msexcel",
        "application/x-msexcel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)
_CLAIM_KEYS = frozenset(
    {"artifact_id", "binding_key", "filename", "content_type", "size_bytes", "sha256"}
)
_OUTPUT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ArtifactPresentationAuthorization:
    artifact_id: str
    binding_key: str
    claim_output: str
    owner_json: str
    object_key: str


def presentation_key(artifact_id: str) -> str:
    return f"artifacts/{artifact_id}/_presentation.json"


def _owner_json(row: Mapping[str, Any]) -> str:
    return json.dumps(row.get("owner_scope") or {}, sort_keys=True, separators=(",", ":"))


def installed_presentation_policy(
    extension: Mapping[str, Any] | None, operation_id: str
) -> tuple[tuple[str, str], ...]:
    """Capture installed policy only; invocation output specs cannot opt themselves in."""
    if not isinstance(extension, Mapping) or not operation_id:
        return ()
    compiled = extension.get("compiled_manifest")
    compiled = compiled if isinstance(compiled, Mapping) else {}
    sources = [extension, compiled]
    for key in ("extension_contract_v2", "contract_v2"):
        contract = extension.get(key) or compiled.get(key)
        if isinstance(contract, Mapping):
            normalized = contract.get("normalized_contract")
            sources.append(normalized if isinstance(normalized, Mapping) else contract)
    captures: list[tuple[tuple[str, str], ...]] = []
    for source in sources:
        rows = source.get("artifact_policies")
        matches = (
            [
                row
                for row in rows
                if isinstance(row, Mapping) and row.get("operation_id") == operation_id
            ]
            if isinstance(rows, list)
            else []
        )
        if len(matches) > 1:
            raise ValueError("artifact_presentation_policy_invalid")
        if not matches:
            continue
        outputs = matches[0].get("outputs") or []
        capture: list[tuple[str, str]] = []
        for output in outputs if isinstance(outputs, list) else []:
            if not isinstance(output, Mapping) or "presentation" not in output:
                continue
            declaration = output["presentation"]
            binding = output.get("binding_key")
            if (
                output.get("kind") != "artifact"
                or not isinstance(binding, str)
                or not binding.strip()
                or not isinstance(declaration, Mapping)
                or set(declaration) != {"claim_output"}
                or not isinstance(declaration.get("claim_output"), str)
                or not _OUTPUT_KEY.fullmatch(declaration["claim_output"])
            ):
                raise ValueError("artifact_presentation_policy_invalid")
            capture.append((binding.strip(), declaration["claim_output"]))
        # This additive protocol starts with one atomic presentation commit per
        # invocation. It does not pretend object storage has multi-row transactions.
        if len(capture) > 1:
            raise ValueError("artifact_presentation_policy_invalid")
        captures.append(tuple(capture))
    if captures and any(capture != captures[0] for capture in captures):
        raise ValueError("artifact_presentation_policy_conflict")
    return captures[0] if captures else ()


def authorize_prepared_presentations(
    prepared: Any, policy: tuple[tuple[str, str], ...]
) -> tuple[ArtifactPresentationAuthorization, ...]:
    authorizations: list[ArtifactPresentationAuthorization] = []
    minted = set(prepared.minted_artifact_output_ids)
    rows = {row["artifact_id"]: row for row in prepared.produced_artifacts}
    for binding, claim_output in policy:
        matches = [row for row in prepared.artifact_outputs if row.get("binding_key") == binding]
        if not matches:
            continue
        if len(matches) != 1 or matches[0].get("artifact_id") not in minted:
            raise ValueError("artifact_presentation_output_not_fresh")
        artifact_id = matches[0]["artifact_id"]
        row = rows[artifact_id]
        authorizations.append(
            ArtifactPresentationAuthorization(
                artifact_id, binding, claim_output, _owner_json(row), str(row["object_key"])
            )
        )
    return tuple(authorizations)


def validate_presentation_requests(
    policy: tuple[tuple[str, str], ...],
    references: list[dict[str, Any]],
    specs: list[dict[str, Any]],
) -> None:
    """Reject ambiguous/by-reference opt-ins before reserving any output objects."""
    for binding, _ in policy:
        referenced = [
            row
            for index, row in enumerate(references)
            if (row.get("binding_key") or f"artifact_{index + 1}") == binding
        ]
        requested = [
            row
            for index, row in enumerate(specs)
            if (
                row.get("binding_key")
                or row.get("name")
                or f"artifact_{len(references) + index + 1}"
            )
            == binding
        ]
        if referenced or len(requested) > 1:
            raise ValueError("artifact_presentation_output_not_fresh")


def validate_presentation_claim(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CLAIM_KEYS:
        raise ValueError("artifact_presentation_claim_invalid")
    claim = dict(value)
    if any(not isinstance(claim[field], str) for field in _CLAIM_KEYS - {"size_bytes"}):
        raise ValueError("artifact_presentation_claim_invalid")
    filename = claim["filename"]
    if (
        not filename
        or filename != filename.strip()
        or len(filename.encode("utf-8")) > 255
        or filename in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("artifact_presentation_filename_invalid")
    if claim["content_type"] not in _MIME_TYPES:
        raise ValueError("artifact_presentation_content_type_invalid")
    if (
        isinstance(claim["size_bytes"], bool)
        or not isinstance(claim["size_bytes"], int)
        or claim["size_bytes"] < 0
        or not _SHA256.fullmatch(claim["sha256"])
    ):
        raise ValueError("artifact_presentation_digest_invalid")
    return claim


def matching_presentation(row: Mapping[str, Any], candidate: Any) -> dict[str, Any] | None:
    """An orphan or changed artifact cannot lend authority to a display sidecar."""
    try:
        claim = validate_presentation_claim(candidate)
    except ValueError:
        return None
    expiry = row.get("expires_at")
    if expiry is not None and float(expiry) <= time.time():
        return None
    if (
        row.get("persistence_status") not in (None, "available")
        or row.get("expired")
        or row.get("upload_error")
        or row.get("artifact_id") != claim["artifact_id"]
        or row.get("sha256") != claim["sha256"]
        or row.get("size_bytes") != claim["size_bytes"]
    ):
        return None
    return claim


class ArtifactPresentationResolver:
    """Canonical lookup plus an explicitly separate post-authorization display read."""

    def __init__(self, plane: Any):
        self._plane = plane

    def __call__(self, artifact_id: str) -> dict[str, Any] | None:
        return self._plane.get_artifact(artifact_id)

    def read_presentation(self, authorized_metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        return self._plane.read_artifact_presentation(dict(authorized_metadata))


def artifact_presentation_view(
    row: Mapping[str, Any], *, resolve_artifact: Any = None
) -> dict[str, Any]:
    """Use only after original-artifact authorization; capability metadata stays intact."""
    view = dict(row)
    view.pop("presentation", None)
    reader = getattr(resolve_artifact, "read_presentation", None)
    candidate = reader(row) if callable(reader) else None
    claim = matching_presentation(row, candidate)
    if claim:
        view["presentation"] = claim
        view["filename"] = claim["filename"]
        view["content_type"] = claim["content_type"]
    return view


def commit_terminal_presentations(
    plane: Any, prepared: Any, artifacts: Mapping[str, Mapping[str, Any]], invocation: Any
) -> list[dict[str, Any]]:
    """Shared final-success consumer; no progress, failure or timeout can commit."""
    if not getattr(prepared, "artifact_presentations", ()):
        return []
    response = invocation.get("response") if isinstance(invocation, Mapping) else None
    if (
        not isinstance(invocation, Mapping)
        or invocation.get("status") != "succeeded"
        or not isinstance(response, Mapping)
        or response.get("ok") is not True
    ):
        return []
    result = response.get("result")
    result = result if isinstance(result, Mapping) else {}
    errors: list[dict[str, Any]] = []
    for authorization in prepared.artifact_presentations:
        if authorization.claim_output not in result:
            continue
        try:
            claim = validate_presentation_claim(result[authorization.claim_output])
            row = artifacts.get(authorization.artifact_id)
            if (
                not isinstance(row, Mapping)
                or claim["artifact_id"] != authorization.artifact_id
                or claim["binding_key"] != authorization.binding_key
                or _owner_json(row) != authorization.owner_json
                or row.get("object_key") != authorization.object_key
                or matching_presentation(row, claim) is None
            ):
                raise ValueError("artifact_presentation_output_mismatch")
            plane.commit_artifact_presentation(
                authorization.artifact_id,
                claim,
                expected_owner_json=authorization.owner_json,
                expected_object_key=authorization.object_key,
            )
        except Exception as exc:
            allowed_codes = {
                "artifact_presentation_claim_invalid",
                "artifact_presentation_filename_invalid",
                "artifact_presentation_content_type_invalid",
                "artifact_presentation_digest_invalid",
                "artifact_presentation_output_mismatch",
                "artifact_presentation_original_unavailable",
                "artifact_presentation_atomic_storage_required",
                "artifact_presentation_conflict",
            }
            code = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc) in allowed_codes
                else "artifact_presentation_commit_failed"
            )
            errors.append(
                {
                    "code": code,
                    "message": "Artifact presentation could not be accepted",
                    "artifact_id": authorization.artifact_id,
                }
            )
    return errors


def finalize_artifact_presentations(
    plane: Any,
    prepared: Any,
    artifacts: Mapping[str, dict[str, Any]],
    invocation: Any,
    *,
    accept_claims: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Commit terminal claims before projecting prepared artifact display metadata."""
    presentation_errors = []
    if accept_claims:
        presentation_errors = commit_terminal_presentations(plane, prepared, artifacts, invocation)
    produced_artifacts = [
        artifact_presentation_view(
            plane.get_artifact(str(meta.get("artifact_id") or ""))
            or artifacts.get(str(meta.get("artifact_id") or ""), meta),
            resolve_artifact=ArtifactPresentationResolver(plane),
        )
        if getattr(prepared, "artifact_presentations", ()) and not presentation_errors
        else dict(artifacts.get(str(meta.get("artifact_id") or ""), meta))
        for meta in prepared.produced_artifacts
    ]
    return produced_artifacts, presentation_errors


def strip_unaccepted_presentation_claims(prepared: Any, invocation: Any) -> None:
    """Keep provider presentation claims out of results unless host acceptance succeeded."""
    if not isinstance(invocation, dict):
        return
    response = invocation.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return
    for authorization in getattr(prepared, "artifact_presentations", ()):
        result.pop(authorization.claim_output, None)


def strip_failed_presentation_claims(prepared: Any, invocation: Any) -> None:
    if (
        prepared is not None
        and isinstance(invocation, Mapping)
        and invocation.get("status") != "succeeded"
    ):
        strip_unaccepted_presentation_claims(prepared, invocation)


def finalize_confirmed_artifact_presentations(
    plane: Any,
    prepared: Any,
    artifacts: Mapping[str, dict[str, Any]],
    invocation: Any,
    *,
    confirmed_success: bool,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Publish claims only after the external-effect ledger confirms success."""
    if not confirmed_success:
        strip_unaccepted_presentation_claims(prepared, invocation)
        return None, []
    try:
        produced_artifacts, errors = finalize_artifact_presentations(
            plane, prepared, artifacts, invocation, accept_claims=True
        )
    except Exception:
        strip_unaccepted_presentation_claims(prepared, invocation)
        return None, [{"code": "artifact_presentation_commit_failed"}]
    if errors:
        strip_unaccepted_presentation_claims(prepared, invocation)
        return None, errors
    return produced_artifacts, []


def finalize_external_effect_presentations(
    plane: Any,
    prepared: Any,
    finalized_grants: Any,
    invocation: dict[str, Any],
    *,
    confirmed_success: bool,
    produced_artifacts: list[dict[str, Any]] | None = None,
    produced_datasets: list[dict[str, Any]] | None = None,
    grant_outputs: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Apply confirmed claims and suppress outputs if presentation acceptance fails."""
    artifacts = produced_artifacts if produced_artifacts is not None else []
    datasets = produced_datasets if produced_datasets is not None else []
    grants = grant_outputs if grant_outputs is not None else {}
    if not getattr(prepared, "artifact_presentations", ()):
        return artifacts, datasets, grants
    if finalized_grants is None:
        strip_unaccepted_presentation_claims(prepared, invocation)
        return artifacts, datasets, grants

    accepted, errors = finalize_confirmed_artifact_presentations(
        plane,
        prepared,
        finalized_grants.artifacts,
        invocation,
        confirmed_success=confirmed_success,
    )
    if accepted is not None:
        artifacts = accepted
        invocation["produced_artifacts"] = accepted
    if errors:
        invocation["status"] = "failed"
        invocation["detail"] = str(errors[0].get("code") or "artifact_presentation_commit_failed")
        existing_errors = invocation.get("errors")
        invocation["errors"] = [
            *(existing_errors if isinstance(existing_errors, list) else []),
            *errors,
        ]
        artifacts, datasets, grants = [], [], {}
        invocation.pop("produced_artifacts", None)
        invocation.pop("produced_datasets", None)
        invocation.pop("grant_outputs", None)
    return artifacts, datasets, grants


def finalize_prepared_grant_presentations(
    plane: Any,
    prepared: Any,
    artifacts: Mapping[str, dict[str, Any]],
    datasets: Mapping[str, Mapping[str, Any]],
    invocation: Any,
    *,
    accept_claims: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Project final artifact and dataset metadata after grant byte finalization."""
    produced_artifacts, presentation_errors = finalize_artifact_presentations(
        plane, prepared, artifacts, invocation, accept_claims=accept_claims
    )
    produced_datasets = [
        datasets.get(str(meta.get("dataset_id") or ""), meta) for meta in prepared.produced_datasets
    ]
    return produced_artifacts, produced_datasets, presentation_errors
