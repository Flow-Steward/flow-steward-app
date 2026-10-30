"""Declared recipient syntax for workflow notification admission.

Only an explicit email format on normalized subscription ``to`` items opts in.
Other schema formats remain annotations. Expression-shaped values containing
``{{`` defer literal checking; this does not promise expansion or delivery.
"""

from __future__ import annotations

from collections.abc import Callable
from email.utils import parseaddr
from typing import Any

_EMAIL_LOCAL_PUNCTUATION = frozenset(".!#$%&'*+-/=?^_`{|}~")
_EMAIL_RECIPIENT_ERROR = "Enter valid email addresses, separated by commas."


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _array_item_formats(schema: dict[str, Any]) -> list[str | None]:
    formats: list[str | None] = []
    types = schema.get("type")
    if types == "array" or (isinstance(types, list) and "array" in types):
        value = _mapping(schema.get("items")).get("format")
        formats.append(value if isinstance(value, str) else None)
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                formats.extend(_array_item_formats(_mapping(branch)))
    return formats


def _notification_recipient_format_projection(
    extension: dict[str, Any] | None,
) -> tuple[str | None, bool]:
    extension = _mapping(extension)
    compiled = _mapping(extension.get("compiled_manifest"))
    schemas = _mapping(compiled.get("schemas") or extension.get("schemas"))
    properties = _mapping(_mapping(schemas.get("subscriptions")).get("properties"))
    formats = _array_item_formats(_mapping(properties.get("to")))
    if not formats or not any(formats):
        return None, False
    if len(set(formats)) != 1:
        return None, True
    return formats[0], False


def notification_recipient_format(extension: dict[str, Any] | None) -> str | None:
    """Project the same compiled item declaration for inventory and admission.

    Scalar CSV alternatives do not declare the format of normalized items.
    Conflicting item declarations are explicitly unverified; unknown formats
    remain their declared names and are not treated as email.
    """
    declared_format, conflicting = _notification_recipient_format_projection(extension)
    return "unverified" if conflicting else declared_format


def _valid_literal_email(value: str) -> bool:
    if "\r" in value or "\n" in value:
        return False
    address = value.strip()
    name, parsed = parseaddr(address)
    if name or parsed != address or address.count("@") != 1 or not address.isascii():
        return False
    local, domain = address.split("@")
    # Plain addr-spec parity with the editor's native email input, including
    # local domains. No account normalization or dotted-domain requirement.
    if not local or any(not (c.isalnum() or c in _EMAIL_LOCAL_PUNCTUATION) for c in local):
        return False
    return all(
        label
        and len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(c.isalnum() or c == "-" for c in label)
        for label in domain.split(".")
    )


def validate_notification_reaction_actions(
    rules: list[dict[str, Any]],
    *,
    extension_for: Callable[[str], dict[str, Any] | None],
) -> None:
    """Reject an enabled declared-email action before replacing any rule list."""
    for rule in rules:
        action = _mapping(rule.get("action"))
        action_type = str(action.get("type") or "").strip().lower()
        action_target = str(action.get("target") or "").strip().lower()
        if not rule.get("enabled", True) or action_type != "notify":
            continue
        if action_target != "extension_channel":
            continue
        provider_key = str(action.get("provider_key") or "").strip()
        if not provider_key:
            continue
        recipient_format, conflicting = _notification_recipient_format_projection(
            extension_for(provider_key)
        )
        if conflicting:
            raise ValueError(
                "Recipient schema declares conflicting formats. Update the provider schema."
            )
        if recipient_format != "email":
            continue
        raw = action.get("recipients")
        if isinstance(raw, list):
            values = raw
        elif isinstance(raw, str):
            values = raw.split(",")
        else:
            values = []
        if not any(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(_EMAIL_RECIPIENT_ERROR)
        for value in values:
            if not isinstance(value, str):
                raise ValueError(_EMAIL_RECIPIENT_ERROR)
            if "{{" in value:
                continue
            if (value.strip() or "\r" in value or "\n" in value) and not _valid_literal_email(
                value
            ):
                raise ValueError(_EMAIL_RECIPIENT_ERROR)
