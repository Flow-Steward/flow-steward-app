from __future__ import annotations

import pytest

PLATFORM_ID = "4ba8a97c-722c-4ab7-9832-4129821ca88a"


def resolve(values, installed=None, journal=None):
    from core.application.lifecycle.runtime_installation_context import (
        resolve_runtime_installation_context,
    )

    return resolve_runtime_installation_context(
        runtime_values=values, installed_identity=installed, journal_identity=journal
    )


def test_platform_identity_survives_restart_without_generation():
    values = {"FS_INSTALLATION_ID": PLATFORM_ID, "FS_DEPLOYMENT_PROFILE": "compact"}
    first = resolve(values)
    assert first.installation_id == PLATFORM_ID
    assert resolve(values) == first


def test_compose_identity_is_not_renamed():
    context = resolve(
        {"FS_DEPLOYMENT_PROFILE": "scalable", "FS_COMPOSE_PROJECT_NAME": "my-stack"},
        "my-stack",
        "my-stack",
    )
    assert context.installation_id == "my-stack"


@pytest.mark.parametrize(
    "values,installed,journal",
    [
        ({"FS_DEPLOYMENT_PROFILE": "compact"}, None, None),
        ({"FS_INSTALLATION_ID": "not-uuid", "FS_DEPLOYMENT_PROFILE": "compact"}, None, None),
        ({"FS_INSTALLATION_ID": PLATFORM_ID}, None, None),
        ({"FS_DEPLOYMENT_PROFILE": "compact"}, "stack", "other"),
        ({"FS_INSTALLATION_ID": PLATFORM_ID, "FS_DEPLOYMENT_PROFILE": "compact"}, "stack", "stack"),
        (
            {"FS_DEPLOYMENT_PROFILE": "compact", "FS_COMPOSE_PROJECT_NAME": "other"},
            "stack",
            "stack",
        ),
    ],
)
def test_missing_or_conflicting_trusted_context_fails(values, installed, journal):
    with pytest.raises(ValueError):
        resolve(values, installed, journal)


def test_generic_expectations_preserve_unknown_count():
    result = resolve(
        {
            "FS_INSTALLATION_ID": PLATFORM_ID,
            "FS_DEPLOYMENT_PROFILE": "scalable",
            "FS_MONITORING_EXPECTATIONS": '{"image-indexer": null, "worker": 3}',
        }
    )
    assert dict(result.expectations) == {"image-indexer": None, "worker": 3}


@pytest.mark.parametrize("raw", ["[]", '{"worker":true}', '{"Bad Name":1}', '{"worker":-1}'])
def test_invalid_expected_inventory_is_rejected(raw):
    with pytest.raises(ValueError):
        resolve(
            {
                "FS_INSTALLATION_ID": PLATFORM_ID,
                "FS_DEPLOYMENT_PROFILE": "scalable",
                "FS_MONITORING_EXPECTATIONS": raw,
            }
        )


def test_monitoring_config_defaults_and_independent_retention():
    from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config

    config = load_monitoring_runtime_config({})
    assert (config.interval_seconds, config.stale_seconds) == (60, 180)
    assert not config.history_enabled
    assert config.current_retention_days == config.history_retention_days == 7
    assert config.cleanup_batch_size == 500
    assert config.queue_max_messages == 10000
    assert config.queue_max_bytes == 16777216
    assert config.operation_timeout_seconds == 2


@pytest.mark.parametrize(
    "values",
    [
        {"FS_MONITORING_INTERVAL_SECONDS": "0"},
        {"FS_MONITORING_INTERVAL_SECONDS": "180"},
        {"FS_MONITORING_HISTORY_ENABLED": "perhaps"},
        {"FS_MONITORING_CURRENT_RETENTION_DAYS": "-1"},
    ],
)
def test_invalid_monitoring_policy_fails(values):
    from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config

    with pytest.raises(ValueError):
        load_monitoring_runtime_config(values)


def test_empty_optional_compose_policy_uses_canonical_defaults():
    from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config

    assert load_monitoring_runtime_config(
        {"FS_MONITORING_INTERVAL_SECONDS": "", "FS_MONITORING_HISTORY_ENABLED": ""}
    ) == load_monitoring_runtime_config({})


def test_dependency_inventory_is_generic_and_configuration_owned():
    result = resolve(
        {
            "FS_INSTALLATION_ID": PLATFORM_ID,
            "FS_DEPLOYMENT_PROFILE": "scalable",
            "FS_MONITORING_DEPENDENCIES": '["external-ingress", "object-store"]',
        }
    )
    assert result.dependencies == ("external-ingress", "object-store")


@pytest.mark.parametrize("raw", ['{"postgres":true}', '["Bad Name"]', '["postgres","postgres"]'])
def test_dependency_inventory_rejects_invalid_or_duplicate_names(raw):
    with pytest.raises(ValueError):
        resolve(
            {
                "FS_INSTALLATION_ID": PLATFORM_ID,
                "FS_DEPLOYMENT_PROFILE": "scalable",
                "FS_MONITORING_DEPENDENCIES": raw,
            }
        )


@pytest.mark.parametrize("storage", ["bundled", "external_s3", "local_fs"])
def test_expected_dependencies_are_only_the_ones_something_checks(storage):
    """A dependency nobody probes could only ever read "not observed"."""
    import json
    from types import SimpleNamespace

    from core.application.lifecycle.runtime_installation_context import (
        deployment_monitoring_dependencies,
    )

    names = json.loads(deployment_monitoring_dependencies(SimpleNamespace(file_storage=storage)))
    assert names == ["postgres", "rabbitmq"]
