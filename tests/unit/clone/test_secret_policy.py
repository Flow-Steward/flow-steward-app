from __future__ import annotations

from dataclasses import dataclass

from core.application.clone.secret_policy import (
    select_clone_source_configuration,
    select_clone_source_secrets,
)
from core.infrastructure.config.env_registry import (
    StackEnvSelection,
    build_installer_env,
    build_lifecycle_env,
    env_specs,
)


def test_clone_restores_broker_credentials_with_broker_state() -> None:
    selection = StackEnvSelection(environment="production", file_storage="local_fs")
    target_value = "target-generated-value"
    source = build_installer_env(
        selection=selection, existing={}, secret_factory=lambda: "source-generated-value"
    )
    target = build_installer_env(
        selection=selection, existing={}, secret_factory=lambda: target_value
    )
    imported = select_clone_source_configuration(
        source, specs=env_specs(), environment="production"
    )
    restored = build_lifecycle_env(selection=selection, existing={**target, **imported})

    # The restored broker volume authenticates the source password, while the
    # target PostgreSQL server and backup encryption remain deployment-local.
    assert restored["RABBITMQ_PASS"] == "source-generated-value"
    assert (
        restored["RABBITMQ_URL"]
        == (
            "amqp://flow_steward:source-generated-value@rabbitmq-fs:5672/"  # pragma: allowlist secret - synthetic broker fixture
        )
    )
    assert restored["POSTGRES_PASSWORD"] == target_value
    assert restored["FS_BACKUP_ENCRYPTION_KEY"] == target_value


@dataclass(frozen=True)
class _Spec:
    name: str
    secret: bool = True
    clone_source_owned: bool = True
    environments: tuple[str, ...] = ("production", "local_development")
    groups: tuple[str, ...] = ("core_runtime",)


def test_clone_configuration_policy_imports_source_values_and_retains_target_shell() -> None:
    values = {
        "FS_EXTENSION_RUNTIME_SIGNING_SECRET": "extension-signing",  # pragma: allowlist secret - synthetic fixture
        "FS_OBSERVABILITY_DSN": "https://source-observability.example/1",
        "POSTGRES_PASSWORD": "target-postgres",  # pragma: allowlist secret - synthetic fixture
        "FS_BACKUP_ENCRYPTION_KEY": "target-backup-key",
        "PUBLIC_VALUE": "not-secret",
        "FS_SESSION_TOKEN": "",
        "FS_DEFAULT_PROVIDER": "source-provider",
        "FS_ALLOWED_ATTACHMENT_MIMES": "text/plain,application/pdf",
        "ARTIFACT_S3_PUBLIC_ENDPOINT_URL": "https://cdn.source.example",
        "FS_BACKUPS_DIR": "/srv/target/backups",
        "FS_REVERSE_PROXY_BACKEND_PORT": "19099",
        "FS_CONFIGURATION_STATE_ROOT": "/run/source-state",
    }
    specs = (
        _Spec("FS_EXTENSION_RUNTIME_SIGNING_SECRET"),
        _Spec("FS_OBSERVABILITY_DSN"),
        _Spec("POSTGRES_PASSWORD", clone_source_owned=False),
        _Spec("FS_BACKUP_ENCRYPTION_KEY", clone_source_owned=False),
        _Spec("PUBLIC_VALUE", secret=False),
        _Spec("FS_SESSION_TOKEN"),
        _Spec("FS_DEFAULT_PROVIDER", secret=False, groups=("provider_defaults",)),
        _Spec(
            "FS_ALLOWED_ATTACHMENT_MIMES",
            secret=False,
            groups=("core_attachment_policy",),
        ),
        _Spec(
            "ARTIFACT_S3_PUBLIC_ENDPOINT_URL",
            secret=False,
            groups=("artifact_storage_external_s3",),
        ),
        _Spec(
            "FS_BACKUPS_DIR",
            secret=False,
            clone_source_owned=False,
            groups=("production_host_paths",),
        ),
        _Spec("FS_REVERSE_PROXY_BACKEND_PORT", secret=False, clone_source_owned=False),
        _Spec("FS_CONFIGURATION_STATE_ROOT", secret=False, clone_source_owned=False),
    )

    selected = select_clone_source_configuration(
        values,
        specs=specs,
        environment="production",
    )

    assert (
        selected
        == {
            "FS_EXTENSION_RUNTIME_SIGNING_SECRET": "extension-signing",  # pragma: allowlist secret - expected fixture
            "FS_OBSERVABILITY_DSN": "https://source-observability.example/1",
            "PUBLIC_VALUE": "not-secret",
            "FS_SESSION_TOKEN": "",
            "FS_DEFAULT_PROVIDER": "source-provider",
            "FS_ALLOWED_ATTACHMENT_MIMES": "text/plain,application/pdf",
            "ARTIFACT_S3_PUBLIC_ENDPOINT_URL": "https://cdn.source.example",
        }
    )


def test_clone_configuration_policy_does_not_materialize_missing_values() -> None:
    selected = select_clone_source_configuration(
        {"PRESENT": "source"},
        specs=(_Spec("PRESENT"), _Spec("ABSENT")),
        environment="production",
    )

    assert selected == {"PRESENT": "source"}


def test_clone_configuration_policy_retains_target_domain_identities() -> None:
    source = {
        "FS_AUTH_TRUSTED_ORIGINS": "https://source.example.test",
        "FS_MCP_ISSUER_URL": "https://source.example.test",
        "FS_MCP_RESOURCE_URL": "https://source.example.test/mcp",
        "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY": "0",
        "FS_BUILD_SHA": "source-build",
        "FS_BUILD_REVISION": "source-revision",
        "FS_DEPLOYMENT_REVISION": "source-deployment",
        "FS_DEFAULT_PROVIDER": "source-provider",
    }
    target = {
        "FS_AUTH_TRUSTED_ORIGINS": "https://target.example.test",
        "FS_MCP_ISSUER_URL": "https://target.example.test",
        "FS_MCP_RESOURCE_URL": "https://target.example.test/mcp",
        "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY": "1",
        "FS_BUILD_SHA": "target-build",
        "FS_BUILD_REVISION": "target-revision",
        "FS_DEPLOYMENT_REVISION": "target-deployment",
    }

    imported = select_clone_source_configuration(
        source,
        specs=env_specs(),
        environment="production",
    )
    restored = {**target, **imported}

    assert restored["FS_AUTH_TRUSTED_ORIGINS"] == "https://target.example.test"
    assert restored["FS_MCP_ISSUER_URL"] == "https://target.example.test"
    assert restored["FS_MCP_RESOURCE_URL"] == "https://target.example.test/mcp"
    assert restored["FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY"] == "1"
    assert restored["FS_BUILD_SHA"] == "target-build"
    assert restored["FS_BUILD_REVISION"] == "target-revision"
    assert restored["FS_DEPLOYMENT_REVISION"] == "target-deployment"
    assert restored["FS_DEFAULT_PROVIDER"] == "source-provider"


def test_legacy_secret_selector_does_not_return_non_secret_configuration() -> None:
    selected = select_clone_source_secrets(
        {"SECRET": "private", "SETTING": "source"},  # pragma: allowlist secret - selector fixture
        specs=(_Spec("SECRET"), _Spec("SETTING", secret=False)),
        environment="production",
    )

    assert selected == {"SECRET": "private"}  # pragma: allowlist secret - expected fixture
