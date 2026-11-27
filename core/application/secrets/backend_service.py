"""Account administration of secret backends (ADR 0066 amendment 2026-11-26, ADR 0082).

An account administrator configures the secret backends that platform modules
registered, adopts a secret that already exists in one into the catalog, and moves
a catalog secret's value to a backend and back. A value never leaves through here:
adopting checks that the backend can read it and discards it, and every answer
carries metadata only.

Entitlement (ADR 0082 §4): configuring, enabling, adopting into, writing to or
moving a value to a backend needs the feature its module names. When the feature
ends, values already kept there stay readable and can always be moved back; new
adoptions and writes are refused. Moving back never deletes the remote secret.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.application.secrets import backend_repository, repository
from core.application.secrets.authority import actor_identity, require_secret_storage_admin
from core.application.settings.platform_contracts import FeatureNotEntitledError
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.security import secret_backend_policy, secret_backends
from core.infrastructure.security.stored_secrets import (
    LocalStoredSecretReader,
    SecretReadContext,
    StoredSecretError,
    StoredSecretMissing,
)

_MAX_JSON_BYTES = 16 * 1024
_MAX_NAME = 200
_NOT_FOUND = "Secret storage configuration not found"
#: Configuration is non-secret by contract: a key that names a credential is refused
#: so nobody pastes one into it. Backend credentials come from the deployment.
_SECRET_LOOKING_KEY = secret_backends.SECRET_LOOKING_KEY
#: Adopting and checking a configuration answer whether something exists in the
#: storage: each is limited per account (ADR 0066 amendment 2026-11-27).
PROBE_MAX_ATTEMPTS = max(1, int(runtime_env.get("FS_SECRET_STORAGE_PROBE_MAX_ATTEMPTS", "20")))
PROBE_WINDOW_SECONDS = max(1, int(runtime_env.get("FS_SECRET_STORAGE_PROBE_WINDOW_SECONDS", "300")))
_PROBE_LIMITED_MESSAGE = "Too many secret storage checks; try again later"
_TARGET_TAKEN_MESSAGE = "Another catalog secret is already kept at that name in this secret storage"
_TARGET_EXISTS_MESSAGE = (
    "A secret already exists at that name in the secret storage. Adopt it instead, or"
    " move again and choose to replace it"
)
_TARGET_UNKNOWN_MESSAGE = (
    "The secret storage cannot tell whether that name is free; move again and choose to"
    " replace it if overwriting is intended"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ServiceValidationError(f"{label}: an object is required")
    try:
        encoded = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError(f"{label}: plain JSON values are required") from exc
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ServiceValidationError(f"{label}: too large")
    return json.loads(encoded)


def _secret_looking_paths(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if _SECRET_LOOKING_KEY.search(str(key)):
                found.append(where)
            found.extend(_secret_looking_paths(item, where))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_secret_looking_paths(item, f"{path}[{index}]"))
    return found


def _validated_name(value: Any) -> str:
    name = _text(value)
    if not name or len(name) > _MAX_NAME:
        raise ServiceValidationError(f"name: 1 to {_MAX_NAME} characters are required")
    return name


def _backend_view(backend: Any, *, entitled: bool) -> dict[str, Any]:
    try:
        config_fields = [dict(field) for field in secret_backends.backend_config_fields(backend)]
    except Exception:
        # Validated when the module registered it; a backend that changed its mind
        # since is drawn without a form rather than breaking the page.
        config_fields = []
    return {
        "kind": backend.kind,
        "label": secret_backends.backend_label(backend),
        "config_fields": config_fields,
        "feature": str(backend.feature).lower(),
        "capabilities": sorted(secret_backends.backend_capabilities(backend)),
        "entitled": entitled,
    }


def _config_view(row: dict[str, Any]) -> dict[str, Any]:
    installed = secret_backends.registered_backend(row["backend_kind"])
    return {
        "id": row["id"],
        "backend": row["backend_kind"],
        # A configuration whose module is gone still has a readable name.
        "backend_label": secret_backends.backend_label(
            installed if installed is not None else SimpleNamespace(kind=row["backend_kind"])
        ),
        "name": row["name"],
        "config": dict(row.get("non_secret_config") or {}),
        "enabled": bool(row.get("enabled")),
        "installed": installed is not None,
        "created_at": row.get("created_at"),
        "created_by": row.get("created_by") or "",
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by") or "",
    }


def _located(config: dict[str, Any], locator: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend_kind": config["backend_kind"],
        "config_id": config["id"],
        "config": dict(config.get("non_secret_config") or {}),
        "locator": dict(locator),
        "enabled": bool(config.get("enabled")),
    }


def _read_error(exc: StoredSecretError) -> Exception:
    """The safe answer when a backend cannot produce a value; never its own text."""
    if isinstance(exc, StoredSecretMissing):
        return ServiceValidationError("No secret exists at that location in the secret storage")
    if exc.retryable:
        return ServiceUnavailableError("Secret storage is temporarily unavailable")
    return ServiceUnavailableError("Secret storage refused the request")


def _accepts_policy(validate: Callable[..., Any]) -> bool:
    """Whether a backend's ``validate_config`` takes the policy as a second argument."""
    try:
        parameters = list(inspect.signature(validate).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters):
        return True
    positional = [
        p
        for p in parameters
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def _outcome(exc: BaseException) -> str:
    """The audit outcome of a refused attempt: its status class, never its text."""
    if isinstance(exc, FeatureNotEntitledError):
        return "not_entitled"
    status = int(getattr(exc, "status_code", 0) or 0)
    return {
        403: "refused",
        404: "not_found",
        409: "conflict",
        400: "invalid_or_missing",
        422: "invalid",
        429: "rate_limited",
    }.get(status, "unavailable")


class SecretBackendAdminService:
    """Configure secret backends and move catalog secret values between stores."""

    def __init__(
        self,
        get_logger: Callable[[], Any],
        *,
        entitlement_service: Callable[[], Any] | None = None,
        encrypt: Callable[[str], str] | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._entitlement_service = entitlement_service
        self._encrypt = encrypt

    # -- plumbing ----------------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database is not configured")
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(bl) as conn, conn.cursor() as cur:
            yield conn, cur

    @staticmethod
    def _require_admin(scope: ScopeContext) -> None:
        require_secret_storage_admin(scope)
        if scope.allowed_project_ids or scope.allowed_workspace_ids:
            # Storage serves every project of the account.
            raise ServiceNotFoundError(_NOT_FOUND)

    def _is_entitled(self, account_id: str, feature: str) -> bool:
        service = self._entitlement_service() if callable(self._entitlement_service) else None
        is_entitled = getattr(service, "is_entitled", None)
        if not callable(is_entitled) or not account_id:
            return False
        try:
            return bool(is_entitled(account_id, feature))
        except Exception:
            return False

    def _require_entitled(self, scope: ScopeContext, backend: Any) -> None:
        if not self._is_entitled(_text(scope.account_id), str(backend.feature).lower()):
            raise FeatureNotEntitledError(str(backend.feature))

    @staticmethod
    def _installed(kind: str) -> Any:
        backend = secret_backends.registered_backend(kind)
        if backend is None:
            raise ServiceUnavailableError("This kind of secret storage is not installed")
        return backend

    @staticmethod
    def _policy_entry(scope: ScopeContext, kind: str) -> dict[str, Any] | None:
        """The deployment's policy entry of ``kind`` for the account; refused -> 403."""
        try:
            return secret_backend_policy.kind_policy(scope.account_id, kind)
        except secret_backend_policy.SecretBackendPolicyRefused as exc:
            raise ServiceForbiddenError(str(exc)) from None

    @classmethod
    def _require_policy_allows(cls, scope: ScopeContext, config: dict[str, Any]) -> None:
        """A saved configuration is used only while the deployment's policy allows it."""
        entry = cls._policy_entry(scope, config["backend_kind"])
        try:
            secret_backend_policy.check_config(dict(config.get("non_secret_config") or {}), entry)
        except secret_backend_policy.SecretBackendPolicyRefused as exc:
            raise ServiceForbiddenError(str(exc)) from None

    @classmethod
    def _validated_config(cls, backend: Any, value: Any, scope: ScopeContext) -> dict[str, Any]:
        config = _json_object(value, label="config")
        if paths := _secret_looking_paths(config):
            raise ServiceValidationError(
                "config holds settings only; credentials come from the deployment"
                f" ({', '.join(sorted(paths))})"
            )
        entry = cls._policy_entry(scope, str(backend.kind))
        validate = getattr(backend, "validate_config", None)
        if callable(validate):
            try:
                validated = (
                    validate(dict(config), entry)
                    if _accepts_policy(validate)
                    else validate(dict(config))
                )
                config = _json_object(validated, label="config")
            except ValueError as exc:
                raise ServiceValidationError(f"config: {exc}") from exc
        try:
            secret_backend_policy.check_config(config, entry)
        except secret_backend_policy.SecretBackendPolicyRefused as exc:
            raise ServiceForbiddenError(str(exc)) from None
        return config

    @staticmethod
    def _validated_locator(backend: Any, value: Any) -> dict[str, Any]:
        locator = _json_object(value, label="locator")
        if not locator:
            raise ServiceValidationError("locator: where the secret is kept is required")
        validate = getattr(backend, "validate_locator", None)
        if callable(validate):
            try:
                locator = _json_object(validate(dict(locator)), label="locator")
            except ValueError as exc:
                raise ServiceValidationError(f"locator: {exc}") from exc
        return locator

    def _config_in_scope(
        self, cur: Any, scope: ScopeContext, config_id: Any, *, lock: bool = False
    ) -> dict[str, Any]:
        config = backend_repository.get_config(
            cur, account_id=scope.account_id, config_id=_text(config_id), for_update=lock
        )
        if not config:
            raise ServiceNotFoundError(_NOT_FOUND)
        return config

    def _usable_config(self, cur: Any, scope: ScopeContext, config_id: Any) -> tuple[Any, Any]:
        """A configuration a new value may go to: turned on, installed and entitled."""
        config = self._config_in_scope(cur, scope, config_id)
        if not config["enabled"]:
            raise ServiceConflictError("This secret storage configuration is turned off")
        backend = self._installed(config["backend_kind"])
        self._require_entitled(scope, backend)
        self._require_policy_allows(scope, config)
        return config, backend

    @staticmethod
    def _secret_in_scope(cur: Any, scope: ScopeContext, secret_id: Any) -> dict[str, Any]:
        secret = repository.get_secret(
            cur, account_id=scope.account_id, secret_id=_text(secret_id), for_update=True
        )
        if not secret:
            raise ServiceNotFoundError("Secret not found")
        return secret

    @staticmethod
    def _audit(
        cur: Any, scope: ScopeContext, command: str, target: dict[str, Any], **payload: Any
    ) -> None:
        kind, actor = actor_identity(scope)
        repository.record_audit(
            cur,
            account_id=scope.account_id,
            command_type=f"secret.{command}",
            actor_kind=kind,
            actor_id=actor,
            target=target,
            payload=payload,
        )

    def _encrypted(self, value: str) -> str:
        if self._encrypt is not None:
            return self._encrypt(value)
        from core.infrastructure.security.secret_codec import encrypt_secret

        return encrypt_secret(value)

    # -- configurations ------------------------------------------------------------

    def overview(self, scope: ScopeContext) -> dict[str, Any]:
        """Installed backend kinds, the account's configurations and its remote secrets."""
        self._require_admin(scope)
        with self._transaction() as (_conn, cur):
            configs = backend_repository.list_configs(cur, account_id=scope.account_id)
            located = backend_repository.list_catalog_locators(cur, account_id=scope.account_id)
        account_id = _text(scope.account_id)
        return {
            "backends": [
                _backend_view(
                    backend, entitled=self._is_entitled(account_id, str(backend.feature).lower())
                )
                for backend in secret_backends.registered_backends()
            ],
            "configs": [_config_view(row) for row in configs],
            "secrets": [
                {
                    "secret_id": row["secret_id"],
                    "config_id": row["config_id"],
                    "backend": row["backend_kind"],
                    "locator": row["locator"],
                }
                for row in located
            ],
        }

    def create_config(
        self,
        scope: ScopeContext,
        *,
        backend: Any,
        name: Any,
        config: Any = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        self._require_admin(scope)
        kind = _text(backend)
        installed = self._installed(kind)
        self._require_entitled(scope, installed)
        validated_name = _validated_name(name)
        validated_config = self._validated_config(installed, config, scope)
        _kind, actor = actor_identity(scope)
        import psycopg

        try:
            with self._transaction() as (_conn, cur):
                row = backend_repository.insert_config(
                    cur,
                    account_id=scope.account_id,
                    backend_kind=kind,
                    name=validated_name,
                    non_secret_config=validated_config,
                    enabled=bool(enabled),
                    actor=actor,
                )
                self._audit(
                    cur, scope, "storage_config_create", {"config_id": row["id"]}, backend=kind
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ServiceConflictError("A secret storage configuration has this name") from exc
        return _config_view(row)

    def update_config(
        self,
        scope: ScopeContext,
        config_id: Any,
        *,
        name: Any = None,
        config: Any = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Rename, change or turn a configuration on or off.

        Turning it off or renaming it needs no entitlement; changing its settings or
        turning it on does, like any new use of the backend.
        """
        self._require_admin(scope)
        _kind, actor = actor_identity(scope)
        import psycopg

        try:
            with self._transaction() as (_conn, cur):
                current = self._config_in_scope(cur, scope, config_id, lock=True)
                new_name = current["name"] if name is None else _validated_name(name)
                new_enabled = current["enabled"] if enabled is None else bool(enabled)
                new_config = dict(current["non_secret_config"])
                if config is not None or (new_enabled and not current["enabled"]):
                    backend = self._installed(current["backend_kind"])
                    self._require_entitled(scope, backend)
                    if config is not None:
                        new_config = self._validated_config(backend, config, scope)
                    else:
                        # Turning a configuration on: the deployment policy applies.
                        self._require_policy_allows(
                            scope, {**current, "non_secret_config": new_config}
                        )
                row = backend_repository.update_config(
                    cur,
                    account_id=scope.account_id,
                    config_id=current["id"],
                    name=new_name,
                    non_secret_config=new_config,
                    enabled=new_enabled,
                    actor=actor,
                )
                self._audit(
                    cur,
                    scope,
                    "storage_config_update",
                    {"config_id": current["id"]},
                    backend=current["backend_kind"],
                    enabled=new_enabled,
                    config_changed=new_config != current["non_secret_config"],
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ServiceConflictError("A secret storage configuration has this name") from exc
        secret_backends.invalidate_cached_secret(scope.account_id)
        return _config_view(row or current)

    def config_health(self, scope: ScopeContext, config_id: Any) -> dict[str, Any]:
        """Whether the configuration can serve values now; a short neutral reason if not."""
        self._require_admin(scope)
        target = {"config_id": _text(config_id)}
        self._consume_probe(scope, "health", target)
        with self._transaction() as (_conn, cur):
            config = self._config_in_scope(cur, scope, config_id)
        result = self._health_result(scope, config)
        self._audit_attempt(
            scope,
            "storage_health",
            {"config_id": config["id"]},
            outcome="healthy" if result["healthy"] else (result["code"] or "unhealthy"),
            backend=config["backend_kind"],
        )
        return result

    def _health_result(self, scope: ScopeContext, config: dict[str, Any]) -> dict[str, Any]:
        backend = secret_backends.registered_backend(config["backend_kind"])
        if backend is None:
            return {"config_id": config["id"], "healthy": False, "code": "not_installed"}
        if not config["enabled"]:
            return {"config_id": config["id"], "healthy": False, "code": "turned_off"}
        try:
            self._require_policy_allows(scope, config)
        except ServiceForbiddenError:
            return {"config_id": config["id"], "healthy": False, "code": "not_allowed"}
        context = secret_backends.backend_context(scope.account_id, _located(config, {}))
        try:
            report = backend.health(context)
            healthy = bool(getattr(report, "healthy", False))
            code = _text(getattr(report, "code", ""))[:64]
        except Exception:
            healthy, code = False, "unavailable"
        return {"config_id": config["id"], "healthy": healthy, "code": code}

    # -- probes: rate limit and audit ---------------------------------------------

    def _consume_probe(self, scope: ScopeContext, operation: str, target: dict[str, Any]) -> None:
        """One attempt of the account's ``operation`` window; 429 when it is spent.

        Adopting and checking health tell whether something exists in, or is
        reachable through, the storage, so each is limited per account. The limiter
        fails closed when its store is unavailable.
        """
        from core.application.settings.model_provider_verification_rate_limit import (
            consume_attempt_window,
        )

        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database is not configured")
        allowed, retry_after = consume_attempt_window(
            bl,
            keys=(f"secret-storage-{operation}:account:{_text(scope.account_id)}",),
            max_attempts=PROBE_MAX_ATTEMPTS,
            window_seconds=PROBE_WINDOW_SECONDS,
            unavailable_message="Secret storage checks are temporarily unavailable",
        )
        if not allowed:
            self._audit_attempt(scope, f"storage_{operation}", target, outcome="rate_limited")
            raise ServiceError(
                _PROBE_LIMITED_MESSAGE,
                status_code=429,
                payload={
                    "code": "rate_limited",
                    "message": _PROBE_LIMITED_MESSAGE,
                    "retry_after": retry_after,
                },
            )

    def _audit_attempt(
        self,
        scope: ScopeContext,
        command: str,
        target: dict[str, Any],
        *,
        outcome: str,
        **payload: Any,
    ) -> None:
        """Record an attempt and its outcome in its own transaction (kept on refusal)."""
        try:
            with self._transaction() as (_conn, cur):
                self._audit(cur, scope, command, target, outcome=outcome, **payload)
        except Exception:
            # The answer to the caller does not depend on the audit row; a failure to
            # write it is logged by the database layer.
            return

    # -- values --------------------------------------------------------------------

    def adopt_secret(
        self,
        scope: ScopeContext,
        *,
        name: Any,
        config_id: Any,
        locator: Any,
        description: Any = "",
    ) -> dict[str, Any]:
        """Make a secret that already exists in a backend a catalog secret; return metadata.

        The backend must be able to read it now. The value is discarded at once.
        """
        from core.application.secrets.service import (
            _validated_description,
            public_secret,
        )
        from core.application.secrets.service import (
            _validated_name as _validated_secret_name,
        )

        self._require_admin(scope)
        validated_name = _validated_secret_name(name)
        validated_description = _validated_description(description)
        _kind, actor = actor_identity(scope)
        target = {"config_id": _text(config_id)}
        self._consume_probe(scope, "adopt", target)
        try:
            return self._adopt(
                scope,
                actor=actor,
                name=validated_name,
                description=validated_description,
                config_id=config_id,
                locator=locator,
                public_secret=public_secret,
            )
        except Exception as exc:
            self._audit_attempt(scope, "storage_adopt", target, outcome=_outcome(exc))
            raise

    def _adopt(
        self,
        scope: ScopeContext,
        *,
        actor: str,
        name: str,
        description: str,
        config_id: Any,
        locator: Any,
        public_secret: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        validated_name, validated_description = name, description
        with self._transaction() as (_conn, cur):
            config, backend = self._usable_config(cur, scope, config_id)
            validated_locator = self._validated_locator(backend, locator)
            if backend_repository.locator_holder(
                cur,
                account_id=scope.account_id,
                config_id=config["id"],
                locator=validated_locator,
            ):
                raise ServiceConflictError(_TARGET_TAKEN_MESSAGE)
            try:
                secret_backends.read_remote_value(
                    scope.account_id,
                    "adoption",
                    _located(config, validated_locator),
                    use_cache=False,
                )
            except StoredSecretError as exc:
                raise _read_error(exc) from None
            secret = repository.insert_secret(
                cur,
                account_id=scope.account_id,
                name=validated_name,
                description=validated_description,
                actor=actor,
            )
            backend_repository.insert_locator(
                cur,
                account_id=scope.account_id,
                ref_key=secret["storage_ref_key"],
                config_id=config["id"],
                backend_kind=config["backend_kind"],
                locator=validated_locator,
                actor=actor,
            )
            self._audit(
                cur,
                scope,
                "storage_adopt",
                {"secret_id": secret["id"]},
                backend=config["backend_kind"],
                config_id=config["id"],
                outcome="adopted",
            )
        view = public_secret(secret, usage_count=0)
        view["storage"] = {"backend": config["backend_kind"], "config_id": config["id"]}
        return view

    def move_to_backend(
        self,
        scope: ScopeContext,
        secret_id: Any,
        *,
        config_id: Any,
        locator: Any,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Write a local value to a backend that accepts writes, then drop the local copy.

        A name another catalog secret is kept at is refused (409). A secret that
        already exists at that name in the storage is never overwritten unless
        ``replace`` is set: a backend with ``create_only`` writes only if absent;
        otherwise Core asks its ``exists`` or, without one, tries to read the name
        first.
        """
        self._require_admin(scope)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            config, backend = self._usable_config(cur, scope, config_id)
            if secret_backends.WRITE not in secret_backends.backend_capabilities(backend):
                raise ServiceValidationError(
                    "This secret storage does not accept new values; adopt the secret"
                    " that already exists there instead"
                )
            validated_locator = self._validated_locator(backend, locator)
            secret = self._secret_in_scope(cur, scope, secret_id)
            ref_key = secret["storage_ref_key"]
            if backend_repository.get_locator(cur, account_id=scope.account_id, ref_key=ref_key):
                raise ServiceConflictError("This secret is already kept in external storage")
            if backend_repository.locator_holder(
                cur,
                account_id=scope.account_id,
                config_id=config["id"],
                locator=validated_locator,
                exclude_ref_key=ref_key,
            ):
                raise ServiceConflictError(_TARGET_TAKEN_MESSAGE)
            create_only = not replace and secret_backends.CREATE_ONLY in (
                secret_backends.backend_capabilities(backend)
            )
            if not replace and not create_only:
                self._require_target_absent(backend, scope, config, validated_locator)
            try:
                value = LocalStoredSecretReader(conn).read(
                    SecretReadContext.for_account(scope.account_id), ref_key
                )
            except StoredSecretError as exc:
                raise ServiceConflictError("This secret has no stored value to move") from exc
            stored = self._write(
                backend, scope, config, validated_locator, value, create_only=create_only
            )
            backend_repository.insert_locator(
                cur,
                account_id=scope.account_id,
                ref_key=ref_key,
                config_id=config["id"],
                backend_kind=config["backend_kind"],
                locator=stored,
                actor=actor,
            )
            backend_repository.delete_local_value(cur, account_id=scope.account_id, ref_key=ref_key)
            repository.touch_value(
                cur, account_id=scope.account_id, secret_id=secret["id"], actor=actor
            )
            self._audit(
                cur,
                scope,
                "storage_move",
                {"secret_id": secret["id"]},
                to=config["backend_kind"],
                config_id=config["id"],
                replace=bool(replace),
            )
        secret_backends.invalidate_cached_secret(scope.account_id, ref_key)
        return {
            "secret_id": str(secret["id"]),
            "storage": {"backend": config["backend_kind"], "config_id": config["id"]},
        }

    def move_to_local(self, scope: ScopeContext, secret_id: Any) -> dict[str, Any]:
        """Copy a remote value into local storage and forget its locator.

        Needs no entitlement: data always comes back. A configuration that was turned
        off is still read for this. The remote secret is never deleted.
        """
        self._require_admin(scope)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id)
            ref_key = secret["storage_ref_key"]
            located = secret_backends.locate(conn, account_id=scope.account_id, ref_key=ref_key)
            if not located:
                raise ServiceConflictError("This secret is already kept locally")
            try:
                value = secret_backends.read_remote_value(
                    scope.account_id, ref_key, {**located, "enabled": True}, use_cache=False
                )
            except StoredSecretError as exc:
                raise _read_error(exc) from None
            from core.infrastructure.db.model_provider_store import upsert_secret_ref

            upsert_secret_ref(
                conn,
                account_id=scope.account_id,
                ref_key=ref_key,
                encrypted_value=self._encrypted(value),
            )
            backend_repository.delete_locator(cur, account_id=scope.account_id, ref_key=ref_key)
            repository.touch_value(
                cur, account_id=scope.account_id, secret_id=secret["id"], actor=actor
            )
            self._audit(
                cur,
                scope,
                "storage_move",
                {"secret_id": secret["id"]},
                to=secret_backends.LOCAL_BACKEND_KIND,
                **{"from": located["backend_kind"]},
            )
        secret_backends.invalidate_cached_secret(scope.account_id, ref_key)
        return {"secret_id": str(secret["id"]), "storage": {"backend": "local"}}

    def replace_remote_value(
        self, cur: Any, scope: ScopeContext, *, ref_key: str, located: dict[str, Any], value: str
    ) -> None:
        """Write a new value of a catalog secret a backend keeps (``secrets.value.write``)."""
        config = backend_repository.get_config(
            cur, account_id=scope.account_id, config_id=located["config_id"]
        )
        if not config or not config["enabled"]:
            raise ServiceConflictError("This secret's storage configuration is turned off")
        backend = self._installed(config["backend_kind"])
        if secret_backends.WRITE not in secret_backends.backend_capabilities(backend):
            raise ServiceConflictError(
                "This secret's value is kept in a secret storage that does not accept new"
                " values; change it there, or move it back to local storage first"
            )
        self._require_entitled(scope, backend)
        self._require_policy_allows(scope, config)
        stored = self._write(backend, scope, config, dict(located["locator"]), value)
        backend_repository.update_locator(
            cur, account_id=scope.account_id, ref_key=ref_key, locator=stored
        )

    @staticmethod
    def _require_target_absent(
        backend: Any, scope: ScopeContext, config: dict[str, Any], locator: dict[str, Any]
    ) -> None:
        """Refuse a first write to a name the storage already holds (409)."""
        context = secret_backends.backend_context(scope.account_id, _located(config, locator))
        exists = getattr(backend, "exists", None)
        try:
            if callable(exists):
                taken = bool(exists(context, dict(locator)))
            else:
                try:
                    backend.read(context, dict(locator))
                    taken = True
                except StoredSecretMissing:
                    taken = False
        except StoredSecretError as exc:
            raise ServiceConflictError(_TARGET_UNKNOWN_MESSAGE) from exc
        except Exception:
            raise ServiceConflictError(_TARGET_UNKNOWN_MESSAGE) from None
        if taken:
            raise ServiceConflictError(_TARGET_EXISTS_MESSAGE)

    @staticmethod
    def _write(
        backend: Any,
        scope: ScopeContext,
        config: dict[str, Any],
        locator: dict[str, Any],
        value: str,
        *,
        create_only: bool = False,
    ) -> dict[str, Any]:
        context = secret_backends.backend_context(scope.account_id, _located(config, locator))
        try:
            if create_only:
                stored = backend.write(context, dict(locator), value, create_only=True)
            else:
                stored = backend.write(context, dict(locator), value)
        except secret_backends.SecretBackendTargetExists:
            raise ServiceConflictError(_TARGET_EXISTS_MESSAGE) from None
        except StoredSecretError as exc:
            raise _read_error(exc) from None
        except Exception:
            raise ServiceUnavailableError("Secret storage refused the request") from None
        if stored is None:
            return dict(locator)
        return _json_object(dict(stored), label="locator")


__all__ = ["SecretBackendAdminService"]
