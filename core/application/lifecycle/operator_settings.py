"""The settings an operator changes, by one name used everywhere.

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

import difflib
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application.management.command_specs.contracts import FieldSpec
    from core.infrastructure.config.env_registry import EnvVarSpec

#: The public page listing every setting; generated from the same source as below.
HOST_SETTINGS_DOCS_URL = "https://docs.flowsteward.org/docs/reference/host-settings"

_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: Patch fields `config set` does not take by name: the internal carrier of host
#: settings, and the placement only `./install.sh --deployment-profile` writes.
_NOT_TYPED_BY_NAME = frozenset({"operator_settings", "data_placement"})


class OperatorSettingError(ValueError):
    """A setting the operator asked for that cannot be set."""


def operator_setting_specs() -> dict[str, EnvVarSpec]:
    """The host settings: the manifest entries flagged settable, by name."""
    # Imported here: the deployment contracts use this module, and they load early.
    from core.infrastructure.config.env_registry import env_specs

    return {spec.name: spec for spec in env_specs() if spec.operator_settable and not spec.secret}


def typed_setting_fields() -> dict[str, FieldSpec]:
    """The typed settings: the `config set` fields, by the name `config show` prints."""
    from core.application.management.command_specs.config import CONFIG_COMMAND_SPECS

    spec = next(item for item in CONFIG_COMMAND_SPECS if item.path == ("config", "set"))
    return {
        field.key: field
        for field in spec.fields
        if not field.positional and field.key not in _NOT_TYPED_BY_NAME
    }


def settable_names() -> tuple[str, ...]:
    """Every name `config set NAME=VALUE` accepts, typed first."""
    return (*typed_setting_fields(), *sorted(operator_setting_specs()))


def parse_operator_settings(raw: object) -> dict[str, str]:
    """Host-setting ``NAME=VALUE`` pairs (comma-separated text, or a mapping) as a dict.

    An empty value resets the setting to its default. Only the shape is checked here;
    :func:`check_operator_settings` checks names and values against the manifest.
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


def unknown_setting_error(name: str) -> OperatorSettingError:
    """A name `config set` does not take: the nearest one it does, and where the list is."""
    close = difflib.get_close_matches(name, settable_names(), n=1)
    hint = f" Did you mean {close[0]}?" if close else ""
    return OperatorSettingError(
        f"{name} is not a setting you can change.{hint} Settings: {HOST_SETTINGS_DOCS_URL}"
    )


def check_operator_settings(settings: Mapping[str, str]) -> dict[str, str]:
    """Refuse a host setting the manifest does not list, or a value it does not accept."""
    specs = operator_setting_specs()
    for name, value in settings.items():
        spec = specs.get(name)
        if spec is None:
            raise unknown_setting_error(name)
        if value and spec.allowed_values and value not in spec.allowed_values:
            raise OperatorSettingError(
                f"{name} accepts {', '.join(spec.allowed_values)}, not {value!r}. "
                f"Settings: {HOST_SETTINGS_DOCS_URL}"
            )
    return dict(settings)


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
    "check_operator_settings",
    "merge_operator_settings",
    "operator_setting_command",
    "operator_setting_specs",
    "parse_operator_settings",
    "settable_names",
    "split_assignments",
    "typed_setting_fields",
    "unknown_setting_error",
]
