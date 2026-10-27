#!/usr/bin/env python3
"""Validate the release-stabilization overlay ledger without reviewer/status coupling."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
LEDGER = Path(__file__).with_name("ledger.yaml")
RELEASE_SEVERITIES = {"P0", "P1", "P2"}
TECHNICAL_STATUSES = {
    "open_confirmed",
    "in_progress",
    "fixed_verified",
    "invalid",
    "deferred_by_scope",
}
REVIEW_STATUSES = {"not_requested", "pending", "accepted", "changes_requested"}
REQUIRED_FIXED_EVIDENCE = {
    "root_cause",
    "failing_test",
    "proving_test",
    "verified_commit",
}
PROVENANCE_SCHEMA = "flow-steward-packaged-provenance-v2"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def _git_available() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", nargs="?", type=Path, default=LEDGER)
    parser.add_argument("--provenance", type=Path, default=None)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_packaged_provenance(
    path: Path | None,
    *,
    ledger_path: Path,
) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if path is None:
        return {}, ["packaged provenance is required when Git metadata is unavailable"]
    try:
        payload = json.loads(path.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"packaged provenance is unreadable: {exc}"]
    if not isinstance(payload, dict):
        return {}, ["packaged provenance must be a JSON object"]
    if payload.get("schema_version") != PROVENANCE_SCHEMA:
        errors.append("packaged provenance schema_version is invalid")
    candidate_commit = str(payload.get("candidate_commit") or "").strip().lower()
    if not SHA_PATTERN.fullmatch(candidate_commit):
        errors.append("packaged provenance candidate_commit must be a full 40-character SHA")
    digests = payload.get("image_digests")
    if not isinstance(digests, dict) or not digests:
        errors.append("packaged provenance image_digests must be a non-empty object")
    elif any(
        not str(name or "").strip()
        or not DIGEST_PATTERN.fullmatch(str(digest or "").strip().lower())
        for name, digest in digests.items()
    ):
        errors.append("packaged provenance image_digests contains an invalid digest")
    if str(payload.get("ledger_sha256") or "").strip().lower() != _sha256(ledger_path):
        errors.append("ledger hash does not match packaged provenance")
    if str(payload.get("validator_sha256") or "").strip().lower() != _sha256(Path(__file__)):
        errors.append("validator hash does not match packaged provenance")
    ancestors = payload.get("ancestor_commits")
    if not isinstance(ancestors, list) or not ancestors:
        errors.append("packaged provenance ancestor_commits must be a non-empty list")
    elif any(not SHA_PATTERN.fullmatch(str(commit or "").strip().lower()) for commit in ancestors):
        errors.append("packaged provenance ancestor_commits contains an invalid commit")
    return payload, errors


def _manifest_contains_commit(provenance: dict, commit: str) -> bool:
    normalized = str(commit or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", normalized):
        return False
    return any(
        str(candidate or "").strip().lower().startswith(normalized)
        for candidate in provenance.get("ancestor_commits") or []
    )


def _git_commit_exists(commit: str) -> bool:
    if not commit:
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _git_commit_contains(*, candidate: str, required: str) -> bool:
    if not candidate or not required:
        return False
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", required, candidate],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def main() -> int:
    args = _parse_args()
    ledger_path = args.ledger.resolve()
    payload = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    errors: list[str] = []
    seen: set[str] = set()
    git_available = _git_available()
    provenance: dict = {}
    provenance_valid = False
    if not git_available:
        provenance, provenance_errors = _load_packaged_provenance(
            args.provenance,
            ledger_path=ledger_path,
        )
        errors.extend(provenance_errors)
        provenance_valid = not provenance_errors

    for row in rows:
        row_id = str(row.get("id") or "").strip()
        if not row_id:
            errors.append("row without id")
            continue
        if row_id in seen:
            errors.append(f"{row_id}: duplicate id")
        seen.add(row_id)

        technical = str(row.get("technical_status") or "").strip()
        review = str(row.get("review_status") or "").strip()
        severity = str(row.get("severity") or "").strip()
        if technical not in TECHNICAL_STATUSES:
            errors.append(f"{row_id}: invalid technical_status {technical!r}")
        if review not in REVIEW_STATUSES:
            errors.append(f"{row_id}: invalid review_status {review!r}")
        if severity in RELEASE_SEVERITIES and technical == "deferred_by_scope":
            errors.append(f"{row_id}: release row cannot be deferred_by_scope")
        if severity not in RELEASE_SEVERITIES and technical != "deferred_by_scope":
            errors.append(f"{row_id}: non-release row must be deferred_by_scope")

        if technical == "fixed_verified":
            for field in REQUIRED_FIXED_EVIDENCE:
                if not str(row.get(field) or "").strip():
                    errors.append(f"{row_id}: fixed_verified missing {field}")
            commit = str(row.get("verified_commit") or "").strip()
            commit_exists = (
                _git_commit_exists(commit)
                if git_available
                else provenance_valid and _manifest_contains_commit(provenance, commit)
            )
            if commit and (git_available or provenance_valid) and not commit_exists:
                errors.append(f"{row_id}: verified_commit does not exist: {commit}")
            if severity in {"P0", "P1"} and not (
                str(row.get("runtime_evidence") or "").strip()
                or str(row.get("packaged_evidence") or "").strip()
            ):
                errors.append(f"{row_id}: fixed P0/P1 requires runtime or packaged evidence")
            if severity in {"P0", "P1"} and bool(row.get("requires_immutable_candidate")):
                candidate_commit = str(row.get("packaged_verified_commit") or "").strip()
                packaged_evidence = str(row.get("packaged_evidence") or "").strip()
                candidate_exists = (
                    _git_commit_exists(candidate_commit)
                    if git_available
                    else provenance_valid
                    and _manifest_contains_commit(provenance, candidate_commit)
                )
                if not candidate_commit or not candidate_exists:
                    errors.append(
                        f"{row_id}: fixed runtime row requires an immutable candidate commit"
                    )
                elif git_available and not _git_commit_contains(
                    candidate=candidate_commit, required=commit
                ):
                    errors.append(
                        f"{row_id}: immutable candidate does not contain verified_commit {commit}"
                    )
                elif not git_available and not _manifest_contains_commit(provenance, commit):
                    errors.append(
                        f"{row_id}: immutable packaged provenance does not contain verified_commit {commit}"
                    )
                if not packaged_evidence:
                    errors.append(
                        f"{row_id}: fixed runtime row requires post-fix packaged_evidence"
                    )

    expected = {f"RS5-{index:03d}" for index in range(1, 112)} | {"RS5-D01", "RS5-D02"}
    missing = sorted(expected - seen)
    unexpected = sorted(seen - expected)
    if missing:
        errors.append(f"missing rows: {', '.join(missing)}")
    if unexpected:
        errors.append(f"unexpected rows: {', '.join(unexpected)}")

    counts = Counter((str(row.get("severity")), str(row.get("technical_status"))) for row in rows)
    for (severity, status), count in sorted(counts.items()):
        print(f"{severity:28} {status:20} {count}")

    if errors:
        print("\nLedger validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if not git_available:
        print(
            "packaged provenance verified: "
            f"candidate={provenance['candidate_commit']} "
            f"images={len(provenance['image_digests'])}."
        )
    print(f"\nLedger valid: {len(rows)} rows; reviewer status is independent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
