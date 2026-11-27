"""Pluggable secret backends behind the stored-secret reader (ADR 0066, ADR 0082).

A **secret backend** keeps stored-secret values outside this database: a secret
manager, a key vault, an HTTP service. Core implements none. A platform module that
declares ``"provides": ["secret_backend"]`` registers one per kind through
:class:`SecretBackendPort` (``container.secret_backends`` in the app role,
``context["secret_backends"]`` in the worker and control hooks). Core's own
``local`` store, values sealed with the installation key in ``secret_refs``, is not
a registration: it is the default and needs no module.

Consumers keep holding the account-qualified reference they always held. A
reference with a row in ``secret_backend_locators`` is read from the backend that
row names, with the non-secret configuration of its ``secret_backend_configs`` row
and the backend's own locator; any other reference is read locally. A remote read
never falls back to a local value: a backend that is not installed in this process,
a configuration that is turned off or a backend that refuses fails closed with the
existing stored-secret errors.

Entitlement is not checked here. When a backend's feature ends, values already kept
there stay readable (ADR 0082 §4: data keeps working); adopting, writing or moving a
value to it is refused by the admin service in ``core.application.secrets``.

Values read remotely are kept in this process only, for at most
:data:`CACHE_SECONDS`, never on disk, in the database or in a log. Every remote read
is logged by reference, backend kind and outcome only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from core.infrastructure.security.stored_secrets import (
    SecretReadContext,
    StoredSecretError,
    StoredSecretMissing,
    StoredSecretReader,
    StoredSecretStorageRefused,
    StoredSecretUnavailable,
    StoredSecretUndecryptable,
)

_logger = logging.getLogger(__name__)

#: The capability a platform module declares in its descriptor to register backends.
SECRET_BACKEND_CAPABILITY = "secret_backend"
#: Core's own store; never registered by a module.
LOCAL_BACKEND_KIND = "local"
#: What a backend may offer besides reading. Reading never implies any of them.
WRITE = "write"
DELETE = "delete"
ROTATE = "rotate"
#: With ``write``: ``write(context, locator, value, create_only=True)`` creates the
#: secret only when nothing is kept at ``locator`` and raises
#: :class:`SecretBackendTargetExists` otherwise (an atomic "create if absent").
CREATE_ONLY = "create_only"
BACKEND_CAPABILITIES = frozenset({WRITE, DELETE, ROTATE, CREATE_ONLY})
#: How long a remotely read value is kept in this process.
CACHE_SECONDS = 60.0
#: The longest a single remote read may take when the caller set no deadline.
DEFAULT_READ_SECONDS = 5.0
_CACHE_MAX_ENTRIES = 1024
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_FEATURE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
#: Configuration is non-secret by contract: a key that names a credential is refused
#: (in a field descriptor and in a saved configuration) so nobody pastes one into it.
#: Backend credentials come from the deployment.
SECRET_LOOKING_KEY = re.compile(
    r"(password|passphrase|secret|token|credential|private[_-]?key|api[_-]?key)", re.IGNORECASE
)
#: The field types of a configuration form: the resource_admin vocabulary without
#: ``secret`` (configuration holds no credential), ``json`` and ``reference``.
CONFIG_FIELD_TYPES = frozenset(
    {"text", "textarea", "number", "select", "checkbox", "csv", "multi_choice"}
)
_CHOICE_FIELD_TYPES = frozenset({"select", "multi_choice"})
_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_CONFIG_FIELDS = 50
_MAX_FIELD_TEXT = 500
_MAX_LABEL = 80


class SecretBackendRefusedError(PermissionError):
    """A registration Core refuses: no declared capability, a duplicate kind or too late."""


class SecretBackendTargetExists(Exception):
    """A create-only write found a secret already kept at the locator; nothing was written."""


@dataclass(frozen=True)
class SecretBackendContext:
    """What one backend call is for.

    ``config`` is the non-secret configuration an administrator saved for the
    account (read-only). The backend's own credentials never come from here: they
    come from the deployment (environment, mounted file, workload identity).
    ``deadline`` is a ``time.monotonic()`` value the backend must respect.
    """

    account_id: str
    config_id: str
    config: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    deadline: float | None = None

    def remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())


@dataclass(frozen=True)
class SecretBackendHealth:
    """Whether a configuration can be used now. ``code`` is a short neutral reason."""

    healthy: bool
    code: str = ""


@runtime_checkable
class SecretBackend(Protocol):
    """One kind of external store, provided by a platform module.

    Required: ``kind`` (lowercase id, e.g. ``acme.vault``), ``feature`` (the
    entitlement a new adoption or write needs), ``capabilities`` (a subset of
    :data:`BACKEND_CAPABILITIES`), :meth:`read` and :meth:`health`.

    :meth:`read` returns the plaintext at ``locator`` or raises a
    :class:`~core.infrastructure.security.stored_secrets.StoredSecretError`:
    ``StoredSecretMissing`` when the locator names nothing,
    ``StoredSecretUnavailable`` for a transient failure or timeout,
    ``StoredSecretStorageRefused`` when the store refuses this installation. Any
    other exception counts as unavailable. Messages are never shown or logged.

    Optional, each only with its capability: ``write(context, locator, value) ->
    locator`` (returns the locator to keep, e.g. with the version it created),
    ``delete(context, locator)`` and ``rotate(context, locator) -> locator``. Also
    optional: ``validate_config(config) -> config`` (or ``validate_config(config,
    policy) -> config``, which also receives the installation's per-account policy
    entry of this kind, ``None`` when unrestricted; see
    :mod:`core.infrastructure.security.secret_backend_policy`),
    ``validate_locator(locator) -> locator``, which raise ``ValueError`` with a safe
    message for bad input, and ``exists(context, locator) -> bool``, which Core asks
    before a first write when the backend has no ``create_only`` capability.

    Optional for the settings page: ``label`` (the plain-language name an
    administrator sees; a humanized ``kind`` without it) and ``config_fields()``,
    the form of the non-secret configuration (see :func:`backend_config_fields`).
    The page asks one "name in the storage" for adopting or moving a value and
    sends it as the locator ``{"name": <text>}``; ``validate_locator`` turns it
    into the backend's own locator.
    """

    kind: str
    feature: str
    capabilities: Collection[str]

    def read(self, context: SecretBackendContext, locator: Mapping[str, Any]) -> str: ...

    def health(self, context: SecretBackendContext) -> SecretBackendHealth: ...


# -- the process-wide registry -------------------------------------------------------

_LOCK = threading.Lock()
_BACKENDS: dict[str, SecretBackend] = {}
_OWNERS: dict[str, str] = {}


def registered_backend(kind: str) -> SecretBackend | None:
    with _LOCK:
        return _BACKENDS.get(str(kind or "").strip())


def registered_backends() -> tuple[SecretBackend, ...]:
    with _LOCK:
        return tuple(_BACKENDS[kind] for kind in sorted(_BACKENDS))


def backend_owner(kind: str) -> str | None:
    """The platform module that registered `kind`."""
    with _LOCK:
        return _OWNERS.get(str(kind or "").strip())


def has_registered_backends() -> bool:
    with _LOCK:
        return bool(_BACKENDS)


def loading_checkpoint() -> tuple[tuple[str, SecretBackend, str], ...]:
    """The registrations now; Core's module loader restores it when a module fails."""
    with _LOCK:
        return tuple((kind, _BACKENDS[kind], _OWNERS.get(kind, "")) for kind in _BACKENDS)


def restore_loading_checkpoint(
    checkpoint: tuple[tuple[str, SecretBackend, str], ...] | None,
) -> None:
    with _LOCK:
        _BACKENDS.clear()
        _OWNERS.clear()
        for kind, backend, owner in checkpoint or ():
            _BACKENDS[kind] = backend
            _OWNERS[kind] = owner
    clear_cache()


def backend_capabilities(backend: SecretBackend) -> frozenset[str]:
    return frozenset(str(item) for item in getattr(backend, "capabilities", ()) or ())


def _validated_backend(backend: Any) -> tuple[str, str]:
    kind = str(getattr(backend, "kind", "") or "").strip()
    if not _KIND_RE.match(kind) or kind == LOCAL_BACKEND_KIND:
        raise ValueError(
            "a secret backend kind is a lowercase id (letters, digits, '.', '_', '-'),"
            f" not '{LOCAL_BACKEND_KIND}'"
        )
    feature = str(getattr(backend, "feature", "") or "").strip().lower()
    if not _FEATURE_RE.match(feature):
        raise ValueError("a secret backend names the feature its new values need")
    if not callable(getattr(backend, "read", None)) or not callable(
        getattr(backend, "health", None)
    ):
        raise TypeError("a secret backend needs read and health")
    capabilities = backend_capabilities(backend)
    unknown = capabilities - BACKEND_CAPABILITIES
    if unknown:
        raise ValueError(f"unknown secret backend capabilities: {', '.join(sorted(unknown))}")
    if CREATE_ONLY in capabilities and WRITE not in capabilities:
        raise ValueError("a secret backend with 'create_only' also needs 'write'")
    for capability in capabilities - {CREATE_ONLY}:
        if not callable(getattr(backend, capability, None)):
            raise TypeError(f"a secret backend with '{capability}' needs a {capability} method")
    label = getattr(backend, "label", None)
    if label is not None and (not isinstance(label, str) or len(label.strip()) > _MAX_LABEL):
        raise ValueError(f"a secret backend label is text of at most {_MAX_LABEL} characters")
    # A module whose form Core cannot draw fails its loading, not the settings page.
    backend_config_fields(backend)
    return kind, feature


# -- what the settings page draws --------------------------------------------------


def backend_label(backend: Any) -> str:
    """The name an administrator reads: the backend's ``label``, else its humanized kind."""
    label = getattr(backend, "label", None)
    if isinstance(label, str) and label.strip():
        return label.strip()[:_MAX_LABEL]
    words = re.sub(r"[._-]+", " ", str(getattr(backend, "kind", "") or "")).strip()
    return words[:1].upper() + words[1:]


def _field_text(field: Mapping[str, Any], key: str, where: str, *, required: bool = False) -> str:
    value = field.get(key)
    if value is None:
        if required:
            raise ValueError(f"{where} needs a {key}")
        return ""
    if not isinstance(value, str) or len(value) > _MAX_FIELD_TEXT:
        raise ValueError(f"{where} {key} must be text of at most {_MAX_FIELD_TEXT} characters")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{where} needs a {key}")
    return text


def _field_option(value: Any, where: str) -> dict[str, str]:
    if isinstance(value, str) and value.strip():
        return {"value": value.strip(), "label": value.strip()}
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} options are text or {{value, label}} mappings")
    option_value = _field_text(value, "value", f"{where} option", required=True)
    option = {
        "value": option_value,
        "label": _field_text(value, "label", f"{where} option") or option_value,
    }
    if description := _field_text(value, "description", f"{where} option"):
        option["description"] = description
    return option


def _field_default(value: Any, field_type: str, where: str) -> Any:
    if field_type == "checkbox":
        if not isinstance(value, bool):
            raise ValueError(f"{where} default must be true or false")
        return value
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where} default must be a number")
        return value
    if field_type in {"multi_choice", "csv"}:
        if not isinstance(value, (list, tuple)) or not all(isinstance(i, str) for i in value):
            raise ValueError(f"{where} default must be a list of text")
        return [item.strip() for item in value if item.strip()]
    if not isinstance(value, str):
        raise ValueError(f"{where} default must be text")
    return value


def _config_field(value: Any, index: int) -> dict[str, Any]:
    where = f"config field {index + 1}"
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} is not a mapping")
    key = _field_text(value, "key", where, required=True)
    where = f"config field '{key}'"
    if not _FIELD_KEY_RE.match(key):
        raise ValueError(f"{where}: a key is a lowercase id (letters, digits, '_')")
    if SECRET_LOOKING_KEY.search(key):
        raise ValueError(f"{where}: configuration holds no credential")
    field_type = _field_text(value, "type", where) or "text"
    if field_type not in CONFIG_FIELD_TYPES:
        raise ValueError(f"{where}: type must be one of {', '.join(sorted(CONFIG_FIELD_TYPES))}")
    field: dict[str, Any] = {
        "key": key,
        "type": field_type,
        "label": _field_text(value, "label", where, required=True),
        "required": value.get("required") is True,
        "advanced": value.get("advanced") is True,
    }
    for text_key in ("help", "placeholder"):
        if text := _field_text(value, text_key, where):
            field[text_key] = text
    raw_options = value.get("options")
    if field_type in _CHOICE_FIELD_TYPES:
        if isinstance(raw_options, (str, bytes, Mapping)) or not isinstance(
            raw_options, (list, tuple)
        ):
            raise ValueError(f"{where} needs a list of options")
        options = [_field_option(item, where) for item in raw_options]
        values = [option["value"] for option in options]
        if not options or len(set(values)) != len(values):
            raise ValueError(f"{where} needs options with distinct values")
        field["options"] = options
    elif raw_options is not None:
        raise ValueError(f"{where} has options but is not a choice field")
    if value.get("default") is not None:
        field["default"] = _field_default(value["default"], field_type, where)
    condition = value.get("visible_when")
    if condition is not None:
        if not isinstance(condition, Mapping):
            raise ValueError(f"{where} visible_when must be a mapping")
        other = _field_text(condition, "field", f"{where} visible_when", required=True)
        contains = _field_text(condition, "contains", f"{where} visible_when", required=True)
        if other == key:
            raise ValueError(f"{where} visible_when names another field")
        field["visible_when"] = {"field": other, "contains": contains}
    return field


def backend_config_fields(backend: Any) -> tuple[dict[str, Any], ...]:
    """The validated form of a backend's non-secret configuration; empty without one.

    ``config_fields()`` returns a sequence of mappings in the resource_admin field
    vocabulary: ``key`` (lowercase id, the configuration key; never a credential
    name), ``type`` (``text`` default, ``textarea``, ``number``, ``checkbox``,
    ``csv``, ``select`` or ``multi_choice``), ``label``, optional ``help``,
    ``placeholder``, ``required``, ``default``, ``advanced`` (shown under
    "Advanced settings"), ``options`` (choice fields: text or ``{value, label,
    description?}``) and ``visible_when`` (``{field, contains}``, another field of
    the same form). Anything else raises ``ValueError``.
    """
    provider = getattr(backend, "config_fields", None)
    if provider is None:
        return ()
    if not callable(provider):
        raise TypeError("a secret backend's config_fields is a method")
    raw = provider()
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, (list, tuple)):
        raise ValueError("config_fields returns a list of field mappings")
    if len(raw) > _MAX_CONFIG_FIELDS:
        raise ValueError(f"config_fields returns at most {_MAX_CONFIG_FIELDS} fields")
    fields = tuple(_config_field(item, index) for index, item in enumerate(raw))
    keys = [field["key"] for field in fields]
    if len(set(keys)) != len(keys):
        raise ValueError("config_fields repeats a key")
    for described in fields:
        condition = described.get("visible_when")
        if condition and condition["field"] not in keys:
            raise ValueError(
                f"config field '{described['key']}' visible_when names an unknown field"
            )
    return fields


class SecretBackendPort:
    """Where a platform module registers its secret backends (ADR 0082).

    Core's module loader opens :meth:`platform_module_scope` around each module's
    hook; a registration outside it, from a module that did not declare
    ``secret_backend``, from a module without a platform module identity, of a kind
    already registered, or after :meth:`seal` is refused with
    :class:`SecretBackendRefusedError` and fails that module's loading. A failed
    module's registrations are rolled back with the process-wide registry.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scope: tuple[str | None, frozenset[str]] | None = None
        self._sealed = False

    @contextmanager
    def platform_module_scope(
        self, module_id: str | None, provides: Collection[str] = ()
    ) -> Iterator[None]:
        scope = (
            str(module_id or "").strip() or None,
            frozenset(str(item).strip().lower() for item in provides),
        )
        with self._lock:
            previous = self._scope
            self._scope = scope
        try:
            yield
        finally:
            with self._lock:
                self._scope = previous

    def seal(self) -> None:
        """Refuse every registration from now on: module loading has finished."""
        with self._lock:
            self._sealed = True

    def register_secret_backend(self, backend: SecretBackend) -> None:
        kind, _feature = _validated_backend(backend)
        with self._lock:
            sealed, scope = self._sealed, self._scope
        if sealed:
            raise SecretBackendRefusedError("secret backends are registered only while loading")
        module_id, provides = scope if scope is not None else (None, frozenset())
        if module_id is None:
            raise SecretBackendRefusedError(
                "only a platform module with an identity may register a secret backend"
            )
        if SECRET_BACKEND_CAPABILITY not in provides:
            raise SecretBackendRefusedError(
                f"platform module '{module_id}' does not declare '{SECRET_BACKEND_CAPABILITY}'"
            )
        with _LOCK:
            if kind in _BACKENDS:
                raise SecretBackendRefusedError(f"secret backend '{kind}' is already registered")
            _BACKENDS[kind] = backend
            _OWNERS[kind] = module_id

    def backends(self) -> tuple[SecretBackend, ...]:
        return registered_backends()

    def backend(self, kind: str) -> SecretBackend | None:
        return registered_backend(kind)


# -- the per-process value cache -----------------------------------------------------


class _ValueCache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str, str], tuple[float, str]] = OrderedDict()

    def get(self, key: tuple[str, str, str]) -> str | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires <= now:
                del self._entries[key]
                return None
            return value

    def put(self, key: tuple[str, str, str], value: str) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def invalidate(self, account_id: str, ref_key: str | None) -> None:
        with self._lock:
            for key in [
                key
                for key in self._entries
                if key[0] == account_id and (ref_key is None or key[1] == ref_key)
            ]:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_CACHE = _ValueCache(CACHE_SECONDS, _CACHE_MAX_ENTRIES)


def invalidate_cached_secret(account_id: str, ref_key: str | None = None) -> None:
    """Forget values this process read for `account_id` (one reference, or all of them)."""
    _CACHE.invalidate(str(account_id or "").strip(), ref_key)


def clear_cache() -> None:
    _CACHE.clear()


def _cache_key(account_id: str, ref_key: str, located: Mapping[str, Any]) -> tuple[str, str, str]:
    """A changed configuration, locator or kind is a different entry, never a stale hit."""
    material = json.dumps(
        {
            "kind": located.get("backend_kind"),
            "config_id": located.get("config_id"),
            "config": located.get("config"),
            "locator": located.get("locator"),
        },
        sort_keys=True,
        default=str,
    )
    return account_id, ref_key, hashlib.sha256(material.encode("utf-8")).hexdigest()


# -- reading -----------------------------------------------------------------------


def backend_context(
    account_id: str,
    located: Mapping[str, Any],
    *,
    deadline: float | None = None,
) -> SecretBackendContext:
    """The context of one call: the caller's deadline, bounded by :data:`DEFAULT_READ_SECONDS`."""
    bounded = time.monotonic() + DEFAULT_READ_SECONDS
    return SecretBackendContext(
        account_id=str(account_id or ""),
        config_id=str(located.get("config_id") or ""),
        config=MappingProxyType(dict(located.get("config") or {})),
        deadline=bounded if deadline is None else min(deadline, bounded),
    )


def _audit(ref_key: str, kind: str, outcome: str) -> None:
    log = _logger.info if outcome == "read" else _logger.warning
    log(
        "secret backend read: ref=%s backend=%s outcome=%s",
        ref_key,
        kind,
        outcome,
        extra={"secret_ref": ref_key, "secret_backend": kind, "outcome": outcome},
    )


def _core_error(exc: StoredSecretError) -> type[StoredSecretError]:
    """The Core error class a backend's error stands for; its own text is dropped."""
    for candidate in (
        StoredSecretMissing,
        StoredSecretStorageRefused,
        StoredSecretUndecryptable,
        StoredSecretUnavailable,
    ):
        if isinstance(exc, candidate):
            return candidate
    return StoredSecretUnavailable


def read_remote_value(
    account_id: str,
    ref_key: str,
    located: Mapping[str, Any],
    *,
    deadline: float | None = None,
    use_cache: bool = True,
) -> str:
    """Read the value of `ref_key` from the backend its locator row names; fail closed."""
    kind = str(located.get("backend_kind") or "")
    if not located.get("enabled", False):
        _audit(ref_key, kind, "configuration_disabled")
        raise StoredSecretStorageRefused(ref_key)
    from core.infrastructure.security.secret_backend_policy import (
        SecretBackendPolicyRefused,
        require_config_allowed,
    )

    try:
        # The deployment's per-account allowlist (ADR 0066 amendment 2026-11-27):
        # a configuration outside it is never read, whoever saved it.
        require_config_allowed(account_id, kind, dict(located.get("config") or {}))
    except SecretBackendPolicyRefused:
        _audit(ref_key, kind, "policy_refused")
        raise StoredSecretStorageRefused(ref_key) from None
    backend = registered_backend(kind)
    if backend is None:
        _audit(ref_key, kind, "backend_not_installed")
        raise StoredSecretStorageRefused(ref_key)
    key = _cache_key(account_id, ref_key, located)
    if use_cache and (cached := _CACHE.get(key)) is not None:
        return cached
    context = backend_context(account_id, located, deadline=deadline)
    try:
        value = backend.read(context, MappingProxyType(dict(located.get("locator") or {})))
    except StoredSecretError as exc:
        _audit(ref_key, kind, exc.code)
        raise _core_error(exc)(ref_key) from None
    except Exception as exc:
        # A backend's own text can carry its endpoint, path or response; only the
        # type is useful to an operator.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- only the backend kind and the exception type are logged, never a value.
        _logger.warning("secret backend %s read failed: %s", kind, type(exc).__name__)
        _audit(ref_key, kind, StoredSecretUnavailable.code)
        raise StoredSecretUnavailable(ref_key) from None
    if not isinstance(value, str):
        _audit(ref_key, kind, "invalid_value")
        raise StoredSecretStorageRefused(ref_key)
    _audit(ref_key, kind, "read")
    if use_cache:
        _CACHE.put(key, value)
    return value


class RoutedStoredSecretReader:
    """Local by default; remote for a reference with a locator. No fallback between them."""

    def __init__(self, conn: Any, *, local: StoredSecretReader) -> None:
        self._conn = conn
        self._local = local

    def read(self, context: SecretReadContext, ref_key: str) -> str:
        key = str(ref_key or "").strip()
        if not key:
            raise StoredSecretMissing(key)
        remaining_seconds = context.remaining_seconds()
        if (remaining_seconds is not None and remaining_seconds <= 0) or self._conn is None:
            raise StoredSecretUnavailable(key)
        if not context.account_id:
            raise StoredSecretMissing(key)
        located = locate(self._conn, account_id=context.account_id, ref_key=key)
        if located is None:
            return self._local.read(context, key)
        return read_remote_value(context.account_id, key, located, deadline=context.deadline)


def locate(conn: Any, *, account_id: str, ref_key: str) -> dict[str, Any] | None:
    """The locator row of `ref_key`, or ``None`` when its value is local."""
    from core.infrastructure.db.secret_backend_store import get_secret_locator

    try:
        return get_secret_locator(conn, account_id=account_id, ref_key=ref_key)
    except Exception as exc:
        # Without the answer the reader cannot know where the value is; it never
        # guesses local.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- only the exception type are logged, never a value.
        _logger.warning("secret locator lookup failed: %s", type(exc).__name__)
        raise StoredSecretUnavailable(ref_key) from None


__all__ = [
    "BACKEND_CAPABILITIES",
    "CACHE_SECONDS",
    "CONFIG_FIELD_TYPES",
    "CREATE_ONLY",
    "DEFAULT_READ_SECONDS",
    "DELETE",
    "LOCAL_BACKEND_KIND",
    "ROTATE",
    "SECRET_BACKEND_CAPABILITY",
    "SECRET_LOOKING_KEY",
    "WRITE",
    "RoutedStoredSecretReader",
    "SecretBackend",
    "SecretBackendContext",
    "SecretBackendHealth",
    "SecretBackendPort",
    "SecretBackendRefusedError",
    "SecretBackendTargetExists",
    "backend_capabilities",
    "backend_config_fields",
    "backend_context",
    "backend_label",
    "backend_owner",
    "clear_cache",
    "has_registered_backends",
    "invalidate_cached_secret",
    "loading_checkpoint",
    "locate",
    "read_remote_value",
    "registered_backend",
    "registered_backends",
    "restore_loading_checkpoint",
]
