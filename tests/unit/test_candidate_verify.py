from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "scripts" / "release" / "candidate_verify.sh"
PUBLISH = ROOT / "scripts" / "release" / "candidate_publish.sh"
COMMIT = "a" * 40


def _write_complete_provenance(release_dir: Path) -> None:
    source_manifest = release_dir / "candidate-source.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-candidate-source-v1",
                "candidate_commit": COMMIT,
                "source_sha256": "a" * 64,
                "entries": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release_input = ROOT / "scripts" / "release" / "candidate_verify.sh"
    machine = {"aarch64": "arm64", "x86_64": "amd64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    image_digests = {
        "fs-app": "sha256:" + "1" * 64,
        "fs-cli": "sha256:" + "2" * 64,
        "fs-mcp": "sha256:" + "3" * 64,
    }
    (release_dir / "candidate-provenance.json").write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-packaged-provenance-v2",
                "candidate_commit": COMMIT,
                "source_sha256": "a" * 64,
                "source_manifest_sha256": hashlib.sha256(source_manifest.read_bytes()).hexdigest(),
                "git_lfs_objects": [],
                "release_inputs": {
                    "scripts/release/candidate_verify.sh": {
                        "sha256": hashlib.sha256(release_input.read_bytes()).hexdigest(),
                        "size": release_input.stat().st_size,
                    }
                },
                "base_images": {"python": "python@sha256:" + "4" * 64},
                "infrastructure_images": {"postgres": "postgres@sha256:" + "5" * 64},
                "image_digests": image_digests,
                "build_arguments": {"BUILD_SHA": COMMIT},
                "architecture": f"linux/{machine}",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    fingerprint = "sha256:" + "6" * 64
    (release_dir / "candidate-snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-candidate-snapshot-v1",
                "harness_version": "phase-1",
                "candidate_commit": COMMIT,
                "compose_project": f"fs-candidate-{COMMIT[:12]}",
                "source_hashes": {},
                "input_presence": {},
                "protected_inputs": {},
                "plain_inputs": {},
                "requirements_digest": "sha256:" + "7" * 64,
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
                "candidate_commit": COMMIT,
                "image_digests": image_digests,
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


def _write_image_inspect_stub(bin_dir: Path) -> Path:
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  for argument in "$@"; do
    if [[ "$argument" == "--format" ]]; then
      printf '__COMMIT__\n'
      break
    fi
  done
  exit 0
fi
service=""
for argument in "$@"; do
  case "$argument" in
    label=com.docker.compose.service=*) service="${argument##*=}" ;;
  esac
done
case "$service" in
  fs-app) printf 'candidate-app\n' ;;
  fs-cli) printf 'candidate-cli\n' ;;
  pg-fs) printf 'candidate-pg\n' ;;
  fs-worker) printf 'candidate-worker\n' ;;
  mailpit) printf 'candidate-mailpit\n' ;;
  rabbitmq-fs) printf 'candidate-rabbit\n' ;;
  fs-mcp) printf 'candidate-mcp\n' ;;
  control-plane) printf 'candidate-control\n' ;;
  *) exit 2 ;;
esac
""".replace("__COMMIT__", COMMIT),
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return docker


def test_safe_candidate_verification_derives_runtime_identity_and_runs_once(tmp_path: Path) -> None:
    release_dir = tmp_path / "release" / COMMIT
    release_dir.mkdir(parents=True)
    (release_dir / "candidate.env").write_text(
        f"FS_BUILD_SHA={COMMIT}\nAPP_PORT=18443\nHTTP_PORT=18080\nMAILPIT_HTTP_PORT=18025\n",
        encoding="utf-8",
    )
    (release_dir / "production.env").write_text(
        "POSTGRES_PASSWORD=candidate-db-password\n"  # pragma: allowlist secret
        "FS_AUTH_BOOTSTRAP_ENABLED=0\n"
        "FS_AUTH_BOOTSTRAP_TOKEN=\n"
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT=\n",
        encoding="utf-8",
    )
    _write_complete_provenance(release_dir)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_image_inspect_stub(bin_dir)
    calls = tmp_path / "calls.jsonl"
    smoke = tmp_path / "smoke.py"
    smoke.write_text(
        """#!/usr/bin/env python3
import json, os, sys
names = [
    'FS_SMOKE_BASE', 'FS_SMOKE_INTERNAL_BASE', 'FS_SMOKE_MAILPIT_BASE',
    'FS_SMOKE_COMPOSE_PROJECT', 'FS_SMOKE_APP_CONTAINER',
    'FS_SMOKE_CLI_CONTAINER', 'FS_SMOKE_PG_CONTAINER', 'FS_SMOKE_WORKER_CONTAINER',
    'FS_SMOKE_MAILPIT_CONTAINER', 'FS_SMOKE_RABBITMQ_CONTAINER',
    'FS_SMOKE_DB_PASSWORD', 'FS_SMOKE_PG_PORT', 'FS_SMOKE_IMAGE_DIGESTS',
    'FS_SMOKE_PROFILES', 'FS_SMOKE_DEPLOY',
    'FS_SMOKE_VERIFY_CLEAN_EXIT',
]
with open(__CALLS_PATH__, 'a', encoding='utf-8') as handle:
    handle.write(json.dumps({'argv': sys.argv[1:], 'env': {n: os.environ.get(n) for n in names}}) + '\\n')
""".replace("__CALLS_PATH__", repr(str(calls))),
        encoding="utf-8",
    )
    smoke.chmod(0o755)
    mcp_ca_file = tmp_path / "candidate-ca.pem"
    mcp_ca_file.write_text("test candidate CA\n", encoding="utf-8")
    evidence_root = tmp_path / "logs/release/evidence"
    result = subprocess.run(
        ["bash", str(VERIFY), "--safe"],
        cwd=ROOT,
        env={
            **os.environ,
            "FS_RELEASE_CANDIDATE_COMMIT": COMMIT,
            "FS_RELEASE_DIR": str(release_dir),
            "FS_RELEASE_SMOKE_RUNNER": str(smoke),
            "FS_RELEASE_EVIDENCE_ROOT": str(evidence_root),
            "FS_RELEASE_RUN_ID": "safe-run-001",
            "FS_SMOKE_EMAIL": "audit@example.invalid",
            "FS_SMOKE_PASSWORD": "protected-password",  # pragma: allowlist secret
            "FS_SMOKE_BEARER_TOKEN": "protected-token",  # pragma: allowlist secret
            "FS_SMOKE_MODEL_PROVIDER": "custom:provider-1",
            "FS_SMOKE_MODEL": "luna",
            "FS_SMOKE_MCP_CA_FILE": str(mcp_ca_file),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert invocations == [
        {
            "argv": [],
            "env": {
                "FS_SMOKE_BASE": "https://localhost:18443",
                "FS_SMOKE_INTERNAL_BASE": "http://fs-app:8099",
                "FS_SMOKE_MAILPIT_BASE": "http://localhost:18025",
                "FS_SMOKE_COMPOSE_PROJECT": f"fs-candidate-{COMMIT[:12]}",
                "FS_SMOKE_APP_CONTAINER": "candidate-app",
                "FS_SMOKE_CLI_CONTAINER": "candidate-cli",
                "FS_SMOKE_PG_CONTAINER": "candidate-pg",
                "FS_SMOKE_WORKER_CONTAINER": "candidate-worker",
                "FS_SMOKE_MAILPIT_CONTAINER": "candidate-mailpit",
                "FS_SMOKE_RABBITMQ_CONTAINER": "candidate-rabbit",
                "FS_SMOKE_DB_PASSWORD": "candidate-db-password",  # pragma: allowlist secret
                "FS_SMOKE_PG_PORT": "5432",
                "FS_SMOKE_IMAGE_DIGESTS": ",".join(
                    (
                        "sha256:" + "1" * 64,
                        "sha256:" + "2" * 64,
                        "sha256:" + "3" * 64,
                    )
                ),
                "FS_SMOKE_PROFILES": "oss,tenant",
                "FS_SMOKE_DEPLOY": "0",
                "FS_SMOKE_VERIFY_CLEAN_EXIT": "1",
            },
        }
    ]
    manifest = evidence_root / COMMIT / "safe-run-001" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["candidate_commit"] == COMMIT
    assert payload["run_id"] == "safe-run-001"
    assert [(phase["name"], phase["status"]) for phase in payload["phases"]] == [
        ("safe-profile", "passed")
    ]
    assert payload["artifacts"][0]["phase"] == "safe-profile"


def test_candidate_verification_rejects_unfinalized_bootstrap_before_smoke(
    tmp_path: Path,
) -> None:
    release_dir = tmp_path / "release" / COMMIT
    release_dir.mkdir(parents=True)
    (release_dir / "candidate.env").write_text(
        f"FS_BUILD_SHA={COMMIT}\nAPP_PORT=18443\nMAILPIT_HTTP_PORT=18025\n",
        encoding="utf-8",
    )
    (release_dir / "production.env").write_text(
        "POSTGRES_PASSWORD=candidate-db-password\n"  # pragma: allowlist secret
        "FS_AUTH_BOOTSTRAP_ENABLED=1\n"
        "FS_AUTH_BOOTSTRAP_TOKEN=temporary-token\n"  # pragma: allowlist secret
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT=2099-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    _write_complete_provenance(release_dir)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_image_inspect_stub(bin_dir)
    smoke = tmp_path / "smoke.sh"
    smoke.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    smoke.chmod(0o755)

    result = subprocess.run(
        ["bash", str(VERIFY), "--safe"],
        cwd=ROOT,
        env={
            **os.environ,
            "FS_RELEASE_CANDIDATE_COMMIT": COMMIT,
            "FS_RELEASE_DIR": str(release_dir),
            "FS_RELEASE_SMOKE_RUNNER": str(smoke),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "candidate bootstrap is not finalized" in result.stderr


def test_release_candidate_verification_requires_detached_checkout_before_smoke(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        '#!/usr/bin/env bash\ncase "$*" in *symbolic-ref*) exit 0 ;; esac\nexit 99\n',
        encoding="utf-8",
    )
    git.chmod(0o755)
    smoke_called = tmp_path / "smoke-called"
    smoke = tmp_path / "smoke.sh"
    smoke.write_text(
        f"#!/usr/bin/env bash\ntouch {shlex.quote(str(smoke_called))}\n",
        encoding="utf-8",
    )
    smoke.chmod(0o755)

    result = subprocess.run(
        ["bash", str(VERIFY), "--release"],
        cwd=ROOT,
        env={
            **os.environ,
            "FS_RELEASE_CANDIDATE_COMMIT": COMMIT,
            "FS_RELEASE_SMOKE_RUNNER": str(smoke),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "exact detached checkout" in result.stderr
    assert not smoke_called.exists()


def test_candidate_publication_runs_safe_verification_once_after_runtime_start(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "publication-calls.txt"

    def command(name: str) -> Path:
        path = tmp_path / name
        path.write_text(
            f"#!/usr/bin/env bash\nprintf '%s %s\\n' '{name}' \"$*\" >>{shlex.quote(str(calls))}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    result = subprocess.run(
        ["bash", str(PUBLISH)],
        cwd=ROOT,
        env={
            **os.environ,
            "FS_RELEASE_CANDIDATE_COMMIT": COMMIT,
            "FS_RELEASE_BUILD_SCRIPT": str(command("build")),
            "FS_RELEASE_PREPARE_SCRIPT": str(command("prepare")),
            "FS_RELEASE_STACK_SCRIPT": str(command("stack")),
            "FS_RELEASE_VERIFY_SCRIPT": str(command("verify")),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "build ",
        "prepare all",
        "stack --profile cli up -d --no-build",
        "verify --safe",
    ]
