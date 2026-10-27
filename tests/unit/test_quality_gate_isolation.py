from __future__ import annotations

from pathlib import Path

import yaml


def test_audit_runner_uses_disposable_infrastructure_namespaces() -> None:
    compose = yaml.safe_load(Path("infra/docker/docker-compose.yaml").read_text())
    services = compose["services"]

    audit_db = services["pg-audit"]
    assert audit_db["environment"]["POSTGRES_DB"] == "flow_steward_audit"
    assert "/var/lib/postgresql/data" in audit_db["tmpfs"]
    assert "ports" not in audit_db

    audit_broker = services["rabbitmq-audit"]
    assert audit_broker["environment"]["RABBITMQ_DEFAULT_VHOST"] == "flow_steward_audit"
    assert "/var/lib/rabbitmq" in audit_broker["tmpfs"]
    assert audit_broker["healthcheck"]["test"] == [
        "CMD",
        "gosu",
        "rabbitmq",
        "rabbitmq-diagnostics",
        "-q",
        "ping",
    ]
    assert "ports" not in audit_broker

    audit = services["fs-audit"]
    environment = audit["environment"]
    assert environment["FS_AUDIT_ISOLATED"] == "1"
    assert environment["FS_STACK_ENVIRONMENT"] == "development"
    assert environment["FS_STACK_ENV_FILE"] == "/dev/null"
    assert environment["FS_AUTH_ALLOW_DISABLED"] == "1"
    assert "@pg-audit:5432/flow_steward_audit" in environment["DATABASE_URL"]
    assert "@rabbitmq-audit:5672/flow_steward_audit" in environment["RABBITMQ_URL"]
    assert environment["ARTIFACT_STORAGE_BACKEND"] == "local_fs"
    assert environment["ARTIFACT_KEY_PREFIX"] == "audit/"
    assert environment["ARTIFACT_LOCAL_ROOT_DIR"] == "/tmp/flow-steward-audit-artifacts"


def test_quality_gate_starts_cleans_and_guards_isolated_infrastructure() -> None:
    script = Path("core/entrypoints/cli/scripts/lint.sh").read_text()

    assert "fs_compose up -d --wait pg-audit rabbitmq-audit" in script
    assert "python -m core.application.tooling.db.init_db" in script
    assert "fs_compose rm -s -f pg-audit rabbitmq-audit" in script
    assert "assert_audit_test_isolation" in script
    assert "assert_audit_test_infra_removed" in script
    # AC17-R97 added targeted-tests to this list: Tier 1 now runs in the container
    # and needs the same disposable pg-audit/rabbitmq-audit fixtures.
    assert (
        "tests|pytest|targeted-tests|runtime-heartbeat-cross-profile|security-tests|security-regressions"
        in script
    )
    assert "FS_AUDIT_ISOLATED:-0" in script
    assert "flow_steward_audit" in script
    assert "pg-audit" in script
    assert "rabbitmq-audit" in script

    start_index = script.index("fs_compose up -d --wait pg-audit rabbitmq-audit")
    seed_index = script.index("python -m core.application.tooling.db.init_db")
    test_index = script.index(
        'fs_compose run --no-deps --rm -T "${lint_container_env_args[@]}"', seed_index
    )
    assert start_index < seed_index < test_index


def test_quality_gate_guards_empty_requested_checks_for_bash_nounset() -> None:
    script = Path("core/entrypoints/cli/scripts/lint.sh").read_text()

    guarded_loop = """if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
      for requested_check in "${REQUESTED_CHECKS[@]}"; do
        case "${requested_check}" in"""
    assert guarded_loop in script
