"""Every name `flow-steward config set NAME=VALUE` accepts, typed and host settings.

Kept apart from operator_settings.py because it loads the `config set` command
specification, which the runtime roles (app, worker, control) never need.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from typing import TYPE_CHECKING

from core.application.lifecycle.operator_settings import (
    HOST_SETTINGS_DOCS_URL,
    OperatorSettingError,
    operator_setting_specs,
)

if TYPE_CHECKING:
    from core.application.management.command_specs.contracts import FieldSpec

#: Patch fields `config set` does not take by name: the internal carrier of host
#: settings, and the placement only `./install.sh --deployment-profile` writes.
_NOT_TYPED_BY_NAME = frozenset({"operator_settings", "data_placement"})


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


__all__ = [
    "check_operator_settings",
    "settable_names",
    "typed_setting_fields",
    "unknown_setting_error",
]
