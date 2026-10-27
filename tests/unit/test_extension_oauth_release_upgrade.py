from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.application.extensions.oauth_contract import (
    OAuthAuthorizationIntent,
    OAuthCredentialField,
    OAuthEndpointPermission,
    OAuthProviderContract,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_release_upgrade import (
    OAuthReleaseImpact,
    OAuthReleaseUpgradeService,
    evaluate_oauth_release_impact,
    oauth_contract_fingerprint,
    oauth_release_impact_hash,
    resolve_oauth_release_contract,
)
from core.application.management.command_models import ExtensionReleaseInstallInput
from core.application.management.errors import ServicePreconditionError
from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle


def _contract(
    *,
    security: str = "security-v1",
    revocation: str = "revocation-v1",
    required: tuple[str, ...] = ("mail.read",),
    optional: tuple[str, ...] = ("mail.write",),
    include_provider: bool = True,
) -> dict:
    return {
        "oauth_providers": (
            [
                {
                    "provider_id": "example",
                    "security_fingerprint": security,
                    "revocation_fingerprint": revocation,
                }
            ]
            if include_provider
            else []
        ),
        "connection_types": [
            {
                "connection_type_id": "example_mail",
                "auth": {
                    "type": "oauth2",
                    "provider_id": "example",
                    "scopes": {
                        "required": list(required),
                        "optional": list(optional),
                    },
                },
            },
            {
                "connection_type_id": "unrelated",
                "auth": {
                    "type": "oauth2",
                    "provider_id": "other",
                    "scopes": {"required": ["other.read"], "optional": []},
                },
            },
        ],
    }


CONFIGURATIONS = [
    {
        "configuration_id": "cfg-example",
        "provider_id": "example",
        "pending_states": [
            {"state_id": "state-mail", "connection_type": "example_mail"},
            {"state_id": "state-files", "connection_type": "example_files"},
        ],
    },
    {
        "configuration_id": "cfg-other",
        "provider_id": "other",
        "pending_states": [{"state_id": "state-other", "connection_type": "unrelated"}],
    },
]

CONNECTIONS = [
    {
        "connection_id": "conn-missing-write",
        "configuration_id": "cfg-example",
        "provider_id": "example",
        "connection_type": "example_mail",
        "granted_scopes": ["mail.read"],
    },
    {
        "connection_id": "conn-has-write",
        "configuration_id": "cfg-example",
        "provider_id": "example",
        "connection_type": "example_mail",
        "granted_scopes": ["mail.read", "mail.write"],
    },
    {
        "connection_id": "conn-other",
        "configuration_id": "cfg-other",
        "provider_id": "other",
        "connection_type": "unrelated",
        "granted_scopes": ["other.read"],
    },
]


def _provider() -> OAuthProviderContract:
    return OAuthProviderContract(
        provider_id="example",
        display_name="Example",
        description="Example provider",
        setup_page_id="oauth_setup",
        help_page_id="oauth_help",
        error_help_anchors={},
        credential_fields=(
            OAuthCredentialField(
                field_id="client_id",
                value_type="text",
                required=True,
                secret=False,
                change_policy="reauthorization_required",
                label="Client ID",
                help_text="Client ID help",
                placeholder="",
                autocomplete="username",
                validation={"min_length": 1, "max_length": 512},
            ),
        ),
        authorization_endpoint=OAuthEndpointPermission("https://identity.example.test/authorize"),
        token_endpoint=OAuthEndpointPermission("https://identity.example.test/token", "none"),
        revocation_endpoint=OAuthEndpointPermission("https://identity.example.test/revoke"),
        callback_id="oauth_authorization",
        authorization_intents=(
            OAuthAuthorizationIntent("connect", {"access_type": "offline"}, ()),
        ),
        post_connect_action_id=None,
    )


def _contract_for_provider(provider: OAuthProviderContract) -> dict:
    contract = _contract()
    contract["oauth_providers"] = [
        {
            "provider_id": provider.provider_id,
            "security_fingerprint": provider_security_fingerprint(provider),
            "revocation_fingerprint": provider_revocation_fingerprint(provider),
        }
    ]
    return contract


@pytest.mark.parametrize(
    ("new_contract", "expected"),
    [
        (
            _contract(required=("mail.read", "mail.write"), optional=()),
            OAuthReleaseImpact(True, ("state-mail",), ("conn-missing-write",)),
        ),
        (
            _contract(optional=("mail.write", "mail.archive")),
            OAuthReleaseImpact(True, (), ()),
        ),
        (
            _contract(optional=()),
            OAuthReleaseImpact(
                True,
                ("state-mail",),
                ("conn-has-write",),
            ),
        ),
        (
            _contract(security="endpoint-or-client-auth-or-flow-changed"),
            OAuthReleaseImpact(
                True,
                ("state-files", "state-mail"),
                ("conn-has-write", "conn-missing-write"),
            ),
        ),
        (
            _contract(revocation="revocation-v2"),
            OAuthReleaseImpact(True, (), ()),
        ),
        (
            _contract(include_provider=False),
            OAuthReleaseImpact(
                True,
                ("state-files", "state-mail"),
                ("conn-has-write", "conn-missing-write"),
            ),
        ),
        (_contract(), OAuthReleaseImpact(False, (), ())),
    ],
)
def test_release_impact_matrix_is_provider_and_connection_exact(
    new_contract: dict,
    expected: OAuthReleaseImpact,
) -> None:
    assert (
        evaluate_oauth_release_impact(
            _contract(),
            new_contract,
            CONFIGURATIONS,
            CONNECTIONS,
        )
        == expected
    )


def test_required_scope_removal_requires_reconnect_only_for_grants_to_reduce() -> None:
    impact = evaluate_oauth_release_impact(
        _contract(required=("mail.read", "mail.write"), optional=()),
        _contract(required=("mail.read",), optional=()),
        CONFIGURATIONS,
        CONNECTIONS,
    )
    assert impact == OAuthReleaseImpact(
        permission_approval_required=True,
        invalidate_pending_state_ids=("state-mail",),
        reauthorize_connection_ids=("conn-has-write",),
    )


@pytest.mark.parametrize(
    "changed_provider",
    [
        replace(
            _provider(),
            authorization_endpoint=OAuthEndpointPermission(
                "https://identity-v2.example.test/authorize"
            ),
        ),
        replace(
            _provider(),
            token_endpoint=OAuthEndpointPermission(
                "https://identity.example.test/token", "client_secret_post"
            ),
        ),
        replace(_provider(), callback_id="oauth_authorization_v2"),
        replace(_provider(), post_connect_action_id="verify_identity"),
    ],
    ids=("endpoint-origin", "client-auth", "callback", "post-connect"),
)
def test_security_boundary_fingerprint_changes_invalidate_all_provider_flows_and_grants(
    changed_provider: OAuthProviderContract,
) -> None:
    impact = evaluate_oauth_release_impact(
        _contract_for_provider(_provider()),
        _contract_for_provider(changed_provider),
        CONFIGURATIONS,
        CONNECTIONS,
    )

    assert impact == OAuthReleaseImpact(
        True,
        ("state-files", "state-mail"),
        ("conn-has-write", "conn-missing-write"),
    )


def test_actual_revocation_endpoint_fingerprint_requires_approval_without_grant_invalidation() -> (
    None
):
    changed = replace(
        _provider(),
        revocation_endpoint=OAuthEndpointPermission("https://identity-v2.example.test/revoke"),
    )

    assert evaluate_oauth_release_impact(
        _contract_for_provider(_provider()),
        _contract_for_provider(changed),
        CONFIGURATIONS,
        CONNECTIONS,
    ) == OAuthReleaseImpact(True, (), ())


def test_impact_hash_is_stable_order_independent_and_candidate_bound() -> None:
    impact = OAuthReleaseImpact(True, ("state-b", "state-a"), ("conn-b", "conn-a"))
    reordered = OAuthReleaseImpact(True, ("state-a", "state-b"), ("conn-a", "conn-b"))

    assert oauth_release_impact_hash(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        impact=impact,
    ) == oauth_release_impact_hash(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        impact=reordered,
    )
    assert oauth_release_impact_hash(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        impact=impact,
    ) != oauth_release_impact_hash(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v3",
        impact=impact,
    )
    assert oauth_release_impact_hash(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        impact=impact,
        candidate_contract_fingerprint="contract-a",
    ) != oauth_release_impact_hash(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        impact=impact,
        candidate_contract_fingerprint="contract-b",
    )


class _UpgradeRepository:
    def __init__(self) -> None:
        self.writes: list[tuple] = []
        self.locked_connections = list(CONNECTIONS)

    def read_oauth_release_inventory(self, **_scope):
        return list(CONFIGURATIONS), list(CONNECTIONS)

    def lock_oauth_release_inventory(self, **_scope):
        return list(CONFIGURATIONS), self.locked_connections

    def apply_oauth_release_impact(self, **values):
        self.writes.append(("apply", values))


def _descriptor(contract: dict) -> dict:
    return {"runtime": {"extension_contract_v2": contract}}


def _persisted_descriptor(contract: dict) -> dict:
    return {
        "runtime": {"extension_contract_v2": {"oauth_providers": "contracts/oauth.yaml"}},
        "extension_contract_v2_normalized": contract,
    }


def _compile_ref_backed_fixture(
    tmp_path: Path,
    name: str,
    *,
    endpoint_url: str = "",
    revocation_url: str = "",
    required_scopes: list[str] | None = None,
) -> dict:
    source = Path("tests/fixtures/extensions/fixture_extension_oauth")
    bundle = tmp_path / name / "fixture_extension_oauth"
    shutil.copytree(source, bundle)
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    contract = manifest["runtime"]["extension_contract_v2"]
    providers = contract.pop("oauth_providers")
    connection_types = contract.pop("connection_types")
    if endpoint_url:
        providers[0]["authorization_endpoint"]["url"] = endpoint_url
    if revocation_url:
        providers[0]["revocation_endpoint"]["url"] = revocation_url
    if required_scopes is not None:
        connection_types[0]["auth"]["scopes"]["required"] = required_scopes
        connection_types[0]["auth"]["scopes"]["optional"] = []
    contracts = bundle / "contracts"
    contracts.mkdir(exist_ok=True)
    (contracts / "oauth.yaml").write_text(
        yaml.safe_dump({"oauth_providers": providers}, sort_keys=False), encoding="utf-8"
    )
    (contracts / "connections.yaml").write_text(
        yaml.safe_dump({"connection_types": connection_types}, sort_keys=False),
        encoding="utf-8",
    )
    contract["oauth_providers"] = "contracts/oauth.yaml"
    contract["connection_types"] = "contracts/connections.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = compile_extension_bundle(bundle)
    assert result["status"] == "valid", result["validation_errors"] + result["compile_errors"]
    persisted = dict(result["compiled_manifest"])
    persisted["extension_contract_v2_normalized"] = {
        "oauth_providers": persisted["oauth_providers"],
        "connection_types": persisted["connection_types"],
    }
    return persisted


def test_actual_ref_backed_persisted_fixture_detects_endpoint_revocation_and_scope_impact(
    tmp_path: Path,
) -> None:
    baseline = _compile_ref_backed_fixture(tmp_path, "baseline")
    endpoint = _compile_ref_backed_fixture(
        tmp_path,
        "endpoint",
        endpoint_url="https://identity-v2.example.test/authorize",
    )
    revocation = _compile_ref_backed_fixture(
        tmp_path,
        "revocation",
        revocation_url="https://identity-v2.example.test/revoke",
    )
    scopes = _compile_ref_backed_fixture(
        tmp_path,
        "scopes",
        required_scopes=["mail.read", "mail.write"],
    )
    baseline_contract = resolve_oauth_release_contract(baseline)
    assert baseline_contract["oauth_providers"][0]["security_fingerprint"]

    assert evaluate_oauth_release_impact(
        baseline_contract,
        resolve_oauth_release_contract(endpoint),
        CONFIGURATIONS,
        CONNECTIONS,
    ).reauthorize_connection_ids == ("conn-has-write", "conn-missing-write")
    assert evaluate_oauth_release_impact(
        baseline_contract,
        resolve_oauth_release_contract(revocation),
        CONFIGURATIONS,
        CONNECTIONS,
    ) == OAuthReleaseImpact(True, (), ())
    assert evaluate_oauth_release_impact(
        baseline_contract,
        resolve_oauth_release_contract(scopes),
        CONFIGURATIONS,
        CONNECTIONS,
    ).reauthorize_connection_ids == ("conn-missing-write",)


def test_persisted_normalized_contract_drives_security_and_revocation_impact() -> None:
    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)

    _raises_input_456_1 = _persisted_descriptor(_contract())
    _raises_input_456_2 = _persisted_descriptor(_contract(security="security-v2"))
    with pytest.raises(ServicePreconditionError) as security_info:
        service.prepare(
            account_id="account-1",
            install_id="install-1",
            extension_id="fixture.extension-oauth",
            old_release_id="release-v1",
            new_release_id="release-v2",
            old_descriptor=_raises_input_456_1,
            new_descriptor=_raises_input_456_2,
            approved_impact_hash="",
        )
    assert security_info.value.payload["impact"]["reauthorize_connection_ids"] == [
        "conn-has-write",
        "conn-missing-write",
    ]

    _raises_input_472_1 = _persisted_descriptor(_contract())
    _raises_input_472_2 = _persisted_descriptor(_contract(revocation="revocation-v2"))
    with pytest.raises(ServicePreconditionError) as revocation_info:
        service.prepare(
            account_id="account-1",
            install_id="install-1",
            extension_id="fixture.extension-oauth",
            old_release_id="release-v1",
            new_release_id="release-v2",
            old_descriptor=_raises_input_472_1,
            new_descriptor=_raises_input_472_2,
            approved_impact_hash="",
        )
    assert revocation_info.value.payload["impact"] == {
        "permission_approval_required": True,
        "invalidate_pending_state_count": 0,
        "reauthorize_connection_ids": [],
    }


def test_confirmation_from_one_install_cannot_acknowledge_another_install_or_account() -> None:
    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)
    old_descriptor = _persisted_descriptor(_contract())
    new_descriptor = _persisted_descriptor(_contract(revocation="revocation-v2"))

    with pytest.raises(ServicePreconditionError) as first_preview:
        service.prepare(
            account_id="account-a",
            install_id="install-a",
            extension_id="fixture.extension-oauth",
            old_release_id="release-v1",
            new_release_id="release-v2",
            old_descriptor=old_descriptor,
            new_descriptor=new_descriptor,
            approved_impact_hash="",
        )

    with pytest.raises(ServicePreconditionError) as second_preview:
        service.prepare(
            account_id="account-b",
            install_id="install-b",
            extension_id="fixture.extension-oauth",
            old_release_id="release-v1",
            new_release_id="release-v2",
            old_descriptor=old_descriptor,
            new_descriptor=new_descriptor,
            approved_impact_hash=first_preview.value.payload["impact_hash"],
        )

    assert second_preview.value.payload["impact_hash"] != first_preview.value.payload["impact_hash"]

    with pytest.raises(ServicePreconditionError) as extension_preview:
        service.prepare(
            account_id="account-a",
            install_id="install-a",
            extension_id="fixture.peer-oauth",
            old_release_id="release-v1",
            new_release_id="release-v2",
            old_descriptor=old_descriptor,
            new_descriptor=new_descriptor,
            approved_impact_hash=first_preview.value.payload["impact_hash"],
        )
    assert (
        extension_preview.value.payload["impact_hash"] != first_preview.value.payload["impact_hash"]
    )


def test_transition_requires_exact_candidate_bound_approval_before_any_write() -> None:
    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)

    _raises_input_542_1 = _descriptor(_contract())
    _raises_input_542_2 = _descriptor(_contract(security="security-v2"))
    with pytest.raises(ServicePreconditionError) as exc_info:
        service.prepare(
            account_id="account-1",
            install_id="install-1",
            extension_id="fixture.extension-oauth",
            old_release_id="release-v1",
            new_release_id="release-v2",
            old_descriptor=_raises_input_542_1,
            new_descriptor=_raises_input_542_2,
            approved_impact_hash="",
        )

    payload = exc_info.value.payload
    assert payload["error"] == "oauth_release_approval_required"
    assert payload["impact"] == {
        "permission_approval_required": True,
        "invalidate_pending_state_count": 2,
        "reauthorize_connection_ids": ["conn-has-write", "conn-missing-write"],
    }
    assert len(payload["impact_hash"]) == 64
    assert repository.writes == []

    _raises_input_564_1 = _descriptor(_contract())
    _raises_input_564_2 = _descriptor(_contract(security="security-v2"))
    with pytest.raises(ServicePreconditionError) as stale_info:
        service.prepare(
            account_id="account-1",
            install_id="install-1",
            extension_id="fixture.extension-oauth",
            old_release_id="release-v1",
            new_release_id="release-v3",
            old_descriptor=_raises_input_564_1,
            new_descriptor=_raises_input_564_2,
            approved_impact_hash=payload["impact_hash"],
        )
    assert stale_info.value.payload["error"] == "oauth_release_approval_required"
    assert repository.writes == []


def test_approved_transition_applies_exact_impact_and_new_provider_fingerprints() -> None:
    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)
    impact = OAuthReleaseImpact(
        True,
        ("state-files", "state-mail"),
        ("conn-has-write", "conn-missing-write"),
    )
    approval = oauth_release_impact_hash(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        impact=impact,
        candidate_contract_fingerprint=oauth_contract_fingerprint(
            _contract(security="security-v2", revocation="revocation-v2")
        ),
    )

    prepared = service.prepare(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        old_descriptor=_descriptor(_contract()),
        new_descriptor=_descriptor(_contract(security="security-v2", revocation="revocation-v2")),
        approved_impact_hash=approval,
    )
    assert repository.writes == []

    service.apply(prepared)

    assert repository.writes == [
        (
            "apply",
            {
                "install_id": "install-1",
                "account_id": "account-1",
                "extension_id": "fixture.extension-oauth",
                "old_release_id": "release-v1",
                "new_release_id": "release-v2",
                "invalidate_pending_state_ids": ("state-files", "state-mail"),
                "reauthorize_connection_ids": (
                    "conn-has-write",
                    "conn-missing-write",
                ),
                "provider_fingerprints": {"example": ("security-v2", "revocation-v2")},
            },
        )
    ]


def test_cosmetic_release_transition_needs_no_approval_but_updates_release_pin() -> None:
    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)

    prepared = service.prepare(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        old_descriptor=_descriptor(_contract()),
        new_descriptor=_descriptor(_contract()),
        approved_impact_hash="",
    )
    service.apply(prepared)

    assert repository.writes[0][1]["new_release_id"] == "release-v2"
    assert repository.writes[0][1]["invalidate_pending_state_ids"] == ()
    assert repository.writes[0][1]["reauthorize_connection_ids"] == ()


def test_locked_inventory_drift_aborts_before_fingerprint_or_lifecycle_writes() -> None:
    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)
    prepared = service.prepare(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        old_descriptor=_descriptor(_contract()),
        new_descriptor=_descriptor(_contract()),
        approved_impact_hash="",
    )
    repository.locked_connections = [
        *CONNECTIONS,
        {
            "connection_id": "conn-arrived-during-preview",
            "configuration_id": "cfg-example",
            "provider_id": "example",
            "connection_type": "example_mail",
            "granted_scopes": ["mail.read"],
        },
    ]

    # A cosmetic transition is unaffected by connection inventory growth.
    service.apply(prepared)
    assert len(repository.writes) == 1

    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)
    changed = _contract(required=("mail.read", "mail.write"), optional=())
    impact = evaluate_oauth_release_impact(_contract(), changed, CONFIGURATIONS, CONNECTIONS)
    prepared = service.prepare(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        old_release_id="release-v1",
        new_release_id="release-v2",
        old_descriptor=_descriptor(_contract()),
        new_descriptor=_descriptor(changed),
        approved_impact_hash=oauth_release_impact_hash(
            account_id="account-1",
            install_id="install-1",
            extension_id="fixture.extension-oauth",
            old_release_id="release-v1",
            new_release_id="release-v2",
            impact=impact,
            candidate_contract_fingerprint=oauth_contract_fingerprint(changed),
        ),
    )
    repository.locked_connections = [
        row for row in repository.locked_connections if row["connection_id"] != "conn-missing-write"
    ]

    with pytest.raises(RuntimeError, match="oauth_release_impact_drift"):
        service.apply(prepared)
    assert repository.writes == []


@pytest.mark.parametrize("include_provider", [True, False])
def test_apply_rejects_locked_disconnecting_connections_before_any_write(
    include_provider: bool,
) -> None:
    repository = _UpgradeRepository()
    service = OAuthReleaseUpgradeService(repository)
    new_contract = _contract(include_provider=include_provider)
    impact = evaluate_oauth_release_impact(_contract(), new_contract, CONFIGURATIONS, CONNECTIONS)
    prepare_values = {
        "account_id": "account-1",
        "install_id": "install-1",
        "extension_id": "fixture.extension-oauth",
        "old_release_id": "release-v1",
        "new_release_id": "release-v2",
        "old_descriptor": _descriptor(_contract()),
        "new_descriptor": _descriptor(new_contract),
    }
    if impact.permission_approval_required:
        with pytest.raises(ServicePreconditionError) as approval_info:
            service.prepare(
                **prepare_values,
                approved_impact_hash="",
            )
        approval = approval_info.value.payload["impact_hash"]
    else:
        approval = ""
    prepared = service.prepare(
        **prepare_values,
        approved_impact_hash=approval,
    )
    repository.locked_connections = [
        {
            **row,
            "enabled": row["connection_id"] == "conn-other",
            "lifecycle_status": (
                "connected" if row["connection_id"] == "conn-other" else "disconnecting"
            ),
        }
        for row in CONNECTIONS
    ]

    with pytest.raises(ServicePreconditionError) as exc_info:
        service.apply(prepared)

    assert exc_info.value.payload == {
        "error": "oauth_release_disconnect_in_progress",
        "connection_ids": ["conn-has-write", "conn-missing-write"],
    }
    assert repository.writes == []


def test_install_workflow_integration_previews_then_applies_only_after_acknowledgement(
    monkeypatch,
) -> None:
    from core.application.extensions import oauth_release_upgrade_integration as integration

    repository = _UpgradeRepository()
    monkeypatch.setattr(
        integration,
        "PostgresOAuthReleaseUpgradeRepository",
        lambda _connection: repository,
    )
    context = SimpleNamespace(persistence_connection=lambda: object())
    existing = SimpleNamespace(
        account_id="account-1",
        install_id="install-1",
        extension_id="fixture.extension-oauth",
        release_id="release-v1",
    )
    previous = SimpleNamespace(
        release_id="release-v1",
        descriptor=_descriptor(_contract()),
    )
    candidate = SimpleNamespace(
        release_id="release-v2",
        descriptor=_descriptor(_contract(security="security-v2")),
    )

    _raises_input_791_1 = ExtensionReleaseInstallInput(release_id="release-v2")
    with pytest.raises(ServicePreconditionError) as exc_info:
        integration.prepare_oauth_release_transition(
            context=context,
            command=_raises_input_791_1,
            existing=existing,
            previous_release=previous,
            candidate_release=candidate,
        )
    assert repository.writes == []

    prepared = integration.prepare_oauth_release_transition(
        context=context,
        command=ExtensionReleaseInstallInput(
            release_id="release-v2",
            approved_oauth_impact_hash=exc_info.value.payload["impact_hash"],
        ),
        existing=existing,
        previous_release=previous,
        candidate_release=candidate,
    )
    assert prepared is not None
    assert repository.writes == []

    prepared.apply()
    assert repository.writes[0][1]["new_release_id"] == "release-v2"
