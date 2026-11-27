"""Core's secret codec as platform modules receive it: bound to the module (ADR 0082 §12).

A platform module encrypts and decrypts its own secrets (an identity provider's
client secret, say) through Core's ports: ``container.external_sign_in`` in the app
role and ``encrypt_secret`` / ``decrypt_secret`` in the control and worker hooks and
jobs. Those ports are bound to the module Core loaded, and the ciphertext is bound
to that module:

- **Format.** ``fsmod1:<module id>:<base64(nonce + AES-256-GCM ciphertext)>``. The key
  is derived from Core's ``ENCRYPTION_KEY`` and the module id
  (:func:`~core.infrastructure.security.secret_codec.derive_secret_storage_key` with
  ``flow-steward.platform-module-secret.v1`` + NUL + module id), and the same
  context is the AEAD's associated data, so a value cannot be relabelled as another
  module's: the label picks the key and a wrong label fails authentication.
- **Who may decrypt.** The module that encrypted the value, and a module that
  declares that module under ``"requires"`` (an add-on built on the module's data,
  e.g. a directory sync reading the sign-in engine's provider secret). Anything
  else is refused with :class:`ModuleSecretScopeError`: a value another module
  encrypted, and every value Core encrypted (Core's codec writes plain base64
  without the label). A module has no port that decrypts a Core connection secret.
- **Values written before the binding.** A module that stored values with the
  earlier unbound codec re-encrypts them with :meth:`ModuleSecretCodec.reseal_legacy_secrets`,
  which only reads and writes rows of a table in the module's own schema
  (``mod_<module id>``), never another schema, and turns each unlabelled value
  into a value bound to the module. ``decrypt`` itself never accepts an unlabelled
  value.

The binding is an interface contract between Core and trusted in-process code, not
isolation: it keeps a module's use of Core's key explicit and limited to its own
data.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Callable, Iterable
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.infrastructure.security import secret_codec

#: Every module-bound value starts with this label.
MODULE_SECRET_PREFIX = "fsmod1:"
_CONTEXT = b"flow-steward.platform-module-secret.v1\x00"
_MODULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_NONCE_BYTES = 12


class ModuleSecretScopeError(PermissionError):
    """A module asked Core's codec for a value outside its scope."""


def _module_id(value: object) -> str:
    module_id = str(value or "")
    if not _MODULE_ID_RE.fullmatch(module_id):
        raise ModuleSecretScopeError(
            f"Core's secret codec is bound to a platform module identity; {module_id!r} is not one"
        )
    return module_id


def _context(module_id: str) -> bytes:
    return _CONTEXT + module_id.encode("utf-8")


def _key(module_id: str, master_key: bytes | None) -> bytes:
    return secret_codec.derive_secret_storage_key(_context(module_id), master_key)


def module_secret_owner(value: str) -> str | None:
    """The module a labelled value is bound to; ``None`` for an unlabelled value."""
    text = str(value or "")
    if not text.startswith(MODULE_SECRET_PREFIX):
        return None
    owner, separator, _body = text[len(MODULE_SECRET_PREFIX) :].partition(":")
    return owner if separator and _MODULE_ID_RE.fullmatch(owner) else None


def module_schema_name(module_id: str) -> str:
    """The module's own schema, as Core's migrator names it: ``mod_`` + mapped id."""
    schema = "mod_" + _module_id(module_id).replace("-", "__").replace(".", "_")
    if not _IDENTIFIER_RE.fullmatch(schema):
        raise ModuleSecretScopeError(f"module {module_id!r} has no usable schema name")
    return schema


def encrypt_module_secret(
    plaintext: str, *, module_id: str, master_key: bytes | None = None
) -> str:
    """Encrypt ``plaintext`` bound to ``module_id``."""
    owner = _module_id(module_id)
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_key(owner, master_key)).encrypt(
        nonce, str(plaintext).encode("utf-8"), _context(owner)
    )
    return f"{MODULE_SECRET_PREFIX}{owner}:{base64.b64encode(nonce + sealed).decode('ascii')}"


def decrypt_module_secret(
    value: str,
    *,
    module_id: str,
    readable: Iterable[str] = (),
    master_key: bytes | None = None,
) -> str:
    """Decrypt a value bound to ``module_id`` or to one of the ``readable`` modules."""
    text = str(value or "")
    if not text:
        return ""
    caller = _module_id(module_id)
    owner = module_secret_owner(text)
    if owner is None:
        raise ModuleSecretScopeError(
            f"platform module {caller} may decrypt only values a platform module encrypted "
            "through Core's module ports; this value is not one (re-encrypt values stored "
            "before the binding with reseal_legacy_secrets)"
        )
    if owner != caller and owner not in {str(item) for item in readable}:
        raise ModuleSecretScopeError(
            f"platform module {caller} may not decrypt a value of module {owner}; it may "
            "decrypt its own values and those of the modules it requires"
        )
    body = text[len(MODULE_SECRET_PREFIX) + len(owner) + 1 :]
    try:
        raw = base64.b64decode(body, validate=True)
        plain = AESGCM(_key(owner, master_key)).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _context(owner)
        )
    except (binascii.Error, ValueError, InvalidTag) as exc:
        raise ModuleSecretScopeError(
            f"the value bound to module {owner} does not decrypt (altered or another key)"
        ) from exc
    return plain.decode("utf-8")


class ModuleSecretCodec:
    """Core's codec bound to one module and the modules it requires."""

    def __init__(
        self,
        module_id: str,
        requires: Iterable[str] = (),
        *,
        master_key: Callable[[], bytes | None] = lambda: None,
        legacy_decrypt: Callable[[str], str] | None = None,
    ) -> None:
        self.module_id = _module_id(module_id)
        self.readable = frozenset(str(item) for item in requires)
        self._master_key = master_key
        self._legacy_decrypt = legacy_decrypt or (lambda value: secret_codec.decrypt_secret(value))

    def encrypt(self, value: str) -> str:
        return encrypt_module_secret(
            str(value or ""), module_id=self.module_id, master_key=self._master_key()
        )

    def decrypt(self, value: str) -> str:
        return decrypt_module_secret(
            str(value or ""),
            module_id=self.module_id,
            readable=self.readable,
            master_key=self._master_key(),
        )

    def reseal_legacy_secrets(
        self, cursor: Any, *, table: str, column: str, json_key: str | None = None
    ) -> int:
        """Re-encrypt the unlabelled values of ``table.column`` bound to this module.

        ``table`` is a table of the module's own schema (``mod_<module id>``);
        ``column`` holds the value, or, with ``json_key``, a ``jsonb`` object that
        holds it under that key. Runs in the caller's transaction on its cursor,
        locks the rows it reads and returns how many distinct values it
        re-encrypted. A value already bound (to any module) is left alone. Safe to
        run again.
        """
        schema = module_schema_name(self.module_id)
        for label, name in (("table", table), ("column", column)):
            if not _IDENTIFIER_RE.fullmatch(str(name or "")):
                raise ModuleSecretScopeError(f"{label} {name!r} is not a plain identifier")
        target = f'"{schema}"."{table}"'
        if json_key is None:
            value_sql = f'"{column}"'
            key_params: tuple[Any, ...] = ()
            update_sql = f'UPDATE {target} SET "{column}" = %s WHERE "{column}" = %s'  # nosec B608 - validated identifiers
        else:
            key = str(json_key)
            if not key or len(key) > 128:
                raise ModuleSecretScopeError("json_key must be a non-empty key of 128 characters")
            value_sql = f'("{column}" ->> %s)'
            key_params = (key,)
            update_sql = (
                f'UPDATE {target} SET "{column}" = '  # nosec B608 - validated identifiers
                f'jsonb_set("{column}", ARRAY[%s::text], to_jsonb(%s::text)) WHERE {value_sql} = %s'
            )
        cursor.execute(
            f"SELECT {value_sql} FROM {target} WHERE {value_sql} <> '' "  # nosec B608 - validated identifiers
            f"AND {value_sql} NOT LIKE %s FOR UPDATE",
            (*key_params, *key_params, *key_params, MODULE_SECRET_PREFIX + "%"),
        )
        legacy = dict.fromkeys(str(_first(row) or "") for row in cursor.fetchall() or ())
        resealed = 0
        for old in legacy:
            if not old:
                continue
            new = self.encrypt(self._legacy_decrypt(old))
            if json_key is None:
                cursor.execute(update_sql, (new, old))
            else:
                cursor.execute(update_sql, (*key_params, new, *key_params, old))
            resealed += 1
        return resealed


def _first(row: Any) -> Any:
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0] if isinstance(row, (tuple, list)) and row else row


class ModuleSecretPort:
    """One codec operation as an unbound service: Core's loader binds it per module.

    Called without a module identity it refuses; ``for_platform_module(module_id,
    requires)`` (what the loader calls for each module's hook and job) returns the
    operation of a :class:`ModuleSecretCodec` bound to that module.
    """

    _OPERATIONS = frozenset({"encrypt", "decrypt", "reseal_legacy_secrets"})

    def __init__(self, operation: str) -> None:
        if operation not in self._OPERATIONS:
            raise ValueError(f"unknown module secret operation {operation!r}")
        self._operation = operation

    def for_platform_module(self, module_id: str, requires: Iterable[str] = ()) -> Any:
        return getattr(ModuleSecretCodec(module_id, requires), self._operation)

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ModuleSecretScopeError(
            "Core's secret codec is available to a platform module only under the "
            "identity Core loaded it with"
        )


__all__ = [
    "MODULE_SECRET_PREFIX",
    "ModuleSecretCodec",
    "ModuleSecretPort",
    "ModuleSecretScopeError",
    "decrypt_module_secret",
    "encrypt_module_secret",
    "module_schema_name",
    "module_secret_owner",
]
