from __future__ import annotations

from core.application.auth.worker_identity_bootstrap import (
    WorkerIdentityBootstrapService,
)


class _IdentityStore:
    def __init__(self, principal: dict[str, object] | None = None) -> None:
        self.principal = principal
        self.created_accounts: list[dict[str, object]] = []
        self.created_tokens: list[dict[str, object]] = []
        self.revoked: list[str] = []

    def resolve_machine_token(self, _token: str) -> dict[str, object] | None:
        return self.principal

    def create_service_account(self, **kwargs: object) -> dict[str, object]:
        self.created_accounts.append(kwargs)
        return {"service_account_key": kwargs["service_account_key"]}

    def create_machine_token(self, **kwargs: object) -> dict[str, object]:
        self.created_tokens.append(kwargs)
        return {"id": "new-token-id", "token": "new-worker-secret"}

    def revoke_machine_token(self, token_id: str, **_kwargs: object) -> dict[str, object]:
        self.revoked.append(token_id)
        return {"id": token_id, "revoked": True}


class _SecretStore:
    def __init__(self, value: str) -> None:
        self.value = value
        self.replacements: list[str] = []

    def read(self) -> str:
        return self.value

    def replace(self, value: str) -> None:
        self.value = value
        self.replacements.append(value)


def test_bootstrap_keeps_valid_unrestricted_worker_identity_without_rotation() -> None:
    store = _IdentityStore(
        {
            "token_id": "existing-id",
            "service_account_key": "flow-steward-worker",  # pragma: allowlist secret - account key name
            "account_id": "account-1",
            "scopes": ["projects.read"],
            "allowed_project_ids": [],
        }
    )

    secrets = _SecretStore("existing")
    result = WorkerIdentityBootstrapService(store=store, secret_store=secrets).ensure(
        account_id="account-1",
    )

    assert result.rotated is False
    assert store.created_tokens == []
    assert secrets.replacements == []


def test_bootstrap_rotates_under_scoped_identity_without_exposing_secret() -> None:
    store = _IdentityStore(
        {
            "token_id": "old-token-id",
            "service_account_key": "flow-steward-worker",  # pragma: allowlist secret - account key name
            "account_id": "account-1",
            "scopes": ["jobs.read"],
            "allowed_project_ids": ["project-existing"],
        }
    )

    secrets = _SecretStore("old-worker-secret")
    result = WorkerIdentityBootstrapService(store=store, secret_store=secrets).ensure(
        account_id="account-1",
    )

    assert result.rotated is True
    assert not hasattr(result, "token")
    assert store.created_accounts[0]["scopes"] == ["projects.read"]
    assert store.created_accounts[0]["allowed_project_ids"] == []
    assert store.created_tokens[0]["allowed_project_ids"] == []
    assert store.revoked == ["old-token-id"]
    assert secrets.value == "new-worker-secret"


def test_bootstrap_revokes_new_token_when_secret_persistence_fails() -> None:
    class _FailingSecrets(_SecretStore):
        def replace(self, value: str) -> None:
            raise OSError("read-only secret store")

    store = _IdentityStore()

    try:
        WorkerIdentityBootstrapService(
            store=store,
            secret_store=_FailingSecrets(""),
        ).ensure(account_id="account-1")
    except OSError as exc:
        assert str(exc) == "read-only secret store"
    else:
        raise AssertionError("secret persistence failure must propagate")

    assert store.revoked == ["new-token-id"]


class _EnumeratingIdentityStore(_IdentityStore):
    """Store that can list a service account's tokens, as the real one can."""

    def __init__(self, principal=None, existing_tokens=()):
        super().__init__(principal)
        self.existing_tokens = [dict(row) for row in existing_tokens]

    def list_service_account_tokens(self, service_account_key, account_id=None, **_kwargs):
        self.requested_key = service_account_key
        return [dict(row) for row in self.existing_tokens]


def test_reprovisioning_revokes_a_previous_token_the_secret_store_no_longer_knows() -> None:
    """RS5-005 regression: old worker credentials must not survive re-provisioning.

    Revocation used to target only the token id resolved from the secret store's
    current value. That value is missing whenever the env file is fresh — a
    reinstall, a lost env file, a new deployment env pointed at an existing
    database — so `principal` was None, no id was known, and the token already
    issued in the database stayed active. Worker credentials accumulated and each
    one kept working.
    """
    store = _EnumeratingIdentityStore(
        principal=None,
        existing_tokens=[{"id": "previous-token-id"}],
    )
    secrets = _SecretStore("")  # nothing on disk, exactly the reinstall case

    result = WorkerIdentityBootstrapService(store=store, secret_store=secrets).ensure(
        account_id="00000000-0000-0000-0000-000000000002"
    )

    assert result.rotated is True
    assert "previous-token-id" in store.revoked, (
        "a previously issued worker token stayed active after re-provisioning"
    )
    assert "new-token-id" not in store.revoked, "the freshly issued token must survive"


def test_reprovisioning_leaves_exactly_one_active_worker_token() -> None:
    store = _EnumeratingIdentityStore(
        principal=None,
        existing_tokens=[{"id": "old-a"}, {"id": "old-b"}, {"id": "new-token-id"}],
    )

    WorkerIdentityBootstrapService(store=store, secret_store=_SecretStore("")).ensure(
        account_id="00000000-0000-0000-0000-000000000002"
    )

    assert sorted(store.revoked) == ["old-a", "old-b"]
