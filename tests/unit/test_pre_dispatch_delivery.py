from __future__ import annotations

import pytest


def _extension(*, pre_dispatch=None):
    config = {"host_mutations": ["project_connection_upsert"]}
    if pre_dispatch is not None:
        config["pre_dispatch_host_mutations"] = pre_dispatch
    return {
        "action_definitions": [
            {
                "action_id": "deliver_notification",
                "handler": {"mode": "extension", "config": config},
            }
        ]
    }


def _prepare_invocation(*, prepared_delivery=None, host_mutations=None):
    response = {
        "ok": True,
        "delivered": False,
        "prepared_delivery": (
            {"reply_markup": {"inline_keyboard": []}}
            if prepared_delivery is None
            else prepared_delivery
        ),
    }
    if host_mutations is not None:
        response["host_mutations"] = host_mutations
    return {"status": "succeeded", "response": response}


def test_pre_dispatch_host_mutation_kinds_are_provider_neutral() -> None:
    from core.application.extensions.pre_dispatch_delivery import (
        pre_dispatch_host_mutation_kinds,
    )

    assert pre_dispatch_host_mutation_kinds(
        _extension(pre_dispatch=["project_connection_upsert"]),
        "deliver_notification",
    ) == ("project_connection_upsert",)
    assert pre_dispatch_host_mutation_kinds(_extension(), "deliver_notification") == ()


def test_validate_prepare_invocation_returns_bounded_opaque_payload() -> None:
    from core.application.extensions.pre_dispatch_delivery import (
        validate_prepare_invocation,
    )

    invocation = _prepare_invocation(
        host_mutations=[{"kind": "project_connection_upsert", "payload": {"connection_id": "c1"}}]
    )

    prepared = validate_prepare_invocation(invocation, allowed_kinds=("project_connection_upsert",))

    assert prepared.payload == {"reply_markup": {"inline_keyboard": []}}


@pytest.mark.parametrize(
    ("invocation", "allowed_kinds", "expected_code"),
    [
        ({"status": "failed", "response": {}}, (), "prepare_invocation_failed"),
        (
            {"status": "succeeded", "response": {"ok": False}},
            (),
            "prepare_response_not_ok",
        ),
        (
            {
                "status": "succeeded",
                "response": {"ok": True, "delivered": True, "prepared_delivery": {}},
            },
            (),
            "prepare_external_effect_forbidden",
        ),
        (
            {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "provider_message_id": "42",
                    "prepared_delivery": {},
                },
            },
            (),
            "prepare_external_effect_forbidden",
        ),
        (
            _prepare_invocation(
                host_mutations=[{"kind": "notification_delivery_log", "payload": {}}]
            ),
            ("project_connection_upsert",),
            "prepare_host_mutation_forbidden",
        ),
        (
            {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "delivered": False,
                    "prepared_delivery": {},
                    "result": {
                        "host_mutations": [{"kind": "notification_delivery_log", "payload": {}}]
                    },
                },
            },
            ("project_connection_upsert",),
            "prepare_host_mutation_forbidden",
        ),
        (
            {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "delivered": False,
                    "prepared_delivery": {},
                    "result": {"host_mutations": "invalid"},
                },
            },
            (),
            "prepare_host_mutation_forbidden",
        ),
        (
            _prepare_invocation(prepared_delivery={"nested": {"api_key": "secret"}}),
            (),
            "prepared_delivery_secret_key_forbidden",
        ),
        (
            _prepare_invocation(prepared_delivery=[]),
            (),
            "prepared_delivery_invalid_json",
        ),
        (
            _prepare_invocation(prepared_delivery={"value": float("nan")}),
            (),
            "prepared_delivery_invalid_json",
        ),
        (
            _prepare_invocation(prepared_delivery={"nested": [[[[[[[[[]]]]]]]]]}),
            (),
            "prepared_delivery_too_deep",
        ),
        (
            _prepare_invocation(prepared_delivery={"value": "x" * 8192}),
            (),
            "prepared_delivery_too_large",
        ),
    ],
)
def test_validate_prepare_invocation_fails_closed(invocation, allowed_kinds, expected_code) -> None:
    from core.application.extensions.pre_dispatch_delivery import (
        PreDispatchDeliveryError,
        validate_prepare_invocation,
    )

    with pytest.raises(PreDispatchDeliveryError) as exc_info:
        validate_prepare_invocation(invocation, allowed_kinds=allowed_kinds)

    assert exc_info.value.code == expected_code


def test_execute_emits_pre_dispatch_kind_detects_phase_violation() -> None:
    from core.application.extensions.pre_dispatch_delivery import (
        execute_emits_pre_dispatch_kind,
    )

    invocation = {
        "response": {
            "host_mutations": [
                {"kind": "project_connection_upsert", "payload": {"connection_id": "c1"}}
            ]
        }
    }

    assert execute_emits_pre_dispatch_kind(invocation, ("project_connection_upsert",))
    assert not execute_emits_pre_dispatch_kind(invocation, ("other",))

    nested_invocation = {
        "response": {
            "result": {"host_mutations": [{"kind": "project_connection_upsert", "payload": {}}]}
        }
    }
    assert execute_emits_pre_dispatch_kind(nested_invocation, ("project_connection_upsert",))
    assert execute_emits_pre_dispatch_kind(
        {"response": {"result": {"host_mutations": "invalid"}}},
        ("project_connection_upsert",),
    )
