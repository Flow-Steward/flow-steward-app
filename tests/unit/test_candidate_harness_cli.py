from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.application.release.candidate_cleanup import DockerInventory
from core.application.release.candidate_harness import (
    CandidateHarness,
    CandidateRunIdentity,
    InputFailure,
    ProductFailure,
    phase,
)
from core.application.release.candidate_store import CandidateEvidenceStore
from core.entrypoints.cli.ops import candidate_harness as candidate_harness_module
from core.entrypoints.cli.ops.candidate_harness import main

SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[2]


def _write_finalized_candidate(root: Path) -> Path:
    release_dir = root / ".flow-steward" / "release" / SHA
    release_dir.mkdir(parents=True)
    fingerprint = "sha256:" + "b" * 64
    (release_dir / "candidate-snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-candidate-snapshot-v1",
                "harness_version": "phase-1",
                "candidate_commit": SHA,
                "compose_project": "fs-candidate-aaaaaaaaaaaa",
                "source_hashes": {},
                "input_presence": {},
                "protected_inputs": {},
                "plain_inputs": {},
                "requirements_digest": "sha256:" + "c" * 64,
                "image_fingerprints": {},
                "fingerprint": fingerprint,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (release_dir / "candidate-finalized.json").write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-candidate-finalized-v1",
                "expected_fingerprint": fingerprint,
                "candidate_commit": SHA,
                "image_digests": {"fs-app": "sha256:" + "d" * 64},
                "resource_identities": {},
                "compose_working_dir": None,
                "compose_config_files": None,
                "extension_digest": None,
                "provenance_checksum": None,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return release_dir


class FakeService:
    def __init__(self) -> None:
        self.cleanup_calls = 0

    def snapshot(self, _args):
        return {"status": "materialized"}

    def preflight(self, _args):
        raise InputFailure("FS_SMOKE_BEARER_TOKEN is missing")

    def status(self, _args):
        return {"status": "not-found"}

    def cleanup(self, _args):
        self.cleanup_calls += 1
        return {"status": "cleaned"}


def test_preflight_reports_first_failure_with_stable_exit_code(capsys) -> None:
    result = main(["preflight", "--candidate", SHA], service=FakeService())

    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["failure_class"] == "input"
    assert "FS_SMOKE_BEARER_TOKEN" in output["reason"]


def test_cleanup_requires_explicit_confirmation(capsys) -> None:
    service = FakeService()

    result = main(["cleanup", "--candidate", SHA], service=service)

    captured = capsys.readouterr()
    assert result == 2
    assert "--confirm" in captured.err
    assert service.cleanup_calls == 0


def test_candidate_path_requires_a_full_sha(tmp_path: Path, capsys) -> None:
    result = main(
        ["status", "--root", str(tmp_path), "--candidate", "../../shared"],
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["failure_class"] == "input"
    assert not (tmp_path.parent / "shared").exists()


def test_snapshot_materialize_reuse_finalize_and_status_share_one_identity(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "scripts" / "smoke", root / "scripts" / "smoke")
    for source, relative in (
        (ROOT / "infra/docker/Dockerfile", "infra/docker/Dockerfile"),
        (ROOT / "infra/mcp/Dockerfile", "infra/mcp/Dockerfile"),
        (
            ROOT / "infra/docker/docker-compose.prod.yaml",
            "infra/docker/docker-compose.prod.yaml",
        ),
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    source_env = root / "candidate.env"
    source_env.write_text("FS_SMOKE_BASE=https://candidate.invalid\n", encoding="utf-8")
    common = [
        "snapshot",
        "--root",
        str(root),
        "--candidate",
        SHA,
        "--source-env",
        str(source_env),
    ]

    assert main([*common, "--materialize"]) == 0
    materialized = json.loads(capsys.readouterr().out)
    assert materialized["reused"] is False
    assert main([*common, "--materialize"]) == 0
    reused = json.loads(capsys.readouterr().out)
    assert reused["reused"] is True
    assert reused["fingerprint"] == materialized["fingerprint"]

    extensions = root / "candidate-extensions"
    extensions.mkdir()
    (extensions / "manifest.txt").write_text("immutable\n", encoding="utf-8")
    assert (
        main(
            [
                *common,
                "--finalize",
                "--image-digest",
                f"fs-app=sha256:{'b' * 64}",
                "--image-tag",
                "flow-steward:candidate-aaaaaaaaaaaa",
                "--extension-dir",
                str(extensions),
            ]
        )
        == 0
    )
    finalized = json.loads(capsys.readouterr().out)
    assert finalized["status"] == "finalized"
    assert finalized["extension_digest"].startswith("sha256:")

    assert main(["status", "--root", str(root), "--candidate", SHA]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "finalized"
    assert status["fingerprint"] == materialized["fingerprint"]


def test_release_verify_and_status_share_exact_candidate_run_identity(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "repo"
    release_dir = _write_finalized_candidate(root)
    calls = tmp_path / "calls.txt"
    runner = tmp_path / "smoke.sh"
    runner.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >>'{calls}'\nprintf 'profile=%s passed\\n' \"$*\"\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    evidence_root = root / "logs/release/evidence"

    result = main(
        [
            "verify",
            "--root",
            str(root),
            "--candidate",
            SHA,
            "--release-dir",
            str(release_dir),
            "--evidence-root",
            str(evidence_root),
            "--run-id",
            "release-run-001",
            "--mode",
            "release",
            "--runner",
            str(runner),
        ]
    )

    verify_output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result == 0
    assert verify_output["run_id"] == "release-run-001"
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--profile release",
        "--profile release",
    ]
    manifest = evidence_root / SHA / "release-run-001" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert [(phase["name"], phase["status"]) for phase in payload["phases"]] == [
        ("release-profile-1", "passed"),
        ("release-profile-2", "passed"),
    ]
    assert len(payload["artifacts"]) == 2

    assert (
        main(
            [
                "status",
                "--root",
                str(root),
                "--candidate",
                SHA,
                "--evidence-root",
                str(evidence_root),
                "--run-id",
                "release-run-001",
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    assert status["evidence_verified"] is True
    assert status["run_id"] == "release-run-001"


def test_failed_verify_retains_external_log_and_returns_product_exit(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "repo"
    release_dir = _write_finalized_candidate(root)
    runner = tmp_path / "smoke.sh"
    runner.write_text(
        "#!/usr/bin/env bash\nprintf '71 PASS / 1 FAIL\\n'\nexit 9\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    evidence_root = root / "logs/release/evidence"

    result = main(
        [
            "verify",
            "--root",
            str(root),
            "--candidate",
            SHA,
            "--release-dir",
            str(release_dir),
            "--evidence-root",
            str(evidence_root),
            "--run-id",
            "failed-run-001",
            "--mode",
            "safe",
            "--runner",
            str(runner),
        ]
    )

    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result == 4
    assert output["failure_class"] == "product"
    manifest = evidence_root / SHA / "failed-run-001" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["phases"][0]["status"] == "failed"
    retained_log = evidence_root / SHA / "failed-run-001" / payload["artifacts"][0]["path"]
    assert retained_log.read_text(encoding="utf-8") == "71 PASS / 1 FAIL\n"


def test_failed_run_verified_evidence_allows_only_exact_run_cleanup(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    _write_finalized_candidate(root)
    evidence_root = root / "logs/release/evidence"
    source_log = tmp_path / "failed.log"
    source_log.write_text("product failure\n", encoding="utf-8")

    def fail() -> None:
        raise ProductFailure("profile failed", log_paths=(source_log,))

    CandidateHarness(
        identity=CandidateRunIdentity(
            candidate_commit=SHA,
            fingerprint="sha256:" + "b" * 64,
            compose_project="fs-candidate-aaaaaaaaaaaa",
            run_id="failed-run-001",
            image_digests={"fs-app": "sha256:" + "d" * 64},
        ),
        store=CandidateEvidenceStore(evidence_root),
    ).run((phase("safe-profile", fail),))

    class Gateway:
        def inventory(self) -> DockerInventory:
            return DockerInventory()

    executed = []
    monkeypatch.setattr(candidate_harness_module, "DockerCliGateway", Gateway)
    monkeypatch.setattr(
        candidate_harness_module,
        "execute_cleanup",
        lambda plan, **_kwargs: executed.append(plan),
    )

    common = [
        "cleanup",
        "--root",
        str(root),
        "--candidate",
        SHA,
        "--evidence-root",
        str(evidence_root),
        "--confirm",
    ]
    assert main([*common, "--run-id", "other-run"]) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["failure_class"] == "harness"
    assert executed == []

    assert main([*common, "--run-id", "failed-run-001"]) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert cleaned["run_id"] == "failed-run-001"
    assert cleaned["external_evidence_verified"] is True
    assert len(executed) == 1


@pytest.mark.parametrize(
    "args,expected_exit,expected_error",
    [(["--help"], 0, ""), (["not-a-candidate-command"], 2, "invalid choice")],
)
def test_main_returns_parser_exit_without_invoking_service(
    capsys, args: list[str], expected_exit: int, expected_error: str
) -> None:
    class UnusedService:
        def __getattr__(self, name):
            pytest.fail(f"parser-only invocation must not access service action {name}")

    assert main(args, service=UnusedService()) == expected_exit

    output = capsys.readouterr()
    if expected_error:
        assert output.out == ""
        assert "usage:" in output.err
        assert expected_error in output.err
    else:
        assert "usage:" in output.out
        assert "snapshot" in output.out
        assert "preflight" in output.out
        assert output.err == ""
