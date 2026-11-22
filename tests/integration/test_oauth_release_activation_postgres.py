"""An extension update that did not change sign-in keeps OAuth connections usable.

On the canonical PostgreSQL schema: an install moves from one release to the
next. A configuration and its connection pinned to the earlier release are
reconciled during activation, and the runtime's own row checks then accept the
connection with no page opened. A release that adds a required scope keeps it
blocked, with an attention code the runtime reports.
"""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_release_activation import (
    REASON_REQUIRED_SCOPE_ADDED,
    RELEASE_REAPPROVAL_REQUIRED,
    reconcile_install_oauth_release,
)
from core.application.extensions.oauth_runtime import (
    OAuthRuntimeTokenError,
    OAuthRuntimeTokenService,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("oauth_release_activation")]

EXT = "fixture.extension-oauth"


def _descriptor(*, required: list[str] | None = None) -> dict[str, Any]:
    from tests.unit.test_extension_oauth_authorization import _descriptor as base

    descriptor = copy.deepcopy(base())
    if required is not None:
        contract = descriptor["runtime"]["extension_contract_v2"]
        contract["connection_types"][0]["auth"]["scopes"]["required"] = required
    return descriptor


def _sql(dsn: str, query: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(query, params)
        return list(cur.fetchall()) if cur.description else []


@pytest.fixture
def stand(disposable_db):
    dsn = disposable_db.dsn
    project_id = str(uuid4())
    old_release, new_release = f"rel-{uuid4().hex[:8]}", f"rel-{uuid4().hex[:8]}"
    provider = compile_oauth_providers(_descriptor())[0]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"Release fixture {project_id}", DEFAULT_ACCOUNT_ID),
        )
        [(tenant_id,)] = conn.execute(
            "SELECT tenant_id FROM projects WHERE id = %s", (project_id,)
        ).fetchall()
        conn.execute(
            "INSERT INTO extension_installs (extension_id, install_id, account_id) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (EXT, str(uuid4()), DEFAULT_ACCOUNT_ID),
        )
        [(install_id,)] = conn.execute(
            "SELECT install_id FROM extension_installs WHERE extension_id = %s AND account_id = %s",
            (EXT, DEFAULT_ACCOUNT_ID),
        ).fetchall()
        for release in (old_release, new_release):
            conn.execute(
                "INSERT INTO extension_releases (release_id, extension_id, release_version) "
                "VALUES (%s, %s, %s)",
                (release, EXT, release),
            )
        # The install is already active on the new release (activation moved it).
        conn.execute(
            "UPDATE extension_installs SET release_id = %s, state = 'active' WHERE install_id = %s",
            (new_release, install_id),
        )
        configuration_id = str(uuid4())
        conn.execute(
            "INSERT INTO extension_oauth_configurations (configuration_id, tenant_id, account_id, "
            "project_id, extension_install_id, extension_id, extension_release_id, provider_id, "
            "display_name, status, non_secret_config, provider_security_fingerprint, "
            "provider_revocation_fingerprint, authorization_revision) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, 'example', 'Example', 'verified', %s::jsonb, %s, %s, 1)",
            (
                configuration_id,
                tenant_id,
                DEFAULT_ACCOUNT_ID,
                project_id,
                install_id,
                EXT,
                old_release,
                json.dumps({"client_id": "client-1"}),
                provider_security_fingerprint(provider),
                provider_revocation_fingerprint(provider),
            ),
        )
        connection_id = f"conn_{uuid4().hex[:10]}"
        conn.execute(
            "INSERT INTO project_connections (connection_id, project_id, extension_id, account_id, "
            "extension_install_id, connection_kind, connection_type, display_name, "
            "connection_config, oauth_configuration_id, oauth_provider_id, "
            "oauth_extension_release_id, oauth_authorization_revision, "
            "oauth_provider_security_fingerprint, oauth_provider_revocation_fingerprint, "
            "oauth_granted_scopes, credential_revision) VALUES (%s, %s, %s, %s, %s, 'extension', "
            "'example_mail', 'Primary', %s::jsonb, %s, 'example', %s, 1, %s, %s, %s::jsonb, 1)",
            (
                connection_id,
                project_id,
                EXT,
                DEFAULT_ACCOUNT_ID,
                install_id,
                json.dumps({"oauth": {"lifecycle_status": "connected"}}),
                configuration_id,
                old_release,
                provider_security_fingerprint(provider),
                provider_revocation_fingerprint(provider),
                json.dumps(["mail.read"]),
            ),
        )
    return {
        "dsn": dsn,
        "project_id": project_id,
        "tenant_id": str(tenant_id),
        "install_id": install_id,
        "old_release": old_release,
        "new_release": new_release,
        "configuration_id": configuration_id,
        "connection_id": connection_id,
    }


def _reconcile(stand: dict[str, Any], descriptor: dict[str, Any]) -> list[dict[str, str]]:
    """This stand's outcome (the install is shared, so earlier stands' rows appear too)."""
    with psycopg.connect(stand["dsn"]) as conn:
        result = reconcile_install_oauth_release(
            conn,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id=stand["install_id"],
            extension_id=EXT,
            release_id=stand["new_release"],
            descriptor=descriptor,
        )
        conn.commit()
    return [row for row in result if row["configuration_id"] == stand["configuration_id"]]


def _runtime_rows(stand: dict[str, Any], descriptor: dict[str, Any]) -> Any:
    """The runtime's own checks before any token is used, on the new release."""
    from core.application.extensions.oauth_repository import ExtensionOAuthRepository

    with psycopg.connect(stand["dsn"]) as conn:
        service = OAuthRuntimeTokenService(
            repository=ExtensionOAuthRepository(conn),
            token_store=None,  # type: ignore[arg-type]
            install_loader=lambda _install_id, _account_id: {
                "release_id": stand["new_release"],
                "enabled": True,
                "extension_id": EXT,
                "compiled_manifest": descriptor,
            },
            transaction_factory=nullcontext,
        )
        try:
            return service._validated_rows(
                scope={
                    "tenant_id": stand["tenant_id"],
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "project_id": stand["project_id"],
                    "extension_install_id": stand["install_id"],
                    "extension_id": EXT,
                    "provider_id": "example",
                    "configuration_id": stand["configuration_id"],
                },
                connection_id=stand["connection_id"],
                connection_type="example_mail",
            )
        finally:
            conn.rollback()


def test_an_update_that_kept_sign_in_leaves_the_connection_usable_at_once(stand) -> None:
    descriptor = _descriptor()
    # Before activation reconciles, the runtime refuses the earlier release's rows.
    with pytest.raises(OAuthRuntimeTokenError) as blocked:
        _runtime_rows(stand, descriptor)
    assert blocked.value.code == "oauth_runtime_blocked"

    outcome = _reconcile(stand, descriptor)

    assert outcome == [
        {"configuration_id": stand["configuration_id"], "outcome": "moved", "reason": ""}
    ]
    [(release, revision)] = _sql(
        stand["dsn"],
        "SELECT extension_release_id, configuration_revision FROM extension_oauth_configurations "
        "WHERE configuration_id = %s",
        (stand["configuration_id"],),
    )
    assert (release, revision) == (stand["new_release"], 1)
    [(pinned,)] = _sql(
        stand["dsn"],
        "SELECT oauth_extension_release_id FROM project_connections WHERE connection_id = %s",
        (stand["connection_id"],),
    )
    assert pinned == stand["new_release"]
    # Steps, polls and deliveries all pass these checks before using a token.
    _install, configuration, _provider, connection, _revision = _runtime_rows(stand, descriptor)
    assert configuration["extension_release_id"] == stand["new_release"]
    assert connection["connection_id"] == stand["connection_id"]


def test_an_update_that_adds_a_required_scope_stays_blocked_with_its_reason(stand) -> None:
    descriptor = _descriptor(required=["mail.read", "mail.send"])

    outcome = _reconcile(stand, descriptor)

    assert outcome == [
        {
            "configuration_id": stand["configuration_id"],
            "outcome": "blocked",
            "reason": REASON_REQUIRED_SCOPE_ADDED,
        }
    ]
    [(release, last_error)] = _sql(
        stand["dsn"],
        "SELECT extension_release_id, last_error FROM extension_oauth_configurations "
        "WHERE configuration_id = %s",
        (stand["configuration_id"],),
    )
    assert release == stand["old_release"]
    assert last_error == {
        "code": RELEASE_REAPPROVAL_REQUIRED,
        "reason": REASON_REQUIRED_SCOPE_ADDED,
    }
    [(config,)] = _sql(
        stand["dsn"],
        "SELECT connection_config FROM project_connections WHERE connection_id = %s",
        (stand["connection_id"],),
    )
    assert config["oauth"]["attention_code"] == REASON_REQUIRED_SCOPE_ADDED
    with pytest.raises(OAuthRuntimeTokenError) as blocked:
        _runtime_rows(stand, descriptor)
    assert blocked.value.code == RELEASE_REAPPROVAL_REQUIRED
