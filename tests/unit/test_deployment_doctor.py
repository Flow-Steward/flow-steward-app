from __future__ import annotations

import json
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.tooling.ops import deployment_doctor as doctor
from core.application.tooling.ops import deployment_doctor_common as doctor_common
from core.application.tooling.ops.deployment_doctor import (
    CheckResult,
    DoctorOptions,
    run_doctor,
)
from core.entrypoints.cli.ops.deployment_doctor import main
from core.entrypoints.cli.ops.deployment_doctor_output import render_human


def test_http_json_reads_complete_large_readiness_payload(monkeypatch) -> None:
    payload = {"ready": True, "checks": {"queue_delivery": {"detail": "x" * 8192}}}
    response = BytesIO(json.dumps(payload).encode())
    response.status = 200  # type: ignore[attr-defined]
    monkeypatch.setattr(doctor_common.urlrequest, "urlopen", lambda *_args, **_kwargs: response)

    status, parsed, detail = doctor_common._http_json("http://fs-app:8099/health/ready", timeout=5)

    assert status == 200
    assert parsed == payload
    assert detail == ""


def test_http_json_reads_complete_large_error_readiness_payload(monkeypatch) -> None:
    payload = {"ready": False, "checks": {"lifecycle_recovery": {"detail": "x" * 8192}}}
    error = doctor_common.urlerror.HTTPError(
        "http://fs-app:8099/health/ready",
        503,
        "Service Unavailable",
        None,
        BytesIO(json.dumps(payload).encode()),
    )

    def _raise(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(doctor_common.urlrequest, "urlopen", _raise)

    status, parsed, _detail = doctor_common._http_json(
        "http://fs-app:8099/health/ready",
        timeout=5,
    )

    assert status == 503
    assert parsed == payload


def test_http_json_rejects_oversized_payload(monkeypatch) -> None:
    response = BytesIO(b"x" * (1024 * 1024 + 1))
    response.status = 200  # type: ignore[attr-defined]
    monkeypatch.setattr(doctor_common.urlrequest, "urlopen", lambda *_args, **_kwargs: response)

    status, parsed, detail = doctor_common._http_json("http://fs-app:8099/health/ready", timeout=5)

    assert status == 200
    assert parsed == {}
    assert detail == "JSON response exceeds the 1 MiB safety limit"


def test_deployment_doctor_passes_minimal_ready_repo(tmp_path: Path) -> None:
    repo = _write_ready_repo(tmp_path)
    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=True,
            static_only=True,
        )
    )

    assert payload["ok"] is True
    assert payload["summary"]["fail"] == 0
    assert any(check["id"] == "required_services" for check in payload["checks"])
    assert "Flow Steward Deployment Doctor" in render_human(payload)


def test_deployment_doctor_uses_explicit_local_storage_backend(tmp_path: Path) -> None:
    repo = _write_ready_repo(tmp_path)
    env_path = repo / ".env.prod"
    lines = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("ARTIFACT_S3_")
    ]
    lines = [
        "ARTIFACT_STORAGE_BACKEND=local_fs"
        if line.startswith("ARTIFACT_STORAGE_BACKEND=")
        else line
        for line in lines
    ]
    lines.append("ARTIFACT_LOCAL_ROOT_DIR=/data/artifacts")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=env_path,
            skip_git=True,
            static_only=True,
        )
    )

    required = next(check for check in payload["checks"] if check["id"] == "required_keys")
    assert required["status"] == "pass"


@pytest.mark.parametrize(
    ("stack_environment", "env_name", "cookie_secure", "invalid_cookie"),
    (
        ("production", ".env.prod", "1", "0"),
        ("local_development", ".env", "0", "1"),
    ),
)
def test_only_initial_installer_readiness_accepts_temporary_auth_bootstrap(
    tmp_path: Path,
    stack_environment: str,
    env_name: str,
    cookie_secure: str,
    invalid_cookie: str,
) -> None:
    repo = _write_ready_repo(tmp_path)
    env_path = repo / env_name
    env_path.write_text(
        _ready_env(
            auth_cookie_secure=cookie_secure,
            include_image_pins=stack_environment == "production",
        ).replace(
            "FS_AUTH_BOOTSTRAP_ENABLED=0",
            "FS_AUTH_BOOTSTRAP_ENABLED=1",
        ),
        encoding="utf-8",
    )

    public_payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=env_path,
            skip_git=True,
            static_only=True,
            stack_environment=stack_environment,
        )
    )
    installer_payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=env_path,
            skip_git=True,
            static_only=True,
            stack_environment=stack_environment,
            allow_initial_auth_bootstrap=True,
        )
    )
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            f"FS_AUTH_COOKIE_SECURE={cookie_secure}",
            f"FS_AUTH_COOKIE_SECURE={invalid_cookie}",
        ),
        encoding="utf-8",
    )
    insecure_installer_payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=env_path,
            skip_git=True,
            static_only=True,
            stack_environment=stack_environment,
            allow_initial_auth_bootstrap=True,
        )
    )

    public_auth = next(row for row in public_payload["checks"] if row["id"] == "auth_defaults")
    installer_auth = next(
        row for row in installer_payload["checks"] if row["id"] == "auth_defaults"
    )
    insecure_auth = next(
        row for row in insecure_installer_payload["checks"] if row["id"] == "auth_defaults"
    )
    assert public_auth["status"] == "fail"
    assert "FS_AUTH_BOOTSTRAP_ENABLED should be 0" in public_auth["detail"]
    assert installer_auth["status"] == "pass"
    assert insecure_auth["status"] == "fail"
    assert f"FS_AUTH_COOKIE_SECURE should be {cookie_secure}" in insecure_auth["detail"]


def test_deployment_doctor_reports_placeholders_without_environment_provider_key(
    tmp_path: Path,
) -> None:
    repo = _write_ready_repo(tmp_path)
    (repo / ".env.prod").write_text(
        _ready_env(
            fs_secret_key="CHANGE_ME_32_PLUS_CHARACTERS",
            openai_api_key="",
        ),
        encoding="utf-8",
    )

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=True,
            static_only=True,
        )
    )

    failed = {check["id"]: check for check in payload["checks"] if check["status"] == "fail"}
    assert payload["ok"] is False
    assert "secret_placeholders" in failed
    assert "llm_provider" not in failed


def test_deployment_doctor_skips_tracked_file_guard_without_git_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _write_ready_repo(tmp_path)

    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["git", "ls-files"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        ),
    )

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=False,
            static_only=True,
        )
    )

    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["no_enterprise_paths"]["status"] == "warn"
    assert checks["no_enterprise_paths"]["detail"] == (
        "Tracked-file guard skipped: no git metadata in this source/archive checkout."
    )


def test_deployment_doctor_uses_canonical_agent_message_table() -> None:
    assert "platform_agent_messages" in doctor.REQUIRED_DB_TABLES
    assert "agent_messages" not in doctor.REQUIRED_DB_TABLES


def test_local_development_doctor_accepts_local_auth_and_missing_image_pins(
    tmp_path: Path,
) -> None:
    repo = _write_ready_repo(tmp_path)
    (repo / ".env").write_text(
        _ready_env(
            auth_cookie_secure="0",
            include_image_pins=False,
        ),
        encoding="utf-8",
    )

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env",
            skip_git=True,
            static_only=True,
            stack_environment="local_development",
            required_compose_services=(
                "traefik",
                "rabbitmq-fs",
                "pg-fs",
                "fs-mcp",
                "fs-app",
                "fs-worker",
                "control-plane",
            ),
        )
    )

    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["required_keys"]["status"] == "pass"
    assert checks["auth_defaults"]["status"] == "pass"
    assert checks["image_pins"]["status"] == "warn"
    assert "Skipped for local development" in checks["image_pins"]["detail"]
    assert payload["summary"]["fail"] == 0


def test_local_development_doctor_allows_localhost_internal_ports(tmp_path: Path) -> None:
    repo = _write_ready_repo(tmp_path)
    (repo / "infra/docker/docker-compose.prod.yaml").write_text(
        """
services:
  traefik:
    image: traefik:test
  rabbitmq-fs:
    image: rabbitmq:test
    ports:
      - "127.0.0.1:${RABBITMQ_AMQP_HOST_PORT:-5673}:5672"
  pg-fs:
    image: postgres:test
    ports:
      - "127.0.0.1:${POSTGRES_HOST_PORT:-55432}:5432"
  fs-mcp:
    image: mcp:test
    ports:
      - "127.0.0.1:${MCP_HOST_PORT:-18100}:8100"
  fs-app:
    image: app:test
  fs-worker:
    image: worker:test
  control-plane:
    image: control:test
""",
        encoding="utf-8",
    )
    (repo / ".env").write_text(
        _ready_env(auth_cookie_secure="0", include_image_pins=False),
        encoding="utf-8",
    )

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env",
            skip_git=True,
            static_only=True,
            stack_environment="local_development",
            required_compose_services=(
                "traefik",
                "rabbitmq-fs",
                "pg-fs",
                "fs-mcp",
                "fs-app",
                "fs-worker",
                "control-plane",
            ),
        )
    )

    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["internal_ports"]["status"] == "pass"
    assert payload["summary"]["fail"] == 0


def test_production_doctor_fails_published_internal_ports(tmp_path: Path) -> None:
    repo = _write_ready_repo(tmp_path)
    (repo / "infra/docker/docker-compose.prod.yaml").write_text(
        """
services:
  traefik:
    image: traefik:test
  rabbitmq-fs:
    image: rabbitmq:test
    ports:
      - "127.0.0.1:5673:5672"
  pg-fs:
    image: postgres:test
    expose:
      - "5432"
  fs-mcp:
    image: mcp:test
    expose:
      - "8100"
  fs-app:
    image: app:test
  fs-worker:
    image: worker:test
  control-plane:
    image: control:test
""",
        encoding="utf-8",
    )

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=True,
            static_only=True,
            stack_environment="production",
        )
    )

    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["internal_ports"]["status"] == "fail"
    assert checks["internal_ports"]["detail"] == "rabbitmq-fs"


def test_postgres_only_doctor_accepts_canonical_environment(tmp_path: Path) -> None:
    repo = _write_ready_repo(tmp_path)
    (repo / ".env.prod").write_text(_ready_env(), encoding="utf-8")

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=True,
            static_only=True,
            required_compose_services=(
                "traefik",
                "rabbitmq-fs",
                "pg-fs",
                "fs-mcp",
                "fs-app",
                "fs-worker",
                "control-plane",
            ),
        )
    )

    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["required_keys"]["status"] == "pass"
    assert checks["secret_placeholders"]["status"] == "pass"


def test_deployment_doctor_main_emits_json(tmp_path: Path, capsys) -> None:
    repo = _write_ready_repo(tmp_path)

    assert (
        main(
            [
                "--repo-root",
                str(repo),
                "--env-file",
                str(repo / ".env.prod"),
                "--skip-git",
                "--static-only",
                "--stack-environment",
                "production",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["summary"]["fail"] == 0


def test_deployment_doctor_main_accepts_stack_environment_for_local_env(
    tmp_path: Path, capsys
) -> None:
    repo = _write_ready_repo(tmp_path)
    (repo / ".env").write_text(
        _ready_env(auth_cookie_secure="0", include_image_pins=False),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--repo-root",
                str(repo),
                "--env-file",
                str(repo / ".env"),
                "--skip-git",
                "--static-only",
                "--json",
                "--stack-environment",
                "local-development",
                "--required-service",
                "traefik",
                "--required-service",
                "rabbitmq-fs",
                "--required-service",
                "pg-fs",
                "--required-service",
                "fs-mcp",
                "--required-service",
                "fs-app",
                "--required-service",
                "fs-worker",
                "--required-service",
                "control-plane",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["auth_defaults"]["status"] == "pass"
    assert checks["image_pins"]["status"] == "warn"


def test_deployment_doctor_host_probe_mode_runs_live_checks_in_current_process(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _write_ready_repo(tmp_path)
    seen: list[str] = []
    http_base_urls: list[str] = []

    def _pass(check_id: str):
        def _inner(*_args, **_kwargs):
            seen.append(check_id)
            return CheckResult(
                id=check_id,
                category="runtime",
                title=check_id,
                status="pass",
            )

        return _inner

    def _http_pass(check_id: str):
        def _inner(
            base_url: str,
            *,
            timeout: float,
            # AC17-R106: the real checks now take the profile so their remediation can
            # name a container this installation runs.
            deployment_profile: str = "scalable",
            allow_lifecycle_mutation: bool = False,
            allow_release_transition: bool = False,
        ):
            del timeout, deployment_profile
            del allow_lifecycle_mutation, allow_release_transition
            seen.append(check_id)
            http_base_urls.append(base_url)
            return CheckResult(
                id=check_id,
                category="runtime",
                title=check_id,
                status="pass",
            )

        return _inner

    monkeypatch.setattr(doctor, "_check_api_readiness", _http_pass("api_ready"))
    monkeypatch.setattr(doctor, "_check_db_connectivity", _pass("db_connectivity"))
    monkeypatch.setattr(doctor, "_check_queue_connectivity", _pass("rabbitmq_socket"))
    monkeypatch.setattr(
        doctor,
        "_check_queue_worker_health",
        _http_pass("queue_worker_health"),
    )
    monkeypatch.setattr(doctor, "_check_mcp_auth_readiness", _pass("mcp_auth"))
    monkeypatch.setattr(doctor, "_check_compose_runtime_services", _pass("compose_runtime"))

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=True,
            probe_mode="host",
            base_url="http://fs-app:8099",
        )
    )

    assert payload["ok"] is True
    assert {
        "api_ready",
        "db_connectivity",
        "rabbitmq_socket",
        "queue_worker_health",
        "mcp_auth",
        "compose_runtime",
    } <= set(seen)
    assert "runtime_python_dependencies" not in {check["id"] for check in payload["checks"]}
    assert http_base_urls == ["http://fs-app:8099", "http://fs-app:8099"]


def test_deployment_doctor_live_failure_blocks_ok(tmp_path: Path, monkeypatch) -> None:
    repo = _write_ready_repo(tmp_path)

    monkeypatch.setattr(
        doctor,
        "_check_api_readiness",
        lambda *_args, **_kwargs: CheckResult(
            id="api_ready",
            category="runtime",
            title="API readiness endpoint is reachable",
            status="fail",
            detail="connection refused",
        ),
    )
    for name, check_id in (
        ("_check_db_connectivity", "db_connectivity"),
        ("_check_queue_connectivity", "rabbitmq_socket"),
        ("_check_queue_worker_health", "queue_worker_health"),
        ("_check_mcp_auth_readiness", "mcp_auth"),
        ("_check_compose_runtime_services", "compose_runtime"),
    ):
        monkeypatch.setattr(
            doctor,
            name,
            lambda *_args, check_id=check_id, **_kwargs: CheckResult(
                id=check_id,
                category="runtime",
                title=check_id,
                status="pass",
            ),
        )

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=True,
            probe_mode="host",
        )
    )

    assert payload["ok"] is False
    assert payload["summary"]["fail"] == 1


def test_warm_cli_resolves_internal_compose_application_url(monkeypatch) -> None:
    monkeypatch.setattr(doctor._common, "_running_in_container", lambda: True)

    resolved = doctor._resolve_base_url(
        {
            "FS_PUBLIC_BASE_URL": "https://localhost:8099",
            "TEST_RUNNER_BASE_URL": "http://fs-app:8099",
        },
        "",
    )

    assert resolved == "http://fs-app:8099"


def test_host_doctor_keeps_public_application_url(monkeypatch) -> None:
    monkeypatch.setattr(doctor._common, "_running_in_container", lambda: False)

    resolved = doctor._resolve_base_url(
        {
            "FS_PUBLIC_BASE_URL": "https://localhost:8099",
            "TEST_RUNNER_BASE_URL": "http://fs-app:8099",
        },
        "",
    )

    assert resolved == "https://localhost:8099"


def test_queue_worker_health_uses_public_readiness_evidence(monkeypatch) -> None:
    requested_urls: list[str] = []

    def _fake_http_json(url: str, *, timeout: float):
        requested_urls.append(url)
        return (
            200,
            {
                "ready": True,
                "checks": {
                    "worker_manager": {"ok": True},
                    "queue_lag": {"ok": True},
                    "queue_delivery": {"ok": True},
                    "lease_health": {"ok": True},
                    "stuck_jobs": {"ok": True},
                },
            },
            "",
        )

    monkeypatch.setattr(doctor._live, "_http_json", _fake_http_json)

    check = doctor._check_queue_worker_health("http://fs-app:8099", timeout=3)

    assert check.status == "pass"
    assert requested_urls == ["http://fs-app:8099/health/ready"]


def test_api_readiness_preserves_exact_failed_check_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor._live,
        "_http_json",
        lambda *_args, **_kwargs: (
            503,
            {
                "ready": False,
                "checks": {
                    "stuck_jobs": {
                        "ok": False,
                        "id": "stuck_jobs",
                        "title": "Stuck jobs",
                        "status": "fail",
                        "detail": "1 persisted stuck job requires intervention.",
                        "remediation": "Recover, cancel, or acknowledge the stuck job.",
                    }
                },
            },
            "",
        ),
    )

    check = doctor._check_api_readiness("http://fs-app:8099", timeout=3)

    assert check.id == "stuck_jobs"
    assert check.status == "fail"
    assert check.detail == "1 persisted stuck job requires intervention."
    assert check.remediation == "Recover, cancel, or acknowledge the stuck job."


def test_candidate_readiness_accepts_only_expected_lifecycle_mutation(monkeypatch) -> None:
    payload = {
        "ready": False,
        "checks": {
            "postgres": {"ok": True},
            "worker_manager": {"ok": True, "state": "lifecycle_paused"},
            "queue_lag": {"ok": True},
            "queue_delivery": {"ok": True},
            "lease_health": {"ok": True},
            "stuck_jobs": {"ok": True},
            "lifecycle_recovery": {"ok": False, "state": "mutation_in_progress"},
        },
    }
    monkeypatch.setattr(
        doctor._live,
        "_http_json",
        lambda *_args, **_kwargs: (503, payload, "HTTP 503"),
    )

    ordinary = doctor._check_api_readiness("http://fs-app:8099", timeout=3)
    candidate = doctor._check_api_readiness(
        "http://fs-app:8099",
        timeout=3,
        allow_lifecycle_mutation=True,
    )
    queue = doctor._check_queue_worker_health(
        "http://fs-app:8099",
        timeout=3,
        allow_lifecycle_mutation=True,
    )

    assert ordinary.status == "fail"
    assert candidate.status == "pass"
    assert queue.status == "pass"

    payload["checks"]["lifecycle_recovery"] = {
        "ok": False,
        "state": "recovery_required",
    }
    assert (
        doctor._check_api_readiness(
            "http://fs-app:8099",
            timeout=3,
            allow_lifecycle_mutation=True,
        ).status
        == "fail"
    )


def test_release_readiness_accepts_only_exact_catalog_transition(monkeypatch) -> None:
    stale_error = "Workflow knowledge index is stale for the requested account/project scope."
    payload = {
        "ready": False,
        "checks": {
            "postgres": {"ok": True},
            "worker_manager": {"ok": True, "state": "lifecycle_paused"},
            "queue_lag": {"ok": True},
            "queue_delivery": {"ok": True},
            "lease_health": {"ok": True},
            "stuck_jobs": {"ok": True},
            "memory_backends": {
                "ok": False,
                "error": stale_error,
                "workflow_knowledge": {
                    "error": stale_error,
                    "graph": {"available": True},
                    "vector": {"available": True},
                    "scope": {
                        "freshness": "stale",
                        "empty": False,
                        "digest_fresh": False,
                        "catalog_digest": "candidate-digest",
                        "stored_digest": "baseline-digest",
                    },
                },
            },
            "workflow_knowledge_sync": {
                "ok": False,
                "detail": "workflow knowledge sync is stale by 900s (limit 480s)",
                "last_failure_reason": "",
                "consecutive_failures": 0,
            },
            "lifecycle_recovery": {"ok": False, "state": "mutation_in_progress"},
        },
    }
    monkeypatch.setattr(
        doctor._live,
        "_http_json",
        lambda *_args, **_kwargs: (503, payload, "HTTP 503"),
    )

    ordinary = doctor._check_api_readiness("http://fs-app:8099", timeout=3)
    release = doctor._check_api_readiness(
        "http://fs-app:8099",
        timeout=3,
        allow_release_transition=True,
    )
    queue = doctor._check_queue_worker_health(
        "http://fs-app:8099",
        timeout=3,
        allow_release_transition=True,
    )

    assert ordinary.status == "fail"
    assert release.status == "pass"
    assert queue.status == "pass"

    payload["checks"]["queue_lag"] = {"ok": False, "detail": "queue lagged"}
    assert (
        doctor._check_api_readiness(
            "http://fs-app:8099",
            timeout=3,
            allow_release_transition=True,
        ).status
        == "fail"
    )

    payload["checks"]["queue_lag"] = {"ok": True}
    payload["checks"]["stuck_jobs"] = {"ok": True}
    sync = payload["checks"].pop("workflow_knowledge_sync")
    assert (
        doctor._check_api_readiness(
            "http://fs-app:8099",
            timeout=3,
            allow_release_transition=True,
        ).status
        == "fail"
    )

    payload["checks"]["workflow_knowledge_sync"] = sync
    payload["checks"]["postgres"] = {"ok": "false"}
    assert (
        doctor._check_api_readiness(
            "http://fs-app:8099",
            timeout=3,
            allow_release_transition=True,
        ).status
        == "fail"
    )
    assert (
        doctor._check_api_readiness(
            "http://fs-app:8099",
            timeout=3,
            allow_lifecycle_mutation=True,
            allow_release_transition=True,
        ).status
        == "fail"
    )

    payload["checks"]["postgres"] = {"ok": True}
    sync["consecutive_failures"] = "0"
    assert (
        doctor._check_api_readiness(
            "http://fs-app:8099",
            timeout=3,
            allow_release_transition=True,
        ).status
        == "fail"
    )

    payload["checks"]["lifecycle_recovery"] = {
        "ok": False,
        "state": "mutation_in_progress",
    }
    payload["checks"]["stuck_jobs"] = {"ok": False}
    assert (
        doctor._check_api_readiness(
            "http://fs-app:8099",
            timeout=3,
            allow_lifecycle_mutation=True,
        ).status
        == "fail"
    )


def test_mcp_readiness_fails_closed_when_auth_is_disabled_in_production(monkeypatch) -> None:
    def _unexpected_http(*_args, **_kwargs):
        raise AssertionError("production auth-disabled readiness must fail before probing MCP")

    monkeypatch.setattr(doctor._live, "_http_json", _unexpected_http)

    check = doctor._check_mcp_auth_readiness(
        "http://fs-mcp:8100/mcp",
        bearer_token="",
        auth_disabled=True,
        stack_environment="production",
        timeout=3,
    )

    assert check.status == "fail"
    assert "disabled in production" in check.detail
    assert "FS_MCP_AUTH_DISABLED=0" in check.remediation


def test_mcp_readiness_allows_explicit_local_development_auth_override(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor._live,
        "_http_json",
        lambda *_args, **_kwargs: (200, {"jsonrpc": "2.0", "id": 1}, ""),
    )

    check = doctor._check_mcp_auth_readiness(
        "http://fs-mcp:8100/mcp",
        bearer_token="",
        auth_disabled=True,
        stack_environment="local_development",
        timeout=3,
    )

    assert check.status == "pass"


@pytest.mark.parametrize("running_in_container", [False, True])
def test_deployment_doctor_delegates_live_checks_to_compose_by_default(
    tmp_path: Path, monkeypatch, running_in_container: bool
) -> None:
    repo = _write_ready_repo(tmp_path)

    monkeypatch.setattr(doctor, "_running_in_container", lambda: running_in_container)
    monkeypatch.setattr(
        doctor,
        "_check_compose_runtime_services",
        lambda *_args, **_kwargs: CheckResult(
            id="compose_runtime",
            category="runtime",
            title="Required Compose services are running",
            status="pass",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "_run_compose_live_probe",
        lambda *_args, **_kwargs: [
            CheckResult(
                id="live_probe_boundary",
                category="runtime",
                title="Live probes run from expected boundary",
                status="pass",
                detail="docker compose exec fs-app",
            )
        ],
    )

    payload = run_doctor(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            skip_git=True,
        )
    )

    checks = {check["id"]: check for check in payload["checks"]}
    assert payload["ok"] is True
    assert checks["compose_runtime"]["status"] == "pass"
    assert checks["live_probe_boundary"]["detail"] == "docker compose exec fs-app"


def test_compose_live_probe_executes_inside_fs_app(tmp_path: Path, monkeypatch) -> None:
    repo = _write_ready_repo(tmp_path)
    captured_command: list[str] = []
    captured_env: dict[str, str] = {}

    def _fake_run(command, **kwargs):
        nonlocal captured_command, captured_env
        captured_command = list(command)
        captured_env = dict(kwargs.get("env") or {})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "checks": [
                        {
                            "id": "live_probe_boundary",
                            "category": "runtime",
                            "title": "Live probes run from expected boundary",
                            "status": "pass",
                            "detail": "container process",
                        },
                        {
                            "id": "api_ready",
                            "category": "runtime",
                            "title": "API readiness endpoint is reachable",
                            "status": "pass",
                        },
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        doctor.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None
    )
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run)

    checks = doctor._run_compose_live_probe(
        options=DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            bearer_token="token-123",
            timeout_seconds=3,
            stack_environment="local_development",
            allow_lifecycle_mutation=True,
            allow_release_transition=True,
        ),
        repo_root=repo,
        env_path=repo / ".env.prod",
        env={"PORT": "8099", "MCP_URL": "http://fs-mcp:8100/mcp"},
    )

    command = captured_command
    assert command[:4] == ["docker", "compose", "-p", "flow-steward"]
    assert "exec" in command
    assert "-T" in command
    assert "fs-app" in command
    assert "MCP_BEARER_TOKEN" in command
    assert "token-123" not in command
    assert "token-123" not in " ".join(command)
    assert captured_env["MCP_BEARER_TOKEN"] == "token-123"
    assert "--live-only" in command
    assert "--probe-mode" in command
    assert "host" in command
    assert "--allow-lifecycle-mutation" in command
    assert "--allow-release-transition" in command
    stack_environment_index = command.index("--stack-environment")
    assert command[stack_environment_index + 1] == "local_development"
    assert checks[0].id == "live_probe_boundary"
    assert checks[0].status == "pass"
    assert [check.id for check in checks].count("live_probe_boundary") == 1
    assert checks[1].id == "api_ready"


def test_docker_compose_command_uses_installer_project_name(tmp_path: Path, monkeypatch) -> None:
    repo = _write_ready_repo(tmp_path)

    monkeypatch.delenv("FS_COMPOSE_PROJECT_NAME", raising=False)
    command = doctor._docker_compose_command(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            compose_files=(repo / "infra/docker/docker-compose.prod.yaml",),
        ),
        repo,
        repo / ".env.prod",
        ["ps", "--format", "json"],
    )

    assert command[:4] == ["docker", "compose", "-p", "flow-steward"]

    monkeypatch.setenv("FS_COMPOSE_PROJECT_NAME", "custom-flow")
    command = doctor._docker_compose_command(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            compose_files=(repo / "infra/docker/docker-compose.prod.yaml",),
        ),
        repo,
        repo / ".env.prod",
        ["ps", "--format", "json"],
    )

    assert command[:4] == ["docker", "compose", "-p", "custom-flow"]

    command = doctor._docker_compose_command(
        DoctorOptions(
            repo_root=repo,
            env_file=repo / ".env.prod",
            compose_files=(repo / "infra/docker/docker-compose.prod.yaml",),
            compose_project="explicit-installed-project",
        ),
        repo,
        repo / ".env.prod",
        ["ps", "--format", "json"],
    )

    assert command[:4] == ["docker", "compose", "-p", "explicit-installed-project"]


def _write_ready_repo(root: Path) -> Path:
    for rel in (
        "infra/docker/docker-compose.prod.yaml",
        "infra/docker/Dockerfile",
        "infra/mcp/Dockerfile",
        ".env.example.prod",
        "db/schema/001_schema_part_01.sql",
        "db/seeds/registry_seed.json",
        "core/entrypoints/cli/scripts/lint.sh",
        "core/entrypoints/cli/scripts/check_no_enterprise_leak.sh",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder\n", encoding="utf-8")
    (root / "infra/docker/docker-compose.prod.yaml").write_text(_ready_compose(), encoding="utf-8")
    (root / ".env.prod").write_text(_ready_env(), encoding="utf-8")
    return root


def _ready_env(
    *,
    fs_secret_key: str = "prod-secret-key-32-characters-long",
    openai_api_key: str = "sk-test-safe-placeholderless-value",
    auth_cookie_secure: str = "1",
    include_image_pins: bool = True,
) -> str:
    values = {
        "APP_PORT": "8099",
        "FS_DB_DSN": "dbname=flow_steward user=flow_steward password=prodpass host=pg-fs port=5432",
        "LANGGRAPH_POSTGRES_URI": "postgresql://flow_steward:prodpass@pg-fs:5432/flow_steward",
        "RABBITMQ_URL": "amqp://flow_steward:prodpass@rabbitmq-fs:5672/",
        "MCP_URL": "http://fs-mcp:8100/mcp",
        "ARTIFACT_STORAGE_BACKEND": "s3",
        "ARTIFACT_S3_BUCKET": "flow-steward-artifacts",
        "ARTIFACT_S3_REGION": "us-east-1",
        "ARTIFACT_S3_USE_SSL": "true",
        "FS_SECRET_KEY": fs_secret_key,
        "FS_METRICS_TOKEN": "metrics-token-32-characters-long",
        "ENCRYPTION_KEY": "a" * 64,
        "FS_BACKUP_ENCRYPTION_KEY": "b" * 64,
        # Production refuses structured-output agent steps without a real salt.
        "SECURITY_PII_SALT": "c" * 64,
        "FS_PUBLIC_BASE_URL": "https://ai.example.test",
        "FS_AUTH_TRUSTED_ORIGINS": "https://ai.example.test",
        "FS_LOCAL_AUTH_ENABLED": "1",
        "FS_AUTH_COOKIE_SECURE": auth_cookie_secure,
        "FS_AUTH_BOOTSTRAP_ENABLED": "0",
        "POSTGRES_PASSWORD": "prodpass",
        "RABBITMQ_PASS": "prodpass",
        "FS_ALLOWED_ATTACHMENT_MIMES": "text/plain",
        "FS_ALLOWED_ATTACHMENT_EXTENSIONS": ".txt",
        "FS_EXTENSION_PAYLOAD_REDACT_KEYS": "password,token",
        "FS_EXTENSION_PAYLOAD_REDACT_EXACT_KEYS": "db_dsn,credentials",
        "FS_EXTENSION_PAYLOAD_REDACT_KEY_FRAGMENTS": "secret,token",
        "OPENAI_API_KEY": openai_api_key,
    }
    if include_image_pins:
        values.update(
            {
                "FS_IMAGE": "flow-steward:0.1.0",
                "MCP_IMAGE": "flow-steward-mcp:0.1.0",
                "TRAEFIK_IMAGE": "traefik:v3.3",
                "POSTGRES_IMAGE": "fs-postgres-extended:pg17",
                "RABBITMQ_IMAGE": "rabbitmq:3.13-management",
            }
        )
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def _ready_compose() -> str:
    service_sections = "\n".join(
        f'  {service}:\n    image: {service}:test\n    expose:\n      - "1"'
        for service in (
            "traefik",
            "rabbitmq-fs",
            "pg-fs",
            "memory-postgres-extensions",
            "fs-mcp",
            "fs-app",
            "fs-worker",
            "control-plane",
        )
    )
    return f"services:\n{service_sections}\n"


def test_compose_probe_invokes_a_command_the_packaged_container_actually_has() -> None:
    """RS5-007 regression: the compose probe could never run.

    Compose mode shells into fs-app to run the live probes from inside the
    deployment. It invoked "core.entrypoints.cli tooling ops deployment-doctor",
    but the packaged CLI exposes no "tooling" command, so every compose probe
    died on a Click usage error ("No such command 'tooling'"). The doctor then
    reported live_probe_boundary as failed and never probed anything — the mode
    that is supposed to tell an operator the truth about a deployment could not
    produce a single real result.
    """
    from core.application.tooling.ops import deployment_doctor_live as live

    captured: dict[str, list[str]] = {}

    def _fake_compose_command(_options, _repo_root, _env_path, prefix):
        captured["prefix"] = list(prefix)
        return ["docker", "compose", *prefix]

    original = live._docker_compose_command
    live._docker_compose_command = _fake_compose_command
    try:
        options = DoctorOptions(
            repo_root=Path("."),
            env_file=Path(".env"),
            probe_mode="compose",
            compose_service="fs-app",
        )
        command = live._compose_probe_command(options, Path("."), Path(".env"))
    except AttributeError:
        # No isolated builder to call; assert on the module source instead.
        source = Path(live.__file__).read_text(encoding="utf-8")
        assert '"tooling",' not in source, (
            "compose probe still shells through a missing CLI command"
        )
        assert "core.entrypoints.cli.ops.deployment_doctor" in source
        return
    finally:
        live._docker_compose_command = original

    assert "tooling" not in command
    assert "core.entrypoints.cli.ops.deployment_doctor" in command


def test_env_duplicate_keys_fail_when_the_values_conflict(tmp_path: Path) -> None:
    """A losing line that reads like a hardened setting is worse than untidy.

    Every reader takes the last assignment, so an operator reading top-down can
    see SECURITY_SCAN_MODE=strict while the deployment actually runs the value
    from a later line. Nothing flagged this before.
    """
    from core.application.tooling.ops.deployment_doctor import _check_env_duplicate_keys

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment = ignored",
                "SECURITY_SCAN_MODE=strict",
                "APP_PORT=8099",
                "",
                "SECURITY_SCAN_MODE=off",
                "APP_PORT=9099",
                "UNIQUE_KEY=value",
            ]
        ),
        encoding="utf-8",
    )

    result = _check_env_duplicate_keys(env_file)

    assert result.status == "fail"
    assert "SECURITY_SCAN_MODE" in result.detail
    assert "APP_PORT" in result.detail
    assert "UNIQUE_KEY" not in result.detail
    # Line numbers and the winner must be reported so the operator can act.
    assert "lines 2, 5" in result.detail
    assert "line 5 wins" in result.detail
    # Values must never be printed — the file holds secrets.
    assert "strict" not in result.detail
    assert "off" not in result.detail


def test_env_duplicate_keys_only_warn_when_the_repeats_agree(tmp_path: Path) -> None:
    from core.application.tooling.ops.deployment_doctor import _check_env_duplicate_keys

    env_file = tmp_path / ".env"
    env_file.write_text("APP_PORT=8099\nAPP_PORT=8099\n", encoding="utf-8")

    result = _check_env_duplicate_keys(env_file)

    assert result.status == "warn", "identical repeats are untidy, not deceptive"
    assert "APP_PORT" in result.detail


def test_env_duplicate_keys_pass_on_a_clean_file(tmp_path: Path) -> None:
    from core.application.tooling.ops.deployment_doctor import _check_env_duplicate_keys

    env_file = tmp_path / ".env"
    env_file.write_text("export APP_PORT=8099\nOTHER=1\n# APP_PORT=1\n", encoding="utf-8")

    result = _check_env_duplicate_keys(env_file)

    assert result.status == "pass"


def test_shipped_env_templates_have_no_duplicate_keys() -> None:
    """The templates operators copy must not teach the problem."""
    from core.application.tooling.ops.deployment_doctor import _check_env_duplicate_keys

    templates = sorted(Path(".").glob(".env.example*"))
    assert templates, "no shipped env templates found"
    for template in templates:
        result = _check_env_duplicate_keys(template)
        assert result.status == "pass", f"{template}: {result.detail}"


class TestMcpBearerTokenResolutionRS5036:
    """The MCP probe must find the token a real deployment actually has.

    The shipped production template declares FS_WORKER_MCP_BEARER_TOKEN and
    nothing else in this family; Compose maps it to MCP_BEARER_TOKEN for the
    worker service alone. Compose-mode probing runs in the app container, which
    therefore has neither MCP_BEARER_TOKEN nor FS_SESSION_TOKEN, so mcp_auth
    reported "no bearer token was provided" on correctly configured
    deployments and passed only when the operator's shell happened to export
    the value.
    """

    def test_worker_token_from_the_env_file_is_accepted(self, monkeypatch) -> None:
        monkeypatch.setattr(doctor._common._runtime_env, "get", lambda *_a, **_k: "")
        env = {"FS_WORKER_MCP_BEARER_TOKEN": "fs_svc_worker_token"}
        assert doctor._resolve_bearer_token(env, "") == "fs_svc_worker_token"

    def test_explicit_flag_still_wins(self, monkeypatch) -> None:
        monkeypatch.setattr(doctor._common._runtime_env, "get", lambda *_a, **_k: "")
        env = {
            "MCP_BEARER_TOKEN": "from_env_file",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker_token",
        }
        assert doctor._resolve_bearer_token(env, "explicit") == "explicit"

    def test_mcp_bearer_token_still_takes_precedence_over_the_worker_token(
        self, monkeypatch
    ) -> None:
        """The existing precedence is unchanged; the worker key is only a fallback."""
        monkeypatch.setattr(doctor._common._runtime_env, "get", lambda *_a, **_k: "")
        env = {
            "MCP_BEARER_TOKEN": "explicit_mcp_token",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker_token",
        }
        assert doctor._resolve_bearer_token(env, "") == "explicit_mcp_token"

    def test_no_token_anywhere_still_resolves_to_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(doctor._common._runtime_env, "get", lambda *_a, **_k: "")
        assert doctor._resolve_bearer_token({}, "") == ""
