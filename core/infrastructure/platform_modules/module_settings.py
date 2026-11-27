"""Deployment-level settings a platform module declares (ADR 0082, module settings).

A module descriptor may carry ``"settings"``: the variables the installation
operator sets for that module, never an account administrator::

    "settings": [
      {"name": "FS_MODULE_ACME_LEDGER_POLICY_FILE", "description": "Policy file path."},
      {"name": "FS_MODULE_ACME_LEDGER_DEV_MODE", "description": "Development switch.",
       "default": "0", "allowed_values": ["0", "1"]}
    ]

Every name starts with :data:`MODULE_SETTING_PREFIX`, so a module can never name a
Core setting, followed by the declaring module's id in upper snake case and an
underscore (:func:`module_setting_name_prefix`: module ``acme.ledger`` declares
``FS_MODULE_ACME_LEDGER_*``, ``acme.sign-in`` declares ``FS_MODULE_ACME_SIGN_IN_*``),
so a module names only settings of its own. Two modules of one set may still not
declare the same name, may not have ids with the same upper-snake prefix, and a
module may not declare a name that starts with another module's longer prefix
(:func:`module_set_setting_conflicts`). ``secret`` may be given only as ``false``: this first version
carries no secrets. A value is one line of at most :data:`MODULE_SETTING_MAX_VALUE_CHARS`
characters.

The operator sets a declared value with ``flow-steward config set NAME=VALUE`` (or
``./install.sh --setting NAME=VALUE`` on Compact). The installation stores it beside
the other host settings and renders every module value into one runtime variable,
:data:`MODULE_SETTINGS_ENV` (a JSON object of name to value), which Compose passes to
the app, worker and control roles on both profiles and the Compact supervisor passes
to the same three child processes. The carrier is ``base64:`` followed by the
base64 of that JSON, so the env file holds no quote or comment character that Compose's
dotenv reader or a shell could read differently. Each role then publishes the settings its loaded
modules declare as ordinary process variables
(:func:`publish_module_settings`): the operator's value, else the declared default.
One fixed variable is what lets a module-specific name reach Compact's allowlisted
child processes without Core listing it.

This module imports only the standard library.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

#: Every module setting name starts with this neutral prefix; no Core setting does.
MODULE_SETTING_PREFIX = "FS_MODULE_"
#: The one runtime variable that carries every module setting the operator set.
MODULE_SETTINGS_ENV = "FS_PLATFORM_MODULE_SETTINGS"

_NAME_PATTERN = re.compile(r"^FS_MODULE_[A-Z0-9][A-Z0-9_]*$")
_REQUIRED_KEYS = frozenset({"name", "description"})
_OPTIONAL_KEYS = frozenset({"default", "secret", "allowed_values"})
_MAX_DESCRIPTION_CHARS = 500
#: The longest value a module setting may have, as a default or an operator value.
MODULE_SETTING_MAX_VALUE_CHARS = 4096
_MAX_VALUE_CHARS = MODULE_SETTING_MAX_VALUE_CHARS
_CARRIER_PREFIX = "base64:"
_MODULE_ID_SEPARATORS = re.compile(r"[^A-Z0-9]+")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModuleSettingDeclaration:
    """One setting a platform module declares, with the module that declared it."""

    name: str
    description: str
    default: str = ""
    allowed_values: tuple[str, ...] = ()
    module_id: str = ""

    def to_json(self) -> dict[str, Any]:
        """The record the module store and ``config show`` report."""
        return {
            "name": self.name,
            "description": self.description,
            "default": self.default,
            "allowed_values": list(self.allowed_values),
            "module": self.module_id,
        }

    @classmethod
    def from_json(cls, raw: object) -> ModuleSettingDeclaration:
        """Read a :meth:`to_json` record (the in-image ``settings`` command's answer)."""
        if not isinstance(raw, dict):
            raise ValueError("a module setting record must be an object")
        name = raw.get("name")
        module_id = raw.get("module")
        description = raw.get("description")
        default = raw.get("default", "")
        allowed = raw.get("allowed_values", [])
        if not isinstance(name, str) or not is_module_setting_name(name):
            raise ValueError(f"module setting record names {name!r}")
        if not isinstance(module_id, str) or not module_id:
            raise ValueError(f"module setting {name} names no module")
        _require_module_owned_name(name, module_id=module_id, where="module setting record")
        if not isinstance(description, str) or not isinstance(default, str):
            raise ValueError(f"module setting {name} has a malformed description or default")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError(f"module setting {name} has malformed allowed_values")
        return cls(
            name=name,
            description=description,
            default=default,
            allowed_values=tuple(allowed),
            module_id=module_id,
        )


def declarations_from_listing(result: Mapping[str, Any]) -> tuple[ModuleSettingDeclaration, ...]:
    """Read the in-image ``settings`` command's ``{"settings": [...]}`` answer."""
    raw = result.get("settings")
    if not isinstance(raw, list):
        raise ValueError("the platform-module settings listing has no 'settings' list")
    return tuple(ModuleSettingDeclaration.from_json(item) for item in raw)


def is_module_setting_name(name: str) -> bool:
    """Whether *name* has the shape of a module setting (prefix and upper-case)."""
    return bool(_NAME_PATTERN.fullmatch(name))


def module_setting_name_prefix(module_id: str) -> str:
    """The prefix every setting of *module_id* starts with.

    ``FS_MODULE_`` + the module id in upper case with each run of other characters
    (``.``, ``-``) turned into ``_`` + ``_``: ``acme.sign-in`` ->
    ``FS_MODULE_ACME_SIGN_IN_``.
    """
    snake = _MODULE_ID_SEPARATORS.sub("_", str(module_id or "").upper()).strip("_")
    if not snake:
        raise ValueError(f"module id {module_id!r} cannot name module settings")
    return f"{MODULE_SETTING_PREFIX}{snake}_"


def _require_module_owned_name(name: str, *, module_id: str, where: str) -> None:
    prefix = module_setting_name_prefix(module_id)
    if not name.startswith(prefix) or len(name) == len(prefix):
        raise ValueError(
            f"{where} name {name!r} must start with {prefix} (the module id {module_id!r} "
            "in upper snake case) and name the setting after it"
        )


def parse_module_settings(
    value: object, *, module_id: str, where: str
) -> tuple[ModuleSettingDeclaration, ...]:
    """Read a descriptor's ``"settings"`` list; raise ``ValueError`` naming the problem."""
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of setting objects")
    declarations: list[ModuleSettingDeclaration] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = f"{where}[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{item} must be an object")
        keys = set(raw)
        missing = sorted(_REQUIRED_KEYS - keys)
        unknown = sorted(keys - _REQUIRED_KEYS - _OPTIONAL_KEYS)
        if missing or unknown:
            raise ValueError(f"{item} has missing keys {missing} and unknown keys {unknown}")
        name = raw["name"]
        if not isinstance(name, str) or not is_module_setting_name(name):
            raise ValueError(
                f"{item} name {name!r} must be upper-case and start with {MODULE_SETTING_PREFIX}"
            )
        _require_module_owned_name(name, module_id=module_id, where=item)
        if name in seen:
            raise ValueError(f"{item} declares {name} twice")
        seen.add(name)
        description = raw["description"]
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > _MAX_DESCRIPTION_CHARS
        ):
            raise ValueError(
                f"{item} description must be non-empty text of at most "
                f"{_MAX_DESCRIPTION_CHARS} characters"
            )
        if "secret" in raw and raw["secret"] is not False:
            raise ValueError(f"{item} secret must be false; module settings carry no secrets")
        default = raw.get("default", "")
        if not isinstance(default, str) or len(default) > _MAX_VALUE_CHARS:
            raise ValueError(
                f"{item} default must be text of at most {_MAX_VALUE_CHARS} characters"
            )
        allowed = _allowed_values(raw.get("allowed_values"), where=item)
        if allowed and default and default not in allowed:
            raise ValueError(f"{item} default {default!r} is not one of its allowed_values")
        declarations.append(
            ModuleSettingDeclaration(
                name=name,
                description=description.strip(),
                default=default,
                allowed_values=allowed,
                module_id=module_id,
            )
        )
    return tuple(declarations)


def _allowed_values(value: object, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"{where} allowed_values must be a non-empty list of non-empty text")
    if len(set(value)) != len(value):
        raise ValueError(f"{where} allowed_values lists a value twice")
    return tuple(value)


def conflicting_module_settings(
    declarations: Iterable[ModuleSettingDeclaration],
) -> dict[str, tuple[str, ...]]:
    """Each setting name declared by more than one module, with those module ids."""
    owners: dict[str, list[str]] = {}
    for declaration in declarations:
        owners.setdefault(declaration.name, []).append(declaration.module_id)
    return {name: tuple(ids) for name, ids in sorted(owners.items()) if len(ids) > 1}


def module_set_setting_conflicts(
    module_ids: Iterable[str],
    declarations: Iterable[ModuleSettingDeclaration] = (),
) -> list[str]:
    """Why a set of modules cannot share the ``FS_MODULE_*`` namespace; empty when it can.

    Two refusals beyond a name declared twice (:func:`conflicting_module_settings`):

    - two module ids with the same upper-snake prefix (``acme.sign-in`` and
      ``acme.sign.in`` both own ``FS_MODULE_ACME_SIGN_IN_``), whether or not either
      declares a setting: a name could not be told apart by its owner;
    - a module declaring a name that starts with another module's longer prefix
      (``acme.login`` declaring ``FS_MODULE_ACME_LOGIN_SYNC_X`` while ``acme.login-sync``
      is in the set): the name looks like the other module's setting.
    """
    prefixes: dict[str, list[str]] = {}
    for module_id in dict.fromkeys(str(item) for item in module_ids):
        try:
            prefixes.setdefault(module_setting_name_prefix(module_id), []).append(module_id)
        except ValueError:
            continue
    problems = [
        f"modules {', '.join(ids)} share the setting prefix {prefix}"
        for prefix, ids in sorted(prefixes.items())
        if len(ids) > 1
    ]
    for declaration in declarations:
        own = module_setting_name_prefix(declaration.module_id)
        for prefix, ids in sorted(prefixes.items()):
            if (
                len(prefix) > len(own)
                and declaration.module_id not in ids
                and declaration.name.startswith(prefix)
            ):
                problems.append(
                    f"module {declaration.module_id} declares {declaration.name}, which "
                    f"starts with {prefix}, the setting prefix of module {', '.join(ids)}"
                )
    return problems


def encode_module_settings(values: Mapping[str, str]) -> str:
    """The :data:`MODULE_SETTINGS_ENV` value for *values*; empty when there are none.

    ``base64:`` + the base64 of the JSON object: only letters, digits, ``+``, ``/``,
    ``=`` and ``:``, which every env-file reader takes literally, whatever quotes,
    ``#`` or spaces the operator's values contain.
    """
    selected = {name: value for name, value in values.items() if is_module_setting_name(name)}
    if not selected:
        return ""
    text = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _CARRIER_PREFIX + base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_module_settings(text: str) -> dict[str, str]:
    """Read a :data:`MODULE_SETTINGS_ENV` value; raise ``ValueError`` when malformed.

    The value :func:`encode_module_settings` writes, or a plain JSON object (a
    development run that exports the variable by hand).
    """
    text = text.strip()
    if not text:
        return {}
    if text.startswith(_CARRIER_PREFIX):
        try:
            text = base64.b64decode(text[len(_CARRIER_PREFIX) :], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(f"{MODULE_SETTINGS_ENV} is not valid base64 JSON") from exc
    payload = json.loads(text)
    if not isinstance(payload, dict) or not all(
        isinstance(name, str) and is_module_setting_name(name) and isinstance(value, str)
        for name, value in payload.items()
    ):
        raise ValueError(
            f"{MODULE_SETTINGS_ENV} must be a JSON object of {MODULE_SETTING_PREFIX}* names "
            "to text values"
        )
    return dict(payload)


def publish_module_settings(
    environment: MutableMapping[str, str],
    declarations: Iterable[ModuleSettingDeclaration],
) -> dict[str, str]:
    """Publish the declared settings into *environment*; return what was published.

    The operator's value from :data:`MODULE_SETTINGS_ENV` wins, then a value the
    process environment already carries (a development run that exports the name
    directly), then the declared default. A name no loaded module declares is never
    published. A malformed carrier is ignored as a whole (the operator CLI never
    writes one), so the roles start on the defaults, and a warning says so: the
    operator's values are not in effect.
    """
    try:
        operator_values = decode_module_settings(str(environment.get(MODULE_SETTINGS_ENV) or ""))
    except ValueError as exc:
        logger.warning(
            "%s cannot be read (%s); the operator's platform-module settings are not "
            "applied and every module setting uses its default. Set them again with "
            "'flow-steward config set' and apply.",
            MODULE_SETTINGS_ENV,
            exc,
        )
        operator_values = {}
    published: dict[str, str] = {}
    for declaration in declarations:
        value = operator_values.get(declaration.name)
        if value is None:
            value = str(environment.get(declaration.name) or "") or declaration.default
        if not value:
            continue
        environment[declaration.name] = value
        published[declaration.name] = value
    return published


__all__ = [
    "MODULE_SETTINGS_ENV",
    "MODULE_SETTING_MAX_VALUE_CHARS",
    "MODULE_SETTING_PREFIX",
    "ModuleSettingDeclaration",
    "conflicting_module_settings",
    "declarations_from_listing",
    "decode_module_settings",
    "encode_module_settings",
    "is_module_setting_name",
    "module_set_setting_conflicts",
    "module_setting_name_prefix",
    "parse_module_settings",
    "publish_module_settings",
]
