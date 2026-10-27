"""Provision the dedicated account-scoped worker grounding identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

WORKER_SERVICE_ACCOUNT_KEY = (
    "flow-steward-worker"  # pragma: allowlist secret - account key NAME, not a credential
)
WORKER_GROUNDING_SCOPES = ("projects.read",)


class WorkerIdentityStore(Protocol):
    def resolve_machine_token(self, token: str) -> dict[str, object] | None: ...

    def create_service_account(self, **kwargs: object) -> dict[str, object] | None: ...

    def create_machine_token(self, **kwargs: object) -> dict[str, object]: ...

    def revoke_machine_token(
        self, token_id: str, account_id: str | None = None
    ) -> dict[str, object] | None: ...

    def list_service_account_tokens(
        self,
        service_account_key: str,
        account_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[dict[str, object]]: ...


class WorkerIdentitySecretStore(Protocol):
    def read(self) -> str: ...

    def replace(self, value: str) -> None: ...


@dataclass(frozen=True)
class WorkerIdentityBootstrapResult:
    rotated: bool
    reason: str


class WorkerIdentityBootstrapService:
    """Keep one least-privilege, future-project-capable worker credential valid."""

    def __init__(
        self,
        *,
        store: WorkerIdentityStore,
        secret_store: WorkerIdentitySecretStore,
    ) -> None:
        self._store = store
        self._secret_store = secret_store

    def ensure(self, *, account_id: str) -> WorkerIdentityBootstrapResult:
        normalized_account = str(account_id or "").strip()
        if not normalized_account:
            raise ValueError("account_id is required")

        current_token = self._secret_store.read().strip()
        principal = self._store.resolve_machine_token(current_token) if current_token else None
        if self._is_valid(principal, account_id=normalized_account):
            return WorkerIdentityBootstrapResult(rotated=False, reason="identity_valid")

        old_token_id = str((principal or {}).get("token_id") or "").strip()
        self._store.create_service_account(
            service_account_key=WORKER_SERVICE_ACCOUNT_KEY,
            display_name="Flow Steward Worker",
            description="Installer-managed identity for worker MCP project grounding.",
            scopes=list(WORKER_GROUNDING_SCOPES),
            allowed_project_ids=[],
            allowed_workflow_ids=[],
            account_id=normalized_account,
            created_by="installer",
            is_active=True,
        )
        issued = self._store.create_machine_token(
            service_account_key=WORKER_SERVICE_ACCOUNT_KEY,
            label="installer-managed-worker",
            scopes=list(WORKER_GROUNDING_SCOPES),
            allowed_project_ids=[],
            allowed_workflow_ids=[],
            account_id=normalized_account,
            created_by="installer",
        )
        new_token_id = str(issued.get("id") or "").strip()
        new_token = str(issued.pop("token", "") or "").strip()
        if not new_token_id or not new_token:
            raise RuntimeError("worker identity issuance returned no credential")
        try:
            self._secret_store.replace(new_token)
        except Exception:
            self._store.revoke_machine_token(new_token_id, account_id=normalized_account)
            raise
        self._revoke_superseded_tokens(
            account_id=normalized_account,
            keep_token_id=new_token_id,
            resolved_old_token_id=old_token_id,
        )
        return WorkerIdentityBootstrapResult(rotated=True, reason="identity_rotated")

    def _revoke_superseded_tokens(
        self,
        *,
        account_id: str,
        keep_token_id: str,
        resolved_old_token_id: str,
    ) -> None:
        """Leave exactly one active installer-managed worker credential behind.

        Revoking only the token we happened to be holding was not enough: the id
        comes from resolving the secret store's current value, so it is empty
        whenever that value is missing or no longer resolvable — a reinstall, a
        lost env file, or a fresh deployment env pointed at an existing database.
        In exactly those cases the previously issued token stayed active forever,
        so worker credentials accumulated and every one of them kept working.

        Enumerate the service account's tokens instead, and revoke everything
        except the one just issued.
        """
        lister = getattr(self._store, "list_service_account_tokens", None)
        if not callable(lister):
            # Older store without enumeration: fall back to the single known id
            # rather than silently revoking nothing.
            if resolved_old_token_id and resolved_old_token_id != keep_token_id:
                self._store.revoke_machine_token(resolved_old_token_id, account_id=account_id)
            return
        existing = lister(WORKER_SERVICE_ACCOUNT_KEY, account_id=account_id) or []
        for row in existing:
            token_id = str((row or {}).get("id") or "").strip()
            if not token_id or token_id == keep_token_id:
                continue
            self._store.revoke_machine_token(token_id, account_id=account_id)

    @staticmethod
    def _is_valid(principal: dict[str, object] | None, *, account_id: str) -> bool:
        return is_valid_worker_identity(principal, account_id=account_id)


def is_valid_worker_identity(principal: dict[str, object] | None, *, account_id: str) -> bool:
    """Validate the installer-managed worker principal without rotating it."""
    if not principal:
        return False
    raw_scopes = principal.get("scopes", [])
    scope_values = raw_scopes if isinstance(raw_scopes, (list, tuple, set, frozenset)) else ()
    scopes = {str(value or "").strip() for value in scope_values}
    raw_project_ids = principal.get("allowed_project_ids", [])
    project_id_values = (
        raw_project_ids if isinstance(raw_project_ids, (list, tuple, set, frozenset)) else ()
    )
    project_ids = [
        str(value or "").strip() for value in project_id_values if str(value or "").strip()
    ]
    scope_granted = bool(
        {"projects.read", "projects:*", "read:*", "admin:all"}.intersection(scopes)
    )
    return bool(
        str(principal.get("account_id") or "").strip() == str(account_id or "").strip()
        and str(principal.get("service_account_key") or "").strip() == WORKER_SERVICE_ACCOUNT_KEY
        and scope_granted
        and not project_ids
    )


__all__ = [
    "WORKER_GROUNDING_SCOPES",
    "WORKER_SERVICE_ACCOUNT_KEY",
    "WorkerIdentityBootstrapResult",
    "WorkerIdentityBootstrapService",
    "is_valid_worker_identity",
]
