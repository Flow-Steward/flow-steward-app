"""The settings an operator changes, by one name used everywhere.

This module is what error messages need: the page link, the command and the host
settings. The full list of names, typed ones included, is in setting_names.py, which
loads the config command specification; runtime roles do not need it.

Whatever name `config show`, an error message or the Host settings page shows,
`flow-steward config set NAME=VALUE` accepts verbatim. Two kinds share that syntax:

- typed settings: the `config set` fields, named as `config show` prints them
  (``public_host``); their ``--public-host`` options stay as synonyms;
- host settings: environment-manifest entries flagged ``operator_settable``
  (``FS_ALLOW_PRIVATE_REMOTE_URLS``). Never a secret, never an installer-managed key.

On Compact, where `config` is not available, a host setting is set with
``./install.sh --setting NAME=VALUE``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.infrastructure.config.env_registry import EnvVarSpec

#: The public page listing every setting; generated from the same source as below.
HOST_SETTINGS_DOCS_URL = "https://docs.flowsteward.org/docs/reference/host-settings"

_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class OperatorSettingError(ValueError):
    """A setting the operator asked for that cannot be set."""


def operator_setting_specs() -> dict[str, EnvVarSpec]:
    """The host settings: the manifest entries flagged settable, by name."""
    # Imported here: the deployment contracts use this module, and they load early.
    from core.infrastructure.config.env_registry import env_specs

    return {spec.name: spec for spec in env_specs() if spec.operator_settable and not spec.secret}


def parse_operator_settings(raw: object) -> dict[str, str]:
    """Host-setting ``NAME=VALUE`` pairs (comma-separated text, or a mapping) as a dict.

    An empty value resets the setting to its default. Only the shape is checked here;
    ``setting_names.check_operator_settings`` checks names and values against the manifest.
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        pairs = [(str(name).strip(), str(value).strip()) for name, value in raw.items()]
    else:
        pairs = [_pair(item) for item in str(raw).split(",") if item.strip()]
    settings: dict[str, str] = {}
    for name, value in pairs:
        if not _NAME.match(name):
            raise OperatorSettingError(f"'{name}' is not a host setting name.")
        settings[name] = value
    return settings


def split_assignments(assignments: object) -> list[tuple[str, str]]:
    """`config set` positional ``NAME=VALUE`` arguments as (name, value) pairs."""
    items = [assignments] if isinstance(assignments, str) else list(assignments or ())
    return [_pair(str(item)) for item in items if str(item).strip()]


def _pair(item: str) -> tuple[str, str]:
    name, separator, value = item.strip().partition("=")
    if not separator or not name.strip():
        raise OperatorSettingError(
            f"'{item.strip()}' is not NAME=VALUE; for example public_host=flow.example.com "
            f"or FS_ALLOW_PRIVATE_REMOTE_URLS=1. Settings: {HOST_SETTINGS_DOCS_URL}"
        )
    return name.strip(), value.strip()


def merge_operator_settings(
    active: Mapping[str, str], changes: Mapping[str, str]
) -> dict[str, str]:
    """The active host settings with the changes applied; an empty value resets one."""
    merged = dict(active)
    for name, value in changes.items():
        if value:
            merged[name] = value
        else:
            merged.pop(name, None)
    return merged


def operator_setting_command(name: str, value: str, *, deployment_profile: str) -> str:
    """The exact command that sets host setting *name* on this kind of installation."""
    if deployment_profile == "compact":
        return (
            f"run ./install.sh --setting {name}={value} from the installation folder "
            "(it keeps the installation and restarts the runtime)"
        )
    return f"run flow-steward config set {name}={value}, then flow-steward config apply"


__all__ = [
    "HOST_SETTINGS_DOCS_URL",
    "OperatorSettingError",
    "merge_operator_settings",
    "operator_setting_command",
    "operator_setting_specs",
    "parse_operator_settings",
    "split_assignments",
]
