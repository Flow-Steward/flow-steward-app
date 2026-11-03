"""Catalog secrets in workflow inputs on real PostgreSQL (ADR 0055, stage 2b)."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.secrets.approval_gate import SecretConfigurationUnapproved
from core.application.secrets.service import SecretCatalogService
from core.application.secrets.workflow_gate import (
    AUTHORITY_STATE_KEY,
    SecretDestinationNotAllowed,
    launch_authority,
    require_catalog_ref_authority,
    storage_ref_fingerprint,
)
from core.application.tooling.db import upgrade_db
from tests.integration.test_secret_catalog_postgres import (
    ACCOUNT,
    _connect,
    _human,
    _project,
    _token,
    create_database,  # noqa: F401 - the shared disposable-database fixture
)

pytestmark = pytest.mark.integration


def _workflow(cur, project_id: str, *, url: str = "https://api.example.test/v1/send") -> str:
    workflow_id = "wf_" + uuid4().hex[:10]
    settings = {
        "callable": {"expected_inputs": [{"name": "api_key", "type": "secret"}]},
        "steps": [
            {
                "step_id": "send",
                "step_kind": "http_request",
                "label": "Send",
                "override": {"http_request": {"method": "POST", "url": url}},
            }
        ],
    }
    cur.execute(
        """
        INSERT INTO registry_workflows (workflow_key, display_name, settings, account_id, scope_kind)
        VALUES (%s, %s, %s::jsonb, %s, 'account')
        """,
        (workflow_id, "Send", json.dumps(settings), ACCOUNT),
    )
    cur.execute(
        "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
        (project_id, workflow_id),
    )
    return workflow_id


@pytest.fixture
def catalog(create_database):  # noqa: F811
    dsn = create_database()
    upgrade_db.baseline_migration_history(dsn=dsn, author="installer")
    conn = _connect(dsn)
    with conn.cursor() as cur:
        project_id = _project(cur, "workflows")
    service = SecretCatalogService(lambda: conn, encrypt=lambda v: "enc")
    secret = service.create_secret(_human(), name="API key", value="v", project_id=project_id)
    yield conn, project_id, service, secret
    conn.close()


def _authority(conn, project_id, workflow_id, **kwargs):
    with conn.cursor() as cur:
        return launch_authority(
            cur, account_id=ACCOUNT, workflow_id=workflow_id, project_id=project_id, **kwargs
        )


def _set_settings(conn, workflow_id, mutate) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT settings FROM registry_workflows WHERE workflow_id = %s", (workflow_id,)
        )
        settings = cur.fetchone()[0]
        mutate(settings)
        cur.execute(
            "UPDATE registry_workflows SET settings = %s::jsonb WHERE workflow_id = %s",
            (json.dumps(settings), workflow_id),
        )


def test_a_bound_input_is_used_only_by_an_approved_definition(catalog) -> None:
    conn, project_id, service, secret = catalog
    with conn.cursor() as cur:
        workflow_id = _workflow(cur, project_id)

    status = service.bind_workflow_input(
        workflow_id, _human(), project_id=project_id, field="api_key", secret_id=secret["id"]
    )
    assert status["status"] == "approval_required"
    with pytest.raises(SecretConfigurationUnapproved):
        _authority(conn, project_id, workflow_id)

    approved = service.approve_workflow(
        workflow_id, _human(), project_id=project_id, fingerprint=status["fingerprint"]
    )
    assert approved["status"] == "approved"
    authority = _authority(conn, project_id, workflow_id)
    assert next(iter(authority.values()))["rules"] == [
        {"kind": "exact_host", "scheme": "https", "host": "api.example.test", "port": 443}
    ]

    # Moving a step on the canvas or renaming it keeps the approval ...
    _set_settings(
        conn, workflow_id, lambda s: s["steps"][0].update(label="Renamed", position={"x": 1})
    )
    assert _authority(conn, project_id, workflow_id)
    # ... changing where the request goes does not.
    _set_settings(
        conn,
        workflow_id,
        lambda s: s["steps"][0]["override"]["http_request"].update(url="https://evil.test/x"),
    )
    with pytest.raises(SecretConfigurationUnapproved):
        _authority(conn, project_id, workflow_id)


def test_a_computed_url_needs_explicit_destinations(catalog) -> None:
    conn, project_id, service, secret = catalog
    with conn.cursor() as cur:
        workflow_id = _workflow(cur, project_id, url="{{ state.inputs.target }}")
    status = service.bind_workflow_input(
        workflow_id, _human(), project_id=project_id, field="api_key", secret_id=secret["id"]
    )
    assert status["dynamic_http_steps"] == ["send"]

    with pytest.raises(ServiceValidationError, match="computed URL"):
        service.approve_workflow(
            workflow_id, _human(), project_id=project_id, fingerprint=status["fingerprint"]
        )
    with pytest.raises(ServiceValidationError, match="not supported"):
        service.approve_workflow(
            workflow_id,
            _human(),
            project_id=project_id,
            fingerprint=status["fingerprint"],
            destinations=[{"kind": "subdomains", "scheme": "https", "host": "example.test"}],
        )
    service.approve_workflow(
        workflow_id,
        _human(),
        project_id=project_id,
        fingerprint=status["fingerprint"],
        destinations=[{"scheme": "https", "host": "api.partner.test", "path_prefix": "/api"}],
    )
    authority = _authority(conn, project_id, workflow_id)
    state = {"runtime_context": {"account_id": ACCOUNT}, AUTHORITY_STATE_KEY: authority}
    key = f"catalog_secret:{secret['id']}"

    require_catalog_ref_authority(conn, state, ref_key=key, url="https://api.partner.test/api/x")
    for refused in (
        "https://api.partner.test/api-other",
        "https://api.partner.test/api/../outside",
        "https://api.partner.test/api/%2e%2e/outside",
        "http://api.partner.test/api/x",
        "https://api.partner.test:8443/api/x",
        "https://evil.test/api/x",
    ):
        with pytest.raises(SecretDestinationNotAllowed):
            require_catalog_ref_authority(conn, state, ref_key=key, url=refused)
    # A run whose launch recorded no authority cannot use the secret at all.
    with pytest.raises(SecretConfigurationUnapproved):
        require_catalog_ref_authority(
            conn, {"runtime_context": {"account_id": ACCOUNT}}, ref_key=key, url=None
        )


def test_a_child_receiving_a_catalog_secret_needs_its_own_approval(catalog) -> None:
    conn, project_id, service, secret = catalog
    with conn.cursor() as cur:
        child_id = _workflow(cur, project_id)
    key = f"catalog_secret:{secret['id']}"

    with pytest.raises(SecretConfigurationUnapproved):
        _authority(conn, project_id, child_id, received_storage_keys=(key,))

    status = service.workflow_approval_status(child_id, _human(), project_id=project_id)
    service.approve_workflow(
        child_id, _human(), project_id=project_id, fingerprint=status["fingerprint"]
    )
    authority = _authority(conn, project_id, child_id, received_storage_keys=(key,))
    assert storage_ref_fingerprint(key) in authority


def test_binding_needs_authority_and_a_secret_input(catalog) -> None:
    conn, project_id, service, secret = catalog
    with conn.cursor() as cur:
        workflow_id = _workflow(cur, project_id)
    with pytest.raises(ServiceForbiddenError):
        service.bind_workflow_input(
            workflow_id,
            _token("admin:all", "*:*", "secrets:*", "*:bind"),
            project_id=project_id,
            field="api_key",
            secret_id=secret["id"],
        )
    with pytest.raises(ServiceValidationError, match="not a secret input"):
        service.bind_workflow_input(
            workflow_id, _human(), project_id=project_id, field="other", secret_id=secret["id"]
        )
    status = service.bind_workflow_input(
        workflow_id, _human(), project_id=project_id, field="api_key", secret_id=secret["id"]
    )
    with pytest.raises(ServiceForbiddenError):
        service.approve_workflow(
            workflow_id,
            _token("secrets:bind", "secrets:write"),
            project_id=project_id,
            fingerprint=status["fingerprint"],
        )
