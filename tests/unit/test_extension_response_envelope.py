from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeDb:
    def cursor(self):
        return None


def test_transaction_db_rebinds_python_descriptors_without_changing_call_shape() -> None:
    from core.application.extensions import response_envelope

    class _DescriptorStore:
        def __init__(self) -> None:
            self.conn = "store-connection"

        @staticmethod
        def normalize(value: str) -> str:
            return f"normalized:{value}"

        @classmethod
        def owner(cls) -> type:
            return cls

        def active_connection(self) -> str:
            return self.conn

    adapter = response_envelope._TransactionDb(_DescriptorStore(), "transaction-connection")

    assert adapter.normalize("value") == "normalized:value"
    assert adapter.owner() is _DescriptorStore
    assert adapter.active_connection() == "transaction-connection"


def test_response_envelope_applies_declared_project_state_update(monkeypatch):
    from core.application.extensions import response_envelope

    captured: dict = {}

    def _put_state(*args, **kwargs):
        captured.update(kwargs)
        return {"state_id": "state_1", "revision": 2, **kwargs}

    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", _put_state)
    invocation = {
        "status": "succeeded",
        "response": {
            "project_state_updates": [
                {
                    "namespace": "snapshots",
                    "state_key": "last_success",
                    "value": {"dataset_handle": "dataset:dst_1"},
                    "expected_revision": 1,
                }
            ]
        },
    }

    result = response_envelope.process_project_state_updates(
        invocation,
        extension={
            "install_id": "install_exact",
            "compiled_manifest": {
                "project_state": {
                    "writes": [{"namespace": "snapshots", "state_key": "last_success"}]
                }
            },
        },
        extension_id="acme.supplier-feed",
        db=_FakeDb(),
        account_id="acc_a",
        project_id="project_1",
        extension_install_id="install_exact",
        connection_id="conn_1",
        actor_id="tester",
    )

    assert invocation["status"] == "succeeded", invocation
    assert result["errors"] == []
    assert captured["namespace"] == "snapshots"
    assert captured["state_key"] == "last_success"
    assert captured["value_jsonb"] == {"dataset_handle": "dataset:dst_1"}
    assert captured["expected_revision"] == 1
    assert captured["connection_id"] == "conn_1"
    assert captured["extension_install_id"] == "install_exact"


def test_response_envelope_fails_closed_for_undeclared_project_state_write(monkeypatch):
    from core.application.extensions import response_envelope

    put_state = MagicMock()
    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", put_state)
    invocation = {
        "status": "succeeded",
        "response": {
            "project_state_updates": [
                {
                    "namespace": "snapshots",
                    "state_key": "last_success",
                    "value_jsonb": {"dataset_handle": "dataset:dst_1"},
                }
            ]
        },
    }

    result = response_envelope.process_project_state_updates(
        invocation,
        extension={"compiled_manifest": {"project_state": {"writes": []}}},
        extension_id="acme.supplier-feed",
        db=_FakeDb(),
        account_id="acc_a",
        project_id="project_1",
    )

    assert result["errors"][0]["code"] == "project_state_write_undeclared"
    assert invocation["status"] == "failed"
    assert invocation["detail"] == "project_state_write_undeclared"
    put_state.assert_not_called()


def test_response_envelope_fails_closed_without_exact_install_owner(monkeypatch):
    from core.application.extensions import response_envelope

    put_state = MagicMock()
    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", put_state)
    invocation = {
        "status": "succeeded",
        "response": {
            "project_state_updates": [
                {
                    "namespace": "snapshots",
                    "state_key": "last_success",
                    "value_jsonb": {"cursor": "next"},
                }
            ]
        },
    }

    result = response_envelope.process_project_state_updates(
        invocation,
        extension={
            "compiled_manifest": {
                "project_state": {
                    "writes": [{"namespace": "snapshots", "state_key": "last_success"}]
                }
            }
        },
        extension_id="acme.supplier-feed",
        db=_FakeDb(),
        account_id="acc_a",
        project_id="project_1",
    )

    assert result["errors"][0]["code"] == "project_state_install_scope_missing"
    assert invocation["status"] == "failed"
    put_state.assert_not_called()


def test_response_envelope_fails_closed_when_project_state_scope_is_missing(monkeypatch):
    from core.application.extensions import response_envelope

    put_state = MagicMock()
    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", put_state)
    invocation = {
        "status": "succeeded",
        "response": {
            "project_state_updates": [
                {
                    "namespace": "snapshots",
                    "state_key": "last_success",
                    "value_jsonb": {"dataset_handle": "dataset:dst_1"},
                }
            ]
        },
    }

    result = response_envelope.process_project_state_updates(
        invocation,
        extension={
            "compiled_manifest": {
                "project_state": {
                    "writes": [{"namespace": "snapshots", "state_key": "last_success"}]
                }
            }
        },
        extension_id="acme.supplier-feed",
        db=_FakeDb(),
        account_id="acc_a",
        project_id="",
    )

    assert result["errors"][0]["code"] == "project_state_scope_missing"
    assert result["errors"][0]["missing"] == ["project_id"]
    assert invocation["status"] == "failed"
    assert invocation["detail"] == "project_state_scope_missing"
    put_state.assert_not_called()


def test_response_envelope_rejects_connection_id_override(monkeypatch):
    from core.application.extensions import response_envelope

    put_state = MagicMock()
    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", put_state)
    invocation = {
        "status": "succeeded",
        "response": {
            "project_state_updates": [
                {
                    "namespace": "snapshots",
                    "state_key": "last_success",
                    "connection_id": "conn_foreign",
                    "value_jsonb": {"dataset_handle": "dataset:dst_1"},
                }
            ]
        },
    }

    result = response_envelope.process_project_state_updates(
        invocation,
        extension={
            "compiled_manifest": {
                "project_state": {
                    "writes": [{"namespace": "snapshots", "state_key": "last_success"}]
                }
            }
        },
        extension_id="acme.supplier-feed",
        db=_FakeDb(),
        account_id="acc_a",
        project_id="project_1",
        connection_id="conn_runtime",
    )

    assert result["errors"][0]["code"] == "project_state_connection_override_forbidden"
    assert invocation["status"] == "failed"
    assert invocation["detail"] == "project_state_connection_override_forbidden"
    put_state.assert_not_called()


def test_response_envelope_applies_declared_host_mutations() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    db.get_project_connection.return_value = {
        "connection_id": "conn_exact",
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "acme.notifier",
        "connection_kind": "extension",
        "connection_type": "notifier_bot",
        "extension_install_id": "install_exact",
        "display_name": "Exact notifier",
        "enabled": True,
        "connection_config": {"channels": []},
        "secret_refs": [],
        "connection_routes": [],
        "ingress_config": {},
        "workflow_mappings": {},
    }
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "save_channel",
        "response": {
            "host_mutations": [
                {
                    "kind": "project_connection_upsert",
                    "payload": {
                        "connection_id": "conn_exact",
                        "extension_install_id": "install_exact",
                        "connection_type": "notifier_bot",
                        "display_name": "Exact notifier",
                        "connection_config": {
                            "channels": [{"channel_id": "ops", "chat_id": "-1001"}]
                        },
                    },
                }
            ]
        },
    }

    response_envelope.process_project_state_updates(
        invocation,
        extension={
            "connection_types": [
                {
                    "connection_type_id": "notifier_bot",
                    "config_schema": {
                        "type": "object",
                        "properties": {"channels": {"type": "array"}},
                        "required": ["channels"],
                        "additionalProperties": False,
                    },
                    "ingress_schema": {
                        "type": "object",
                        "additionalProperties": False,
                    },
                }
            ],
            "install_metadata": {
                "runtime_grants": {
                    "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
                }
            },
            "action_definitions": [
                {
                    "action_id": "save_channel",
                    "handler": {
                        "mode": "extension",
                        "config": {"host_mutations": ["project_connection_upsert"]},
                    },
                }
            ],
        },
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_1",
        extension_install_id="install_exact",
    )

    assert invocation["status"] == "succeeded", invocation
    assert invocation["host_mutations"]["errors"] == []
    db.upsert_project_connection.assert_called_once()
    assert db.upsert_project_connection.call_args.kwargs["project_id"] == "project_1"
    assert db.upsert_project_connection.call_args.kwargs["extension_id"] == "acme.notifier"
    assert db.upsert_project_connection.call_args.kwargs["connection_id"] == "conn_exact"
    assert db.upsert_project_connection.call_args.kwargs["extension_install_id"] == "install_exact"
    assert db.upsert_project_connection.call_args.kwargs["connection_type"] == "notifier_bot"
    assert db.upsert_project_connection.call_args.kwargs["display_name"] == "Exact notifier"
    assert db.upsert_project_connection.call_args.kwargs["connection_config"]["channels"] == [
        {"channel_id": "ops", "chat_id": "-1001"}
    ]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("connection_config", {"label": "TOO-LONG", "undeclared": "TOPSECRET"}),
        ("ingress_config", {"cursor": "TOO-LONG", "undeclared": "TOPSECRET"}),
    ],
)
def test_response_envelope_rejects_invalid_final_connection_partitions_without_writing(
    field: str, invalid_value: dict[str, str]
) -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    db.get_project_connection.return_value = {
        "connection_id": "conn_exact",
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "acme.notifier",
        "connection_kind": "extension",
        "connection_type": "notifier_bot",
        "extension_install_id": "install_exact",
        "enabled": True,
        "connection_config": {"label": "ok"},
        "secret_refs": [],
        "connection_routes": [],
        "ingress_config": {"cursor": "ok"},
        "workflow_mappings": {},
    }
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "update_connection",
        "response": {
            "host_mutations": [
                {
                    "kind": "project_connection_upsert",
                    "payload": {
                        "connection_id": "conn_exact",
                        "extension_install_id": "install_exact",
                        field: invalid_value,
                    },
                }
            ]
        },
    }
    strict_string = {
        "type": "object",
        "properties": {
            "label" if field == "connection_config" else "cursor": {
                "type": "string",
                "maxLength": 3,
            }
        },
        "additionalProperties": False,
    }
    extension = {
        "extension_install_id": "install_exact",
        "connection_types": [
            {
                "connection_type_id": "notifier_bot",
                "config_schema": strict_string if field == "connection_config" else {},
                "ingress_schema": strict_string if field == "ingress_config" else {},
            }
        ],
        "install_metadata": {
            "runtime_grants": {
                "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
            }
        },
        "action_definitions": [
            {
                "action_id": "update_connection",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["project_connection_upsert"]},
                },
            }
        ],
    }

    response_envelope.process_host_mutations(
        invocation,
        extension=extension,
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_1",
        extension_install_id="install_exact",
    )

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "invalid_payload"
    assert invocation["host_mutations"]["errors"][0]["code"] == "invalid_payload"
    assert "TOPSECRET" not in invocation["host_mutations"]["errors"][0]["message"]
    db.upsert_project_connection.assert_not_called()


def test_response_envelope_atomically_claims_ingress_token_before_accepting_events() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    db.get_project_connection.return_value = {
        "connection_id": "conn_exact",
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "acme.notifier",
        "extension_install_id": "install_exact",
    }
    db.consume_project_connection_ingress_token.return_value = False
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "ingress_event",
        "response": {
            "host_mutations": [
                {
                    "kind": "project_connection_upsert",
                    "payload": {
                        "connection_id": "conn_exact",
                        "extension_install_id": "install_exact",
                        "consume_ingress_token": "opaque-once",
                    },
                }
            ],
            "events": [{"event_type": "notification.interaction_received"}],
        },
    }
    extension = {
        "extension_install_id": "install_exact",
        "install_metadata": {
            "runtime_grants": {
                "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
            }
        },
        "action_definitions": [
            {
                "action_id": "ingress_event",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["project_connection_upsert"]},
                },
            }
        ],
    }

    response_envelope.process_host_mutations(
        invocation,
        extension=extension,
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_1",
    )

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "host_mutation_failed"
    db.upsert_project_connection.assert_not_called()


def test_response_envelope_rejects_subprocess_connection_identity_takeover() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    db.get_project_connection.return_value = {
        "connection_id": "conn_owned",
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "acme.notifier",
        "extension_install_id": "install_exact",
    }
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "update_connection",
        "response": {
            "host_mutations": [
                {
                    "kind": "project_connection_upsert",
                    "payload": {
                        "connection_id": "conn_foreign",
                        "extension_install_id": "install_exact",
                        "connection_config": {"enabled": False},
                    },
                }
            ]
        },
    }
    extension = {
        "extension_install_id": "install_exact",
        "install_metadata": {
            "runtime_grants": {
                "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
            }
        },
        "action_definitions": [
            {
                "action_id": "update_connection",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["project_connection_upsert"]},
                },
            }
        ],
    }

    response_envelope.process_host_mutations(
        invocation,
        extension=extension,
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_1",
    )

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "host_mutation_failed"
    db.upsert_project_connection.assert_not_called()


def test_response_envelope_rejects_undeclared_host_mutation() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "read_only",
        "response": {
            "host_mutations": [
                {"kind": "project_connection_upsert", "payload": {"connection_config": {}}}
            ]
        },
    }

    response_envelope.process_project_state_updates(
        invocation,
        extension={
            "install_metadata": {
                "runtime_grants": {
                    "mutations": {"notification_delivery_log": {"provider_key": "acme.notifier"}}
                }
            },
            "action_definitions": [{"action_id": "read_only", "handler": {"mode": "extension"}}],
        },
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_1",
    )

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "host_mutation_undeclared"
    db.upsert_project_connection.assert_not_called()


def test_notification_delivery_mutation_uses_authoritative_project_scope() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "retry",
        "response": {
            "host_mutations": [
                {
                    "kind": "notification_delivery_log",
                    "payload": {
                        "project_id": "project_foreign",
                        "provider_key": "acme.notifier",
                        "event_type": "job.failed",
                        "severity": "error",
                        "status": "delivered",
                        "request": {},
                        "response": {"ok": True},
                    },
                }
            ]
        },
    }

    response_envelope.process_project_state_updates(
        invocation,
        extension={
            "install_metadata": {
                "runtime_grants": {
                    "mutations": {"notification_delivery_log": {"provider_key": "acme.notifier"}}
                }
            },
            "action_definitions": [
                {
                    "action_id": "retry",
                    "handler": {
                        "mode": "extension",
                        "config": {"host_mutations": ["notification_delivery_log"]},
                    },
                }
            ],
        },
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_authoritative",
    )

    assert invocation["status"] == "succeeded"
    assert db.log_notification_delivery.call_args.kwargs["project_id"] == "project_authoritative"


def test_a_host_mutation_not_declared_in_runtime_grants_is_refused() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "replace_routes",
        "response": {
            "host_mutations": [{"kind": "notification_routes_save", "payload": {"routes": []}}]
        },
    }
    extension = {
        "action_definitions": [
            {
                "action_id": "replace_routes",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["notification_routes_save"]},
                },
            }
        ]
    }

    response_envelope.process_host_mutations(
        invocation,
        extension=extension,
        extension_id="evil.extension",
        db=db,
        account_id="acc_a",
        project_id="project_1",
    )

    assert invocation["detail"] == "host_mutation_not_granted"
    db.save_notification_routes.assert_not_called()


def test_connection_mutation_preserves_secret_refs_and_allows_empty_fields() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    db.get_project_connection.return_value = {
        "connection_id": "conn_owned",
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "acme.notifier",
        "extension_install_id": "install_exact",
        "connection_kind": "extension",
        "connection_type": "notifier_bot",
        "enabled": True,
        "connection_config": {"old": True},
        "secret_refs": {"refs": {"bot_token": "secret_ref"}},
        "connection_routes": [{"route_key": "old"}],
        "ingress_config": {"mode": "webhook"},
        "workflow_mappings": {"one": "two"},
    }
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "clear",
        "response": {
            "host_mutations": [
                {
                    "kind": "project_connection_upsert",
                    "payload": {
                        "connection_config": {},
                        "connection_routes": [],
                        "ingress_config": {},
                    },
                }
            ]
        },
    }
    extension = {
        "extension_install_id": "install_exact",
        "connection_types": [
            {
                "connection_type_id": "notifier_bot",
                "config_schema": {"type": "object"},
                "ingress_schema": {"type": "object"},
            }
        ],
        "install_metadata": {
            "runtime_grants": {
                "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
            }
        },
        "action_definitions": [
            {
                "action_id": "clear",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["project_connection_upsert"]},
                },
            }
        ],
    }

    response_envelope.process_host_mutations(
        invocation,
        extension=extension,
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_1",
    )

    saved = db.upsert_project_connection.call_args.kwargs
    assert saved["secret_refs"] == {"refs": {"bot_token": "secret_ref"}}
    assert saved["connection_config"] == {}
    assert saved["connection_routes"] == []
    assert saved["ingress_config"] == {}


def test_connection_mutation_preserves_every_unowned_persisted_column() -> None:
    from core.application.extensions import response_envelope

    existing = {
        "connection_id": "conn_owned",
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "acme.notifier",
        "extension_install_id": "install_exact",
        "connection_kind": "extension",
        "display_name": "Production notifier",
        "connection_type": "notifier_bot",
        "environment": "production",
        "credential_revision": 9,
        "runtime_mode": "remote",
        "remote_base_url": "https://notifier.example.test",
        "health_status": "healthy",
        "runtime_status": "ready",
        "last_seen_ok_at": 1_725_000_123.5,
        "last_error": "previous transient error",
        "enabled": True,
        "connection_config": {"channels": [{"channel_id": "ops"}]},
        "secret_refs": {"refs": {"bot_token": "secret_ref"}},
        "connection_routes": [{"route_key": "ops"}],
        "ingress_config": {"pending_update_count": 3},
        "workflow_mappings": {"workflow-a": "ops"},
        "tags": ["production", "critical"],
    }
    db = MagicMock()
    db.get_project_connection.return_value = existing
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "save_inbound",
        "response": {
            "host_mutations": [
                {
                    "kind": "project_connection_upsert",
                    "payload": {"ingress_config": {"pending_update_count": 4}},
                }
            ]
        },
    }
    extension = {
        "extension_install_id": "install_exact",
        "connection_types": [
            {
                "connection_type_id": "notifier_bot",
                "config_schema": {"type": "object"},
                "ingress_schema": {"type": "object"},
            }
        ],
        "install_metadata": {
            "runtime_grants": {
                "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
            }
        },
        "action_definitions": [
            {
                "action_id": "save_inbound",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["project_connection_upsert"]},
                },
            }
        ],
    }

    response_envelope.process_host_mutations(
        invocation,
        extension=extension,
        extension_id="acme.notifier",
        db=db,
        account_id="acc_a",
        project_id="project_1",
    )

    saved = db.upsert_project_connection.call_args.kwargs
    for field in (
        "connection_id",
        "account_id",
        "connection_kind",
        "display_name",
        "connection_type",
        "environment",
        "credential_revision",
        "extension_install_id",
        "runtime_mode",
        "remote_base_url",
        "health_status",
        "runtime_status",
        "last_seen_ok_at",
        "last_error",
        "enabled",
        "connection_config",
        "secret_refs",
        "connection_routes",
        "workflow_mappings",
        "tags",
    ):
        assert saved[field] == existing[field]
    assert saved["project_id"] == "project_1"
    assert saved["extension_id"] == "acme.notifier"
    assert saved["ingress_config"] == {"pending_update_count": 4}


def test_host_mutation_batch_validates_before_any_write() -> None:
    from core.application.extensions import response_envelope

    db = MagicMock()
    invocation = {
        "status": "succeeded",
        "operation_type": "action",
        "operation_id": "batch",
        "response": {
            "host_mutations": [
                {
                    "kind": "notification_delivery_log",
                    "payload": {
                        "provider_key": "fixture.notification-provider",
                        "event_type": "job.failed",
                    },
                },
                {
                    "kind": "notification_delivery_log",
                    "payload": {"provider_key": "foreign", "event_type": "job.failed"},
                },
            ]
        },
    }
    extension = {
        "install_metadata": {
            "runtime_grants": {
                "mutations": {
                    "notification_delivery_log": {"provider_key": "fixture.notification-provider"}
                }
            }
        },
        "action_definitions": [
            {
                "action_id": "batch",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["notification_delivery_log"]},
                },
            }
        ],
    }

    response_envelope.process_host_mutations(
        invocation,
        extension=extension,
        extension_id="fixture.notification-provider",
        db=db,
        account_id="acc_a",
        project_id="project_1",
    )

    assert invocation["detail"] == "host_mutation_provider_forbidden"
    db.log_notification_delivery.assert_not_called()
