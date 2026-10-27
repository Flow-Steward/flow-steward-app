from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LINT_SCRIPT = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "lint.sh"
NPM_AUDIT_RETRY = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "npm_audit_retry.sh"
SEMGREP_RETRY = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "semgrep_retry.sh"
FAILURE_POLICY_FILES = (
    ROOT / "AGENTS.md",
    ROOT / "docs" / "standards" / "testing-standard.md",
    ROOT / "docs" / "standards" / "agent-workflow-standard.md",
    ROOT / "docs" / "standards" / "enforcement-matrix.md",
)


def _shell_function(source: str, name: str, next_name: str) -> str:
    return source.split(f"{name}()", 1)[1].split(f"{next_name}()", 1)[0]


def test_targeted_frontend_runner_works_without_bundled_extensions(tmp_path: Path) -> None:
    frontend = tmp_path / "assets" / "frontend" / "src"
    frontend.mkdir(parents=True)
    (frontend / "probe.test.tsx").write_text("export {};\n", encoding="utf-8")
    body = _shell_function(
        LINT_SCRIPT.read_text(encoding="utf-8"),
        "run_check_frontend_targeted_tests",
        "run_check_frontend_build",
    )
    script = (
        "set -euo pipefail\n"
        'ROOT_DIR="$1"\nTEST_PATHS=src/probe.test.tsx\n'
        'prepare_frontend_workspace() { mkdir -p "$2/frontend"; }\n'
        'run_step() { printf "%s\\n" "$1"; }\n'
        f"run_check_frontend_targeted_tests(){body}\n"
        "run_check_frontend_targeted_tests\n"
    )
    result = subprocess.run(
        ["bash", "-s", "--", str(tmp_path)],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "Frontend targeted workspace install",
        "Frontend targeted setup",
        "Frontend targeted tests",
    ]


def test_legacy_modes_execute_only_their_owned_tier() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    mode_case = source.rsplit('case "${MODE}" in', 1)[1].split("esac", 1)[0]

    assert "fast) tier=2" in mode_case
    assert "full) tier=3" in mode_case
    assert "core.application.tooling.quality.verify" in source


def test_host_gate_repairs_and_checks_quality_permissions_before_docker_setup() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    host_branch = source.split('if [[ "${INSIDE}" -eq 0 && "${_in_container}" -eq 0 ]]; then', 1)[1]

    repair = 'quality_permissions.sh" repair --repo-root "${ROOT_DIR}"'
    check = 'quality_permissions.sh" check --repo-root "${ROOT_DIR}"'
    docker_setup = "fs_compose up -d --wait pg-audit rabbitmq-audit"

    assert repair in host_branch
    assert check in host_branch
    assert host_branch.index(repair) < host_branch.index(check) < host_branch.index(docker_setup)
    assert "chmod a+rwx" not in host_branch


def test_host_gate_requests_one_container_permission_preflight() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")

    assert "local_inside_args=(--inside --permission-preflight)" in source
    assert 'if [[ "${permission_preflight}" -eq 1 ]]; then' in source
    assert "permission_preflight=1" in source


def test_frontend_tier_three_node_does_not_repeat_lint_or_audit() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    body = _shell_function(
        source, "run_check_frontend_test_build", "run_check_frontend_dependency_audit"
    )

    assert "npm run test" in body
    assert "npm run build" in body
    assert "npm run lint" not in body
    assert "npm_audit_retry.sh" not in body


def test_frontend_tier_two_lint_does_not_repeat_dependency_audit() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    body = _shell_function(source, "run_check_frontend_lint", "run_check_frontend_test_build")

    assert "npm run lint" in body
    assert "npm_audit_retry.sh" not in body


def test_backend_tier_three_collects_all_unit_test_failures() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    body = _shell_function(
        source, "run_check_unit_tests_with_coverage", "assert_audit_test_isolation"
    )

    assert "-m pytest -q -ra -x tests/unit" not in body
    assert "${pytest_fail_fast_arg}tests/unit" in body


def test_backend_unit_suite_runs_on_a_fixed_number_of_xdist_workers() -> None:
    """ADR 0037: a fixed worker count, loadgroup for xdist_group, one coverage report."""
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    body = _shell_function(
        source, "run_check_unit_tests_with_coverage", "assert_audit_test_isolation"
    )

    assert "local unit_test_workers=4" in body
    assert 'pytest_parallel_args="-n ${unit_test_workers} --dist loadgroup "' in body
    assert "-m pytest -q -ra ${pytest_parallel_args}${pytest_fail_fast_arg}tests/unit" in body
    assert "--cov-report=xml:logs/coverage/unit/coverage.xml" in body
    assert "| tee logs/coverage/unit/coverage.txt" in body
    # Workers must collect identical test lists; one hash seed per run keeps them in step.
    assert 'unit_test_hash_seed="$(od -An -N4 -tu4 /dev/urandom' in body
    assert (
        "PYTHONHASHSEED=${unit_test_hash_seed} ./core/entrypoints/cli/scripts/run-core-only-tests.sh"
        in body
    )
    targeted = _shell_function(
        source, "run_check_targeted_tests", "run_check_unit_tests_with_coverage"
    )
    assert "pytest_parallel_args" not in targeted
    assert "-m pytest -q -x --no-cov" in targeted


def test_selected_backend_test_check_remains_fail_fast() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    body = _shell_function(
        source, "run_check_unit_tests_with_coverage", "assert_audit_test_isolation"
    )

    assert 'if [[ ${#REQUESTED_CHECKS[@]} -gt 0 && "${quality_owner_tier}" != "3" ]]; then' in body
    assert 'pytest_fail_fast_arg="-x "' in body


def _container_dispatch_block(source: str) -> str:
    start = source.index("local_inside_args=(--inside --permission-preflight)")
    end = source.index("lint_container_env_args=()", start)
    return source[start:end]


def test_every_host_flag_reaches_the_container(tmp_path: Path) -> None:
    """AC17-R89: the guards below watch where `-x` is decided, not where it is delivered.

    `--quality-owner-tier` was parsed and validated on the host and then dropped
    while building the container command, so the canonical backend-tests entry in
    infra/tooling/quality/check-registry.json always ran fail-fast -- against the
    MUST in docs/standards/testing-standard.md -- and every text assertion about
    the fail-fast condition still passed. `--evidence-mode` was dropped the same
    way, leaving the container on its `reuse` default. The owner reports this rule
    has come back more than once; a guard on the delivery site is what was missing.
    """
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    block = _container_dispatch_block(source)

    for flag, variable in (
        ("--quality-owner-tier", "${quality_owner_tier}"),
        ("--evidence-mode", "${EVIDENCE_MODE}"),
        ("--fingerprint-source", "${FINGERPRINT_SOURCE}"),
        ("--trust", "${evidence_trust}"),
        ("--commit-sha", "${candidate_commit_sha}"),
    ):
        assert flag in block, f"{flag} is never forwarded into the container"
        if variable is not None:
            assert variable in block, f"{flag} is forwarded without its value"


def test_an_extension_checkout_is_streamed_never_mounted() -> None:
    """Docker Desktop serves stale files through a bind mount under the user's home.

    The checkout is read on the host and piped into the one container that runs the
    extension checks; no `-v` names it, so the container also cannot write back.
    """
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    block = _container_dispatch_block(source)

    assert '--extension-id "${extension_id}"' in block
    assert "local_inside_args+=(--extension-from-stdin)" in block
    assert "extension_source_archive \\\n        | fs_compose run" in source
    assert not re.search(r'-v "?\$\{extension_root', source)
    archive = source.split("extension_source_archive() {", 1)[1].split("\n}\n", 1)[0]
    assert "--exclude=.git" in archive


@pytest.mark.parametrize(
    "args",
    [
        ["--checks", "extension-tests"],
        ["--checks", "extension-static", "--extension-root", "relative/path"],
        ["--checks", "ruff", "--extension-root", "/tmp"],
        ["--checks", "extension-tests,targeted-tests", "--extension-root", "/tmp"],
    ],
)
def test_extension_checks_refuse_an_ambiguous_checkout_before_any_docker_work(args) -> None:
    result = subprocess.run(
        ["bash", str(LINT_SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2, result.stderr
    assert "Running selected checks" not in result.stdout


def test_selected_checks_forward_the_tier_that_disables_fail_fast() -> None:
    """The --checks branch specifically: this is the path check-registry.json uses."""
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    block = _container_dispatch_block(source)
    checks_branch = block.split("else", 1)[0]

    assert "--checks" in checks_branch
    assert "--quality-owner-tier" in checks_branch
    assert "${quality_owner_tier}" in checks_branch


def test_canonical_docs_distinguish_focused_and_full_failure_collection() -> None:
    for path in FAILURE_POLICY_FILES:
        policy = path.read_text(encoding="utf-8")
        assert "Tier 1" in policy, path
        assert "fail-fast" in policy, path
        assert "Tier 3" in policy, path
        assert "without `-x` or `--maxfail=1`" in policy, path
        assert "complete failure list" in policy, path
        assert "grouped by root cause" in policy, path


def test_lint_script_skips_ruff_when_missing(tmp_path: Path) -> None:
    shim = tmp_path / "python-shim"
    shim.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ]; then\n  exit 1\nfi\nexec /usr/local/bin/python "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    env = dict(os.environ)
    env["FS_PYTHON_BIN"] = str(shim)
    manifest = tmp_path / "tracked-files.txt"
    manifest.write_text("README.md\n", encoding="utf-8")
    env["FS_BOUNDARY_TRACKED_FILES_MANIFEST"] = str(manifest)

    completed = subprocess.run(
        ["bash", str(LINT_SCRIPT), "--checks", "ruff", "--inside"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "Ruff is not installed" in completed.stdout or "Ruff lint" in completed.stdout


@pytest.mark.parametrize("sonar_local", ["0", "1"])
def test_host_sonar_workflow_uses_audit_runtime_and_current_quality_cli(
    tmp_path: Path,
    sonar_local: str,
) -> None:
    invocation_log = tmp_path / "docker-invocations.txt"
    tracked_files = tmp_path / "tracked-files.txt"
    tracked_files.write_text("README.md\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >>"$FS_TEST_DOCKER_LOG"\n'
        'if [[ " $* " == *" fs-audit "* ]]; then cat >/dev/null || true; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"UP\"}'\n",
        encoding="utf-8",
    )
    fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)

    host_python = fake_bin / "python3"
    host_python.write_text(
        "#!/bin/sh\nprintf 'unexpected host Python\\n' >&2\nexit 97\n",
        encoding="utf-8",
    )
    host_python.chmod(host_python.stat().st_mode | stat.S_IXUSR)

    # Core-only gates bind a source tree without .git. Keep the real SDK digest
    # contract by providing child-only metadata, never creating ROOT/.git or
    # updating the real checkout's index.
    git_fixture = tmp_path / "git-metadata"
    git_env = {
        **os.environ,
        "GIT_DIR": str(git_fixture / ".git"),
        "GIT_WORK_TREE": str(ROOT),
        "GIT_INDEX_FILE": str(git_fixture / ".git" / "index"),
    }
    subprocess.run(["git", "init", "-q", str(git_fixture)], env=git_env, check=True)
    subprocess.run(
        ["git", "add", "--", "core/infrastructure/extension_sdk/public"],
        cwd=ROOT,
        env=git_env,
        check=True,
    )
    env = dict(git_env)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FS_BOUNDARY_TRACKED_FILES_MANIFEST": str(tracked_files),
            "FS_TEST_DOCKER_LOG": str(invocation_log),
            "SONAR_TOKEN": "test-sonar-token",
            "SONAR_LOCAL": sonar_local,
            "SONAR_WAIT_URL": "http://localhost:9001",
            "SONAR_HOST_URL": "http://sonarqube:9000",
            "SONAR_EXPORT": "1",
            "SONAR_BACKLOG": "1",
        }
    )

    completed = subprocess.run(
        ["bash", str(LINT_SCRIPT), "--sonar-full"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    invocations = invocation_log.read_text(encoding="utf-8")
    if sonar_local == "1":
        assert (
            "fs-audit python -m core.entrypoints.cli quality sonar-ensure-rules "
            "--confirm --sonar-token-stdin --host-url http://sonarqube:9000"
        ) in invocations
    else:
        assert "sonar-ensure-rules" not in invocations
        assert "--profile quality up" not in invocations
    scanner_runs = [
        line for line in invocations.splitlines() if " sonar-scanner sonar-scanner " in line
    ]
    assert len(scanner_runs) == 1
    assert ("--no-deps" in scanner_runs[0]) == (sonar_local == "0")
    assert (
        "fs-audit python -m core.entrypoints.cli quality sonar-export-issues "
        "--confirm --sonar-token-stdin --host-url http://sonarqube:9000"
    ) in invocations
    assert (
        "fs-audit python -m core.entrypoints.cli quality sonar-generate-backlog --confirm"
    ) in invocations
    assert "tooling quality" not in invocations
    assert "test-sonar-token" not in invocations
    assert "unexpected host Python" not in completed.stderr


def test_frontend_dependency_audit_has_one_tier_owned_executor() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")

    assert source.count("npm_audit_retry.sh") == 1
    assert "frontend-audit|frontend-dependency-audit" in source


def _npm_audit_retry_result(
    tmp_path: Path, *, transient: bool
) -> tuple[subprocess.CompletedProcess[str], int]:
    counter = tmp_path / "counter"
    fake_npm = tmp_path / "npm"
    failure = (
        "npm error getaddrinfo EAI_AGAIN registry.npmjs.org"
        if transient
        else "npm audit found 1 high severity vulnerability"
    )
    fake_npm.write_text(
        "#!/usr/bin/env bash\n"
        'count="$(cat "$FS_TEST_NPM_COUNTER" 2>/dev/null || printf 0)"\n'
        "count=$((count + 1))\n"
        'printf "%s" "$count" >"$FS_TEST_NPM_COUNTER"\n'
        f'if [ "$count" -eq 1 ]; then printf "%s\\n" "{failure}" >&2; exit 1; fi\n'
        'printf "found 0 vulnerabilities\\n"\n',
        encoding="utf-8",
    )
    fake_npm.chmod(fake_npm.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "FS_TEST_NPM_COUNTER": str(counter),
            "FS_NPM_AUDIT_RETRY_DELAY_SECONDS": "0",
        }
    )
    result = subprocess.run(
        ["bash", str(NPM_AUDIT_RETRY), "--audit-level=high"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, int(counter.read_text(encoding="utf-8"))


def test_npm_audit_retries_transient_dns_failure(tmp_path: Path) -> None:
    result, attempts = _npm_audit_retry_result(tmp_path, transient=True)

    assert result.returncode == 0
    assert attempts == 2
    assert "transient npm audit transport failure" in result.stderr


def test_npm_audit_does_not_retry_vulnerability_failure(tmp_path: Path) -> None:
    result, attempts = _npm_audit_retry_result(tmp_path, transient=False)

    assert result.returncode == 1
    assert attempts == 1


def _semgrep_retry_result(
    tmp_path: Path, *, transient: bool
) -> tuple[subprocess.CompletedProcess[str], int]:
    counter = tmp_path / "counter"
    fake_semgrep = tmp_path / "semgrep"
    failure = (
        "Failed to resolve 'semgrep.dev' ([Errno -3] Temporary failure in name resolution)"
        if transient
        else "1 blocking code finding"
    )
    fake_semgrep.write_text(
        "#!/usr/bin/env bash\n"
        'count="$(cat "$FS_TEST_SEMGREP_COUNTER" 2>/dev/null || printf 0)"\n'
        "count=$((count + 1))\n"
        'printf "%s" "$count" >"$FS_TEST_SEMGREP_COUNTER"\n'
        f'if [ "$count" -eq 1 ]; then printf "%s\\n" "{failure}" >&2; exit 1; fi\n'
        'printf "0 findings\n"\n',
        encoding="utf-8",
    )
    fake_semgrep.chmod(fake_semgrep.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "FS_TEST_SEMGREP_COUNTER": str(counter),
            "FS_SEMGREP_RETRY_DELAY_SECONDS": "0",
        }
    )
    result = subprocess.run(
        ["bash", str(SEMGREP_RETRY), "semgrep", "--error"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, int(counter.read_text(encoding="utf-8"))


def test_semgrep_retries_transient_dns_failure(tmp_path: Path) -> None:
    result, attempts = _semgrep_retry_result(tmp_path, transient=True)

    assert result.returncode == 0
    assert attempts == 2
    assert "transient Semgrep transport failure" in result.stderr


def test_semgrep_does_not_retry_finding_failure(tmp_path: Path) -> None:
    result, attempts = _semgrep_retry_result(tmp_path, transient=False)

    assert result.returncode == 1
    assert attempts == 1


def test_full_host_gate_does_not_repeat_tier_two_security_checks() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")

    assert "FS_DEFER_FRONTEND_AUDIT" not in source
    assert "FS_DEFER_PIP_AUDIT" not in source
    assert "FS_DEFER_SEMGREP" not in source
    assert "defer_frontend_audit" not in source
    assert "defer_pip_audit" not in source
    assert "defer_semgrep" not in source


def test_full_refreshes_commit_bound_tier_two_before_long_test_infrastructure() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    host_branch = source.split('if [[ "${INSIDE}" -eq 0 && "${_in_container}" -eq 0 ]]; then', 1)[1]

    tier_two = 'bash /app/core/entrypoints/cli/scripts/lint.sh "${final_tier_two_args[@]}"'
    test_infra = "fs_compose up -d --wait pg-audit rabbitmq-audit"
    tier_three = 'bash /app/core/entrypoints/cli/scripts/lint.sh "${local_inside_args[@]}"'

    assert "final_tier_two_args=(--inside --permission-preflight --fast)" in host_branch
    assert "--fingerprint-source commit" in host_branch
    assert '--commit-sha "${candidate_commit_sha}"' in host_branch
    assert (
        host_branch.index(tier_two) < host_branch.index(test_infra) < host_branch.index(tier_three)
    )


def test_full_validates_cumulative_evidence_before_reporting_success() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    host_branch = source.split('if [[ "${INSIDE}" -eq 0 && "${_in_container}" -eq 0 ]]; then', 1)[1]

    external = '"${SCRIPT_DIR}/run-external-tier-three.sh"'
    release_gate = "python -m core.application.tooling.quality.release_gate"
    success = 'if [[ "${audit_exit_code}" -eq 0 ]]; then'

    assert release_gate in host_branch
    assert host_branch.index(external) < host_branch.index(release_gate)
    assert host_branch.index(release_gate) < host_branch.index(
        success, host_branch.index(release_gate)
    )


@pytest.mark.parametrize("tier_two_exit", (31, 32, 33))
def test_full_stops_before_long_suites_when_final_tier_two_is_not_green(
    tmp_path: Path, tier_two_exit: int
) -> None:
    invocation_log = tmp_path / "docker-invocations.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >>"$FS_TEST_DOCKER_LOG"\n'
        'if [[ "$1 $2" == "image inspect" && "$*" == *".Id"* ]]; then\n'
        '  printf "sha256:%s %s\\n" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" "$(cat "$FS_TEST_IMAGE_LABEL")"\n'
        "fi\n"
        'if [[ " $* " == *" build "* ]]; then\n'
        '  for arg in "$@"; do [[ "$arg" != AUDIT_TOOLCHAIN_DIGEST=* ]] || printf "%s" "${arg#*=}" >"$FS_TEST_IMAGE_LABEL"; done\n'
        "fi\n"
        'if [[ " $* " == *" run "* && " $* " == *" --fast "* ]]; then\n'
        '  exit "$FS_TEST_TIER_TWO_EXIT"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)
    harness_root = tmp_path / "repo"
    scripts = harness_root / "core/entrypoints/cli/scripts"
    scripts.mkdir(parents=True)
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    auto_detection = """_in_container=0
if [[ -f \"/.dockerenv\" ]] || grep -qE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null; then
  _in_container=1
fi"""
    assert source.count(auto_detection) == 1
    harness_lint = scripts / "lint.sh"
    harness_lint.write_text(source.replace(auto_detection, "_in_container=0"), encoding="utf-8")
    harness_lint.chmod(harness_lint.stat().st_mode | stat.S_IXUSR)
    quality_permissions = scripts / "quality_permissions.sh"
    quality_permissions.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    quality_permissions.chmod(quality_permissions.stat().st_mode | stat.S_IXUSR)
    docker_compose_env = scripts / "docker_compose_env.sh"
    docker_compose_env.write_text(
        "#!/usr/bin/env bash\n"
        'fs_compose() { docker compose "$@"; }\n'
        'fs_compose_service_image() { printf "%s-%s:latest\\n" harness "$1"; }\n',
        encoding="utf-8",
    )
    registry = harness_root / "infra/tooling/quality/check-registry.json"
    registry.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "infra/tooling/quality/check-registry.json", registry)
    subprocess.run(("git", "init", "-q", str(harness_root)), check=True)
    subprocess.run(("git", "-C", str(harness_root), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(harness_root),
            "-c",
            "user.name=Quality test",
            "-c",
            "user.email=quality-test@example.invalid",
            "commit",
            "-qm",
            "candidate",
        ),
        check=True,
    )
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FS_LINT_CONTAINER_FALLBACK": "1",
            "FS_TEST_DOCKER_LOG": str(invocation_log),
            "FS_TEST_TIER_TWO_EXIT": str(tier_two_exit),
            "FS_TEST_IMAGE_LABEL": str(tmp_path / "image-label"),
            "FS_TEST_AUDIT_IMAGE_ID": "sha256:" + "f" * 64,
        }
    )

    completed = subprocess.run(
        ["bash", str(harness_lint), "--full"],
        cwd=harness_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    invocations = invocation_log.read_text(encoding="utf-8")
    assert completed.returncode == tier_two_exit
    assert "-e FS_TEST_AUDIT_IMAGE_ID=sha256:" + "a" * 64 in invocations
    assert "-e FS_TEST_AUDIT_IMAGE_ID=sha256:" + "f" * 64 not in invocations
    calls = invocations.splitlines()
    inspect_at = next(i for i, call in enumerate(calls) if call.startswith("image inspect "))
    build_at = next(
        i for i, call in enumerate(calls) if " build --build-arg AUDIT_TOOLCHAIN_DIGEST=" in call
    )
    first_run_at = next(i for i, call in enumerate(calls) if call.startswith("compose run "))
    assert inspect_at < build_at < first_run_at, "the gate ran fs-audit before checking its tools"
    assert "--fingerprint-source commit" in invocations
    assert "--fast" in invocations
    assert "exec -T fs-app" not in invocations
    assert "up -d --wait pg-audit rabbitmq-audit" not in invocations
    assert "core.application.tooling.quality.release_gate" not in invocations
    assert "Quality gate passed (full)" not in completed.stdout


def test_internal_full_delta_does_not_repeat_provider_neutral_tier_two_owner() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    mode_dispatch = source.rsplit("if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then", 1)[1]

    assert (
        'if [[ "${MODE}" == "full" ]]; then\n    run_check_provider_neutral_core'
        not in mode_dispatch
    )


def test_frontend_audit_remains_a_supported_mandatory_check() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")

    assert "run_check_frontend_dependency_audit()" in source
    assert "frontend-audit|frontend-dependency-audit" in source


def test_core_python_quality_targets_exclude_external_extensions() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    target_block = source.split("TARGET_CANDIDATES=(", 1)[1].split(")", 1)[0]

    assert '"core"' in target_block
    assert '"extensions"' not in target_block


def test_deptry_excludes_generated_release_candidate_workspaces() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    function_body = source.split("run_check_deptry()", 1)[1].split(
        "run_check_pylint_duplicate()", 1
    )[0]

    assert r"\\.flow-steward|logs" in function_body


def test_semgrep_defaults_to_four_workers() -> None:
    # Measured 2026-10-14 in fs-audit over core/: one worker 525 s at 526 MiB
    # peak, four workers 208 s at 587 MiB. One worker bought no real memory.
    source = LINT_SCRIPT.read_text(encoding="utf-8")

    assert 'SEMGREP_JOBS="${FS_SEMGREP_JOBS:-4}"' in source
    assert '--jobs "${SEMGREP_JOBS}"' in source


def test_secret_gate_does_not_send_external_extension_files_to_detect_secrets(
    tmp_path: Path,
) -> None:
    capture_path = tmp_path / "detect-secrets-arguments.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_hook = fake_bin / "detect-secrets-hook"
    fake_hook.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        "import sys\n"
        f"pathlib.Path({str(capture_path)!r}).write_text(\n"
        "    '\\n'.join(sys.argv[1:]), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    fake_hook.chmod(fake_hook.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env["FS_QUALITY_EVIDENCE_MODE"] = "off"
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    source_root = Path(env.get("PYTEST_SOURCE_REPO_ROOT", ROOT)).resolve()
    lint_script = source_root / "core/entrypoints/cli/scripts/lint.sh"

    completed = subprocess.run(
        [
            "bash",
            str(lint_script),
            "--checks",
            "detect-secrets",
            "--evidence-mode",
            "off",
            "--inside",
        ],
        cwd=source_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    arguments = capture_path.read_text(encoding="utf-8").splitlines()
    assert "core/entrypoints/cli/scripts/lint.sh" in arguments
    assert not any(path.startswith("extensions/") for path in arguments)


def test_secret_gate_uses_portable_relative_baseline_path() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    function_body = source.split("run_detect_secrets()", 1)[1].split("run_pip_audit()", 1)[0]

    assert 'local baseline_file=".secrets.baseline"' in function_body
    assert 'local baseline_file="${ROOT_DIR}/.secrets.baseline"' not in function_body


def test_secret_baseline_findings_have_audit_decisions() -> None:
    baseline = json.loads((ROOT / ".secrets.baseline").read_text(encoding="utf-8"))
    findings = [finding for entries in baseline["results"].values() for finding in entries]

    assert findings
    assert all(isinstance(finding.get("is_secret"), bool) for finding in findings)


def test_secret_scan_never_rewrites_the_tracked_baseline() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    function_body = source.split("run_detect_secrets()", 1)[1].split("run_pip_audit()", 1)[0]

    assert 'temporary_baseline="$(mktemp "' in function_body
    assert 'cp -- "${baseline_file}" "${temporary_baseline}"' in function_body
    assert '--baseline "${temporary_baseline}"' in function_body
    assert '--baseline "${baseline_file}"' not in function_body


def test_secret_scan_accepts_exact_metadata_only_refresh_without_rescan() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    function_body = source.split("run_detect_secrets()", 1)[1].split("run_pip_audit()", 1)[0]

    assert "run_detect_secrets_hook()" in function_body
    assert "--json" in function_body
    assert function_body.count("run_detect_secrets_hook") == 2
    assert '"${scan_rc}" -eq 3' in function_body
    assert "is_detect_secrets_metadata_only" in function_body
    assert "Counter" in function_body
    assert "count <= before_findings[signature]" in function_body
    assert "temporary baseline metadata refreshed; no rescan required" in function_body
    assert "elif run_detect_secrets_hook; then" not in function_body


def test_pip_audit_uses_a_network_timeout_suitable_for_full_dependency_graphs() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    function_body = source.split("run_pip_audit()", 1)[1].split(
        "run_security_regression_suite()", 1
    )[0]

    assert function_body.count("--timeout 60") == 4


def test_pip_audit_covers_the_hashed_cli_artifact_lock() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    function_body = source.split("run_pip_audit()", 1)[1].split(
        "run_security_regression_suite()", 1
    )[0]

    assert 'requirements_cli="requirements-cli.lock"' in function_body
    assert '"cli:${cli_report}"' in function_body
    assert "core.application.tooling.quality.pip_audit_acceptance" in function_body


def test_pip_audit_audits_every_lock_an_image_installs_as_pinned() -> None:
    """The images install from the locks; resolving the unpinned requirements afresh never sees them (0.7.23)."""
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    function_body = source.split("run_pip_audit()", 1)[1].split(
        "run_security_regression_suite()", 1
    )[0]
    declared = re.search(r"local product_locks=\(([^)]*)\)", function_body)
    assert declared, "run_pip_audit must name the product locks"
    audited = set(declared.group(1).split())
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    installed = set(re.findall(r"-r (requirements-[a-z]+\.lock)", dockerfile))

    assert installed, "the Dockerfile installs no lock"
    # The CLI lock is audited on its own, with its dependency graph resolved.
    assert installed - {"requirements-cli.lock"} <= audited
    assert "--require-hashes --disable-pip" in function_body
    assert '"${lock_specs[@]}"' in function_body


def test_enterprise_boundary_guard_fails_when_git_enumeration_fails(tmp_path: Path) -> None:
    guard = ROOT / "core/entrypoints/cli/scripts/check_no_enterprise_leak.sh"
    git_shim = tmp_path / "git"
    git_shim.write_text("#!/bin/sh\nexit 128\n", encoding="utf-8")
    git_shim.chmod(git_shim.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env.pop("FS_BOUNDARY_TRACKED_FILES_MANIFEST", None)

    completed = subprocess.run(
        ["bash", str(guard)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "could not enumerate tracked files" in completed.stderr.lower()


def test_enterprise_boundary_guard_ignores_binary_asset_bytes(tmp_path: Path) -> None:
    source_guard = ROOT / "core/entrypoints/cli/scripts/check_no_enterprise_leak.sh"
    repo = tmp_path / "repo"
    guard = repo / "core/entrypoints/cli/scripts/check_no_enterprise_leak.sh"
    guard.parent.mkdir(parents=True)
    guard.write_text(source_guard.read_text(encoding="utf-8"), encoding="utf-8")
    guard.chmod(guard.stat().st_mode | stat.S_IXUSR)
    binary_asset = repo / "assets/frontend/src/font.woff2"
    binary_asset.parent.mkdir(parents=True)
    binary_asset.write_bytes(b"\x89PNG\r\n\x1a\nEnterprise\x00")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", str(binary_asset.relative_to(repo))], cwd=repo, check=True)

    completed = subprocess.run(
        ["bash", str(guard)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_enterprise_boundary_guard_explicitly_excludes_binary_matches() -> None:
    guard = ROOT / "core/entrypoints/cli/scripts/check_no_enterprise_leak.sh"
    source = guard.read_text(encoding="utf-8")

    assert source.count("xargs grep -In") == 3


def test_backend_test_runners_build_their_own_frontend_shell() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    for runner, following in (
        ("run_check_targeted_tests", "run_check_unit_tests_with_coverage"),
        ("run_check_unit_tests_with_coverage", "assert_audit_test_isolation"),
    ):
        assert "ensure_frontend_shell || return $?" in _shell_function(source, runner, following)


@pytest.mark.parametrize(
    "bind_mount_directory_metadata", [False, True], ids=["ordinary-fs", "bind-mount"]
)
def test_frontend_shell_is_built_once_per_source_content(
    tmp_path: Path, bind_mount_directory_metadata: bool
) -> None:
    """A fresh checkout gets a shell; unchanged sources reuse it; a change rebuilds."""
    repo = tmp_path / "repo"
    frontend = repo / "assets" / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "package.json").write_text("{}\n", encoding="utf-8")
    (frontend / "src" / "main.tsx").write_text("export const v = 1;\n", encoding="utf-8")
    (frontend / "src" / "main.test.tsx").write_text("test\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    builds = tmp_path / "builds.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "npm").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "npx").write_text(
        "#!/bin/sh\n"
        f"echo build >> {builds}\n"
        'mkdir -p dist/.vite && echo "<div id=root></div>" > dist/index.html\n',
        encoding="utf-8",
    )
    for tool in ("npm", "npx"):
        (fake_bin / tool).chmod(0o755)
    real_tar = shutil.which("tar")
    assert real_tar is not None
    if bind_mount_directory_metadata:
        # Reproduce GNU tar's observed failure restoring root-directory metadata
        # on the Docker Desktop bind mount, while keeping real archive IO elsewhere.
        (fake_bin / "tar").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-C" ] && [ "$2" = "$FS_TEST_BIND_DIST" ] && [ "$3" = "-xf" ]; then\n'
            '  echo "tar: .: Directory renamed before its status could be extracted" >&2\n'
            "  exit 2\n"
            "fi\n"
            'exec "$FS_TEST_REAL_TAR" "$@"\n',
            encoding="utf-8",
        )
        (fake_bin / "tar").chmod(0o755)
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    script = "\n".join(
        (
            "set -euo pipefail",
            'ROOT_DIR="$1"',
            'quality_cache_dir="$2"',
            "ensure_frontend_shell()"
            + _shell_function(source, "ensure_frontend_shell", "prepare_frontend_workspace"),
            "prepare_frontend_workspace()"
            + _shell_function(source, "prepare_frontend_workspace", "run_check_frontend_lint"),
            "ensure_frontend_shell",
        )
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FS_TEST_BIND_DIST": str(frontend / "dist"),
        "FS_TEST_REAL_TAR": real_tar,
    }
    cache = tmp_path / "cache"
    cache.mkdir()

    def run() -> None:
        subprocess.run(
            ["bash", "-c", script, "_", str(repo), str(cache)], env=env, check=True, text=True
        )

    run()
    assert (frontend / "dist" / "index.html").is_file()
    run()
    (frontend / "src" / "main.test.tsx").write_text("changed test\n", encoding="utf-8")
    run()
    assert builds.read_text(encoding="utf-8").count("build") == 1

    # A new checkout has no dist or stamp, but shares the same content cache.
    shutil.rmtree(frontend / "dist")
    run()
    assert builds.read_text(encoding="utf-8").count("build") == 1, (
        "fresh checkout restored from cache"
    )
    assert (frontend / "dist" / "index.html").is_file()

    (frontend / "src" / "main.tsx").write_text("export const v = 2;\n", encoding="utf-8")
    run()
    assert builds.read_text(encoding="utf-8").count("build") == 2

    (frontend / "src" / "main.tsx").write_text("export const v = 1;\n", encoding="utf-8")
    run()
    assert builds.read_text(encoding="utf-8").count("build") == 2, "restored from cache"
    assert (frontend / "dist" / "index.html").is_file()


AUDIT_TOOLCHAIN_FILES = (
    "infra/docker/Dockerfile",
    "requirements-runtime.lock",
    "requirements-audit.txt",
    "requirements-semgrep.txt",
    "requirements-cli.lock",
)


def _audit_toolchain_inputs(source: str) -> set[str]:
    body = _shell_function(source, "audit_toolchain_digest", "ensure_audit_image_current")
    return set(body.split("local inputs=(", 1)[1].split(")", 1)[0].split())


def _audit_image_script(source: str) -> str:
    return "\n".join(
        (
            "set -euo pipefail",
            'ROOT_DIR="$1"',
            'log="$2"',
            'label="$3"',
            'inspect_exit="$4"',
            'build_exit="$5"',
            'calls="$6"',
            "audit_image_checked=0",
            'fs_compose_service_image() { printf "%s-%s:latest\\n" probe "$1"; }',
            'docker() { printf "docker %s\\n" "$*" >>"${log}"; printf "%s\\n" "${label}";'
            ' return "${inspect_exit}"; }',
            'fs_compose() { printf "fs_compose %s\\n" "$*" >>"${log}"; return "${build_exit}"; }',
            "audit_toolchain_digest()"
            + _shell_function(source, "audit_toolchain_digest", "ensure_audit_image_current"),
            "ensure_audit_image_current()"
            + _shell_function(source, "ensure_audit_image_current", "run_sonar_quality_command"),
            'if [[ "${label}" == current ]]; then label="$(audit_toolchain_digest "${ROOT_DIR}")"; fi',
            "status=0",
            'for _ in $(seq "${calls}"); do ensure_audit_image_current || status=$?; done',
            'printf "status=%s digest=%s\\n" "${status}" "$(audit_toolchain_digest "${ROOT_DIR}")"',
        )
    )


def _write_audit_toolchain(root: Path) -> None:
    for name in AUDIT_TOOLCHAIN_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name} v1\n", encoding="utf-8")


def _run_audit_image_check(
    tmp_path: Path,
    *,
    label: str,
    inspect_exit: int = 0,
    build_exit: int = 0,
    calls: int = 2,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    root = tmp_path / "repo"
    if not root.exists():
        _write_audit_toolchain(root)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            _audit_image_script(LINT_SCRIPT.read_text(encoding="utf-8")),
            "_",
            str(root),
            str(log),
            label,
            str(inspect_exit),
            str(build_exit),
            str(calls),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    digest = re.search(r"digest=([0-9a-f]{40})", completed.stdout)
    assert digest is not None, completed.stdout
    return completed, log.read_text(encoding="utf-8").splitlines(), digest.group(1)


def test_audit_image_built_from_current_toolchain_is_used_as_is(tmp_path: Path) -> None:
    completed, calls, _ = _run_audit_image_check(tmp_path, label="current")

    assert "status=0" in completed.stdout
    assert "rebuild" not in completed.stdout
    assert len(calls) == 1, "one docker image inspect per gate run, nothing else"
    assert calls[0].startswith("docker image inspect ")
    assert "org.flowsteward.audit-toolchain" in calls[0]
    assert calls[0].endswith(" probe-fs-audit:latest")


@pytest.mark.parametrize(
    ("label", "inspect_exit", "reason"),
    (
        ("0" * 40, 0, "was built from toolchain 000000000000"),
        ("", 0, "carries no toolchain digest"),
        ("<no value>", 0, "carries no toolchain digest"),
        ("unknown", 0, "carries no toolchain digest"),
        ("", 1, "is missing"),
    ),
)
def test_audit_image_is_rebuilt_when_its_toolchain_differs(
    tmp_path: Path, label: str, inspect_exit: int, reason: str
) -> None:
    completed, calls, digest = _run_audit_image_check(
        tmp_path, label=label, inspect_exit=inspect_exit
    )

    assert "status=0" in completed.stdout
    assert reason in completed.stdout
    assert calls[1:] == [f"fs_compose build --build-arg AUDIT_TOOLCHAIN_DIGEST={digest} fs-audit"]
    assert len(calls) == 2, "a second call in the same gate run neither inspects nor builds"


def test_audit_image_build_failure_stops_the_gate(tmp_path: Path) -> None:
    completed, calls, _ = _run_audit_image_check(tmp_path, label="0" * 40, build_exit=1, calls=1)

    assert "status=2" in completed.stdout
    assert "does not run on a stale toolchain" in completed.stderr
    assert calls[-1].startswith("fs_compose build ")


def test_audit_toolchain_digest_follows_tools_not_source(tmp_path: Path) -> None:
    _, _, original = _run_audit_image_check(tmp_path, label="current", calls=1)
    source_file = tmp_path / "repo" / "core" / "module.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("VALUE = 1\n", encoding="utf-8")

    _, _, after_source_edit = _run_audit_image_check(tmp_path, label="current", calls=1)
    assert after_source_edit == original

    for name in AUDIT_TOOLCHAIN_FILES:
        (tmp_path / "repo" / name).write_text(f"{name} v2\n", encoding="utf-8")
        _, calls, changed = _run_audit_image_check(tmp_path, label=original, calls=1)
        assert changed != original, f"{name} does not reach the audit toolchain digest"
        assert calls[-1].startswith("fs_compose build "), name
        original = changed


@pytest.mark.parametrize(
    ("metadata", "inspect_exit", "expected_status"),
    (
        ("sha256:" + "a" * 64 + " " + "b" * 40, 0, 0),
        ("", 0, 2),
        ("probe-fs-audit:latest " + "b" * 40, 0, 2),
        ("sha256:abcd " + "b" * 40, 0, 2),
        ("sha256:" + "A" * 64 + " " + "b" * 40, 0, 2),
        ("sha256:" + "a" * 64 + " " + "c" * 40, 0, 2),
        ("sha256:" + "a" * 64 + " " + "b" * 40 + "\nextra", 0, 2),
        ("sha256:" + "a" * 64 + " " + "b" * 40, 1, 2),
    ),
)
def test_audit_image_resolver_overwrites_inherited_selector_and_refuses_bad_metadata(
    tmp_path: Path, metadata: str, inspect_exit: int, expected_status: int
) -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    body = _shell_function(source, "resolve_audit_test_image", "run_sonar_quality_command")
    log = tmp_path / "docker.log"
    script = (
        "set -euo pipefail\n"
        'ROOT_DIR="$1"; log="$2"; probe_metadata="$3"; inspect_exit="$4"\n'
        'export FS_TEST_AUDIT_IMAGE_ID="sha256:' + "f" * 64 + '"\n'
        'fs_compose_service_image() { printf "%s\\n" probe-fs-audit:latest; }\n'
        'audit_toolchain_digest() { printf "%s\\n" ' + "b" * 40 + "; }\n"
        'docker() { printf "%s\\n" "$*" >>"$log"; printf "%s\\n" "$probe_metadata"; return "$inspect_exit"; }\n'
        f"resolve_audit_test_image(){body}\n"
        "status=0; resolve_audit_test_image || status=$?\n"
        'printf "status=%s selector=%s\\n" "$status" "${FS_TEST_AUDIT_IMAGE_ID-unset}"\n'
    )

    result = subprocess.run(
        ["bash", "-c", script, "_", str(tmp_path), str(log), metadata, str(inspect_exit)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    expected_selector = "sha256:" + "a" * 64 if expected_status == 0 else "unset"
    assert result.stdout.strip() == f"status={expected_status} selector={expected_selector}"
    calls = log.read_text().splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("image inspect ")
    assert calls[0].endswith("probe-fs-audit:latest")
    assert ".Id" in calls[0]
    assert "org.flowsteward.audit-toolchain" in calls[0]


def _dockerfile_stage_chain(target: str) -> list[tuple[str, list[str]]]:
    stages: dict[str, tuple[str, list[str]]] = {}
    current = ""
    dockerfile = ROOT / "infra" / "docker" / "Dockerfile"
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"FROM\s+(\S+)\s+AS\s+(\S+)", line)
        if match:
            current = match.group(2)
            stages[current] = (match.group(1), [])
        elif current:
            stages[current][1].append(line)
    chain = []
    while target in stages:
        parent, lines = stages[target]
        chain.append((target, lines))
        target = parent
    return chain


def test_audit_toolchain_digest_covers_every_file_the_audit_stages_install() -> None:
    """The digest must name every non-source file the `audit` target's stages COPY."""
    copied: set[str] = set()
    chain = _dockerfile_stage_chain("audit")
    assert [name for name, _ in chain][-1] == "base"
    for stage, lines in chain:
        for line in lines:
            if not line.startswith("COPY ") or "--from=" in line:
                continue
            sources = [token for token in line.split()[1:-1] if not token.startswith("--")]
            if stage == "audit" and sources == ["."]:
                continue  # the checkout; the gate bind-mounts it over /app
            copied.update(sources)

    inputs = _audit_toolchain_inputs(LINT_SCRIPT.read_text(encoding="utf-8"))
    assert inputs == copied | {"infra/docker/Dockerfile"}
    assert inputs == set(AUDIT_TOOLCHAIN_FILES)


def test_audit_image_is_checked_before_every_first_fs_audit_run() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    host_check = source.index("ensure_audit_image_current || exit $?")
    assert host_check < source.index("--entrypoint chown fs-audit", host_check)
    for helper, following in (
        ("run_sonar_quality_command", "sonar_container_path"),
        ("normalize_sonar_python_coverage_report", "run_host_sonar_scan"),
        ("run_host_sonar_scan", "run_step"),
    ):
        body = _shell_function(source, helper, following)
        assert body.index("ensure_audit_image_current || return $?") < body.index(
            "fs_compose run"
        ), helper


def test_audit_sdk_package_edit_rebuilds_installed_toolchain(tmp_path: Path) -> None:
    _run_audit_image_check(tmp_path, label="current", calls=1)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sdk = repo / "core/infrastructure/extension_sdk/public"
    package = sdk / "flowsteward_extension_sdk"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_text("PUBLIC_CONTRACT = 1\n", encoding="utf-8")
    metadata = sdk / "pyproject.toml"
    metadata.write_text('[project]\nname = "sdk-fixture"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _, _, original = _run_audit_image_check(tmp_path, label="current", calls=1)

    for path, text in [
        (module, "PUBLIC_CONTRACT = 2\n"),
        (metadata, '[project]\nname = "sdk-fixture-revised"\n'),
        (package / "added.py", "PUBLIC_FEATURE = 1\n"),
    ]:
        path.write_text(text, encoding="utf-8")
        _, calls, changed = _run_audit_image_check(tmp_path, label=original, calls=1)
        assert changed != original, path
        assert (
            calls[-1] == f"fs_compose build --build-arg AUDIT_TOOLCHAIN_DIGEST={changed} fs-audit"
        )
        original = changed

    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _, calls, staged = _run_audit_image_check(tmp_path, label=original, calls=1)
    assert staged == original, "staging identical SDK bytes must not rebuild the image"
    assert not any(call.startswith("fs_compose build ") for call in calls)

    module.unlink()
    _, calls, changed = _run_audit_image_check(tmp_path, label=original, calls=1)
    assert changed != original
    assert calls[-1].startswith("fs_compose build ")


@pytest.mark.parametrize("failure", ["missing-metadata", "enumeration-failure"])
def test_audit_sdk_enumeration_failure_stops_before_image_use(tmp_path: Path, failure: str) -> None:
    repo = tmp_path / "repo"
    _write_audit_toolchain(repo)
    sdk = repo / "core/infrastructure/extension_sdk/public"
    sdk.mkdir(parents=True)
    (sdk / "pyproject.toml").write_text('[project]\nname = "sdk-fixture"\n', encoding="utf-8")
    log = tmp_path / "image-actions.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_DIR": str(repo / ".git"),
        "GIT_WORK_TREE": str(repo),
        "GIT_INDEX_FILE": str(repo / ".git" / "index"),
    }
    if failure == "enumeration-failure":
        subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
        real_git = shutil.which("git")
        assert real_git is not None
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        git_shim = fake_bin / "git"
        git_shim.write_text(
            '#!/bin/sh\nfor arg in "$@"; do\n'
            '  if [ "$arg" = ls-files ]; then exit 23; fi\ndone\n'
            'exec "$FS_TEST_REAL_GIT" "$@"\n',
            encoding="utf-8",
        )
        git_shim.chmod(git_shim.stat().st_mode | stat.S_IXUSR)
        env.update({"PATH": f"{fake_bin}:{env['PATH']}", "FS_TEST_REAL_GIT": real_git})

    source = LINT_SCRIPT.read_text(encoding="utf-8")
    script = "\n".join(
        (
            "set -euo pipefail",
            'ROOT_DIR="$1"; log="$2"; audit_image_checked=0',
            'fs_compose_service_image() { printf "%s\\n" probe-fs-audit:latest; }',
            'docker() { printf "docker %s\\n" "$*" >>"$log"; }',
            'fs_compose() { printf "fs_compose %s\\n" "$*" >>"$log"; }',
            "audit_toolchain_digest()"
            + _shell_function(source, "audit_toolchain_digest", "ensure_audit_image_current"),
            "ensure_audit_image_current()"
            + _shell_function(source, "ensure_audit_image_current", "run_sonar_quality_command"),
            "ensure_audit_image_current",
        )
    )
    result = subprocess.run(
        ["bash", "-c", script, "_", str(repo), str(log)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == (128 if failure == "missing-metadata" else 23)
    assert log.read_text(encoding="utf-8") == "", "invalid SDK input must not inspect or build"
