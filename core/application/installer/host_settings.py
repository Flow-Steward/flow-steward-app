"""`./install.sh --setting NAME=VALUE`: which host settings change, and the keys to write."""

from __future__ import annotations

from core.application.installer.contracts import InstallConfig
from core.application.installer.errors import InstallerFailure


def checked_operator_settings(changes: dict[str, str]) -> dict[str, str]:
    """`./install.sh --setting` names only settings the manifest lets the operator set."""
    from core.application.lifecycle.operator_settings import OperatorSettingError
    from core.application.lifecycle.setting_names import check_operator_settings

    try:
        return check_operator_settings(changes)
    except OperatorSettingError as exc:
        raise InstallerFailure(
            code="installer_setting_invalid",
            cause=str(exc),
            remediation=(
                "./install.sh --setting changes host settings only; correct NAME=VALUE and "
                "rerun ./install.sh."
            ),
            exit_code=3,
            step="preflight",
        ) from exc


def applied_operator_settings(
    config: InstallConfig, changes: dict[str, str]
) -> tuple[InstallConfig, dict[str, str]] | None:
    """The configuration with *changes* merged, and the keys to write; None if nothing changes.

    A reset writes the manifest default: the key may already be in the file.
    """
    from core.application.lifecycle.operator_settings import (
        merge_operator_settings,
        operator_setting_specs,
    )

    merged = merge_operator_settings(config.operator_settings, changes)
    if merged == config.operator_settings:
        return None
    candidate = InstallConfig.model_validate(
        {**config.model_dump(mode="json"), "operator_settings": merged}
    )
    specs = operator_setting_specs()
    environment = "local_development" if config.environment == "development" else "production"
    return candidate, {
        name: value or specs[name].default_for_environment(environment)
        for name, value in changes.items()
    }


__all__ = ["applied_operator_settings", "checked_operator_settings"]
