"""Atomic lifecycle use case for write-only workflow input secret defaults."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.registry.workflow_secret_defaults import (
    SecretDefaultRejected,
    secret_default_ref_belongs_to,
    secret_default_ref_owner_prefix,
    secret_default_workflow_owner_prefix,
    set_secret_default,
)

_MESSAGE_ACCESS_DENIED = "Access denied."
_MESSAGE_WORKFLOW_SECRET_STORAGE_IS_UNAVAILABLE = (  # pragma: allowlist secret - user-facing error text
    "Workflow secret storage is unavailable."
)


class WorkflowSecretDefaultRepository(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None: ...

    def save_workflow(
        self,
        data: dict[str, Any],
        *,
        author: str,
        expected_revision: int | None,
    ) -> int: ...

    def encrypt(self, plaintext: str) -> str: ...

    def write_ciphertext(
        self,
        transaction: Any,
        *,
        account_id: str,
        ref_key: str,
        ciphertext: str,
    ) -> None: ...

    def delete_owner_ciphertexts(
        self,
        transaction: Any,
        *,
        account_id: str,
        owner_prefix: str,
        keep_ref_key: str = "",
    ) -> None: ...

    def purge_workflow_secrets(
        self,
        transaction: Any,
        *,
        account_id: str,
        owner_prefix: str,
    ) -> None: ...


class WorkflowSecretMutationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.current_revision = current_revision

    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.current_revision is not None:
            payload["current_revision"] = self.current_revision
        return payload


class WorkflowMetadataValidationError(ValueError):
    def __init__(self, validation: dict[str, Any]) -> None:
        super().__init__("Workflow metadata validation failed.")
        self.validation = validation


def _text(value: Any) -> str:
    return str(value or "").strip()


def _secret_input_row(existing: dict[str, Any], field_name: str) -> dict[str, Any]:
    settings = existing.get("settings")
    callable_block = settings.get("callable") if isinstance(settings, dict) else None
    rows = callable_block.get("expected_inputs") if isinstance(callable_block, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or _text(row.get("name")) != field_name:
            continue
        if _text(row.get("type")).lower() != "secret":
            raise WorkflowSecretMutationError(
                f"Workflow input '{field_name}' is not a secret input.",
                code="workflow_input_not_secret",
                status_code=400,
            )
        return row
    raise WorkflowSecretMutationError(
        f"Workflow has no input named '{field_name}'.",
        code="workflow_input_not_found",
        status_code=404,
    )


def _guard_revision(expected_revision: int | None, existing: dict[str, Any]) -> int | None:
    current_revision = existing.get("revision")
    if current_revision is None:
        return None
    if expected_revision is None:
        raise WorkflowSecretMutationError(
            "Reload the workflow and retry with expected_revision.",
            code="workflow_revision_required",
            status_code=428,
            current_revision=int(current_revision),
        )
    if int(expected_revision) < 1:
        raise WorkflowSecretMutationError(
            "expected_revision must be a positive integer.",
            code="workflow_revision_invalid",
            status_code=400,
        )
    return int(expected_revision)


def normalize_disabled_workflow_validation(
    validation: dict[str, Any] | None,
    *,
    final_enabled: bool,
) -> dict[str, Any]:
    normalized = dict(validation or {})
    if final_enabled:
        return normalized
    errors = [dict(item) for item in (normalized.get("errors") or []) if isinstance(item, dict)]
    filtered_errors = [
        issue
        for issue in errors
        if _text(issue.get("code")) != "workflow.execution_bindings_missing"
    ]
    if len(filtered_errors) == len(errors):
        return normalized
    normalized["errors"] = filtered_errors
    normalized["valid"] = not filtered_errors
    return normalized


class _WorkflowValidationFailure(Exception):
    """Carry a validation failure through repository rollback without relabeling it."""

    def __init__(self, error: Exception) -> None:
        super().__init__("Workflow validation failed")
        self.error = error


class WorkflowSecretDefaultService:
    """Set, replace, or clear one secret and its workflow reference atomically."""

    def __init__(self, repository: WorkflowSecretDefaultRepository) -> None:
        self.repository = repository

    def purge_workflow_secrets(
        self,
        *,
        workflow_id: str,
        account_id: str,
        transaction: Any,
    ) -> None:
        """Purge every ciphertext version owned by one account-scoped workflow."""
        normalized_workflow_id = _text(workflow_id)
        normalized_account_id = _text(account_id)
        if not normalized_workflow_id or not normalized_account_id or transaction is None:
            raise WorkflowSecretMutationError(
                _MESSAGE_ACCESS_DENIED,
                code="workflow_secret_scope_forbidden",
                status_code=403,
            )
        try:
            self.repository.purge_workflow_secrets(
                transaction,
                account_id=normalized_account_id,
                owner_prefix=secret_default_workflow_owner_prefix(normalized_workflow_id),
            )
        except (SecretDefaultRejected, WorkflowSecretMutationError):
            raise
        except Exception as exc:
            raise WorkflowSecretMutationError(
                _MESSAGE_WORKFLOW_SECRET_STORAGE_IS_UNAVAILABLE,
                code="workflow_secret_storage_unavailable",
                status_code=503,
            ) from exc

    @contextmanager
    def _mutation_transaction(self) -> Iterator[Any]:
        try:
            with self.repository.transaction() as transaction:
                yield transaction
        except _WorkflowValidationFailure as exc:
            raise exc.error from exc.error.__cause__
        except (
            SecretDefaultRejected,
            WorkflowMetadataValidationError,
            WorkflowRevisionConflictError,
            WorkflowSecretMutationError,
        ):
            raise
        except Exception as exc:
            raise WorkflowSecretMutationError(
                _MESSAGE_WORKFLOW_SECRET_STORAGE_IS_UNAVAILABLE,
                code="workflow_secret_storage_unavailable",
                status_code=503,
            ) from exc

    def _load_for_mutation(
        self,
        *,
        workflow_id: str,
        field_name: str,
        account_id: str,
        expected_revision: int | None,
    ) -> tuple[dict[str, Any], dict[str, Any], int | None]:
        existing = self.repository.get_workflow(workflow_id)
        if not isinstance(existing, dict) or _text(existing.get("workflow_id")) != workflow_id:
            raise WorkflowSecretMutationError(
                "Workflow was not found.",
                code="workflow_not_found",
                status_code=404,
            )
        if not account_id:
            raise WorkflowSecretMutationError(
                _MESSAGE_ACCESS_DENIED,
                code="workflow_secret_scope_forbidden",
                status_code=403,
            )
        stored_account_id = _text(existing.get("account_id"))
        if stored_account_id and stored_account_id != account_id:
            raise WorkflowSecretMutationError(
                _MESSAGE_ACCESS_DENIED,
                code="workflow_secret_scope_forbidden",
                status_code=403,
            )
        mutable = copy.deepcopy(existing)
        row = _secret_input_row(mutable, field_name)
        guarded_revision = _guard_revision(expected_revision, mutable)
        previous_ref = _text(row.get("default_secret_ref"))
        if previous_ref and not secret_default_ref_belongs_to(
            previous_ref,
            workflow_id=workflow_id,
            field_name=field_name,
        ):
            raise SecretDefaultRejected("stored secret reference ownership is invalid")
        return mutable, row, guarded_revision

    @staticmethod
    def _secret_fields(settings: Any) -> dict[str, str]:
        callable_block = settings.get("callable") if isinstance(settings, dict) else None
        rows = callable_block.get("expected_inputs") if isinstance(callable_block, dict) else None
        fields: dict[str, str] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or _text(row.get("type")).lower() != "secret":
                continue
            name = _text(row.get("name"))
            if name:
                fields[name] = _text(row.get("default_secret_ref"))
        return fields

    def _cleanup_displaced_secret_fields(
        self,
        transaction: Any,
        *,
        workflow_id: str,
        account_id: str,
        previous_settings: Any,
        final_settings: Any,
    ) -> None:
        """Delete secret versions whose workflow-field authority left final settings."""
        normalized_workflow_id = _text(workflow_id)
        normalized_account_id = _text(account_id)
        if not normalized_workflow_id or not normalized_account_id:
            raise WorkflowSecretMutationError(
                _MESSAGE_ACCESS_DENIED,
                code="workflow_secret_scope_forbidden",
                status_code=403,
            )
        previous_fields = self._secret_fields(previous_settings)
        final_fields = self._secret_fields(final_settings)
        displaced_fields = sorted(set(previous_fields) - set(final_fields))
        try:
            for field_name, ref_key in (*previous_fields.items(), *final_fields.items()):
                if ref_key and not secret_default_ref_belongs_to(
                    ref_key,
                    workflow_id=normalized_workflow_id,
                    field_name=field_name,
                ):
                    raise SecretDefaultRejected("stored secret reference ownership is invalid")
            for field_name in displaced_fields:
                self.repository.delete_owner_ciphertexts(
                    transaction,
                    account_id=normalized_account_id,
                    owner_prefix=secret_default_ref_owner_prefix(
                        normalized_workflow_id, field_name
                    ),
                )
        except (SecretDefaultRejected, WorkflowSecretMutationError):
            raise
        except Exception as exc:
            raise WorkflowSecretMutationError(
                _MESSAGE_WORKFLOW_SECRET_STORAGE_IS_UNAVAILABLE,
                code="workflow_secret_storage_unavailable",
                status_code=503,
            ) from exc

    def save_workflow_metadata(
        self,
        *,
        workflow_id: str,
        account_id: str,
        existing: dict[str, Any],
        final_payload: dict[str, Any],
        author: str,
        expected_revision: int | None,
        validate_saved: Callable[[], dict[str, Any]],
        final_enabled: bool,
    ) -> tuple[int, dict[str, Any]]:
        """Persist final metadata, validate it, and retire displaced secrets atomically."""
        with self._mutation_transaction() as transaction:
            saved_revision = self.repository.save_workflow(
                final_payload,
                author=author,
                expected_revision=expected_revision,
            )
            try:
                validation = normalize_disabled_workflow_validation(
                    validate_saved(),
                    final_enabled=final_enabled,
                )
            except Exception as exc:
                raise _WorkflowValidationFailure(exc) from exc
            if final_enabled and not bool(validation.get("valid", False)):
                raise WorkflowMetadataValidationError(validation)
            self._cleanup_displaced_secret_fields(
                transaction,
                workflow_id=workflow_id,
                account_id=account_id,
                previous_settings=existing.get("settings"),
                final_settings=final_payload.get("settings"),
            )
        return int(saved_revision), validation

    def set_secret(
        self,
        *,
        workflow_id: str,
        field_name: str,
        plaintext: str,
        account_id: str,
        author: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        normalized_workflow_id = _text(workflow_id)
        normalized_field_name = _text(field_name)
        normalized_account_id = _text(account_id)
        with self._mutation_transaction() as transaction:
            existing, row, guarded_revision = self._load_for_mutation(
                workflow_id=normalized_workflow_id,
                field_name=normalized_field_name,
                account_id=normalized_account_id,
                expected_revision=expected_revision,
            )
            pending: dict[str, str] = {}
            ref_key = set_secret_default(
                workflow_id=normalized_workflow_id,
                field_name=normalized_field_name,
                plaintext=plaintext,
                account_id=normalized_account_id,
                encrypt=self.repository.encrypt,
                write_ciphertext=lambda account, key, ciphertext: pending.update(
                    {"account_id": account, "ref_key": key, "ciphertext": ciphertext}
                ),
                version_token=(str(guarded_revision + 1) if guarded_revision is not None else ""),
            )
            row["default_secret_ref"] = ref_key
            row.pop("default", None)
            saved_revision = self.repository.save_workflow(
                existing,
                author=author,
                expected_revision=guarded_revision,
            )
            self.repository.write_ciphertext(
                transaction,
                account_id=pending["account_id"],
                ref_key=pending["ref_key"],
                ciphertext=pending["ciphertext"],
            )
            self.repository.delete_owner_ciphertexts(
                transaction,
                account_id=normalized_account_id,
                owner_prefix=secret_default_ref_owner_prefix(
                    normalized_workflow_id, normalized_field_name
                ),
                keep_ref_key=ref_key,
            )
        return {
            "workflow_id": normalized_workflow_id,
            "name": normalized_field_name,
            "default_configured": True,
            "revision": int(saved_revision),
        }

    def clear_secret(
        self,
        *,
        workflow_id: str,
        field_name: str,
        account_id: str,
        author: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        normalized_workflow_id = _text(workflow_id)
        normalized_field_name = _text(field_name)
        normalized_account_id = _text(account_id)
        with self._mutation_transaction() as transaction:
            existing, row, guarded_revision = self._load_for_mutation(
                workflow_id=normalized_workflow_id,
                field_name=normalized_field_name,
                account_id=normalized_account_id,
                expected_revision=expected_revision,
            )
            row.pop("default_secret_ref", None)
            row.pop("default", None)
            saved_revision = self.repository.save_workflow(
                existing,
                author=author,
                expected_revision=guarded_revision,
            )
            self.repository.delete_owner_ciphertexts(
                transaction,
                account_id=normalized_account_id,
                owner_prefix=secret_default_ref_owner_prefix(
                    normalized_workflow_id, normalized_field_name
                ),
            )
        return {
            "workflow_id": normalized_workflow_id,
            "name": normalized_field_name,
            "default_configured": False,
            "revision": int(saved_revision),
        }


__all__ = [
    "WorkflowMetadataValidationError",
    "WorkflowSecretDefaultRepository",
    "WorkflowSecretDefaultService",
    "WorkflowSecretMutationError",
    "normalize_disabled_workflow_validation",
]
