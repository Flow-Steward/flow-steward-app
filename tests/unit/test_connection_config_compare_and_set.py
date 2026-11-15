"""Exercise conditional connection writes through the real host and PostgreSQL."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.response_envelope import process_host_mutations
from core.infrastructure.db.platform_stores import PlatformIntegrationStore

KIND = "project_connection_config_compare_and_set"
pytestmark = pytest.mark.integration


@pytest.fixture
def connection_case(disposable_db):
    suffix = uuid4().hex
    project_id = str(uuid4())
    extension_id = f"fixture.conditional-config-{suffix}"
    install_id = f"install-{suffix}"
    release_id = f"release-{suffix}"
    connection_id = f"connection-{suffix}"
    store = PlatformIntegrationStore(disposable_db.dsn)
    try:
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, "Conditional config", DEFAULT_ACCOUNT_ID),
            )
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, account_id,
                    source_kind, descriptor_jsonb, descriptor_sha256
                ) VALUES (%s, %s, '1.0.0', %s, 'file', '{}'::jsonb, %s)
                """,
                (release_id, extension_id, DEFAULT_ACCOUNT_ID, "a" * 64),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id
                ) VALUES (%s, %s, %s, %s)
                """,
                (extension_id, DEFAULT_ACCOUNT_ID, install_id, release_id),
            )
        store.upsert_project_connection(
            project_id=project_id,
            extension_id=extension_id,
            connection_id=connection_id,
            account_id=DEFAULT_ACCOUNT_ID,
            connection_type="service_account",
            extension_install_id=install_id,
            display_name="Existing account",
            enabled=False,
            credential_revision=7,
            connection_config={"oauth": {"subject": "original", "revision": 1}},
        )
        extension = {
            "extension_id": extension_id,
            "extension_install_id": install_id,
            "connection_types": [
                {
                    "connection_type_id": "service_account",
                    "config_schema": {
                        "type": "object",
                        "properties": {
                            "anchor": {"type": ["string", "null"]},
                            "selected_id": {"type": "string"},
                            "oauth": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                    "ingress_schema": {"type": "object"},
                }
            ],
            "install_metadata": {
                "runtime_grants": {"mutations": {KIND: {"scope": "owning_extension"}}}
            },
            "action_definitions": [
                {
                    "action_id": "select_account",
                    "handler": {"mode": "extension", "config": {"host_mutations": [KIND]}},
                }
            ],
        }
        yield {
            "store": store,
            "dsn": disposable_db.dsn,
            "extension": extension,
            "scope": {
                "account_id": DEFAULT_ACCOUNT_ID,
                "project_id": project_id,
                "extension_id": extension_id,
                "extension_install_id": install_id,
                "connection_id": connection_id,
            },
        }
    finally:
        store.conn.close()


def _mutation(value: str) -> dict:
    return {
        "kind": KIND,
        "payload": {
            "expect": [{"path": "anchor", "state": "absent_or_equals", "value": value}],
            "set": {"selected_id": value, "anchor": value},
        },
    }


def _apply(case, mutations, *, store=None, scope=None):
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "select_account",
        "response": {"ok": True, "host_mutations": mutations},
    }
    result = process_host_mutations(
        invocation,
        extension=case["extension"],
        db=case["store"] if store is None else store,
        **(case["scope"] if scope is None else scope),
    )
    return invocation, result


def _persisted(case):
    scope = dict(case["scope"])
    scope.pop("account_id")
    return case["store"].get_project_connection(**scope, enabled_only=False)


def test_conditional_config_write_persists_and_preserves_host_state(connection_case):
    before = _persisted(connection_case)
    invocation, result = _apply(connection_case, [_mutation("A")])

    assert result == {"applied": [{"kind": KIND, "index": 0}], "errors": []}
    assert invocation["status"] == "succeeded"
    after = _persisted(connection_case)
    assert after["connection_config"] == {
        "oauth": {"subject": "original", "revision": 1},
        "anchor": "A",
        "selected_id": "A",
    }
    for field in (
        "enabled",
        "credential_revision",
        "display_name",
        "secret_refs",
        "ingress_config",
    ):
        assert after[field] == before[field]


def test_parallel_first_conditional_writes_have_one_winner(connection_case):
    ready = Barrier(2)

    def bind(value):
        store = PlatformIntegrationStore(connection_case["dsn"])
        try:
            ready.wait(timeout=10)
            return _apply(connection_case, [_mutation(value)], store=store)[1]
        finally:
            store.conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(bind, ["A", "B"]))

    assert sum(not result["errors"] for result in outcomes) == 1
    loser = next(result for result in outcomes if result["errors"])
    assert loser["applied"] == []
    assert loser["errors"][0]["code"] == "host_mutation_precondition_failed"
    config = _persisted(connection_case)["connection_config"]
    assert config["anchor"] in {"A", "B"}
    assert config["selected_id"] == config["anchor"]


def test_same_value_replay_succeeds_and_changed_anchor_conflicts(connection_case):
    assert _apply(connection_case, [_mutation("A")])[1]["errors"] == []
    assert _apply(connection_case, [_mutation("A")])[1]["errors"] == []

    invocation, result = _apply(connection_case, [_mutation("B")])

    assert invocation["status"] == "failed"
    assert result["applied"] == []
    assert result["errors"][0]["code"] == "host_mutation_precondition_failed"
    assert _persisted(connection_case)["connection_config"]["anchor"] == "A"


def test_conflict_rolls_back_prior_mutation_in_same_batch(connection_case):
    _, result = _apply(connection_case, [_mutation("A"), _mutation("B")])

    assert result["applied"] == []
    assert result["errors"][0]["code"] == "host_mutation_precondition_failed"
    assert _persisted(connection_case)["connection_config"] == {
        "oauth": {"subject": "original", "revision": 1}
    }


def test_invalid_config_rolls_back_all_writes(connection_case):
    invalid = _mutation("A")
    invalid["payload"]["set"]["undeclared"] = "must-not-persist"
    _, result = _apply(connection_case, [invalid])

    assert result["applied"] == []
    assert result["errors"][0]["code"] == "invalid_payload"
    assert _persisted(connection_case)["connection_config"] == {
        "oauth": {"subject": "original", "revision": 1}
    }


@pytest.mark.parametrize("section", ["expect", "set"])
@pytest.mark.parametrize("path", ["oauth", "oauth.subject"])
def test_host_owned_oauth_paths_are_rejected(connection_case, section, path):
    mutation = _mutation("A")
    if section == "expect":
        mutation["payload"]["expect"] = [{"path": path, "state": "absent"}]
    else:
        mutation["payload"]["set"][path] = "forbidden"

    _, result = _apply(connection_case, [mutation])

    assert result["applied"] == []
    assert result["errors"]
    assert _persisted(connection_case)["connection_config"] == {
        "oauth": {"subject": "original", "revision": 1}
    }


@pytest.mark.parametrize(
    "field", ["account_id", "project_id", "extension_id", "extension_install_id", "connection_id"]
)
def test_foreign_scope_cannot_modify_connection(connection_case, field):
    scope = {**connection_case["scope"], field: str(uuid4())}
    _, result = _apply(connection_case, [_mutation("A")], scope=scope)

    assert result["applied"] == []
    assert result["errors"]
    assert _persisted(connection_case)["connection_config"] == {
        "oauth": {"subject": "original", "revision": 1}
    }


def test_absent_precondition_distinguishes_explicit_null(connection_case):
    mutation = {
        "kind": KIND,
        "payload": {"expect": [{"path": "anchor", "state": "absent"}], "set": {"anchor": None}},
    }
    assert _apply(connection_case, [mutation])[1]["errors"] == []
    _, rejected = _apply(connection_case, [mutation])
    assert rejected["errors"][0]["code"] == "host_mutation_precondition_failed"

    equals_null = deepcopy(mutation)
    equals_null["payload"]["expect"] = [{"path": "anchor", "state": "equals", "value": None}]
    equals_null["payload"]["set"] = {"anchor": "now-present"}
    assert _apply(connection_case, [equals_null])[1]["errors"] == []
    assert _persisted(connection_case)["connection_config"]["anchor"] == "now-present"


def test_conditional_write_preserves_oauth_refreshed_after_invocation_snapshot(connection_case):
    mutation = _mutation("A")
    assert _persisted(connection_case)["connection_config"]["oauth"]["revision"] == 1
    with connection_case["store"].conn.cursor() as cur:
        cur.execute(
            """UPDATE project_connections
                  SET connection_config = jsonb_set(connection_config, '{oauth}',
                      '{"subject":"refreshed","revision":2}'::jsonb)
                WHERE connection_id = %s""",
            (connection_case["scope"]["connection_id"],),
        )
    assert _apply(connection_case, [mutation])[1]["errors"] == []
    assert _persisted(connection_case)["connection_config"]["oauth"] == {
        "subject": "refreshed",
        "revision": 2,
    }
