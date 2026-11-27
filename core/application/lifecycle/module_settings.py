"""Platform-module settings the installation operator sets (ADR 0082, module settings).

A module setting is a host setting that a platform module declares instead of the
environment manifest: its name starts with ``FS_MODULE_`` and it is accepted only
while an active module declares it. It is stored beside the manifest's host settings
(``DeploymentConfig.operator_settings``), so ``config set``, ``config apply``,
``./install.sh --setting``, rollback and ``config show`` treat both alike; the only
differences are where the name is checked (here, against the active module set) and
how the value reaches the roles (one carrier variable, see
``core.infrastructure.platform_modules.module_settings``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from core.application.lifecycle.operator_settings import (
    HOST_SETTINGS_DOCS_URL,
    OperatorSettingError,
)
from core.infrastructure.platform_modules.module_settings import (
    MODULE_SETTING_MAX_VALUE_CHARS,
    MODULE_SETTING_PREFIX,
    MODULE_SETTINGS_ENV,
    ModuleSettingDeclaration,
    encode_module_settings,
    is_module_setting_name,
)

if TYPE_CHECKING:
    from core.application.lifecycle.contracts import DeploymentConfig

#: Reads the settings the installation's active platform-module set declares.
ModuleSettingSource = Callable[["DeploymentConfig"], Sequence[ModuleSettingDeclaration]]


class ConfigModuleSetting(BaseModel):
    """One module setting as ``config show`` reports it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    module: str = ""
    description: str = ""
    value: str = ""
    default: str = ""
    allowed_values: tuple[str, ...] = ()
    #: ``set``: the operator's value; ``default``: the module's default applies;
    #: ``undeclared``: a stored value no active module declares (it reaches no role).
    state: Literal["set", "default", "undeclared"]


def module_setting_names(settings: Mapping[str, str]) -> dict[str, str]:
    """The module settings among *settings* (host settings and module settings mixed)."""
    return {name: value for name, value in settings.items() if is_module_setting_name(name)}


def without_module_settings(settings: Mapping[str, str]) -> dict[str, str]:
    """*settings* without the module settings: the manifest's host settings only."""
    return {name: value for name, value in settings.items() if not is_module_setting_name(name)}


def check_module_settings(
    changes: Mapping[str, str],
    declared: Sequence[ModuleSettingDeclaration],
    *,
    active: Mapping[str, str],
) -> dict[str, str]:
    """Refuse a module setting no active module declares, or a value it does not allow.

    A value is one line of at most ``MODULE_SETTING_MAX_VALUE_CHARS`` characters.

    An empty value resets a setting; resetting a stored value is accepted even when
    no active module declares it any more, so the operator can clear a leftover.
    """
    by_name = {declaration.name: declaration for declaration in declared}
    for name, value in changes.items():
        declaration = by_name.get(name)
        if declaration is None:
            if not value and name in active:
                continue
            raise OperatorSettingError(
                f"{name} is not a setting you can change: no active platform module "
                f"declares it. Module settings start with {MODULE_SETTING_PREFIX} and exist "
                f"only while the module that declares them is active. Settings: "
                f"{HOST_SETTINGS_DOCS_URL}"
            )
        if value and declaration.allowed_values and value not in declaration.allowed_values:
            raise OperatorSettingError(
                f"{name} ({declaration.module_id}) accepts "
                f"{', '.join(declaration.allowed_values)}, not {value!r}."
            )
        if "\n" in value or "\r" in value or "\x00" in value:
            raise OperatorSettingError(f"{name} must be one line of text.")
        if len(value) > MODULE_SETTING_MAX_VALUE_CHARS:
            raise OperatorSettingError(
                f"{name} is {len(value)} characters long; a module setting is at most "
                f"{MODULE_SETTING_MAX_VALUE_CHARS} characters."
            )
    return dict(changes)


def module_settings_environment(operator_settings: Mapping[str, str]) -> dict[str, str]:
    """The runtime variable that carries the stored module settings to the roles.

    Always rendered, empty when none is set: a value left in the file would
    otherwise keep a reset setting alive.
    """
    return {MODULE_SETTINGS_ENV: encode_module_settings(module_setting_names(operator_settings))}


def module_setting_rows(
    stored: Mapping[str, str],
    declared: Sequence[ModuleSettingDeclaration] | None,
) -> tuple[ConfigModuleSetting, ...]:
    """Every declared module setting and every stored one, for ``config show``.

    ``declared`` is ``None`` when the active module set could not be read: only the
    stored values are listed then, without their module.
    """
    values = module_setting_names(stored)
    rows: dict[str, ConfigModuleSetting] = {}
    for declaration in declared or ():
        value = values.get(declaration.name, "")
        rows[declaration.name] = ConfigModuleSetting(
            name=declaration.name,
            module=declaration.module_id,
            description=declaration.description,
            value=value or declaration.default,
            default=declaration.default,
            allowed_values=declaration.allowed_values,
            state="set" if value else "default",
        )
    for name, value in values.items():
        if name not in rows:
            rows[name] = ConfigModuleSetting(
                name=name,
                value=value,
                state="set" if declared is None else "undeclared",
            )
    return tuple(rows[name] for name in sorted(rows))


__all__ = [
    "ConfigModuleSetting",
    "ModuleSettingSource",
    "check_module_settings",
    "module_setting_names",
    "module_setting_rows",
    "module_settings_environment",
    "without_module_settings",
]
