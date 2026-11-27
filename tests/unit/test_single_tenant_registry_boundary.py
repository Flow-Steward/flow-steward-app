"""Account boundary of the field and data-contract catalogs (ADR 0020, step C4a).

Fields and data contracts belong to an account, so a caller of any account reaches
the catalog, and every store call is made for the caller's own account only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import normalize_scope
from core.entrypoints.admin import data_contracts as data_contract_routes
from core.entrypoints.admin import fields as field_routes

FOREIGN_ACCOUNT_ID = "00000000-0000-0000-0000-0000000000bb"


def _client_for_account(account_id: str) -> TestClient:
    app = FastAPI()
    app.include_router(field_routes.router)
    app.include_router(data_contract_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "username": "admin",
            "roles": ["admin"],
        }
        request.state.scope_context = normalize_scope(
            account_id=account_id,
            actor_id="admin@example.com",
            principal_type="admin",
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


FIELD_BODY = {
    "field_key": "rr006.field",
    "display_name": "RR-006 field",
    "value_type": "text",
}
CONTRACT_BODY = {
    "contract_id": "rr006.contract",
    "display_name": "RR-006 contract",
    "kind": "json",
    "schema": {"type": "object"},
}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/fields", None),
        ("GET", "/api/fields/rr006.field", None),
        ("POST", "/api/fields", FIELD_BODY),
        ("PUT", "/api/fields/rr006.field", FIELD_BODY),
        ("PATCH", "/api/fields/rr006.field", {"display_name": "foreign write"}),
        ("PATCH", "/api/fields/rr006.field/toggle", None),
        ("DELETE", "/api/fields/rr006.field", None),
        ("GET", "/api/data-contracts", None),
        ("GET", "/api/data-contracts/rr006.contract", None),
        ("POST", "/api/data-contracts", CONTRACT_BODY),
        ("PUT", "/api/data-contracts/rr006.contract", CONTRACT_BODY),
        (
            "PATCH",
            "/api/data-contracts/rr006.contract",
            {"display_name": "foreign write"},
        ),
        ("PATCH", "/api/data-contracts/rr006.contract/toggle", None),
        ("DELETE", "/api/data-contracts/rr006.contract", None),
    ],
)
def test_every_store_call_is_made_for_the_callers_account(
    monkeypatch,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    field_db = MagicMock()
    field_db.get_field.return_value = None
    field_db.list_fields.return_value = []
    field_db.toggle_field.return_value = True
    contract_db = MagicMock()
    contract_db.get_data_contract.return_value = None
    contract_db.list_data_contracts.return_value = []
    contract_db.toggle_data_contract.return_value = True
    monkeypatch.setattr(field_routes, "_get_db", lambda: field_db)
    monkeypatch.setattr(data_contract_routes, "_get_db", lambda: contract_db)

    response = _client_for_account(FOREIGN_ACCOUNT_ID).request(method, path, json=body)

    assert response.status_code != 403, response.text
    calls = [*field_db.mock_calls, *contract_db.mock_calls]
    assert calls
    for name, _args, kwargs in calls:
        assert kwargs.get("account_id") == FOREIGN_ACCOUNT_ID, name


def test_another_accounts_row_is_answered_as_not_found(monkeypatch) -> None:
    field_db = MagicMock()
    field_db.get_field.return_value = None
    contract_db = MagicMock()
    contract_db.get_data_contract.return_value = None
    monkeypatch.setattr(field_routes, "_get_db", lambda: field_db)
    monkeypatch.setattr(data_contract_routes, "_get_db", lambda: contract_db)
    client = _client_for_account(FOREIGN_ACCOUNT_ID)

    assert client.get("/api/fields/rr006.field").status_code == 404
    assert client.get("/api/data-contracts/rr006.contract").status_code == 404
    field_db.get_field.assert_called_once_with("rr006.field", account_id=FOREIGN_ACCOUNT_ID)
    contract_db.get_data_contract.assert_called_once_with(
        "rr006.contract", account_id=FOREIGN_ACCOUNT_ID
    )


def test_default_account_can_read_deployment_registry_catalogs(monkeypatch) -> None:
    field_db = MagicMock()
    field_db.list_fields.return_value = []
    contract_db = MagicMock()
    contract_db.list_data_contracts.return_value = []
    monkeypatch.setattr(field_routes, "_get_db", lambda: field_db)
    monkeypatch.setattr(data_contract_routes, "_get_db", lambda: contract_db)
    client = _client_for_account(DEFAULT_ACCOUNT_ID)

    assert client.get("/api/fields").status_code == 200
    assert client.get("/api/data-contracts").status_code == 200
    assert field_db.list_fields.call_args.kwargs["account_id"] == DEFAULT_ACCOUNT_ID
    assert contract_db.list_data_contracts.call_args.kwargs["account_id"] == DEFAULT_ACCOUNT_ID
