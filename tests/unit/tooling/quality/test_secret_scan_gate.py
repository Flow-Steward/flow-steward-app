"""Exercise the actual shell gate with the installed pinned detector."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "core/entrypoints/cli/scripts/lint.sh"


def test_gate_retry_scans_only_the_failed_changed_file_and_exports_proofs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    baseline = json.loads((ROOT / ".secrets.baseline").read_text())
    baseline["results"] = {}
    baseline["generated_at"] = datetime.now(UTC).isoformat()
    (repo / ".secrets.baseline").write_text(json.dumps(baseline))
    original_baseline = (repo / ".secrets.baseline").read_bytes()
    (repo / "clean.py").write_text("VALUE = 1\n")
    (repo / "flagged.py").write_text("PASS" + "WORD = 'unit-only-placeholder-value'\n")
    (repo / "extensions/example").mkdir(parents=True)
    (repo / "extensions/example/external.py").write_text("VALUE = 3\n")
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Unit",
            "-c",
            "user.email=unit@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "producer",
        ],
        check=True,
    )
    source_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    package = repo / "core/application/tooling/quality"
    package.mkdir(parents=True)
    for name in ("file_scan_cache.py", "secret_scan.py"):
        (package / name).write_bytes(
            (ROOT / "core/application/tooling/quality" / name).read_bytes()
        )
    (repo / "core/entrypoints/cli/scripts").mkdir(parents=True)
    (repo / "core/entrypoints/cli/scripts/lint.sh").write_bytes(SCRIPT.read_bytes())
    for name in (
        "infra/docker/Dockerfile",
        "infra/docker/sitecustomize.py",
        "requirements-audit.txt",
        "requirements-runtime.lock",
        "requirements-cli.lock",
        "requirements-semgrep.txt",
    ):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    calls = tmp_path / "calls.jsonl"
    python = tmp_path / "python-spy"
    python.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        f"with open({str(calls)!r},'a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    python.chmod(0o755)
    source = SCRIPT.read_text()
    toolchain = source[
        source.index("audit_toolchain_digest() {") : source.index("\n# Compose builds")
    ]
    function = source[source.index("run_detect_secrets() {") : source.index("\nrun_pip_audit() {")]
    runner = tmp_path / "gate.sh"
    runner.write_text(toolchain + "\n" + function + "\nrun_detect_secrets\n")
    env = {
        **os.environ,
        "PYTHON_BIN": str(python),
        "EVIDENCE_MODE": "reuse",
        "SCRIPT_DIR": str(SCRIPT.parent),
        "ROOT_DIR": str(repo),
        "HOME": str(tmp_path / "producer-home"),
    }
    (Path(env["HOME"]) / ".cache/flow-steward-quality").mkdir(parents=True)
    durations = []

    def run():
        started = time.monotonic()
        result = subprocess.run(
            ["bash", str(runner)], cwd=repo, env=env, text=True, capture_output=True
        )
        durations.append(time.monotonic() - started)
        assert (repo / ".secrets.baseline").read_bytes() == original_baseline
        return result

    first = run()
    assert first.returncode == 1, first.stdout + first.stderr
    manifest = repo / "logs/quality/file-results/detect-secrets.json"
    assert manifest.is_file(), "completed clean-file proofs were lost with the failed batch"
    assert [item["path"] for item in json.loads(manifest.read_text())["files"]] == ["clean.py"]

    # A fresh runner and checkout import only file proofs, then change SHA.
    new_repo = tmp_path / "consumer-repo"
    repo.rename(new_repo)
    repo = new_repo
    manifest = repo / "logs/quality/file-results/detect-secrets.json"
    env["ROOT_DIR"] = str(repo)
    env["HOME"] = str(tmp_path / "consumer-home")
    cache_root = Path(env["HOME"]) / ".cache/flow-steward-quality/file-results"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "core.application.tooling.quality.file_scan_cache",
            "import",
            "--cache-root",
            str(cache_root),
            "--source",
            str(manifest),
        ],
        check=True,
    )
    (repo / "flagged.py").write_text("VALUE = 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "flagged.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Unit",
            "-c",
            "user.email=unit@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "repair",
        ],
        check=True,
    )
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]) != source_sha
    retry = run()
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "scanning 1 of 2 files" in retry.stdout
    assert len(json.loads(manifest.read_text())["files"]) == 2
    warm = run()
    assert warm.returncode == 0, warm.stdout + warm.stderr
    hook_calls = [
        json.loads(line)
        for line in calls.read_text().splitlines()
        if "secret_scan" in line and '"hook"' in line
    ]
    assert len(hook_calls) == 2, "the unchanged third gate repeated an already completed scan"
    assert "clean.py" in hook_calls[0] and "flagged.py" in hook_calls[0]
    assert "clean.py" not in hook_calls[1] and "flagged.py" in hook_calls[1]
    assert not any(path.startswith("extensions/") for call in hook_calls for path in call)
    # A dependency change must invalidate file proofs even at the same scanner version.
    dependencies = repo / "requirements-audit.txt"
    dependencies.write_text(dependencies.read_text() + "\n# changed scanner toolchain\n")
    upgraded = run()
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert "scanning 2 of 2 files" in upgraded.stdout
    print(
        json.dumps(
            {
                "scope": "actual shell gate with real detector, cross-checkout/SHA import",
                "scanned_files": [2, 1, 0, 2],
                "wall_seconds": durations,
            }
        )
    )
    env["EVIDENCE_MODE"] = "off"
    diagnostic = run()
    assert diagnostic.returncode == 0, diagnostic.stdout + diagnostic.stderr
    assert "scanning 2 of 2 files" in diagnostic.stdout
    assert not manifest.exists(), "evidence-off exported reusable passes"
