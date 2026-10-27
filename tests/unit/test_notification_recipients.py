"""Admission is pure, declared by the provider, and never a send attempt."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from core.application.workflows.notification_recipients import (
    notification_recipient_format,
    validate_notification_reaction_actions,
)


def _extension(to):
    return {"schemas": {"subscriptions": {"properties": {"to": to}}}}


def _email_extension():
    return _extension({"type": "array", "items": {"type": "string", "format": "email"}})


@pytest.mark.parametrize(
    "schema,expected",
    [
        ({"type": "array", "items": {"type": "string", "format": "email"}}, "email"),
        ({"type": "array", "items": {"type": "string", "format": "uuid"}}, "uuid"),
        ({"type": "string", "format": "email"}, None),
        ({"type": "string", "items": {"format": "email"}}, None),
        ({"type": "array", "items": {"type": "string"}}, None),
        ({"type": "array"}, None),
        (
            {
                "oneOf": [
                    {"type": "array", "items": {"format": "email"}},
                    {"type": "array", "items": {"format": "uuid"}},
                ]
            },
            "unverified",
        ),
    ],
)
def test_format_requires_normalized_array_item_declaration(schema, expected):
    extension = _extension(schema)
    assert notification_recipient_format(extension) == expected
    assert notification_recipient_format({"compiled_manifest": extension}) == expected


def test_recipient_target_kind_or_provider_identity_never_implies_email():
    assert (
        notification_recipient_format(
            {
                "extension_id": "acme.mailer",
                "external_effects": [
                    {"observability": {"target_kind": "recipient"}, "channel": "email"}
                ],
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "recipients",
    [
        "Ops+tag@Example.COM, anna@localhost",
        [" Ops+tag@Example.COM ", "anna@localhost"],
        ["{{ event.email }}"],
    ],
)
def test_admission_preserves_csv_list_case_order_and_expression_bytes(recipients):
    rules = [
        {
            "enabled": True,
            "action": {
                "type": "notify",
                "target": "extension_channel",
                "provider_key": "acme.mailer",
                "recipients": recipients,
            },
        }
    ]
    before = deepcopy(rules)
    validate_notification_reaction_actions(rules, extension_for=lambda _: _email_extension())
    assert rules == before


@pytest.mark.parametrize(
    "recipients",
    [
        "ops@example.com, not-an-email",
        ["{{ event.email }}", "not-an-email"],
        [42],
        ["ops@example.com", "\r\n"],
    ],
)
def test_all_malformed_literals_refuse_the_whole_list(recipients):
    rules = [
        {
            "action": {
                "type": "notify",
                "target": "extension_channel",
                "provider_key": "acme.mailer",
                "recipients": recipients,
            }
        }
    ]
    before = deepcopy(rules)
    with pytest.raises(ValueError, match="valid email addresses"):
        validate_notification_reaction_actions(rules, extension_for=lambda _: _email_extension())
    assert rules == before


def test_disabling_a_malformed_draft_needs_no_provider_resolution():
    resolver = MagicMock(side_effect=AssertionError("disabled rule must not resolve provider"))
    validate_notification_reaction_actions(
        [
            {
                "enabled": False,
                "action": {
                    "type": "notify",
                    "target": "extension_channel",
                    "provider_key": "acme.mailer",
                    "recipients": ["invalid"],
                },
            }
        ],
        extension_for=resolver,
    )
    resolver.assert_not_called()


def test_unsupported_declared_formats_remain_generic():
    validate_notification_reaction_actions(
        [
            {
                "action": {
                    "type": "notify",
                    "target": "extension_channel",
                    "provider_key": "acme.identifiers",
                    "recipients": ["route:ops"],
                }
            }
        ],
        extension_for=lambda _: _extension({"type": "array", "items": {"format": "uuid"}}),
    )


def test_unknown_unverified_format_remains_generic():
    extension = _extension({"type": "array", "items": {"type": "string", "format": "unverified"}})
    assert notification_recipient_format(extension) == "unverified"
    rules = [
        {
            "action": {
                "type": "notify",
                "target": "extension_channel",
                "provider_key": "acme.identifiers",
                "recipients": ["route:ops"],
            }
        }
    ]
    before = deepcopy(rules)

    validate_notification_reaction_actions(rules, extension_for=lambda _: extension)

    assert rules == before


def test_conflicting_item_formats_still_refuse_admission():
    extension = _extension(
        {
            "oneOf": [
                {"type": "array", "items": {"format": "email"}},
                {"type": "array", "items": {"format": "uuid"}},
            ]
        }
    )
    rules = [
        {
            "action": {
                "type": "notify",
                "target": "extension_channel",
                "provider_key": "acme.mailer",
                "recipients": ["route:ops"],
            }
        }
    ]
    before = deepcopy(rules)

    with pytest.raises(ValueError, match="conflicting formats"):
        validate_notification_reaction_actions(rules, extension_for=lambda _: extension)

    assert rules == before
