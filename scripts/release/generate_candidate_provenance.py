#!/usr/bin/env python3
"""Generate the immutable provenance input used by core-only release validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "infra/tooling/release/ledger.yaml"
DEFAULT_VALIDATOR = DEFAULT_LEDGER.with_name("validate_ledger.py")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
PINNED_IMAGE_PATTERN = re.compile(r"(?:.+@)?sha256:[0-9a-f]{64}")


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_digest(value: str) -> tuple[str, str]:
    name, separator, digest = str(value or "").partition("=")
    name = name.strip()
    digest = digest.strip().lower()
    if not separator or not name or not DIGEST_PATTERN.fullmatch(digest):
        raise argparse.ArgumentTypeError("image digest must be SERVICE=sha256:<64 hex>")
    return name, digest


def _parse_input_source(value: str) -> tuple[str, str]:
    name, separator, source = str(value or "").partition("=")
    if not separator or not name.strip() or not source.strip():
        raise argparse.ArgumentTypeError("deployment input must be NAME=SOURCE_PATH")
    return name.strip(), source.strip()


def _parse_named_value(value: str) -> tuple[str, str]:
    name, separator, item = str(value or "").partition("=")
    if not separator or not name.strip() or not item.strip():
        raise argparse.ArgumentTypeError("value must be NAME=VALUE")
    return name.strip(), item.strip()


def _parse_pinned_image(value: str) -> tuple[str, str]:
    name, image = _parse_named_value(value)
    if not PINNED_IMAGE_PATTERN.fullmatch(image):
        raise argparse.ArgumentTypeError(
            "release image must be NAME=[REGISTRY/]IMAGE@sha256:<64 hex> or NAME=sha256:<64 hex>"
        )
    return name, image


def _deployment_input_record(name: str, source: str) -> dict[str, object]:
    path = Path(source)
    if not path.is_file() or not os.access(path, os.R_OK) or path.stat().st_size <= 0:
        raise SystemExit(f"deployment input {name} source is not readable: {source}")
    if path.name.startswith(".env"):
        present = False
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name and value.strip():
                present = True
                break
        if not present:
            raise SystemExit(f"deployment input {name} is not present in {source}")
    return {"present": True}


def _release_input_record(source: str) -> dict[str, object]:
    path = Path(source)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise SystemExit(f"release input is not readable: {source}")
    return {"sha256": _sha256(path), "size": path.stat().st_size}


def _ledger_commits(ledger: dict) -> set[str]:
    return {
        str(row.get(field) or "").strip().lower()
        for row in ledger.get("rows") or []
        for field in ("verified_commit", "packaged_verified_commit")
        if str(row.get(field) or "").strip()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-commit", default="HEAD")
    parser.add_argument("--image-digest", action="append", type=_parse_digest, required=True)
    parser.add_argument("--deployment-input-source", action="append", type=_parse_input_source)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--sdk-attestation", type=Path, required=True)
    parser.add_argument("--release-input", action="append", type=_parse_named_value, required=True)
    parser.add_argument("--base-image", action="append", type=_parse_pinned_image, required=True)
    parser.add_argument(
        "--infrastructure-image", action="append", type=_parse_pinned_image, required=True
    )
    parser.add_argument("--build-argument", action="append", type=_parse_named_value, required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--validator", type=Path, default=DEFAULT_VALIDATOR)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    candidate_commit = _git("rev-parse", f"{args.candidate_commit}^{{commit}}")
    ancestors = [line for line in _git("rev-list", candidate_commit).splitlines() if line]
    ancestor_set = set(ancestors)
    ledger_path = args.ledger.resolve()
    validator_path = args.validator.resolve()
    ledger = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    unresolved = sorted(
        commit
        for commit in _ledger_commits(ledger)
        if not any(ancestor.startswith(commit) for ancestor in ancestor_set)
    )
    if unresolved:
        raise SystemExit(
            "candidate does not contain ledger verified commits: " + ", ".join(unresolved)
        )

    output = args.output
    if output is None:
        output = ROOT / "logs/release" / candidate_commit / "candidate-provenance.json"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    source_manifest_path = args.source_manifest.resolve()
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if (
        source_manifest.get("schema_version") != "flow-steward-candidate-source-v1"
        or source_manifest.get("candidate_commit") != candidate_commit
        or not re.fullmatch(r"[0-9a-f]{64}", str(source_manifest.get("source_sha256") or ""))
    ):
        raise SystemExit("candidate source manifest is invalid or belongs to another commit")
    sdk_attestation_path = args.sdk_attestation.resolve()
    sdk_attestation = json.loads(sdk_attestation_path.read_text(encoding="utf-8"))
    if (
        sdk_attestation.get("schema") != "flow-steward-extension-sdk-wheel-v2"
        or not re.fullmatch(r"[0-9a-f]{64}", str(sdk_attestation.get("source_sha256") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(sdk_attestation.get("wheel_sha256") or ""))
        or not str(sdk_attestation.get("version") or "").strip()
        or not str(sdk_attestation.get("wheel_filename") or "").strip()
    ):
        raise SystemExit("extension SDK wheel attestation is invalid")
    payload = {
        "schema_version": "flow-steward-packaged-provenance-v2",
        "candidate_commit": candidate_commit,
        "image_digests": dict(args.image_digest),
        "deployment_inputs": {
            name: _deployment_input_record(name, source)
            for name, source in (args.deployment_input_source or [])
        },
        "source_manifest_sha256": _sha256(source_manifest_path),
        "source_sha256": source_manifest["source_sha256"],
        "extension_sdk": sdk_attestation,
        "extension_sdk_attestation_sha256": _sha256(sdk_attestation_path),
        "release_inputs": {
            name: _release_input_record(source) for name, source in args.release_input
        },
        "git_lfs_objects": [
            {
                "path": entry["path"],
                "oid": entry["lfs_oid"],
                "size": entry["lfs_size"],
            }
            for entry in source_manifest.get("entries") or []
            if "lfs_oid" in entry
        ],
        "base_images": dict(args.base_image),
        "infrastructure_images": dict(args.infrastructure_image),
        "build_arguments": dict(args.build_argument),
        "architecture": args.architecture,
        "ledger_sha256": _sha256(ledger_path),
        "validator_sha256": _sha256(validator_path),
        "ancestor_commits": ancestors,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
