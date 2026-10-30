"""Typed application use cases for project-bound incoming webhooks."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from core.application.management.errors import ServiceNotFoundError
from core.application.webhooks.signature_verifiers import (
    WEBHOOK_SIGNATURE_AUTH_MODE,
    WebhookSignatureConfigError,
    normalize_webhook_signature_config,
    webhook_signature_reserved_headers,
)
from core.application.workflows.project_credential_versions import (
    WorkflowCredentialVersionConflict,
    guard_project_credential_versions,
)
from core.application.workflows.workflow_trigger_guards import (
    lock_entrypoint,
    workflow_trigger_transaction,
)

_MESSAGE_SECRET_STORAGE_IS_UNAVAILABLE = (  # pragma: allowlist secret - user-facing error text
    "Secret storage is unavailable"
)
_MESSAGE_WORKFLOW_ENTRYPOINT_NOT_FOUND = "Workflow entrypoint not found"

_AUTH_MODES = frozenset(
    {
        "shared_secret_header",
        "bearer_token",
        "basic_auth",
        "hmac_sha256",
        WEBHOOK_SIGNATURE_AUTH_MODE,
    }
)
_DEFAULT_CONTENT_TYPES = ("application/json",)


class WorkflowWebhookCreationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class WorkflowWebhookValidationError(WorkflowWebhookCreationError):
    def __init__(self, message: str) -> None:
        super().__init__("workflow_entrypoint_validation_failed", message, status_code=400)


@contextmanager
def _webhook_transaction(
    bl: Any, workflow_id: str, *, account_id: str = "", include_trashed: bool = False
) -> Iterator[Any]:
    try:
        with workflow_trigger_transaction(
            bl, workflow_id, account_id=account_id, include_trashed=include_trashed
        ) as cursor:
            yield cursor
    except WorkflowCredentialVersionConflict as exc:
        raise WorkflowWebhookCreationError(
            "workflow_credential_version_conflict", str(exc), status_code=409
        ) from exc
    except ServiceNotFoundError as exc:
        raise WorkflowWebhookCreationError(
            "workflow_entrypoint_not_found", _MESSAGE_WORKFLOW_ENTRYPOINT_NOT_FOUND, status_code=404
        ) from exc


def _text(value: Any) -> str:
    return str(value or "").strip()


def _auth_mode(value: Any) -> str:
    mode = _text(value or "bearer_token").lower() or "bearer_token"
    if mode not in _AUTH_MODES:
        raise WorkflowWebhookValidationError("Unsupported auth_mode")
    return mode


def _signing_secret(value: Any, *, mode: str) -> str:
    """Normalize a write-only operator-supplied signing secret.

    The value is never persisted in `auth_config_jsonb`, serialized, logged or
    revealed; it is handed straight to project secret storage.

    Importing a secret is only meaningful for `webhook_signature`, where the
    source service issues it. Accepting one for a reusable-secret mode would
    silently overwrite a generated credential the operator can no longer read
    back, so it is rejected rather than ignored.
    """
    secret = str(value or "").strip()
    if not secret:
        return ""
    if mode != WEBHOOK_SIGNATURE_AUTH_MODE:
        raise WorkflowWebhookValidationError(
            f"signing_secret is only supported for auth_mode '{WEBHOOK_SIGNATURE_AUTH_MODE}'"
        )
    if len(secret) > 4096:
        raise WorkflowWebhookValidationError("signing_secret is too long")
    if any(character in secret for character in "\r\n\t") or any(
        ord(character) < 32 for character in secret
    ):
        raise WorkflowWebhookValidationError("signing_secret must not contain control characters")
    return secret


def _secret_ref(entry_key: str, auth_mode: str) -> str:
    clean = "".join(
        character if character.isalnum() or character in {"_", "-", "."} else "_"
        for character in _text(entry_key)
    )
    return f"workflow_entrypoint.{clean or 'entrypoint'}.secret.{auth_mode}"


def _auth_config(
    mode: str,
    value: Any,
    *,
    entry_key: str,
    existing: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    payload = dict(value) if isinstance(value, Mapping) else {}
    current = dict(existing or {})
    if mode == "shared_secret_header":
        header_name = (
            _text(payload.get("header_name") or current.get("header_name") or "X-Workflow-Secret")
            or "X-Workflow-Secret"
        )
        reference = _text(current.get("secret_ref")) or _secret_ref(entry_key, mode)
        return {"header_name": header_name, "secret_ref": reference}, reference
    if mode == "bearer_token":
        reference = _text(current.get("secret_ref")) or _secret_ref(entry_key, mode)
        return {"secret_ref": reference}, reference
    if mode == "basic_auth":
        username = _text(payload.get("username") or current.get("username") or "workflow")
        reference = _text(current.get("password_secret_ref")) or _secret_ref(entry_key, mode)
        return {"username": username, "password_secret_ref": reference}, reference
    if mode == WEBHOOK_SIGNATURE_AUTH_MODE:
        reference = _text(current.get("secret_ref")) or _secret_ref(entry_key, mode)
        try:
            return (
                normalize_webhook_signature_config(payload or current, secret_ref=reference),
                reference,
            )
        except WebhookSignatureConfigError as exc:
            raise WorkflowWebhookValidationError(str(exc)) from exc
    header_name = (
        _text(payload.get("header_name") or current.get("header_name") or "X-Workflow-Signature")
        or "X-Workflow-Signature"
    )
    prefix = _text(payload.get("prefix") or current.get("prefix") or "sha256=")
    reference = _text(current.get("secret_ref")) or _secret_ref(entry_key, mode)
    return {"header_name": header_name, "prefix": prefix, "secret_ref": reference}, reference


def _content_types(value: Any, *, reject_empty: bool) -> tuple[str, ...]:
    if value is None:
        return _DEFAULT_CONTENT_TYPES
    if not isinstance(value, list):
        raise WorkflowWebhookValidationError("allowed_content_types must be an array")
    normalized = tuple(sorted({_text(item).lower() for item in value if _text(item)}))
    if reject_empty and not normalized:
        raise WorkflowWebhookValidationError("At least one allowed content type must be selected")
    return normalized


def _mapping(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkflowWebhookValidationError("input_mapping must be an object")
    return {
        target: source
        for key, raw_source in value.items()
        if (target := _text(key)) and (source := _text(raw_source))
    }


def _mapping_selector_allowed(source: str, auth_config: Mapping[str, Any]) -> bool:
    root, _, tail = _text(source).partition(".")
    if root != "headers":
        return root in {"body", "query", "form", "files"}
    header = _text(tail).lower()
    blocked = {
        "authorization",
        "cookie",
        "set-cookie",
        "idempotency-key",
        "x-request-id",
        "x-correlation-id",
        "x-api-key",
    }
    configured = {
        name
        for name in (
            _text(auth_config.get("header_name")).lower(),
            *webhook_signature_reserved_headers(auth_config),
        )
        if name
    }
    return (
        bool(header)
        and header not in blocked
        and "secret" not in header
        and header not in configured
    )


def _validated_mapping(
    value: Any,
    *,
    expected_input_names: set[str],
    auth_config: Mapping[str, Any],
) -> dict[str, str]:
    mapping = _mapping(value)
    invalid_targets = sorted(set(mapping) - expected_input_names)
    if invalid_targets:
        raise WorkflowWebhookValidationError(
            f"Unknown Expected Inputs in mapping: {', '.join(invalid_targets)}"
        )
    invalid_selectors = sorted(
        source for source in mapping.values() if not _mapping_selector_allowed(source, auth_config)
    )
    if invalid_selectors:
        raise WorkflowWebhookValidationError(
            f"Unsupported input mapping selectors: {', '.join(invalid_selectors)}"
        )
    return mapping


def _restrictions(value: Any) -> dict[str, list[str]]:
    payload = dict(value) if isinstance(value, Mapping) else {}
    return {
        "exact_ips": [
            _text(item)
            for item in (payload.get("exact_ips") or payload.get("ip_allowlist") or [])
            if _text(item)
        ],
        "cidrs": [
            _text(item)
            for item in (payload.get("cidrs") or payload.get("cidr_allowlist") or [])
            if _text(item)
        ],
    }


def _limit(value: Any, *, default: int, minimum: int, maximum: int | None = None) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    normalized = max(minimum, normalized)
    return min(normalized, maximum) if maximum is not None else normalized


def _response_mode(value: Any) -> str:
    mode = _text(value).lower()
    if mode in {"", "accepted", "async", "queued", "202"}:
        return "accepted"
    if mode in {"ok", "sync", "immediate", "200"}:
        return "ok"
    if mode in {"no_content", "empty", "204"}:
        return "no_content"
    raise WorkflowWebhookValidationError("Unsupported response_mode")


@dataclass(frozen=True)
class WorkflowWebhookCreateCommand:
    workflow_id: str
    project_id: str
    entry_key: str
    fields: dict[str, Any]
    auth_mode: str
    auth_config: dict[str, Any]
    signing_secret: str = ""

    @classmethod
    def from_request(
        cls,
        *,
        workflow_id: str,
        project_id: str,
        entry_key: str,
        request: Mapping[str, Any],
        expected_input_names: set[str],
    ) -> WorkflowWebhookCreateCommand:
        mode = _auth_mode(request.get("auth_mode"))
        config, reference = _auth_config(mode, request.get("auth_config"), entry_key=entry_key)
        mapping = _validated_mapping(
            request.get("input_mapping"),
            expected_input_names=expected_input_names,
            auth_config=config,
        )
        fields = {
            "entry_key": entry_key,
            "enabled": bool(request.get("enabled", True)),
            "auth_mode": mode,
            "auth_config_jsonb": config,
            "source_restrictions_jsonb": _restrictions(request.get("source_restrictions")),
            "allowed_content_types_jsonb": list(
                _content_types(request.get("allowed_content_types"), reject_empty=True)
            ),
            "max_body_bytes": _limit(
                request.get("max_body_bytes"), default=46137344, minimum=1, maximum=104857600
            ),
            "max_file_bytes": _limit(request.get("max_file_bytes"), default=10485760, minimum=1),
            "max_files": _limit(request.get("max_files"), default=4, minimum=0),
            "secret_ref": reference,
            "idempotency_mode": _text(request.get("idempotency_mode") or "none") or "none",
            "input_mapping_jsonb": mapping,
            "response_mode": _response_mode(request.get("response_mode")),
            "description": _text(request.get("description")),
        }
        return cls(
            workflow_id=_text(workflow_id),
            project_id=_text(project_id),
            entry_key=_text(entry_key),
            fields=fields,
            auth_mode=mode,
            auth_config=config,
            signing_secret=_signing_secret(request.get("signing_secret"), mode=mode),
        )


@dataclass(frozen=True)
class WorkflowWebhookUpdateCommand:
    workflow_id: str
    entry_key: str
    existing_project_id: str
    requested_project_id: str
    fields: dict[str, Any]
    auth_mode: str
    auth_config: dict[str, Any]
    rotate_secret: bool
    signing_secret: str = ""
    expected_entrypoint: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_request(
        cls,
        *,
        workflow_id: str,
        entry_key: str,
        existing: Mapping[str, Any],
        request: Mapping[str, Any],
        expected_input_names: set[str],
    ) -> WorkflowWebhookUpdateCommand:
        current_mode = _auth_mode(existing.get("auth_mode"))
        requested_mode = (
            existing.get("auth_mode")
            if request.get("auth_mode") is None
            else request.get("auth_mode")
        )
        mode = _auth_mode(requested_mode)
        raw_config = (
            existing.get("auth_config_jsonb")
            if request.get("auth_config") is None
            else request.get("auth_config")
        )
        config, reference = _auth_config(
            mode,
            raw_config,
            entry_key=entry_key,
            existing=existing.get("auth_config_jsonb") if mode == current_mode else None,
        )
        mapping_value = (
            existing.get("input_mapping_jsonb")
            if request.get("input_mapping") is None
            else request.get("input_mapping")
        )
        mapping = _validated_mapping(
            mapping_value,
            expected_input_names=expected_input_names,
            auth_config=config,
        )
        content_types_value = (
            existing.get("allowed_content_types_jsonb")
            if request.get("allowed_content_types") is None
            else request.get("allowed_content_types")
        )
        existing_project_id = _text(existing.get("project_id"))
        requested_project_id = _text(request.get("project_id")) or existing_project_id
        fields = {
            "enabled": bool(
                existing.get("enabled", True)
                if request.get("enabled") is None
                else request.get("enabled")
            ),
            "auth_mode": mode,
            "auth_config_jsonb": config,
            "source_restrictions_jsonb": _restrictions(
                existing.get("source_restrictions_jsonb")
                if request.get("source_restrictions") is None
                else request.get("source_restrictions")
            ),
            "allowed_content_types_jsonb": list(
                _content_types(
                    content_types_value,
                    reject_empty=request.get("allowed_content_types") is not None,
                )
            ),
            "max_body_bytes": _limit(
                existing.get("max_body_bytes")
                if request.get("max_body_bytes") is None
                else request.get("max_body_bytes"),
                default=46137344,
                minimum=1,
                maximum=104857600,
            ),
            "max_file_bytes": _limit(
                existing.get("max_file_bytes")
                if request.get("max_file_bytes") is None
                else request.get("max_file_bytes"),
                default=10485760,
                minimum=1,
            ),
            "max_files": _limit(
                existing.get("max_files")
                if request.get("max_files") is None
                else request.get("max_files"),
                default=4,
                minimum=0,
            ),
            "secret_ref": reference,
            "idempotency_mode": _text(
                existing.get("idempotency_mode")
                if request.get("idempotency_mode") is None
                else request.get("idempotency_mode")
            )
            or "none",
            "input_mapping_jsonb": mapping,
            "response_mode": _response_mode(
                existing.get("response_mode")
                if request.get("response_mode") is None
                else request.get("response_mode")
            ),
            "description": _text(
                existing.get("description")
                if request.get("description") is None
                else request.get("description")
            ),
        }
        return cls(
            workflow_id=_text(workflow_id),
            entry_key=_text(entry_key),
            existing_project_id=existing_project_id,
            expected_entrypoint=dict(existing),
            requested_project_id=requested_project_id,
            fields=fields,
            auth_mode=mode,
            auth_config=config,
            rotate_secret=mode != current_mode or requested_project_id != existing_project_id,
            signing_secret=_signing_secret(request.get("signing_secret"), mode=mode),
        )

    @classmethod
    def for_rotation(
        cls,
        *,
        workflow_id: str,
        entry_key: str,
        existing: Mapping[str, Any],
        project_id: str | None,
        expected_input_names: set[str],
    ) -> WorkflowWebhookUpdateCommand:
        command = cls.from_request(
            workflow_id=workflow_id,
            entry_key=entry_key,
            existing=existing,
            request={"project_id": project_id},
            expected_input_names=expected_input_names,
        )
        rotated_command: WorkflowWebhookUpdateCommand = replace(command, rotate_secret=True)
        return rotated_command


class WorkflowWebhookCreationService:
    def __init__(
        self,
        *,
        resolve_project_binding: Callable[[Any, str, str], tuple[str | None, dict[str, Any]]],
        create_entrypoint: Callable[..., dict[str, Any]],
    ) -> None:
        self._resolve_project_binding = resolve_project_binding
        self._create_entrypoint = create_entrypoint

    def create(
        self,
        *,
        bl: Any,
        command: WorkflowWebhookCreateCommand,
        authorize_project: Callable[[str], Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        project_id, _ = self._resolve_project_binding(bl, command.workflow_id, command.project_id)
        if not project_id:
            raise WorkflowWebhookCreationError(
                "workflow_project_not_linked",
                "Workflow is not linked to the selected project",
                status_code=409,
            )
        if authorize_project is not None:
            authorize_project(str(project_id))
        get_project = getattr(bl, "get_project", None)
        if not callable(get_project):
            raise WorkflowWebhookCreationError(
                "workflow_project_lookup_unavailable",
                "Workflow project storage is unavailable; retry later",
                status_code=503,
            )
        try:
            project = get_project(str(project_id))
        except Exception as exc:
            raise WorkflowWebhookCreationError(
                "workflow_project_lookup_unavailable",
                "Workflow project storage is unavailable; retry later",
                status_code=503,
            ) from exc
        account_id = _text(project.get("account_id")) if isinstance(project, Mapping) else ""
        if not account_id:
            raise WorkflowWebhookCreationError(
                "workflow_project_account_unresolved",
                "Workflow project account is unavailable",
                status_code=409,
            )
        save_secret = getattr(bl, "save_project_secret", None)
        if not callable(save_secret):
            raise WorkflowWebhookCreationError(
                "secret_storage_unavailable",
                _MESSAGE_SECRET_STORAGE_IS_UNAVAILABLE,
                status_code=503,
            )
        conn = getattr(bl, "conn", None)
        if conn is None:
            raise WorkflowWebhookCreationError(
                "transactional_storage_unavailable",
                "transactional storage is unavailable",
                status_code=503,
            )
        imported_secret = command.signing_secret
        secret_value = imported_secret or secrets.token_urlsafe(24)
        with _webhook_transaction(bl, command.workflow_id, account_id=account_id) as cursor:
            guard_project_credential_versions(
                cursor, project_id=str(project_id), secret_key=str(command.fields["secret_ref"])
            )
            stored = save_secret(
                project_id=str(project_id),
                secret_key=str(command.fields["secret_ref"]),
                secret_value=secret_value,
            )
            if stored is False:
                raise WorkflowWebhookCreationError(
                    "secret_storage_unavailable",
                    _MESSAGE_SECRET_STORAGE_IS_UNAVAILABLE,
                    status_code=503,
                )
            created = self._create_entrypoint(
                bl,
                workflow_id=command.workflow_id,
                project_id=str(project_id),
                account_id=account_id,
                entry_kind="webhook",
                **command.fields,
            )
        # An operator-supplied secret is never echoed back: the caller already has
        # it, and returning it would put a provider credential in a response body.
        return created, ("" if imported_secret else secret_value)


class WorkflowWebhookUpdateService:
    def __init__(
        self,
        *,
        resolve_project_binding: Callable[[Any, str, str], tuple[str | None, dict[str, Any]]],
        update_entrypoint: Callable[..., dict[str, Any]],
    ) -> None:
        self._resolve_project_binding = resolve_project_binding
        self._update_entrypoint = update_entrypoint

    def update(
        self,
        *,
        bl: Any,
        command: WorkflowWebhookUpdateCommand,
        authorize_project: Callable[[str], Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        project_id, _ = self._resolve_project_binding(
            bl, command.workflow_id, command.requested_project_id
        )
        if not project_id:
            raise WorkflowWebhookCreationError(
                "workflow_project_not_linked",
                "Workflow is not linked to the selected project",
                status_code=409,
            )
        if authorize_project is not None:
            authorize_project(str(project_id))
        imported_secret = command.signing_secret
        # Replacing an imported secret is a deliberate write that is *not* a
        # rotation: updating non-secret signature settings leaves the stored
        # credential untouched.
        writes_secret = bool(imported_secret) or command.rotate_secret
        save_secret = getattr(bl, "save_project_secret", None)
        if writes_secret and not callable(save_secret):
            raise WorkflowWebhookCreationError(
                "secret_storage_unavailable",
                _MESSAGE_SECRET_STORAGE_IS_UNAVAILABLE,
                status_code=503,
            )
        secret_value = imported_secret or (
            secrets.token_urlsafe(24) if command.rotate_secret else None
        )
        conn = getattr(bl, "conn", None)
        if conn is None:
            raise WorkflowWebhookCreationError(
                "transactional_storage_unavailable",
                "transactional storage is unavailable",
                status_code=503,
            )
        with _webhook_transaction(bl, command.workflow_id) as cursor:
            current = lock_entrypoint(
                cursor, entry_key=command.entry_key, workflow_id=command.workflow_id
            )
            if any(
                current.get(name) != value for name, value in command.expected_entrypoint.items()
            ):
                raise WorkflowWebhookCreationError(
                    "workflow_entrypoint_changed",
                    "Workflow entrypoint changed; reload and retry",
                    status_code=409,
                )
            if str(current.get("project_id") or "") != command.existing_project_id:
                raise WorkflowWebhookCreationError(
                    "workflow_entrypoint_changed",
                    "Workflow entrypoint changed; reload and retry",
                    status_code=409,
                )
            if secret_value is not None and callable(save_secret):
                guard_project_credential_versions(
                    cursor, project_id=str(project_id), secret_key=str(command.fields["secret_ref"])
                )
                stored = save_secret(
                    project_id=str(project_id),
                    secret_key=str(command.fields["secret_ref"]),
                    secret_value=secret_value,
                )
                if stored is False:
                    raise WorkflowWebhookCreationError(
                        "secret_storage_unavailable",
                        _MESSAGE_SECRET_STORAGE_IS_UNAVAILABLE,
                        status_code=503,
                    )
            updated = self._update_entrypoint(
                bl,
                entry_key=command.entry_key,
                project_id=str(project_id),
                **command.fields,
            )
        return updated, (None if imported_secret else secret_value)


class WorkflowWebhookMutationService:
    """Application-owned non-secret webhook entrypoint mutations."""

    def __init__(
        self,
        *,
        regenerate_entry_key: Callable[..., dict[str, Any]],
        delete_entrypoint: Callable[..., bool],
    ) -> None:
        self._regenerate_entry_key = regenerate_entry_key
        self._delete_entrypoint = delete_entrypoint

    def regenerate(self, *, bl: Any, entry_key: str, next_entry_key: str) -> dict[str, Any]:
        from core.application.workflows.workflow_entrypoint_repository import (
            load_workflow_entrypoint,
        )

        existing = load_workflow_entrypoint(bl, entry_key)
        with _webhook_transaction(bl, str(existing.get("workflow_id") or "")) as cursor:
            lock_entrypoint(
                cursor, entry_key=entry_key, workflow_id=str(existing.get("workflow_id") or "")
            )
            return self._regenerate_entry_key(
                bl, entry_key=str(entry_key), next_entry_key=str(next_entry_key)
            )

    def delete(
        self,
        *,
        bl: Any,
        entry_key: str,
        workflow_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        from core.application.workflows.workflow_credential_retirement import (
            retire_entrypoint_credentials,
        )
        from core.application.workflows.workflow_entrypoint_repository import (
            load_workflow_entrypoint,
        )

        existing = load_workflow_entrypoint(bl, entry_key, include_trashed=True)
        owner_id = str(existing.get("workflow_id") or "")
        if workflow_id is not None and workflow_id != owner_id:
            raise WorkflowWebhookCreationError(
                "workflow_entrypoint_not_found",
                _MESSAGE_WORKFLOW_ENTRYPOINT_NOT_FOUND,
                status_code=404,
            )
        with _webhook_transaction(bl, owner_id, include_trashed=True) as cursor:
            current = lock_entrypoint(cursor, entry_key=entry_key, workflow_id=owner_id)
            if project_id is not None and project_id != str(current.get("project_id") or ""):
                raise WorkflowWebhookCreationError(
                    "workflow_entrypoint_not_found",
                    _MESSAGE_WORKFLOW_ENTRYPOINT_NOT_FOUND,
                    status_code=404,
                )
            retire_entrypoint_credentials(cursor, [current])
            return bool(self._delete_entrypoint(bl, entry_key=str(entry_key)))


def create_workflow_webhook_creation_service(
    *, resolve_project_binding: Callable[[Any, str, str], tuple[str | None, dict[str, Any]]]
) -> WorkflowWebhookCreationService:
    from core.application.workflows import workflow_entrypoint_repository

    return WorkflowWebhookCreationService(
        resolve_project_binding=resolve_project_binding,
        create_entrypoint=workflow_entrypoint_repository.create_workflow_entrypoint,
    )


def create_workflow_webhook_update_service(
    *, resolve_project_binding: Callable[[Any, str, str], tuple[str | None, dict[str, Any]]]
) -> WorkflowWebhookUpdateService:
    from core.application.workflows import workflow_entrypoint_repository

    return WorkflowWebhookUpdateService(
        resolve_project_binding=resolve_project_binding,
        update_entrypoint=workflow_entrypoint_repository.update_workflow_entrypoint,
    )


def create_workflow_webhook_mutation_service() -> WorkflowWebhookMutationService:
    from core.application.workflows import workflow_entrypoint_repository

    return WorkflowWebhookMutationService(
        regenerate_entry_key=workflow_entrypoint_repository.regenerate_workflow_entrypoint_key,
        delete_entrypoint=workflow_entrypoint_repository.delete_workflow_entrypoint,
    )


__all__ = [
    "WorkflowWebhookCreateCommand",
    "WorkflowWebhookCreationError",
    "WorkflowWebhookCreationService",
    "WorkflowWebhookMutationService",
    "WorkflowWebhookUpdateCommand",
    "WorkflowWebhookUpdateService",
    "WorkflowWebhookValidationError",
    "create_workflow_webhook_creation_service",
    "create_workflow_webhook_mutation_service",
    "create_workflow_webhook_update_service",
]
