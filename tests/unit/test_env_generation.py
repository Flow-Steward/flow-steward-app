from __future__ import annotations

from pathlib import Path

from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

_LIFECYCLE_SECRET_LINES = (
    "FS_SECRET_KEY=" + "a" * 64,
    "ENCRYPTION_KEY=" + "b" * 64,
    "FS_BACKUP_ENCRYPTION_KEY=" + "c" * 64,
    "POSTGRES_PASSWORD=" + "d" * 64,
    "RABBITMQ_PASS=" + "e" * 64,
    "FS_METRICS_TOKEN=" + "f" * 64,
    "SECURITY_PII_SALT=" + "g" * 64,
    "ARTIFACT_S3_ACCESS_KEY=" + "i" * 64,
    "ARTIFACT_S3_SECRET_KEY=" + "j" * 64,
)


def _write_lifecycle_secrets(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "# BEGIN FLOW STEWARD INSTALLER MANAGED",
                *_LIFECYCLE_SECRET_LINES,
                "# END FLOW STEWARD INSTALLER MANAGED",
                "",
            )
        ),
        encoding="utf-8",
    )


def test_production_postgres_external_s3_env_is_minimal(tmp_path: Path) -> None:
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment="production",
        file_storage="external_s3",
        s3_endpoint="https://s3.example.test",
    )

    service.write_active_configuration(config)

    text = (tmp_path / ".env.prod").read_text(encoding="utf-8")
    assert "ENVIRONMENT=production" in text
    env = service.store.read_env_values(tmp_path / ".env.prod")
    assert len(env["ENCRYPTION_KEY"]) == 64
    assert "ARTIFACT_STORAGE_BACKEND=s3" in text
    assert "ARTIFACT_S3_BUCKET=flow-steward-artifacts" in text
    assert "ARTIFACT_S3_REGION=us-east-1" in text
    assert "ARTIFACT_S3_USE_SSL=true" in text
    assert "SONAR_" not in text
    assert "E2E_ADMIN_USERNAME" not in text
    assert "SECURITY_SCAN_MODE" not in text
    assert "http://seaweedfs:8333" not in text


def test_development_postgres_bundled_env_uses_bundled_storage_defaults(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    config = DeploymentConfig(environment="development", file_storage="bundled")

    service.write_environment(config)

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ENVIRONMENT=development" in text
    env = service.store.read_env_values(tmp_path / ".env")
    assert len(env["ENCRYPTION_KEY"]) == 64
    assert "ARTIFACT_S3_ENDPOINT_URL=http://seaweedfs:8333" in text
    assert "ARTIFACT_S3_USE_SSL=false" in text
    assert env["ARTIFACT_S3_ACCESS_KEY"]
    assert env["ARTIFACT_S3_SECRET_KEY"]


def test_switching_from_bundled_to_external_s3_clears_bundled_defaults(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    service.write_environment(DeploymentConfig(environment="development", file_storage="bundled"))
    service.write_environment(
        DeploymentConfig(
            environment="development",
            file_storage="external_s3",
            s3_endpoint="https://s3.example.test",
        )
    )

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ARTIFACT_S3_ENDPOINT_URL=http://seaweedfs:8333" not in text
    assert "ARTIFACT_S3_ENDPOINT_URL=https://s3.example.test" in text
    assert "ARTIFACT_S3_USE_SSL=true" in text
    assert "ARTIFACT_S3_USE_SSL=false" not in text


def test_development_external_s3_preserves_explicit_http_endpoint_ssl_override(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# BEGIN FLOW STEWARD INSTALLER MANAGED",
                *_LIFECYCLE_SECRET_LINES,
                "ARTIFACT_S3_ENDPOINT_URL=http://minio:9000",
                "ARTIFACT_S3_USE_SSL=false",
                "# END FLOW STEWARD INSTALLER MANAGED",
                "",
            ]
        ),
        encoding="utf-8",
    )

    service.write_environment(
        DeploymentConfig(
            environment="development",
            file_storage="external_s3",
            s3_endpoint="http://minio:9000",
            allow_insecure_s3_http=True,
        )
    )

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ARTIFACT_S3_ENDPOINT_URL=http://minio:9000" in text
    assert "ARTIFACT_S3_USE_SSL=false" in text


def test_development_env_rewrites_stack_critical_defaults(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# BEGIN FLOW STEWARD INSTALLER MANAGED",
                *_LIFECYCLE_SECRET_LINES,
                "ENVIRONMENT=production",
                "FS_STACK_ENVIRONMENT=production",
                "FS_AUTH_COOKIE_SECURE=1",
                "# END FLOW STEWARD INSTALLER MANAGED",
                "",
            ]
        ),
        encoding="utf-8",
    )

    service.write_environment(DeploymentConfig(environment="development"))

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ENVIRONMENT=development" in text
    assert "FS_STACK_ENVIRONMENT=development" in text
    assert "FS_AUTH_COOKIE_SECURE=0" in text
    assert "FS_AUTH_COOKIE_SECURE=1" not in text
    assert "local_development" not in text


def test_development_without_quality_tools_omits_quality_group(tmp_path: Path) -> None:
    service = _service(tmp_path)

    service.write_environment(DeploymentConfig(environment="development"))

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SONAR_" not in text


def test_development_with_quality_tools_includes_quality_group(tmp_path: Path) -> None:
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment="development",
        development_tools=("sonarqube",),
    )

    service.write_environment(config)

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SONAR_IMAGE=sonarqube:community" in text
    assert "SONAR_HOST_URL=http://sonarqube:9000" in text


def test_development_with_test_and_audit_tools_includes_selected_groups(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment="development",
        development_tools=("test_runner", "audit_tools"),
    )

    service.write_environment(config)

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "E2E_ADMIN_USERNAME=e2e_admin@example.com" in text
    assert "TEST_RUNNER_BASE_URL=http://fs-app:8099" in text
    assert "SECURITY_SCAN_MODE=monitor" in text
    assert "SECURITY_PII_SALT=" in text


def _service(tmp_path: Path) -> ConfigurationService:
    _write_lifecycle_secrets(tmp_path / ".env")
    _write_lifecycle_secrets(tmp_path / ".env.prod")
    store = LifecycleConfigurationStore(tmp_path)
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    return ConfigurationService(store=store)
