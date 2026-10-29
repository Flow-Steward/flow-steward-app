"""Host settings set by name: only the manifest's settable ones, carried to the runtime."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.application.lifecycle.configuration_models import ConfigSetRequest
from core.application.lifecycle.contracts import ConfigurationPatch, DeploymentConfig
from core.application.lifecycle.operator_settings import (
    HOST_SETTINGS_DOCS_URL,
    OperatorSettingError,
    check_operator_settings,
    merge_operator_settings,
    operator_setting_command,
    operator_setting_specs,
    parse_operator_settings,
)
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from tests.unit.lifecycle.test_configuration_service import (
    _service,
    _write_lifecycle_secret_environment,
)

PRIVATE = "FS_ALLOW_PRIVATE_REMOTE_URLS"


def test_the_private_address_switch_is_the_settable_setting() -> None:
    specs = operator_setting_specs()

    assert PRIVATE in specs
    assert specs[PRIVATE].allowed_values == ("0", "1")
    assert not any(spec.secret for spec in specs.values())


def test_pairs_parse_and_an_empty_value_is_a_reset() -> None:
    assert parse_operator_settings(f"{PRIVATE}=1") == {PRIVATE: "1"}
    assert parse_operator_settings(f" {PRIVATE} = , OTHER_NAME=2") == {
        PRIVATE: "",
        "OTHER_NAME": "2",
    }
    with pytest.raises(OperatorSettingError, match="NAME=VALUE"):
        parse_operator_settings(PRIVATE)
    with pytest.raises(OperatorSettingError, match="not a host setting name"):
        parse_operator_settings("fs_allow=1")


@pytest.mark.parametrize(
    ("settings", "message"),
    (
        ({"FS_ALLOW_PRIVATE_REMOTE_URL": "1"}, f"Did you mean {PRIVATE}"),
        ({"FS_SECRET_KEY": "x"}, "not a setting you can change"),
        ({"FS_PUBLIC_HOST": "example.test"}, "not a setting you can change"),
        ({PRIVATE: "yes"}, "accepts 0, 1"),
    ),
)
def test_unknown_names_secrets_managed_keys_and_bad_values_are_refused(
    settings: dict[str, str], message: str
) -> None:
    with pytest.raises(OperatorSettingError, match=message):
        check_operator_settings(settings)


def test_config_set_checks_the_settings_and_a_patch_merges_them() -> None:
    assert ConfigSetRequest(operator_settings=f"{PRIVATE}=1").operator_settings == {PRIVATE: "1"}
    with pytest.raises(ValidationError, match="not a setting you can change"):
        ConfigSetRequest(operator_settings="FS_SECRET_KEY=x")

    assert merge_operator_settings({PRIVATE: "1", "OTHER": "2"}, {PRIVATE: ""}) == {"OTHER": "2"}
    assert merge_operator_settings({}, {PRIVATE: "1"}) == {PRIVATE: "1"}


def test_the_command_is_the_one_for_the_installation_profile() -> None:
    compact = operator_setting_command(PRIVATE, "1", deployment_profile="compact")
    scalable = operator_setting_command(PRIVATE, "1", deployment_profile="scalable")

    assert f"./install.sh --setting {PRIVATE}=1" in compact
    assert f"flow-steward config set {PRIVATE}=1" in scalable
    assert "flow-steward config apply" in scalable


def test_a_pending_setting_merges_into_the_active_ones_and_reaches_the_environment(
    tmp_path: Path,
) -> None:
    _write_lifecycle_secret_environment(tmp_path / ".env.prod")
    service = _service(tmp_path)
    active = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        operator_settings={"OTHER_SETTING": "kept"},
    )

    candidate = service._candidate(active, ConfigurationPatch(operator_settings=f"{PRIVATE}=1"))
    _target, _path, environment = service.prepare_environment(candidate)

    assert candidate.operator_settings == {"OTHER_SETTING": "kept", PRIVATE: "1"}
    assert environment[PRIVATE] == "1"
    reset = service._candidate(candidate, ConfigurationPatch(operator_settings=f"{PRIVATE}="))
    assert PRIVATE not in reset.operator_settings
    assert PRIVATE not in service.prepare_environment(reset)[2]


def test_stack_toml_carries_the_settings_and_omits_the_key_when_none_are_set(
    tmp_path: Path,
) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    store.write_active(DeploymentConfig(operator_settings={PRIVATE: "1"}))

    assert f'operator_settings = {{ {PRIVATE} = "1" }}' in store.active_path.read_text("utf-8")
    assert store.read_active().operator_settings == {PRIVATE: "1"}

    # A release before this one reads stack.toml as a closed shape: no settings, no key.
    store.write_active(DeploymentConfig())
    assert "operator_settings" not in store.active_path.read_text("utf-8")
    assert "operator_settings" not in DeploymentConfig().model_dump(mode="json")


def test_config_set_takes_typed_and_host_settings_by_the_name_shown() -> None:
    request = ConfigSetRequest.model_validate(
        {"assignments": ["public_host=flow.example.com", "public_port=443", f"{PRIVATE}=1"]}
    )

    assert request.public_host == "flow.example.com"
    assert request.public_port == 443
    assert request.operator_settings == {PRIVATE: "1"}
    # The typed option is a synonym; given both ways, the value must agree.
    agreed = ConfigSetRequest.model_validate(
        {"public_host": "flow.example.com", "assignments": ["public_host=flow.example.com"]}
    )
    assert agreed.public_host == "flow.example.com"
    with pytest.raises(ValidationError, match="given twice"):
        ConfigSetRequest.model_validate(
            {"public_host": "a.example.com", "assignments": ["public_host=b.example.com"]}
        )


@pytest.mark.parametrize(
    ("assignment", "message"),
    (
        ("public_hots=flow.example.com", "Did you mean public_host"),
        ("data_placement=compact_volume", "not a setting you can change"),
        ("FS_SECRET_KEY=x", "not a setting you can change"),
        (f"{PRIVATE}=2", "accepts 0, 1"),
        ("public_host", "is not NAME=VALUE"),
    ),
)
def test_a_name_config_set_does_not_take_is_refused_with_the_nearest_and_the_page(
    assignment: str, message: str
) -> None:
    with pytest.raises(ValidationError) as refused:
        ConfigSetRequest.model_validate({"assignments": [assignment]})

    assert message in str(refused.value)
    assert HOST_SETTINGS_DOCS_URL in str(refused.value)
