from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "infra/tooling/release/validate_ledger.py"
LEDGER = ROOT / "infra/tooling/release/ledger.yaml"
PROVENANCE_GENERATOR = ROOT / "scripts/release/generate_candidate_provenance.py"


def _git_metadata_available() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _without_git_path(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    git = bin_dir / "git"
    git.write_text("#!/usr/bin/env sh\nexit 127\n", encoding="utf-8")
    git.chmod(0o755)
    return f"{bin_dir}:{os.environ.get('PATH', '')}"


def _packaged_provenance(tmp_path: Path, ledger_path: Path = LEDGER) -> Path:
    payload = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    verified_commits = sorted(
        {
            str(row.get(field) or "").strip()
            for row in payload["rows"]
            for field in ("verified_commit", "packaged_verified_commit")
            if str(row.get(field) or "").strip()
        }
    )
    provenance = {
        "schema_version": "flow-steward-packaged-provenance-v2",
        "candidate_commit": "a" * 40,
        "image_digests": {"fs-app": "sha256:" + "b" * 64},
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "validator_sha256": hashlib.sha256(VALIDATOR.read_bytes()).hexdigest(),
        "ancestor_commits": [commit.ljust(40, "0") for commit in verified_commits],
    }
    path = tmp_path / "candidate-provenance.json"
    path.write_text(json.dumps(provenance), encoding="utf-8")
    return path


def test_validator_accepts_the_authoritative_contiguous_rs5_row_set(
    tmp_path: Path,
) -> None:
    """Catch a validator that treats the latest release-audit intake rows as foreign."""
    args = [sys.executable, str(VALIDATOR)]
    if not _git_metadata_available():
        provenance = _packaged_provenance(tmp_path)
        args.extend([str(LEDGER), "--provenance", str(provenance)])
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "unexpected rows" not in result.stderr
    assert "missing rows" not in result.stderr


def test_validator_rejects_runtime_closure_without_post_fix_candidate(tmp_path: Path) -> None:
    payload = yaml.safe_load(LEDGER.read_text(encoding="utf-8"))
    row = next(item for item in payload["rows"] if item["id"] == "RS5-012")
    row["technical_status"] = "fixed_verified"
    row["verified_commit"] = row["source_fix_commit"]
    row["packaged_verified_commit"] = ""
    row["packaged_evidence"] = ""
    candidate = tmp_path / "ledger.yaml"
    candidate.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(candidate)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "fixed runtime row requires an immutable candidate commit" in result.stderr
    assert "fixed runtime row requires post-fix packaged_evidence" in result.stderr


def test_core_only_validator_requires_packaged_provenance_when_git_is_absent(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        cwd=ROOT,
        env={**os.environ, "PATH": _without_git_path(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "packaged provenance is required when Git metadata is unavailable" in result.stderr


def test_core_only_validator_accepts_exact_packaged_provenance(tmp_path: Path) -> None:
    provenance = _packaged_provenance(tmp_path)

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(LEDGER), "--provenance", str(provenance)],
        cwd=ROOT,
        env={**os.environ, "PATH": _without_git_path(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "packaged provenance verified" in result.stdout


def test_core_only_validator_rejects_tampered_ledger(tmp_path: Path) -> None:
    provenance = _packaged_provenance(tmp_path)
    tampered = tmp_path / "ledger.yaml"
    tampered.write_bytes(LEDGER.read_bytes() + b"\n# tampered after packaging\n")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(tampered), "--provenance", str(provenance)],
        cwd=ROOT,
        env={**os.environ, "PATH": _without_git_path(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "ledger hash does not match packaged provenance" in result.stderr


def test_provenance_generator_emits_validator_accepted_manifest(tmp_path: Path) -> None:
    provenance = tmp_path / "generated-provenance.json"
    if _git_metadata_available():
        candidate_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=ROOT, text=True
        ).strip()
        source_manifest = tmp_path / "candidate-source.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "flow-steward-candidate-source-v1",
                    "candidate_commit": candidate_commit,
                    "source_sha256": "d" * 64,
                }
            ),
            encoding="utf-8",
        )
        sdk_attestation = tmp_path / "sdk-wheel-attestation.json"
        sdk_attestation.write_text(
            json.dumps(
                {
                    "schema": "flow-steward-extension-sdk-wheel-v2",
                    "version": "0.2.0",
                    "source_sha256": "a" * 64,
                    "wheel_sha256": "b" * 64,
                    "wheel_filename": "flowsteward_extension_sdk-0.2.0-py3-none-any.whl",
                }
            ),
            encoding="utf-8",
        )
        generated = subprocess.run(
            [
                sys.executable,
                str(PROVENANCE_GENERATOR),
                "--candidate-commit",
                "HEAD",
                "--image-digest",
                "fs-app=sha256:" + "c" * 64,
                "--source-manifest",
                str(source_manifest),
                "--sdk-attestation",
                str(sdk_attestation),
                "--release-input",
                f"candidate-source.json={source_manifest}",
                "--base-image",
                "python=sha256:" + "e" * 64,
                "--infrastructure-image",
                "postgres=sha256:" + "f" * 64,
                "--build-argument",
                f"BUILD_SHA={candidate_commit}",
                "--architecture",
                (
                    "linux/arm64"
                    if os.uname().machine.lower() in {"arm64", "aarch64"}
                    else "linux/amd64"
                ),
                "--output",
                str(provenance),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert generated.returncode == 0, generated.stderr or generated.stdout
    else:
        provenance = _packaged_provenance(tmp_path)

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(LEDGER), "--provenance", str(provenance)],
        cwd=ROOT,
        env={**os.environ, "PATH": _without_git_path(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
