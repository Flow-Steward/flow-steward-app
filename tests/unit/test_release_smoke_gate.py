from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REPO_ROOT = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", REPO_ROOT)).resolve()
RUNNER = REPO_ROOT / "scripts" / "smoke" / "release-smoke.sh"
MANIFEST = REPO_ROOT / "scripts" / "smoke" / "checks.json"
GROUP_D = REPO_ROOT / "scripts" / "smoke" / "groupD" / "smoke_D.sh"
GROUP_D_COMMON = REPO_ROOT / "scripts" / "smoke" / "groupD" / "smoke_common.sh"
GROUP_B_SNIPPETS = REPO_ROOT / "scripts" / "smoke" / "groupB" / "snip.sh"
GROUP_C = REPO_ROOT / "scripts" / "smoke" / "groupC" / "smoke_groupC.sh"
GROUP_E = REPO_ROOT / "scripts" / "smoke" / "groupE" / "smoke_E.sh"
REFERENCE_RUNNER = REPO_ROOT / "scripts" / "smoke" / "run_referenced_group.sh"
SEMANTIC_RUNNER = REPO_ROOT / "scripts" / "smoke" / "candidate_semantic_checks.py"
RESIDUE_RUNNER = REPO_ROOT / "scripts" / "smoke" / "candidate_residue_check.py"
FIXTURE_REGISTRY = REPO_ROOT / "scripts" / "smoke" / "fixture_families.json"


def _run_gate(
    tmp_path: Path,
    *,
    manifest: dict[str, object],
    group_source: str,
    worker_count: int = 1,
    worker_log: str = "",
    arguments: tuple[str, ...] = ("A",),
) -> subprocess.CompletedProcess[str]:
    mcp_ca_file = tmp_path / "candidate-mcp-ca.pem"
    mcp_ca_file.write_text("test candidate CA\n", encoding="utf-8")
    manifest_path = tmp_path / "checks.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    group_root = tmp_path / "groups"
    group_dir = group_root / "groupA"
    group_dir.mkdir(parents=True)
    group_runner = group_dir / "smoke_A.sh"
    group_runner.write_text(group_source, encoding="utf-8")
    group_runner.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        '#!/usr/bin/env bash\nprintf \'{"build_sha":"%s"}\\n200\' "${FS_FAKE_HEALTH_SHA}"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = ps ]; then\n'
        "  printf '%s\\n' 'candidate-app|fs-app' 'candidate-worker|fs-worker' "
        "'candidate-mcp|fs-mcp' 'candidate-control|control-plane'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = logs ]; then\n'
        '  container="${*: -1}"\n'
        '  if [ "$container" = candidate-worker ]; then printf \'%s\\n\' "$FS_FAKE_WORKER_LOG"; fi\n'
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = inspect ]; then \n'
        '  if [ "${2:-}" = candidate-app ]; then\n'
        '    printf \'%s|sha256:test|%s\\n\' "${FS_FAKE_IMAGE_SHA}" "${FS_FAKE_COMPOSE_PROJECT}"\n'
        '  elif [ "${2:-}" = candidate-cli ] || [ "${2:-}" = candidate-worker ]; then\n'
        "    printf 'sha256:test|%s\\n' \"${FS_FAKE_COMPOSE_PROJECT}\"\n"
        "  else\n"
        "    printf 'sha256:dependency|%s\\n' \"${FS_FAKE_COMPOSE_PROJECT}\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = exec ] && [[ "$*" = *FS_PUBLIC_BASE_URL* ]]; then\n'
        "  printf '%s' \"${FS_FAKE_PUBLIC_BASE:-https://candidate.example.test}\"\n"
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = exec ] && [ "${2:-}" = candidate-pg ]; then\n'
        "  printf '%s' '{\"projects\":0,\"jobs\":0}'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = exec ] && [ "${2:-}" = candidate-rabbitmq ]; then exit 0; fi\n'
        'if [ "${1:-}" = exec ] && [ "${2:-}" = candidate-app ]; then printf 0; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "FS_SMOKE_MANIFEST": str(manifest_path),
            "FS_SMOKE_GROUP_ROOT": str(group_root),
            "FS_SMOKE_WORKER_COUNT": str(worker_count),
            "FS_SMOKE_BASE": "https://candidate.example.test",
            "FS_SMOKE_CANDIDATE_COMMIT": "candidate-commit",
            "FS_SMOKE_IMAGE_DIGESTS": "fs-app=sha256:test",
            "FS_SMOKE_COMPOSE_PROJECT": "candidate-project",
            "FS_SMOKE_APP_CONTAINER": "candidate-app",
            "FS_SMOKE_CLI_CONTAINER": "candidate-cli",
            "FS_SMOKE_PG_CONTAINER": "candidate-pg",
            "FS_SMOKE_WORKER_CONTAINER": "candidate-worker",
            "FS_SMOKE_MAILPIT_CONTAINER": "candidate-mailpit",
            "FS_SMOKE_RABBITMQ_CONTAINER": "candidate-rabbitmq",
            "FS_SMOKE_DB_HOST": "candidate-pg",
            "FS_SMOKE_INTERNAL_BASE": "http://candidate-app:8099",
            "FS_SMOKE_MAILPIT_BASE": "http://candidate-mailpit:8025",
            "FS_SMOKE_MCP_CA_FILE": str(mcp_ca_file),
            "FS_SMOKE_BEARER_TOKEN": "protected-smoke-token",  # pragma: allowlist secret
            "FS_SMOKE_MODEL_PROVIDER": "custom:provider-1",
            "FS_SMOKE_MODEL": "smoke-model-1",
            "FS_FAKE_HEALTH_SHA": "candidate-commit",
            "FS_FAKE_IMAGE_SHA": "candidate-commit",
            "FS_FAKE_COMPOSE_PROJECT": "candidate-project",
            "FS_FAKE_WORKER_LOG": worker_log,
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
        }
    )
    return subprocess.run(
        ["bash", str(RUNNER), *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _manifest(*checks: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "flow-steward-release-smoke-v1",
        "checks": list(checks),
    }


def _check(check_id: str, *, profile: str = "oss", required: bool = True) -> dict[str, object]:
    return {
        "id": check_id,
        "group": check_id[0],
        "profile": profile,
        "required": required,
        "description": f"contract for {check_id}",
    }


def test_canonical_manifest_has_unique_semantic_ids() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = [check["id"] for check in data["checks"]]

    assert len(ids) == len(set(ids))
    assert all(check["description"] for check in data["checks"])
    assert all(check["profile"] in {"oss", "tenant", "deploy"} for check in data["checks"])
    assert all(isinstance(check["required"], bool) for check in data["checks"])
    covered = {
        regression for check in data["checks"] for regression in check.get("regressions", [])
    }
    assert {f"RS5-{number:03d}" for number in range(48, 57)} <= covered


def test_selected_group_aggregates_results_and_required_skip_fails(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        manifest=_manifest(
            _check("A01"),
            _check("A02"),
            _check("A03", profile="tenant", required=False),
        ),
        group_source="""#!/usr/bin/env bash
printf 'A01 PASS terminal state and effect verified\\n'
printf 'A02 SKIP dependency unavailable\\n'
""",
    )

    assert result.returncode == 1
    assert "A01 PASS" in result.stdout
    assert "A02 FAIL required check was skipped" in result.stdout
    assert "profile_excluded=1" in result.stdout
    assert "pass=1 fail=1 skip=0 selected=2" in result.stdout
    assert "manifest=3" in result.stdout
    assert not list(tmp_path.glob("flow-steward-smoke.*"))


def test_missing_required_group_result_is_a_failure(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        manifest=_manifest(_check("A01"), _check("A02")),
        group_source="""#!/usr/bin/env bash
printf 'A01 PASS observed\\n'
""",
    )

    assert result.returncode == 1
    assert "A02 FAIL group emitted no result" in result.stdout
    assert "pass=1 fail=1 skip=0 selected=2" in result.stdout


def test_nonzero_group_exit_cannot_be_hidden_by_pass_lines(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        manifest=_manifest(_check("A01")),
        group_source="""#!/usr/bin/env bash
printf 'A01 PASS observed-before-runner-crash\\n'
exit 9
""",
    )

    assert result.returncode != 0
    assert "A01 FAIL group runner exited 9" in result.stdout


def test_release_profile_includes_oss_tenant_and_deploy_checks(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        manifest=_manifest(
            _check("A01", profile="oss"),
            _check("A02", profile="tenant"),
            _check("A03", profile="deploy"),
        ),
        group_source="""#!/usr/bin/env bash
for check_id in "$@"; do printf '%s PASS candidate-backed\\n' "$check_id"; done
""",
        arguments=("--profile", "release", "A"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "environment_profiles=oss,tenant,deploy" in result.stdout
    assert "pass=3 fail=0 skip=0 selected=3" in result.stdout
    assert "manifest=3" in result.stdout


def test_release_profile_fails_on_unexpected_candidate_error_within_its_log_window(
    tmp_path: Path,
) -> None:
    result = _run_gate(
        tmp_path,
        manifest=_manifest(_check("A01")),
        group_source="""#!/usr/bin/env bash
printf 'A01 PASS candidate-backed\\n'
""",
        worker_log="ERROR worker protocol error after candidate mutation",
        arguments=("--profile", "release", "A"),
    )

    assert result.returncode == 1
    assert "candidate_log_error service=fs-worker" in result.stdout
    assert "candidate_log_window=failed" in result.stdout


def test_log_window_ignores_lowercase_library_error_fields(monkeypatch) -> None:
    from scripts.smoke import candidate_log_window_check

    library_shutdown = (
        "INFO [pika.connection] AMQP stack terminated: "
        "error-arg=None; pending-error=ConnectionClosedByClient"
    )
    monkeypatch.setattr(
        candidate_log_window_check,
        "_run_docker",
        lambda *_args: subprocess.CompletedProcess(
            args=["docker", "logs"], returncode=0, stdout=library_shutdown, stderr=""
        ),
    )

    assert (
        candidate_log_window_check._unexpected_lines(
            service="fs-app",
            container="candidate-app",
            since="2026-08-15T00:00:00Z",
            until="2026-08-15T00:01:00Z",
            allowlist={"fs-app": set()},
        )
        == []
    )


def test_runner_derives_denominator_and_scopes_docker_discovery_to_candidate() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "manifest=72" not in source
    assert '"$MANIFEST_TOTAL"' in source
    assert "label=com.docker.compose.project=$FS_SMOKE_COMPOSE_PROJECT" in source
    assert "release profile selected $SELECTED of $MANIFEST_TOTAL" in source


def test_selected_consumer_requirements_are_preflighted_before_candidate_access() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "candidate_requirement_preflight" in source
    assert source.index("candidate_requirement_preflight") < source.index("health_response=")
    assert "requirements.json" in source
    assert "required candidate input is missing" in source
    assert "required operator script is not executable" in source
    assert "rabbitmq) container_var=FS_SMOKE_RABBITMQ_CONTAINER" in source


def test_release_cleanup_fails_gate_on_product_queue_or_artifact_residue() -> None:
    gate = RUNNER.read_text(encoding="utf-8")
    residue = RESIDUE_RUNNER.read_text(encoding="utf-8")

    assert gate.index('"$SCRIPT_DIR/candidate_residue_check.py"') < gate.index("totals pass=")
    assert "cleanup_residue=failed" in gate
    for contract in (
        "projects",
        "jobs",
        "project_workflows",
        "project_datasets",
        "project_connections",
        "extension_callback_registrations",
        "personal_access_tokens",
        "machine_tokens",
        "extension_project_state",
        "messages_unacknowledged",
        "FS_ARTIFACT_ROOT",
    ):
        assert contract in residue


def test_safe_profile_enforces_residue_and_exact_log_window_checks() -> None:
    gate = RUNNER.read_text(encoding="utf-8")

    assert "FS_SMOKE_VERIFY_CLEAN_EXIT:-0" in gate
    assert gate.count('if [ "$VERIFY_CLEAN_EXIT" = 1 ]; then') >= 3
    assert '"$SCRIPT_DIR/candidate_residue_check.py"' in gate
    assert '"$SCRIPT_DIR/candidate_log_window_check.py"' in gate


def test_residue_detection_uses_the_central_fixture_family_registry() -> None:
    residue = RESIDUE_RUNNER.read_text(encoding="utf-8")
    registry = json.loads(FIXTURE_REGISTRY.read_text(encoding="utf-8"))

    assert "fixture_families.json" in residue
    assert "project_name_prefixes" in residue
    assert "d09-disabled-" in registry["project_name_prefixes"]
    assert "release-semantic-" in registry["project_name_prefixes"]
    assert "regb" in registry["project_name_prefixes"]
    assert registry["registry_workflow_id_prefixes"] == [
        "a23_gate_",
        "d09_override_typo_",
        "d11_",
        "d12_",
        "e12_cutover_",
        "regb",
    ]
    assert registry["registry_agent_id_prefixes"] == ["regb"]
    assert registry["registry_skill_id_prefixes"] == ["regb"]
    assert registry["registry_tool_id_prefixes"] == ["regb"]
    assert registry["registry_guardrail_name_prefixes"] == ["regb", "regC-smoke-"]
    assert registry["registry_data_contract_id_prefixes"] == ["regb"]


def test_b08_retries_public_skill_trash_after_removing_the_blocking_agent() -> None:
    source = GROUP_B_SNIPPETS.read_text(encoding="utf-8")
    b08_source = source[source.index("b08(){") : source.index("# ---------- B09")]
    agent_delete = b08_source.index("/trash/registry_agent/$ag/hard-delete")
    retrash = b08_source.index("/trash/registry_skill/$sk/trash", agent_delete)
    final_delete = b08_source.index("/trash/registry_skill/$sk/hard-delete", retrash)

    assert agent_delete < retrash < final_delete


def test_d09_cleans_an_unexpected_disabled_project_creation() -> None:
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    d09_source = source[source.index("def _d09(") : source.index("def _d10(")]

    assert "stale_project_id" in d09_source
    assert "_cleanup_project(api, stale_project_id)" in d09_source
    # The window it guards is the disabled one, on the probe's own workflow.
    assert "disabled_bind" in d09_source


def test_residue_query_includes_every_registered_project_family(monkeypatch) -> None:
    from scripts.smoke import candidate_residue_check

    captured: list[list[str]] = []

    def fake_run(command: list[str]) -> str:
        captured.append(command)
        return json.dumps(
            {
                "projects": 0,
                "jobs": 0,
                "workflow_links": 0,
                "dataset_links": 0,
                "connections": 0,
                "callbacks": 0,
                "tokens": 0,
                "machine_tokens": 0,
                "extension_state": 0,
            }
        )

    monkeypatch.setattr(candidate_residue_check, "_run", fake_run)
    monkeypatch.setenv("FS_SMOKE_PG_CONTAINER", "candidate-pg")
    monkeypatch.setenv("FS_SMOKE_BEARER_TOKEN", "active-verifier-token")

    assert not any(candidate_residue_check._database_counts().values())
    sql = captured[0][-1]
    for prefix in json.loads(FIXTURE_REGISTRY.read_text(encoding="utf-8"))["project_name_prefixes"]:
        assert f"{prefix}%" in sql
    assert "active-verifier-token" not in sql
    assert "token_hash <>" in sql
    for typed_count in (
        "registry_workflows_active",
        "registry_workflows_trashed",
        "registry_agents_active",
        "registry_agents_trashed",
        "registry_skills_active",
        "registry_skills_trashed",
        "registry_tools_active",
        "registry_tools_trashed",
        "registry_guardrails_active",
        "registry_guardrails_trashed",
        "registry_data_contracts_active",
        "registry_data_contracts_trashed",
    ):
        assert f"'{typed_count}'" in sql
    assert "deleted_at is null" in sql
    assert "deleted_at is not null" in sql


def test_d12_abnormal_termination_residue_is_actionable_and_publicly_cleanable(
    monkeypatch,
    capsys,
) -> None:
    from scripts.smoke import candidate_residue_check, candidate_semantic_checks

    residue_counts = {
        "registry_workflows_active": 1,
        "registry_workflows_trashed": 1,
    }
    monkeypatch.setattr(candidate_residue_check, "_database_counts", lambda: residue_counts)
    monkeypatch.setattr(candidate_residue_check, "_queue_counts", dict)
    monkeypatch.setattr(candidate_residue_check, "_artifact_count", lambda: 0)
    monkeypatch.setenv("FS_SMOKE_CLEANUP_TIMEOUT_SECONDS", "0")

    assert candidate_residue_check.main() == 1
    before_cleanup = json.loads(capsys.readouterr().out)
    assert before_cleanup == {
        "status": "residue",
        "counts": {**residue_counts, "artifact_files": 0},
    }

    calls: list[tuple[str, str]] = []

    class Api:
        def request(self, method: str, path: str, _payload=None):
            calls.append((method, path))
            if method == "DELETE" and "d12_trashed" in path:
                return 404, {"detail": "already trashed"}
            return 200, {"status": "ok"}

    api = Api()
    candidate_semantic_checks._cleanup_registry_workflow_fixture(
        api,
        "d12_active_fixture",
        check_id="D12",
    )
    candidate_semantic_checks._cleanup_registry_workflow_fixture(
        api,
        "d12_trashed_fixture",
        check_id="D12",
    )
    assert calls == [
        ("DELETE", "/api/workflows/d12_active_fixture"),
        ("POST", "/api/trash/registry_workflow/d12_active_fixture/hard-delete"),
        ("DELETE", "/api/workflows/d12_trashed_fixture"),
        ("POST", "/api/trash/registry_workflow/d12_trashed_fixture/hard-delete"),
    ]

    residue_counts = {
        "registry_workflows_active": 0,
        "registry_workflows_trashed": 0,
    }
    assert candidate_residue_check.main() == 0
    after_cleanup = json.loads(capsys.readouterr().out)
    assert after_cleanup == {
        "status": "clean",
        "counts": {**residue_counts, "artifact_files": 0},
    }


def test_release_profile_rejects_candidate_health_commit_mismatch(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        manifest=_manifest(_check("A01")),
        group_source="""#!/usr/bin/env bash
printf 'A01 PASS should-not-run\\n'
""",
        arguments=("--profile", "release", "A"),
    )
    # Override the fake health response after _run_gate's environment setup by
    # proving the runner's comparison separately with a tiny wrapper manifest.
    env = os.environ.copy()
    env.update(
        {
            "FS_SMOKE_MANIFEST": str(tmp_path / "checks.json"),
            "FS_SMOKE_GROUP_ROOT": str(tmp_path / "groups"),
            "FS_SMOKE_WORKER_COUNT": "1",
            "FS_SMOKE_BASE": "https://candidate.example.test",
            "FS_SMOKE_CANDIDATE_COMMIT": "candidate-commit",
            "FS_SMOKE_IMAGE_DIGESTS": "fs-app=sha256:test",
            "FS_SMOKE_COMPOSE_PROJECT": "candidate-project",
            "FS_SMOKE_APP_CONTAINER": "candidate-app",
            "FS_SMOKE_CLI_CONTAINER": "candidate-cli",
            "FS_SMOKE_PG_CONTAINER": "candidate-pg",
            "FS_SMOKE_WORKER_CONTAINER": "candidate-worker",
            "FS_SMOKE_MAILPIT_CONTAINER": "candidate-mailpit",
            "FS_SMOKE_RABBITMQ_CONTAINER": "candidate-rabbitmq",
            "FS_SMOKE_DB_HOST": "candidate-pg",
            "FS_SMOKE_INTERNAL_BASE": "http://candidate-app:8099",
            "FS_SMOKE_MAILPIT_BASE": "http://candidate-mailpit:8025",
            "FS_SMOKE_MCP_CA_FILE": str(tmp_path / "candidate-mcp-ca.pem"),
            "FS_SMOKE_BEARER_TOKEN": "protected-smoke-token",  # pragma: allowlist secret
            "FS_SMOKE_MODEL_PROVIDER": "custom:provider-1",
            "FS_SMOKE_MODEL": "smoke-model-1",
            "FS_FAKE_HEALTH_SHA": "different-commit",
            "FS_FAKE_IMAGE_SHA": "candidate-commit",
            "FS_FAKE_COMPOSE_PROJECT": "candidate-project",
            "PATH": f"{tmp_path / 'bin'}:{env.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
        }
    )
    result = subprocess.run(
        ["bash", str(RUNNER), "--profile", "release", "A"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "candidate health commit mismatch" in result.stdout
    assert "should-not-run" not in result.stdout


def test_release_manifest_makes_isolation_and_deploy_checks_mandatory() -> None:
    checks = {
        check["id"]: check for check in json.loads(MANIFEST.read_text(encoding="utf-8"))["checks"]
    }

    assert all(checks[check_id]["required"] for check_id in ("C04", "C05", "C06", "C07", "C08"))
    assert all(checks[check_id]["required"] for check_id in ("E09", "E10", "E11", "E12", "E14"))
    assert checks["D09"]["required"] is True
    assert "RS5-057" in checks["D09"]["regressions"]
    assert checks["D10"]["required"] is True
    assert checks["D10"]["regressions"] == ["RS5-057", "RS5-069"]
    assert checks["D11"]["required"] is True
    assert checks["D11"]["regressions"] == [
        "RS5-073",
        "RS5-074",
        "RS5-075",
        "RS5-076",
        "RS5-077",
        "RS5-078",
    ]
    assert "D12" not in checks
    assert checks["A25"]["regressions"] == ["RS5-048", "RS5-057"]
    assert checks["A26"]["regressions"] == ["RS5-057", "RS5-065"]
    assert checks["A27"]["regressions"] == ["RS5-049", "RS5-057"]
    assert checks["A29"]["regressions"] == ["RS5-057", "RS5-072"]
    # quality policy / quality gate checks and the IT-project preflight check are retired
    assert {"A28", "A30", "B02"}.isdisjoint(checks)
    assert len(checks) == 68


def test_groups_a_and_e_do_not_use_source_pytest_references() -> None:
    source = REFERENCE_RUNNER.read_text(encoding="utf-8")

    assert "pytest" not in source
    assert "test_targets" not in source
    assert "FS_SMOKE_BASE" in source


def test_candidate_read_checks_do_not_accept_generic_http_json_success() -> None:
    source = REFERENCE_RUNNER.read_text(encoding="utf-8")

    assert "JSON=valid" not in source
    assert 'json.loads(os.environ["BODY"])' not in source
    assert "semantic_assertion" in source


def test_packaged_extension_semantics_accept_the_canonical_active_state() -> None:
    """Whatever the candidate packages, not one named bundle."""
    from scripts.smoke.semantic_assertions import semantic_assertion

    assert (
        semantic_assertion(
            "A06",
            {
                "extensions": [
                    {
                        "extension_id": "acme.packaged",
                        "state": "active",
                        "health_status": "unknown",
                    }
                ]
            },
        )
        == ""
    )


def test_no_probe_pins_a_workflow_an_extension_ships() -> None:
    """A13 runs through the semantic runner on its own fixture, never a shipped workflow."""
    source = REFERENCE_RUNNER.read_text(encoding="utf-8")

    assert "dataset_workflow_id" not in source
    assert "FS_SMOKE_DATASET_WORKFLOW_ID" not in source


def test_required_candidate_semantic_checks_have_a_repo_owned_executable_default() -> None:
    source = REFERENCE_RUNNER.read_text(encoding="utf-8")

    assert SEMANTIC_RUNNER.is_file()
    assert os.access(SEMANTIC_RUNNER, os.X_OK)
    assert 'SEMANTIC_RUNNER="$SCRIPT_DIR/candidate_semantic_checks.py"' in source
    for check_id, override in {
        "A06": "FS_SMOKE_EXTENSION_HEALTH_SCRIPT",
        "A13": "FS_SMOKE_DATASET_INPUT_SCRIPT",
        "A19": "FS_SMOKE_TRACE_REACTION_SCRIPT",
        "A20": "FS_SMOKE_EXECUTION_ENVELOPE_SCRIPT",
        "A21": "FS_SMOKE_WORKFLOW_REUSE_SCRIPT",
        "A22": "FS_SMOKE_SCHEDULE_LOCK_SCRIPT",
        "A23": "FS_SMOKE_HITL_SCRIPT",
        "A24": "FS_SMOKE_PUBLIC_MCP_SCRIPT",
    }.items():
        assert f"{check_id}) printf '%s' \"${{{override}:-$SEMANTIC_RUNNER}}\"" in source


def test_candidate_semantic_runner_owns_packaged_extension_health_contract() -> None:
    """A06 asserts every packaged bundle is healthy, and names none of them."""
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")

    assert '"A06": _a06' in source
    assert "run_extension_health" in source
    assert "packaged_extension_health" in source
    assert "flowsteward." not in source.split("def _a06", 1)[1].split("def ", 1)[0]


def test_a06_passes_a_stand_without_extensions_and_fails_an_unhealthy_one() -> None:
    """A release ships no extensions, so an empty stand is the clean case."""
    from scripts.smoke.semantic_assertions import semantic_assertion

    assert semantic_assertion("A06", {"extensions": []}) == ""
    assert "acme.broken" in semantic_assertion(
        "A06", {"extensions": [{"extension_id": "acme.broken", "state": "error"}]}
    )


def test_candidate_semantic_runner_owns_hitl_fixture_contract() -> None:
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")

    assert '"A23": _a23' in source
    assert '"/api/workflows"' in source
    assert 'workflow_id = f"a23_gate_' in source
    assert '"step_kind": "transform"' in source
    assert "approved=False" in source
    assert "approved=True" in source
    assert "Idempotency-Key" in source
    assert "feedback_propagated" in source
    assert "duplicate_decision_suppressed" in source
    assert "terminal_cancel" in source


def test_candidate_semantic_runner_owns_public_mcp_ingress_contract() -> None:
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")

    assert '"A24": _a24' in source
    assert "FS_SMOKE_MCP_CA_FILE" in source
    assert '"method": "initialize"' in source
    assert '"method": "tools/list"' in source
    assert '"project_jobs"' in source
    assert '"workflow_trigger"' in source
    assert '"scopes": ["projects.read", "jobs.read"]' in source
    assert '"payload": {"message": "main"}' in source
    assert "anonymous_mcp_denied" in source
    assert "machine_foreign_project_denied" in source


def test_mcp_tool_payload_decodes_prefixed_domain_error() -> None:
    from scripts.smoke import candidate_semantic_checks

    domain_payload = {
        "data": None,
        "error": {"code": "forbidden", "message": "Project is not in scope"},
        "ok": False,
    }
    response = {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": "Error executing tool project_jobs: " + json.dumps(domain_payload),
                }
            ],
            "isError": True,
        }
    }

    assert candidate_semantic_checks._mcp_tool_payload(response) == domain_payload


def test_cutover_fixture_uses_general_project_without_it_runtime_mapping() -> None:
    from scripts.smoke import candidate_semantic_checks

    class FixtureApi:
        def __init__(self) -> None:
            self.project_payload = {}
            self.workflow_payload = {}
            self.change_payload = {}
            self.enable_payload = {}

        def request(self, method, path, payload=None, **_kwargs):
            workflow_path = f"/api/workflows/{self.workflow_payload.get('workflow_id')}"
            if method == "POST" and path == "/api/workflows":
                self.workflow_payload = dict(payload or {})
                return 200, {"status": "ok"}
            if method == "GET" and path == workflow_path:
                return 200, {"revision": 1 if not self.change_payload else 2}
            if method == "POST" and path == f"{workflow_path}/changes":
                self.change_payload = dict(payload or {})
                return 200, {"valid": True, "issues": [], "applied": True}
            if method == "PATCH" and path == workflow_path:
                self.enable_payload = dict(payload or {})
                return 200, {"status": "updated"}
            if method == "GET" and path == "/api/workspaces":
                return 200, [{"id": "workspace-1", "workspace_key": "default"}]
            if method == "POST" and path == "/api/projects":
                self.project_payload = dict(payload or {})
                return 201, {"project_id": "project-1"}
            raise AssertionError(f"unexpected request: {method} {path}")

    api = FixtureApi()

    _, project_id = candidate_semantic_checks._create_cutover_fixture(api)

    assert project_id == "project-1"
    # Metadata first and disabled, then one change set at its revision, then enabled.
    assert api.workflow_payload["enabled"] is False
    assert "phases" not in api.workflow_payload
    assert api.change_payload["expected_revision"] == 1
    created = [
        op["step"]["step_id"]
        for op in api.change_payload["operations"]
        if op["op"] == "create_step"
    ]
    assert created == ["length_1", "length_2", "length_3", "done"]
    assert api.enable_payload == {"enabled": True, "expected_revision": 2}
    callable_contract = api.workflow_payload["settings"]["callable"]
    assert "mcp" in callable_contract["trigger_channels"]
    assert api.project_payload["workspace_id"] == "workspace-1"
    assert "project_type" not in api.project_payload
    assert "project_root" not in api.project_payload
    assert "container_root" not in api.project_payload
    assert "git_root" not in api.project_payload


def test_candidate_semantic_runner_owns_callback_and_guardrail_contracts() -> None:
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    referenced = (REPO_ROOT / "scripts" / "smoke" / "run_referenced_group.sh").read_text(
        encoding="utf-8"
    )

    assert '"A25": _a25' in source
    assert '"A26": _a26' in source
    assert '"A27": _a27' in source
    assert '"A29": _a29' in source
    assert '"A28"' not in source
    assert '"A30"' not in source
    assert "callback_token" in source
    # Signed with the receiving extension's own declared header names, which the
    # probe supplies — no bundle's spelling of them belongs in the runner.
    assert "signature_header: str = PROBE_CALLBACK_SIGNATURE_HEADER" in source
    assert "a25-expired" in source
    assert "replay_status == 409" in source
    assert '"_agents"' in source
    assert "A25)" in referenced
    assert "A26)" in referenced
    assert "A27)" in referenced
    assert "A29)" in referenced
    assert "A28)" not in referenced
    assert "A30)" not in referenced


def test_callback_semantic_check_exercises_owned_connection_and_rotation(monkeypatch) -> None:
    from scripts.smoke import candidate_semantic_checks

    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, object]] = []
            self.delivery_seen = False

        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path, payload))
            if path == "/api/projects/owned/connections":
                return 200, {"connection_id": "connection-1"}
            if path.endswith("/callbacks/events"):
                if payload == {"project_id": "owned"}:
                    return 400, {"error": "connection required"}
                if payload == {"project_id": "foreign", "connection_id": "connection-1"}:
                    return 400, {"error": "foreign connection"}
                return (
                    200,
                    {
                        "id": "registration-1",
                        "callback_token": "token-1",
                        "callback_secret": "secret-1",  # pragma: allowlist secret - synthetic callback fixture
                    },
                )
            if "/callbacks?project_id=owned" in path:
                return 200, {"callbacks": [{"callback_url": "https://candidate/<redacted>"}]}
            if path.endswith("/registration-1/status"):
                return 200, {"ok": True}
            if path.endswith("/registration-1/rotate"):
                return 200, {"callback_token": "token-2"}
            if path.endswith("/token-1"):
                if payload.get("source") == "A25-invalid":
                    return 401, {"error": "invalid"}
                if payload.get("source") == "A25-expired":
                    return 401, {"error": "expired"}
                if payload == {}:
                    return 404, {"error": "old token"}
                if self.delivery_seen:
                    return 409, {"error": "replayed"}
                self.delivery_seen = True
                return 200, {"ok": True}
            if path.endswith("/token-2"):
                return 200, {"ok": True}
            return 200, {}

    projects = iter(("owned", "foreign"))
    project_kwargs = []
    project_workflows = []

    def fake_project(_api, workflow_id, **kwargs):
        project_workflows.append(workflow_id)
        project_kwargs.append(kwargs)
        return next(projects)

    monkeypatch.setattr(
        candidate_semantic_checks,
        "_probe_workflow_id",
        lambda *_args: "extension.install-1.acme_probe_workflow",
    )
    monkeypatch.setattr(candidate_semantic_checks, "_project", fake_project)
    monkeypatch.setattr(candidate_semantic_checks, "_cleanup_project", lambda *_args: None)
    evidence = candidate_semantic_checks.Evidence("A25")

    # The lifecycle directly: `_a25` installs a real probe over HTTP, which is
    # what the live run exercises and what a fake client cannot stand in for.
    candidate_semantic_checks._a25_callback_lifecycle(Api(), evidence, "acme.probe")

    assert evidence.assertions
    assert all(row["passed"] for row in evidence.assertions)
    assert project_kwargs == [{}, {}]
    assert project_workflows == [
        "extension.install-1.acme_probe_workflow",
        "extension.install-1.acme_probe_workflow",
    ]


def test_guardrail_semantic_check_preserves_agent_scope_after_patch(monkeypatch) -> None:
    """A26 brings its own workflow and agent, and removes both afterwards.

    A fresh install ships no extension, so there is nothing on the candidate to
    borrow: a check that looked one up would fail on every clean release.
    """
    from scripts.smoke import candidate_semantic_checks

    calls = []

    class Api:
        def request(self, method, path, payload=None, **_kwargs):
            calls.append((method, path))
            if method == "POST" and path == "/api/agents":
                assert payload["enabled"] is False
                return 201, {"status": "created", "agent_id": payload["agent_id"]}
            if method == "POST" and path == "/api/guardrails":
                return 201, {"id": "guardrail-1"}
            if method == "PATCH" and path == "/api/guardrails/guardrail-1":
                assert "agents" not in payload
                return 200, {"status": "updated"}
            if method == "GET" and path == "/api/guardrails/guardrail-1":
                return 200, {
                    "display_name": "A26 guardrail updated",
                    "description": "after",
                    "mode": "semantic",
                    "severity": "warning",
                    "config": {"paths": ["docs/standards"]},
                    "prompt_text": "after prompt",
                    "agent_prompt": "after guidance",
                    "_agents": [agent_ids[0]],
                }
            return 200, {}

    agent_ids = []
    real_agent = candidate_semantic_checks._a26_agent

    def agent(api):
        status, agent_id = real_agent(api)
        agent_ids.append(agent_id)
        return status, agent_id

    project_workflows = []
    cleaned = []
    monkeypatch.setattr(candidate_semantic_checks, "_a26_agent", agent)
    monkeypatch.setattr(candidate_semantic_checks, "_create_hitl_workflow", lambda _api: "wf-1")
    monkeypatch.setattr(
        candidate_semantic_checks,
        "_project",
        lambda _api, workflow_id: (project_workflows.append(workflow_id), "project-1")[-1],
    )
    monkeypatch.setattr(
        candidate_semantic_checks,
        "_cleanup_hitl_fixture",
        lambda _api, project_id, workflow_id: cleaned.append((project_id, workflow_id)),
    )
    evidence = candidate_semantic_checks.Evidence("A26")

    candidate_semantic_checks._a26(Api(), evidence)

    assert evidence.assertions
    assert all(row["passed"] for row in evidence.assertions)
    assert project_workflows == ["wf-1"]
    assert cleaned == [("project-1", "wf-1")]
    assert ("DELETE", f"/api/agents/{agent_ids[0]}") in calls
    assert ("POST", f"/api/trash/registry_agent/{agent_ids[0]}/hard-delete") in calls


def test_a13_proves_the_named_input_contract_through_the_public_lifecycle() -> None:
    """A declared input is published, and a submitted value reaches the envelope.

    Three simulations stood here, pinning the old A13: that it imported a
    bundle's descriptor, that it reused a preinstalled scanned release instead of
    re-importing, and that a scanned release without projections failed the
    check. None of that is A13's job any more — whether every packaged bundle
    installs and is healthy is `packaged_extension_health`, and whether a
    particular bundle publishes its workflows is that bundle's own release suite.

    What survives from them is the rule that made them worth writing: the check
    reaches the candidate the way an operator does, never through the internal
    bootstrap endpoint. That is asserted here, together with A13's own contract.
    """
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    a13_source = source[source.index("def _a13(") : source.index("def _a19(")]

    assert '"A13": _a13' in source
    assert "installed_probe_extension" in a13_source
    assert "named_input_declared" in a13_source
    assert "named_input_persisted" in a13_source
    # The submitted value is read back from the job's own task envelope, not
    # from the request that was just sent.
    assert "task_envelope" in a13_source

    # Installed over the public endpoint the operator uses, never bootstrapped.
    probe_source = (REPO_ROOT / "scripts" / "smoke" / "synthetic_extension.py").read_text(
        encoding="utf-8"
    )
    assert '"/api/extensions/install"' in probe_source
    assert "/api/extensions/bootstrap/system" not in probe_source
    assert "/api/extensions/bootstrap/system" not in source
    assert "descriptors/import/file" not in a13_source


def test_populated_delete_semantic_check_rejects_stale_plan_then_retries(monkeypatch) -> None:
    """A stale hard-delete plan is refused, and the retry after replanning succeeds.

    The population is built on the probe now, so the two fixture-lifecycle
    assertions this used to count — enabling a bundle and putting it back — are
    gone with the wrappers that existed only to do that. The artifact half of the
    old fixture moved to an extension's own release suite, where an artifact
    actually exists.
    """
    from scripts.smoke import candidate_semantic_checks

    class Api:
        def __init__(self) -> None:
            self.hard_plans = 0

        def request(self, method, path, payload=None, **_kwargs):
            if path == "/api/projects/project-1/connections":
                return 200, {"connection_id": "connection-1"}
            if method == "DELETE":
                return 200, {"status": "trashed"}
            if path.endswith("/plan") and payload["action_type"] == "restore":
                return 200, {"plan_version": "restore-v1"}
            if path.endswith("/execute") and payload["action_type"] == "restore":
                return 200, {"status": "restored"}
            if path.endswith("/plan") and payload["action_type"] == "hard_delete":
                self.hard_plans += 1
                return 200, {"plan_version": f"hard-v{self.hard_plans}"}
            if path.endswith("/execute") and payload["plan_version"] == "hard-v1":
                return 409, {"detail": "Lifecycle plan is stale"}
            if path.endswith("/execute") and payload["plan_version"] == "hard-v2":
                return 200, {"status": "hard_deleted"}
            return 200, {}

    populated = {
        "project": 1,
        "workflow": 1,
        "dataset": 1,
        "connection": 1,
        "extension_state": 1,
        "job": 1,
    }
    stale = {**populated, "connection": 2}
    counts = iter((populated, populated, stale, stale, dict.fromkeys(populated, 0), {"project": 0}))

    project_kwargs = []
    project_workflows = []
    monkeypatch.setattr(
        candidate_semantic_checks,
        "_probe_workflow_id",
        lambda *_args: "extension.install-1.acme_probe_workflow",
    )
    monkeypatch.setattr(
        candidate_semantic_checks,
        "_project",
        lambda _api, workflow_id, **kwargs: (
            project_workflows.append(workflow_id),
            project_kwargs.append(kwargs),
            "project-1",
        )[-1],
    )
    monkeypatch.setattr(candidate_semantic_checks, "_task", lambda *_args, **_kwargs: "job-1")
    monkeypatch.setattr(candidate_semantic_checks, "_job_detail", lambda *_args: ("completed", {}))
    monkeypatch.setattr(
        candidate_semantic_checks, "_seed_populated_project_state", lambda **_: None
    )
    monkeypatch.setattr(
        candidate_semantic_checks, "_populated_project_counts", lambda *_args: next(counts)
    )
    monkeypatch.setattr(candidate_semantic_checks, "_psql", lambda *_args: "")
    monkeypatch.setattr(candidate_semantic_checks, "_cleanup_project", lambda *_args: None)
    monkeypatch.setattr(
        candidate_semantic_checks, "_assert_product_removed_project_queues", lambda *_args: None
    )
    evidence = candidate_semantic_checks.Evidence("A27")

    # The fixture directly: `_a27` installs a real probe over HTTP.
    candidate_semantic_checks._a27_populated_project_fixture(Api(), evidence, "acme.probe")

    assert [row["name"] for row in evidence.assertions] == [
        "populated_project_fixture",
        "populated_project_restore",
        "populated_project_stale_plan_atomic",
        "populated_project_clean_retry",
    ]
    assert all(row["passed"] for row in evidence.assertions)
    assert project_kwargs == [{}]
    assert project_workflows == ["extension.install-1.acme_probe_workflow"]


def test_candidate_workflow_output_reads_finalized_artifact_collection() -> None:
    from scripts.smoke import candidate_semantic_checks

    artifact = {
        "artifact_id": "artifact-1",
        "artifact_handle": "artifact:artifact-1",
        "size_bytes": 3168,
    }

    assert (
        candidate_semantic_checks._workflow_output(
            {"workflow_run": {"result": {"artifacts": {"catalog_artifact": artifact}}}},
            "catalog_artifact",
        )
        == artifact
    )


def test_populated_project_seed_resolves_required_extension_install(monkeypatch) -> None:
    from scripts.smoke import candidate_semantic_checks

    statements = []
    monkeypatch.setattr(candidate_semantic_checks, "_psql", statements.append)

    dataset_key = candidate_semantic_checks._seed_populated_project_state(
        project_id="00000000-0000-0000-0000-000000000010",
        connection_id="connection-1",
    )

    assert len(statements) == 1
    assert "join lateral (select install_id from extension_installs" in statements[0]
    assert "ei.install_id" in statements[0]
    assert "pc.extension_install_id" not in statements[0]
    assert dataset_key.startswith("release-smoke-a27-")
    assert "insert into registry_datasets" in statements[0]
    assert "select 'release-smoke-a27-" in statements[0]
    assert "p.workspace_id" in statements[0]
    assert "'specific_projects_only'" in statements[0]


def test_the_platform_semantic_runner_owns_only_generic_lifecycle() -> None:
    """D09 asserts platform behaviour on a synthetic probe, never a bundle's matrix.

    The demo workflow matrix that used to live here moved to the owning bundle's
    own release suite under `extensions/<bundle>/release/checks.py`, which reaches
    the candidate over the public API only. What stays is what the platform owns
    whichever bundles ship: projections and catalog rows following the enabled
    state, a rejected write leaving nothing behind, and cleanup.

    The forbidden list below is written by prefix rather than by name, because a
    test that spells out a bundle id to forbid it has written the very coupling
    the boundary inventory exists to keep out.
    """
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    group_d = GROUP_D.read_text(encoding="utf-8")

    assert '"D09": _d09' in source
    d09_source = source[source.index("def _d09(") : source.index("def _d10(")]

    # Driven by the throwaway probe, through identifiers the API hands back.
    assert "installed_probe_extension" in d09_source
    for key in (
        "extension_projection_lifecycle",
        "extension_public_catalog_lifecycle",
        "workflow_effective_availability_contract",
        "job_detail_canonical_current_step",
        "workflow_override_envelope_rejected_before_persistence",
        "extension_public_cleanup",
        "global_search_family_registry",
    ):
        assert key in d09_source, key
    assert "parallel_worktree_cleanup" not in source

    # No bundle identity, and no bundle's workflow, may appear in it.
    for prefix in ("flowsteward.", "flowsteward_", "demo_", "showcase", "catalog_artifact"):
        assert prefix not in d09_source, prefix

    # Whatever suites the bundles declare are run, without being named here —
    # and each one in its own container, never in this process.
    assert '"BUNDLES": _bundle_release_checks' in source
    assert "discover_bundle_suites" in source
    assert "run_bundle_suite" in source
    assert "exec_module" not in source

    assert "D09()" in group_d
    assert '"D10": _d10' in source
    assert "D10()" in group_d
    assert '"D11": _d11' in source
    assert '"D12": _d12' not in source


def test_candidate_global_search_probe_uses_candidate_owned_category_contract() -> None:
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    probe_source = source[
        source.index("def _global_search_family_registry(") : source.index("def _d09(")
    ]

    assert "from core" not in probe_source
    assert 'unsupported.get("supported_categories")' in probe_source
    assert 'category != "all"' in probe_source


def test_workflow_required_inputs_follow_public_contract() -> None:
    from scripts.smoke import candidate_semantic_checks

    class Api:
        def request(self, method, path, payload=None, **_kwargs):
            assert method == "GET"
            assert path == "/api/workflows/workflow-1/contract"
            assert payload is None
            return 200, {
                "interface": {
                    "input_schema": {
                        "type": "object",
                        "required": ["latitude", "longitude", "timezone"],
                        "properties": {
                            "latitude": {"type": "number", "default": 54.69},
                            "longitude": {"type": "number", "examples": [25.28]},
                            "timezone": {"type": "string", "enum": ["auto"]},
                        },
                    }
                }
            }

    assert candidate_semantic_checks._workflow_required_inputs(Api(), "workflow-1") == {
        "latitude": 54.69,
        "longitude": 25.28,
        "timezone": "auto",
    }


def test_d11_step_status_reads_public_workflow_run_evidence() -> None:
    from scripts.smoke.candidate_semantic_checks import _d11_step_status

    detail = {
        "workflow_run": {
            "steps": [
                {"step_id": "create_dataset", "status": "completed"},
                {"step_id": "read_rows", "status": "completed"},
            ]
        }
    }

    assert _d11_step_status(detail, "create_dataset") == "completed"
    assert _d11_step_status(detail, "read_rows") == "completed"
    assert _d11_step_status(detail, "missing") == ""


def test_d10_mailpit_matcher_preserves_supported_plain_text_formatting() -> None:
    from scripts.smoke.candidate_semantic_checks import _mailpit_message_matches

    message = {
        "From": {"Name": "Flow Steward candidate", "Address": "flow-steward@candidate.local"},
        "To": [{"Address": "release@candidate.local"}],
        "Subject": "D10 formatted Žara",
        "Text": "Release evidence\r\n• owner: Žara\r\n• status: ready\r\n",
        "HTML": "",
    }

    assert _mailpit_message_matches(
        message,
        sender="flow-steward@candidate.local",
        recipient="release@candidate.local",
        subject="D10 formatted Žara",
        body="Release evidence\n• owner: Žara\n• status: ready",
    )


def test_d10_owns_generic_external_effect_persistence() -> None:
    """D10 asserts effect persistence on the probe's action, not a bundle's operation.

    The mail-delivery assertions that used to live here — saved templates, their
    delivered messages, and the template scope denial — moved to the owning
    bundle's `release/checks.py`, which reaches the candidate over the public API
    and the mail sink that extension delivers into.
    What stays is the generic half: the row the platform writes around any action
    that claims an effect, the status it records when one refuses, and the fact
    that an action cannot reach across a project or an account.
    """
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    d10_source = source[source.index("def _d10(") : source.index("def _probe_workflow_id(")]

    assert "installed_probe_extension" in d10_source
    for key in (
        "self_hosted_private_network_override",
        "external_effect_audit_persistence",
        "external_effect_failure_status",
        "extension_action_scope_denial",
    ):
        assert key in d10_source, key

    # A refused action must be recorded as failed, not merely absent, and the
    # succeeded row must carry what makes a delivery auditable.
    assert "has_provider_message_id" in d10_source
    assert "has_idempotency_key" in d10_source
    assert "effects_after" in d10_source

    # Written by prefix: naming a bundle to forbid it writes the coupling itself.
    for prefix in ("flowsteward.", "flowsteward_", "send_email", "smtp", "mailpit"):
        assert prefix not in d10_source.lower(), prefix


def test_a29_observes_a_bounded_post_delete_runtime_window() -> None:
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    a29_source = source[
        source.index("def _a29(") : source.index("def _d11_effective_workflow_defaults(")
    ]

    assert "post_delete_executor_quiescence_window" in a29_source
    assert 'api.request("GET", "/health/ready")' in a29_source
    assert "candidate_log_window_check.py" in a29_source
    assert "time.sleep(3)" in a29_source
    # The fence is proven by the purge the product exposes, refused for the
    # quiescence reason, not by the direct endpoint that refuses every project.
    assert "_purge_trashed_project(api, project_id)" in a29_source
    assert '== "project_executors_not_quiesced"' in a29_source
    assert "/api/trash/project/{project_id}/hard-delete" not in a29_source


def test_catalog_state_checks_list_and_detail_for_every_public_kind() -> None:
    from scripts.smoke.candidate_semantic_checks import _extension_public_catalog_state

    artifact_ids = {
        kind: [f"extension.install-1.demo_{kind}"]
        for kind in ("agent", "skill", "tool", "workflow", "dataset")
    }

    class Api:
        def __init__(self, active: bool) -> None:
            self.active = active

        def request(self, _method, path, _payload=None, **_kwargs):
            expected = next(
                artifact_id
                for values in artifact_ids.values()
                for artifact_id in values
                if artifact_id.replace(".", "%2E") in path or artifact_id in path
            )
            if self.active:
                return 200, {"id": expected}
            return (200, []) if "?search=" in path else (404, {"detail": "not found"})

    active_ok, _ = _extension_public_catalog_state(Api(True), artifact_ids, active=True)
    disabled_ok, _ = _extension_public_catalog_state(Api(False), artifact_ids, active=False)

    assert active_ok is True
    assert disabled_ok is True


def test_release_b01_exports_provider_identity_to_route_validation() -> None:
    source = GROUP_B_SNIPPETS.read_text(encoding="utf-8")
    b01_source = source.split("b01(){", 1)[1].split("# ---------- B03", 1)[0]

    assert (
        b01_source.index("api GET /api/model-providers")
        < b01_source.index('FS_PROVIDER_ID="$PROVIDER_ID"')
        < b01_source.index("python3 -c")
    )
    submitted_job_source = b01_source[b01_source.index("job=$(api POST /api/workflow-runs") :]
    assert submitted_job_source.index("b01_observe_and_settle_job") < (
        submitted_job_source.index("cleanup_project")
    )


def _run_b01_job_settlement_driver(
    tmp_path: Path,
    *,
    observed_statuses: list[str],
    settled_statuses: list[str] | None = None,
    cancel_status: int = 200,
    cancel_body: str = '{"status":"cancellation_requested"}',
) -> subprocess.CompletedProcess[str]:
    statuses = tmp_path / "observed-statuses"
    statuses.write_text("\n".join(observed_statuses) + "\n", encoding="utf-8")
    settled = tmp_path / "settled-statuses"
    settled.write_text(
        "\n".join(settled_statuses or ["cancelled"]) + "\n",
        encoding="utf-8",
    )
    trace = tmp_path / "api-trace"
    trace.write_text("", encoding="utf-8")
    observed_index = tmp_path / "observed-index"
    observed_index.write_text("1", encoding="utf-8")
    settled_index = tmp_path / "settled-index"
    settled_index.write_text("1", encoding="utf-8")
    driver = tmp_path / "b01-job-settlement-driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -uo pipefail
snippets="$1"
observed_statuses="$2"
settled_statuses="$3"
trace="$4"
observed_index="$5"
settled_index="$6"
set --
source "$snippets"
next_value() {{
  values="$1"; index_file="$2"
  index=$(cat "$index_file")
  value=$(sed -n "${{index}}p" "$values")
  [ -n "$value" ] || value=$(tail -n 1 "$values")
  printf '%s' "$((index + 1))" > "$index_file"
  printf '%s\n' "$value"
}}
PSQL() {{
  case "$1" in
    *"select status from jobs"*) next_value "$observed_statuses" "$observed_index" ;;
    *"select coalesce(worker_id"*) printf '%s\n' worker-1 ;;
    *) printf '\n' ;;
  esac
}}
api() {{
  method="$1"; path="$2"
  printf '%s %s\n' "$method" "$path" >> "$trace"
  case "$method $path" in
    "POST /api/jobs/job-1/cancel")
      printf '%s__HTTP_{cancel_status}__' '{cancel_body}' ;;
    "GET /api/jobs/job-1")
      state=$(next_value "$settled_statuses" "$settled_index")
      printf '{{"status":"%s"}}__HTTP_200__' "$state" ;;
    *) printf '%s__HTTP_500__' '{{"code":"unexpected_test_request"}}' ;;
  esac
}}
FS_SMOKE_B01_OBSERVATION_ATTEMPTS={len(observed_statuses)}
FS_SMOKE_B01_OBSERVATION_DELAY_SECONDS=0
FS_SMOKE_B01_SETTLE_ATTEMPTS={len(settled_statuses or ["cancelled"])}
FS_SMOKE_B01_SETTLE_DELAY_SECONDS=0
b01_observe_and_settle_job job-1 project-1
result=$?
printf 'result=%s status=%s worker=%s\n' "$result" "$B01_JOB_STATUS" "$B01_WORKER_ID"
exit "$result"
""",
        encoding="utf-8",
    )

    return subprocess.run(
        [
            "bash",
            str(driver),
            str(GROUP_B_SNIPPETS),
            str(statuses),
            str(settled),
            str(trace),
            str(observed_index),
            str(settled_index),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_b01_terminal_job_skips_cancellation_before_cleanup(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    result = _run_b01_job_settlement_driver(
        tmp_path,
        observed_statuses=[terminal_status],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"status={terminal_status}" in result.stdout
    assert (tmp_path / "api-trace").read_text(encoding="utf-8") == ""


def test_b01_approval_job_is_cancelled_and_settled_before_cleanup(tmp_path: Path) -> None:
    result = _run_b01_job_settlement_driver(
        tmp_path,
        observed_statuses=["awaiting_approval"],
        settled_statuses=["cancelled"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=cancelled" in result.stdout
    assert (tmp_path / "api-trace").read_text(encoding="utf-8").splitlines() == [
        "POST /api/jobs/job-1/cancel",
        "GET /api/jobs/job-1",
    ]


def test_b01_job_exceeding_wait_is_cancelled_then_boundedly_settled(
    tmp_path: Path,
) -> None:
    result = _run_b01_job_settlement_driver(
        tmp_path,
        observed_statuses=["running", "running"],
        settled_statuses=["running", "cancelled"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=cancelled" in result.stdout
    assert (tmp_path / "api-trace").read_text(encoding="utf-8").splitlines() == [
        "POST /api/jobs/job-1/cancel",
        "GET /api/jobs/job-1",
        "GET /api/jobs/job-1",
    ]


def test_b01_permanent_cancel_failure_retains_fixture_with_typed_diagnostics(
    tmp_path: Path,
) -> None:
    result = _run_b01_job_settlement_driver(
        tmp_path,
        observed_statuses=["running"],
        cancel_status=503,
        cancel_body='{"code":"worker_unavailable","detail":"cannot cancel"}',
    )

    assert result.returncode == 1
    assert "/api/jobs/job-1/cancel HTTP 503" in result.stderr
    assert '"code":"worker_unavailable"' in result.stderr
    assert "retained job=job-1 project=project-1" in result.stderr
    assert (tmp_path / "api-trace").read_text(encoding="utf-8").splitlines() == [
        "POST /api/jobs/job-1/cancel"
    ]


def test_group_b_project_cleanup_retries_only_typed_executor_quiescence(tmp_path) -> None:
    attempts = tmp_path / "attempts"
    attempts.write_text("0", encoding="utf-8")
    driver = tmp_path / "cleanup-driver.sh"
    driver.write_text(
        """#!/usr/bin/env bash
set -uo pipefail
snippets="$1"
attempts_file="$2"
set --
source "$snippets"
api(){
  method="$1"; path="$2"
  case "$method $path" in
    "POST /api/trash/project/project-1/trash")
      printf '%s__HTTP_200__' '{"status":"trashed"}' ;;
    "POST /api/trash/project/project-1/plan")
      printf '%s__HTTP_200__' '{"plan_version":"plan-v1"}' ;;
    "POST /api/trash/project/project-1/execute")
      [ "$3" = '{"action_type":"hard_delete","plan_version":"plan-v1"}' ] || {
        printf '%s__HTTP_500__' '{"code":"unexpected_execute_body"}'
        return
      }
      attempts=$(cat "$attempts_file")
      attempts=$((attempts + 1))
      printf '%s' "$attempts" > "$attempts_file"
      if [ "$attempts" = 1 ]; then
        printf '%s__HTTP_409__' '{"code":"project_executors_not_quiesced","detail":"worker settling"}'
      else
        printf '%s__HTTP_200__' '{"status":"hard_deleted"}'
      fi ;;
    "GET /api/projects/project-1")
      printf '%s__HTTP_404__' '{"detail":"Project not found"}' ;;
    *) printf '%s__HTTP_500__' '{"code":"unexpected_test_request"}' ;;
  esac
}
FS_SMOKE_CLEANUP_RETRY_DELAY_SECONDS=0 cleanup_project project-1 B01
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(driver), str(GROUP_B_SNIPPETS), str(attempts)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert attempts.read_text(encoding="utf-8") == "2"


def test_group_b_project_cleanup_reports_permanent_failure_at_its_endpoint(tmp_path) -> None:
    attempts = tmp_path / "attempts"
    attempts.write_text("0", encoding="utf-8")
    driver = tmp_path / "cleanup-driver.sh"
    driver.write_text(
        """#!/usr/bin/env bash
set -uo pipefail
snippets="$1"
attempts_file="$2"
set --
source "$snippets"
api(){
  method="$1"; path="$2"
  case "$method $path" in
    "POST /api/trash/project/project-1/trash")
      printf '%s__HTTP_200__' '{"status":"trashed"}' ;;
    "POST /api/trash/project/project-1/plan")
      printf '%s__HTTP_200__' '{"plan_version":"plan-v1"}' ;;
    "POST /api/trash/project/project-1/execute")
      attempts=$(cat "$attempts_file")
      printf '%s' "$((attempts + 1))" > "$attempts_file"
      printf '%s__HTTP_409__' '{"code":"project_has_active_jobs","detail":"approval is pending"}' ;;
    *) printf '%s__HTTP_500__' '{"code":"unexpected_test_request"}' ;;
  esac
}
FS_SMOKE_CLEANUP_RETRY_DELAY_SECONDS=0 cleanup_project project-1 B01
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(driver), str(GROUP_B_SNIPPETS), str(attempts)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert attempts.read_text(encoding="utf-8") == "1"
    assert "/api/trash/project/project-1/execute HTTP 409" in result.stderr
    assert '"code":"project_has_active_jobs"' in result.stderr
    assert "retained project=project-1" in result.stderr


def test_deploy_gate_proves_clean_candidate_preflight_without_mutation() -> None:
    source = GROUP_E.read_text(encoding="utf-8")

    assert "e14()" in source
    assert "candidate_stack.sh" in source
    assert "release-preflight-fixture-secret" in source
    assert 'before="$(docker ps -aq' in source
    assert 'after="$(docker ps -aq' in source
    assert "candidate_prepare.sh database" in source
    assert "candidate_prepare.sh worker-identity" in source
    assert "incomplete durable input exited 2 before candidate Compose mutation" in source


def test_candidate_project_cleanup_requires_product_owned_queue_removal(monkeypatch) -> None:
    from scripts.smoke import candidate_semantic_checks

    class Api:
        def __init__(self, hard_delete_status: int = 200) -> None:
            self.hard_delete_status = hard_delete_status
            self.calls: list[tuple[str, str]] = []

        def request(self, method, path, payload=None, **_kwargs):
            self.calls.append((method, path))
            if path.endswith("/jobs?per_page=100"):
                return 200, {"jobs": [{"id": "job-1", "status": "in_progress"}]}
            if path.endswith("/plan"):
                return 200, {"plan_version": "plan-v1"}
            if path.endswith("/execute"):
                assert payload == {"action_type": "hard_delete", "plan_version": "plan-v1"}
                return self.hard_delete_status, {"status": "hard_deleted"}
            return 200, {"status": "trashed"}

    docker_calls: list[list[str]] = []

    queue_inventory = ["platform.project.unrelated\n"]

    def fake_run(command, **_kwargs):
        docker_calls.append(command)
        if "list_queues" in command:
            return subprocess.CompletedProcess(command, 0, queue_inventory[0], "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("FS_SMOKE_RABBITMQ_CONTAINER", "candidate-rabbit")
    monkeypatch.setattr(candidate_semantic_checks.subprocess, "run", fake_run)
    settled: list[str] = []
    monkeypatch.setattr(
        candidate_semantic_checks,
        "_settle_job_for_cleanup",
        lambda _api, job_id: settled.append(job_id),
    )

    api = Api()
    candidate_semantic_checks._cleanup_project(api, "project-1")
    assert api.calls == [
        ("GET", "/api/projects/project-1/jobs?per_page=100"),
        ("DELETE", "/api/projects/project-1"),
        ("POST", "/api/trash/project/project-1/plan"),
        ("POST", "/api/trash/project/project-1/execute"),
    ]
    assert settled == ["job-1"]
    assert [call for call in docker_calls if "purge_queue" in call] == []

    queue_inventory[0] = (
        "platform.project.project-1\n"
        "platform.project.project-1.dead_letter\n"
        "platform.project.unrelated\n"
    )
    _raises_input_1728_1 = Api()
    with pytest.raises(RuntimeError, match="product left project queues"):
        candidate_semantic_checks._cleanup_project(_raises_input_1728_1, "project-1")
    assert [call[-1] for call in docker_calls if "purge_queue" in call] == [
        "platform.project.project-1",
        "platform.project.project-1.dead_letter",
    ]

    _raises_input_1735_1 = Api(hard_delete_status=409)
    with pytest.raises(RuntimeError, match="cleanup"):
        candidate_semantic_checks._cleanup_project(_raises_input_1735_1, "project-1")


def test_candidate_project_cleanup_retries_only_executor_quiescence_conflict(
    monkeypatch,
) -> None:
    from scripts.smoke import candidate_semantic_checks

    class Api:
        def __init__(self) -> None:
            self.hard_delete_attempts = 0

        def request(self, method, path, payload=None, **_kwargs):
            if path.endswith("/jobs?per_page=100"):
                return 200, {"jobs": []}
            if path.endswith("/plan"):
                return 200, {"plan_version": "plan-v1"}
            if path.endswith("/execute"):
                self.hard_delete_attempts += 1
                if self.hard_delete_attempts == 1:
                    return 409, {"code": "project_executors_not_quiesced"}
                return 200, {"status": "hard_deleted"}
            return 200, {"status": "trashed"}

    monkeypatch.setenv("FS_SMOKE_RABBITMQ_CONTAINER", "candidate-rabbit")
    monkeypatch.setattr(candidate_semantic_checks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        candidate_semantic_checks,
        "_assert_product_removed_project_queues",
        lambda _project_id: None,
    )
    api = Api()

    candidate_semantic_checks._cleanup_project(api, "project-1")

    assert api.hard_delete_attempts == 2


def test_hitl_fixture_cleanup_uses_executor_quiescence_retry(monkeypatch) -> None:
    from scripts.smoke import candidate_semantic_checks

    class Api:
        def __init__(self) -> None:
            self.hard_delete_attempts = 0
            self.paths: list[str] = []

        def request(self, method, path, payload=None, **_kwargs):
            self.paths.append(path)
            if path.endswith("/jobs?per_page=100"):
                return 200, {"jobs": []}
            if path == "/api/trash/project/project-1/plan":
                return 200, {"plan_version": "plan-v1"}
            if path == "/api/trash/project/project-1/execute":
                self.hard_delete_attempts += 1
                if self.hard_delete_attempts == 1:
                    return 409, {"code": "project_executors_not_quiesced"}
            return 200, {"status": "ok"}

    monkeypatch.setattr(candidate_semantic_checks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        candidate_semantic_checks,
        "_assert_product_removed_project_queues",
        lambda _project_id: None,
    )
    api = Api()

    candidate_semantic_checks._cleanup_hitl_fixture(api, "project-1", "workflow-1")

    assert api.hard_delete_attempts == 2
    assert "/api/trash/registry_workflow/workflow-1/hard-delete" in api.paths


def test_candidate_job_cleanup_settles_inflight_work_before_project_delete(
    monkeypatch,
) -> None:
    import inspect

    from scripts.smoke import candidate_semantic_checks

    calls: list[tuple[str, str]] = []

    class Api:
        def request(self, method, path, payload=None, **_kwargs):
            calls.append((method, path))
            return 200, {"status": "cancelled"}

    monkeypatch.setattr(
        candidate_semantic_checks,
        "_wait_job_terminal",
        lambda *_args, **_kwargs: "cancelled",
    )

    candidate_semantic_checks._settle_job_for_cleanup(Api(), "job-1")

    assert calls == [("POST", "/api/jobs/job-1/cancel")]
    assert "_settle_job_for_cleanup(api, submitted_job_id)" in inspect.getsource(
        candidate_semantic_checks._a24
    )


def test_candidate_hitl_fixture_proves_rework_idempotency_and_cancel(monkeypatch) -> None:
    from scripts.smoke import candidate_semantic_checks

    class FakeApi:
        account = "00000000-0000-0000-0000-000000000002"

        def __init__(self) -> None:
            self.decisions: dict[str, dict[str, object]] = {}
            self.approvals: list[dict[str, object]] = []
            self.approved = False

        def request(self, method, path, payload=None, *, extra_headers=None, **_kwargs):
            if path == "/api/workflows?per_page=500":
                return 200, {"workflows": [{"workflow_id": "weekly_report", "enabled": True}]}
            if path == "/api/jobs/job-hitl/approval":
                approval_id = "gate-2" if self.approvals else "gate-1"
                return 200, {"approval_id": approval_id, "thread_id": "thread-1"}
            if path == "/api/jobs/job-hitl/approvals":
                return 200, {"job_id": "job-hitl", "approvals": list(self.approvals)}
            if path == "/api/approvals/thread-1":
                key = str((extra_headers or {}).get("Idempotency-Key") or "")
                if key in self.decisions:
                    return 202, {**self.decisions[key], "deduplicated": True}
                row = {
                    "approved": bool(payload["approved"]),
                    "comment": str(payload["comment"]),
                }
                self.decisions[key] = {"status": "in_progress", "deduplicated": False}
                self.approvals.append(row)
                self.approved = bool(payload["approved"])
                return 202, self.decisions[key]
            if path == "/api/jobs/job-cancel/cancel":
                return 200, {"cancelled": True}
            raise AssertionError((method, path, payload))

    api = FakeApi()
    jobs = iter(("job-hitl", "job-cancel"))
    effect_counts = iter((1, 1, 2))
    monkeypatch.setattr(
        candidate_semantic_checks, "_create_hitl_workflow", lambda *_args: "a23_gate_fixture"
    )
    monkeypatch.setattr(candidate_semantic_checks, "_project", lambda *_args: "project-1")
    monkeypatch.setattr(candidate_semantic_checks, "_task", lambda *_args, **_kwargs: next(jobs))
    monkeypatch.setattr(
        candidate_semantic_checks, "_hitl_effect_count", lambda *_args: next(effect_counts)
    )
    monkeypatch.setattr(candidate_semantic_checks, "_cleanup_hitl_fixture", lambda *_args: None)

    def fake_wait(_api, job_id, _statuses, **_kwargs):
        if job_id == "job-cancel":
            return {"status": "cancelled", "error": None}
        if api.approved:
            return {"status": "completed", "thread_id": "thread-1"}
        return {"status": "awaiting_approval", "thread_id": "thread-1"}

    monkeypatch.setattr(candidate_semantic_checks, "_wait_job_state", fake_wait)
    evidence = candidate_semantic_checks.Evidence("A23")

    candidate_semantic_checks._a23(api, evidence)

    assert evidence.assertions
    assert all(row["passed"] for row in evidence.assertions), evidence.assertions


def test_candidate_semantic_fixture_creates_a_plain_project_for_the_workflow() -> None:
    from scripts.smoke import candidate_semantic_checks

    class Api:
        def request(self, method, path, payload=None, **_kwargs):
            if (method, path) == ("GET", "/api/workspaces"):
                return 200, {"workspaces": [{"id": "workspace-1", "workspace_key": "default"}]}
            assert (method, path) == ("POST", "/api/projects")
            assert payload["name"].startswith("release-semantic-")
            assert payload["workspace_id"] == "workspace-1"
            assert payload["workflow_ids"] == ["weekly_report"]
            for retired in ("project_type", "project_root", "container_root", "git_root"):
                assert retired not in payload, retired
            return 201, {"project_id": "project-1"}

    assert candidate_semantic_checks._project(Api(), "weekly_report") == "project-1"


def test_release_profile_requires_explicit_model_route_and_mcp_credential() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    group_e = GROUP_E.read_text(encoding="utf-8")

    assert "release profile requires protected FS_SMOKE_BEARER_TOKEN" in source
    assert "release profile requires explicit FS_SMOKE_MODEL_PROVIDER" in source
    assert "release profile requires explicit FS_SMOKE_MODEL" in source
    assert "read -r MCP_BEARER_TOKEN" in group_e
    assert 'docker exec -i "$CLI_CONTAINER"' in group_e


def test_live_smoke_semantics_reject_false_success_shortcuts() -> None:
    group_b = GROUP_B_SNIPPETS.read_text(encoding="utf-8")
    group_c = GROUP_C.read_text(encoding="utf-8")

    assert "project_type" not in group_b
    assert "WF=\"${SMOKE_WF:-$(api GET '/api/workflows?per_page=500&enabled_only=true'" in group_b
    assert "B01 FAIL (no enabled workflow is available; set SMOKE_WF)" in group_b
    assert "FS_SMOKE_MODEL_PROVIDER" in group_b
    assert "FS_SMOKE_MODEL" in group_b
    assert 'PROJECT_MODEL_DEFAULTS=""' in group_b
    assert r"$PROJECT_MODEL_DEFAULTS" in group_b
    assert "FS_SMOKE_MODEL is required for a chat provider" in group_b
    assert "account-scoped provider route/model is not ready" in group_b
    assert "/api/model-providers" in group_b
    assert 'row.get("verification_status")' in group_b
    assert "failed before worker attribution" in group_b
    assert "b01_job_is_terminal" in group_b
    assert "completed|failed|cancelled|max_iterations|retryable_interrupted_step" in group_b
    assert 'cancel_path="/api/jobs/$job_id/cancel"' in group_b
    assert 'settle_path="/api/jobs/$job_id"' in group_b
    assert '[ "$st" = completed ]' in group_b
    assert "B04 FAIL (PUT -> HTTP $c" in group_b
    assert '[ "$locked" -ge 1 ]' in group_c
    assert "invalid identifier unexpectedly returned HTTP" in group_c
    assert "transport failure" in group_c


def test_mutating_groups_use_the_selected_candidate_base() -> None:
    group_c = GROUP_C.read_text(encoding="utf-8")
    group_d = GROUP_D_COMMON.read_text(encoding="utf-8")

    assert "BASE=${BASE:-${FS_SMOKE_BASE:-https://localhost:8099}}" in group_c
    assert 'BASE="${BASE:-${FS_SMOKE_BASE:-https://localhost:8099}}"' in group_d


def test_release_groups_have_no_cross_stack_container_or_mailpit_defaults() -> None:
    group_c = GROUP_C.read_text(encoding="utf-8")
    group_d = GROUP_D.read_text(encoding="utf-8")
    group_b = GROUP_B_SNIPPETS.read_text(encoding="utf-8")

    assert "localhost:8025" not in group_c
    assert "docker ps --format '{{.Names}}'" not in group_d
    assert "flow-steward-fs-app-1" not in group_d
    assert 'host\\":\\"pg-fs' not in group_b
    assert "FS_SMOKE_MAILPIT_BASE" in group_c
    assert "FS_SMOKE_WORKER_CONTAINER" in group_d
    assert "FS_SMOKE_DB_HOST" in group_b


def test_deploy_checks_execute_real_candidate_operations() -> None:
    group_e = GROUP_E.read_text(encoding="utf-8")
    semantic = SEMANTIC_RUNNER.read_text(encoding="utf-8")

    assert 'restore-parity-capture --output "$baseline" --confirm --plain' in group_e
    assert 'restore-parity-verify --baseline "$baseline" --confirm --plain' in group_e

    assert "database upgrade" in group_e
    assert "restore-parity-capture" in group_e
    assert "restore-parity-verify" in group_e
    assert "FS_SMOKE_ROLLBACK_SCRIPT" in group_e
    assert "FS_SMOKE_UPGRADE_SCRIPT" in group_e
    assert "FS_SMOKE_REBUILD_SCRIPT" in group_e
    assert "FS_SMOKE_RESTART_SCRIPT" in group_e
    assert '"$CUTOVER_RUNNER" E12' in group_e
    assert '"E12": _e12' in semantic
    assert '"cutover-smoke"' in semantic
    assert "bearer-token-stdin" in semantic
    assert "password-stdin" in semantic
    assert '"/api/auth/tokens"' in semantic


def test_candidate_cutover_uses_internal_tls_proxy_for_secure_session_cookie(monkeypatch) -> None:
    from scripts.smoke import candidate_semantic_checks

    monkeypatch.setenv("FS_SMOKE_BASE", "https://localhost:18799")
    monkeypatch.setenv("FS_SMOKE_INTERNAL_BASE", "http://fs-app:8099")

    assert candidate_semantic_checks._cutover_base_url() == "https://traefik:18799"


def test_deploy_cli_checks_use_the_packaged_module_entrypoint(tmp_path: Path) -> None:
    """E09 delegates to the durable project-bound maintenance wrapper."""
    fake_upgrade = REPO_ROOT / ".flow-steward" / f"test-upgrade-{tmp_path.name}.sh"
    fake_upgrade.parent.mkdir(parents=True, exist_ok=True)
    fake_upgrade.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_upgrade.chmod(0o755)
    try:
        result = subprocess.run(
            ["bash", str(GROUP_E), "E09"],
            cwd=REPO_ROOT,
            env={**os.environ, "FS_SMOKE_UPGRADE_SCRIPT": str(fake_upgrade)},
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        fake_upgrade.unlink(missing_ok=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "E09 PASS" in result.stdout


def test_ambiguous_queue_topology_fails_before_group_mutation(tmp_path: Path) -> None:
    mutation_marker = tmp_path / "mutated"
    result = _run_gate(
        tmp_path,
        manifest=_manifest(_check("A01")),
        worker_count=2,
        group_source=f"""#!/usr/bin/env bash
touch {mutation_marker}
printf 'A01 PASS should-not-run\\n'
""",
    )

    assert result.returncode != 0
    assert "worker_count=2" in result.stdout
    assert "exactly one queue consumer is required" in result.stdout
    assert not mutation_marker.exists()


def test_d05_never_accepts_nonterminal_job_as_success(tmp_path: Path) -> None:
    group_dir = tmp_path / "groupD"
    group_dir.mkdir()
    copied_runner = group_dir / "smoke_D.sh"
    copied_runner.write_text(GROUP_D.read_text(encoding="utf-8"), encoding="utf-8")
    (group_dir / "smoke_common.sh").write_text(
        f"""#!/usr/bin/env bash
set -uo pipefail
EXT_ROOT={tmp_path}/extensions
REPO_ROOT={tmp_path}
RUNID=test
SMOKE_RC=0
sleep() {{ :; }}
mkbundle() {{ mkdir -p "$1"; }}
uninstall_ext() {{ printf '200\\n'; }}
last_code() {{ printf '200\\n'; }}
PSQL() {{ printf '1\\n'; }}
assert_project_queues_removed() {{ :; }}
jget() {{ python3 -c 'import json,sys; print(json.load(sys.stdin).get("'"$1"'", ""))'; }}
ok() {{ printf '%s PASS\\n' "$1"; }}
bad() {{ printf '%s FAIL: %s\\n' "$1" "$2"; SMOKE_RC=1; }}
fs() {{
  case "$1 $2" in
    'GET /api/workspaces') printf '{{"workspaces":[{{"id":"ws","workspace_key":"default"}}]}}' ;;
    'GET /api/workflows?search=smoked_r_workflow') printf '[{{"workflow_id":"wf"}}]' ;;
    'POST /api/projects') printf '{{"project_id":"project"}}' ;;
    'POST /api/workflow-runs') printf '{{"job_id":"job"}}' ;;
    'GET /api/jobs/job') printf '{{"status":"in_progress","thread_state":{{"weather_raw":{{"ok":true}}}}}}' ;;
    *) printf '{{}}' ;;
  esac
}}
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(copied_runner), "D05"],
        cwd=tmp_path,
        env={
            **os.environ,
            "FS_SMOKE_ISOLATED": "1",
            "FS_SMOKE_PG_CONTAINER": "test-pg",
            "FS_SMOKE_JOB_TIMEOUT_SECONDS": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "D05 FAIL" in result.stdout
    assert "status=in_progress" in result.stdout


def test_group_d_project_fixture_cleanup_hard_deletes_and_purges_owned_queues(
    tmp_path: Path,
) -> None:
    group_dir = tmp_path / "groupD"
    group_dir.mkdir()
    copied_runner = group_dir / "smoke_D.sh"
    copied_runner.write_text(GROUP_D.read_text(encoding="utf-8"), encoding="utf-8")
    calls = tmp_path / "calls.log"
    (group_dir / "smoke_common.sh").write_text(
        f"""#!/usr/bin/env bash
set -uo pipefail
EXT_ROOT={tmp_path}/extensions
REPO_ROOT={tmp_path}
RUNID=test
SMOKE_RC=0
CALLS={calls}
mkbundle() {{ mkdir -p "$1"; }}
uninstall_ext() {{ printf '200\n'; }}
last_code() {{ printf '201\n'; }}
PSQL() {{ printf '1\n'; }}
jget() {{ python3 -c 'import json,sys; print(json.load(sys.stdin).get("'"$1"'", ""))'; }}
ok() {{ printf '%s PASS\n' "$1"; }}
bad() {{ printf '%s FAIL: %s\n' "$1" "$2"; SMOKE_RC=1; }}
fs() {{
  printf '%s %s\n' "$1" "$2" >> "$CALLS"
  case "$1 $2" in
    'GET /api/workspaces') printf '{{"workspaces":[{{"id":"ws","workspace_key":"default"}}]}}' ;;
    'GET /api/workflows?search=smoked_j_workflow') printf '[{{"workflow_id":"wf"}}]' ;;
    'POST /api/projects') printf '{{"project_id":"project"}}' ;;
    'POST /api/trash/project/project/plan') printf '{{"plan_version":"plan-v1"}}' ;;
    *) printf '{{}}' ;;
  esac
}}
assert_project_queues_removed() {{
  printf 'ASSERT_QUEUES_REMOVED %s\n' "$1" >> "$CALLS"
  [ "${{FAIL_QUEUE_ASSERT:-0}}" != 1 ]
}}
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(copied_runner), "D06"],
        cwd=tmp_path,
        env={
            **os.environ,
            "FS_SMOKE_ISOLATED": "1",
            "FS_SMOKE_PG_CONTAINER": "test-pg",
            "FS_SMOKE_RABBITMQ_CONTAINER": "test-rabbit",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    cleanup_start = call_lines.index("DELETE /api/projects/project")
    assert call_lines[cleanup_start : cleanup_start + 4] == [
        "DELETE /api/projects/project",
        "POST /api/trash/project/project/plan",
        "POST /api/trash/project/project/execute",
        "ASSERT_QUEUES_REMOVED project",
    ]

    failed_cleanup = subprocess.run(
        ["bash", str(copied_runner), "D06"],
        cwd=tmp_path,
        env={
            **os.environ,
            "FAIL_QUEUE_ASSERT": "1",
            "FS_SMOKE_ISOLATED": "1",
            "FS_SMOKE_PG_CONTAINER": "test-pg",
            "FS_SMOKE_RABBITMQ_CONTAINER": "test-rabbit",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed_cleanup.returncode != 0
    assert "D06 FAIL" in failed_cleanup.stdout


def test_group_d_writes_scan_fixtures_to_candidate_extensions_mount(tmp_path: Path) -> None:
    extensions_root = tmp_path / "candidate-extensions"
    extensions_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = inspect ]; then\n'
        f"  printf '/host_mnt{extensions_root}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    env.pop("EXT_ROOT", None)
    env["FS_SMOKE_APP_CONTAINER"] = "candidate-app"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{GROUP_D_COMMON}"; printf "%s\\n" "$EXT_ROOT"',
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == str(extensions_root)


def test_release_smoke_retires_exclusive_chat_probe_and_population() -> None:
    source = SEMANTIC_RUNNER.read_text(encoding="utf-8")
    for retired in (
        "/api/chat/",
        "_control_provider_fixture",
        "chat_sessions",
        "chat_messages",
        "chat_session_summaries",
    ):
        assert retired not in source
    checks = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "A21" in {row["id"] for row in checks["checks"]}


@pytest.mark.parametrize("extra_id", ["", "disabled", "foreign"])
def test_a21_checks_scoped_reusable_workflow_discovery(monkeypatch, extra_id) -> None:
    from scripts.smoke import candidate_semantic_checks as checks

    calls = []

    class Api:
        account = "00000000-0000-0000-0000-000000000002"

        def request(self, method, path, payload=None, **kwargs):
            calls.append((method, path))
            if path == "/api/workflows?per_page=500":
                return 200, {
                    "workflows": [
                        {"workflow_id": "weekly/report", "enabled": True},
                        {"workflow_id": "disabled", "enabled": False},
                    ]
                }
            assert method == "GET"
            assert (
                path
                == "/api/projects/project-1/workflow-authoring/examples?query=weekly%2Freport&limit=50"
            )
            examples = [{"workflow_id": "weekly/report", "bundle_yaml": "workflows: []"}]
            if extra_id:
                examples.append({"workflow_id": extra_id, "bundle_yaml": "workflows: []"})
            return 200, {"examples": examples}

    monkeypatch.setattr(checks, "_project", lambda *_: "project-1")
    cleanup = []
    monkeypatch.setattr(checks, "_cleanup_project", lambda _api, project: cleanup.append(project))

    def disabled_workflows(sql):
        assert "json_agg(workflow_key)" in sql
        assert "account_id=" in sql
        assert "and not enabled" in sql
        return '["disabled"]'

    monkeypatch.setattr(checks, "_psql", disabled_workflows)
    evidence = checks.Evidence("A21")
    checks._a21(Api(), evidence)
    rows = {row["name"]: row["passed"] for row in evidence.assertions}
    assert rows["authorized_enabled_workflow_reuse"] is (not extra_id)
    assert rows["disabled_workflows_not_selected"] is (extra_id != "disabled")
    assert cleanup == ["project-1"]
    assert len(calls) == 3


def test_populated_project_seed_preserves_shared_entities_without_chat(monkeypatch) -> None:
    from scripts.smoke import candidate_semantic_checks as checks

    statements = []
    monkeypatch.setattr(checks, "_psql", lambda sql: statements.append(sql))
    dataset = checks._seed_populated_project_state(
        project_id="project-1", connection_id="connection-1"
    )
    sql = statements[0]
    assert dataset.startswith("release-smoke-a27-")
    for table in ("registry_datasets", "project_datasets", "extension_project_state"):
        assert f"insert into {table}" in sql
    assert "chat" not in sql
