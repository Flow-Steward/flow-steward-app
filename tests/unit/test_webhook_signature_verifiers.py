"""Unit tests for the generic configuration-driven webhook signature verifier."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io

import pytest

from core.application.webhooks.signature_verifiers import (
    WebhookSignatureConfigError,
    normalize_webhook_signature_config,
    verify_webhook_signature,
    webhook_signature_reserved_headers,
)

SECRET = "shhh-signing-secret"  # pragma: allowlist secret
SECRET_REF = "workflow_entrypoint.inventory.secret.webhook_signature"  # pragma: allowlist secret
BODY = b'{"event":"invoice.paid","id":"evt_1"}'
NOW = 1_760_000_000.0


def _hex_digest(payload: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _base64_digest(payload: bytes) -> str:
    return base64.b64encode(
        hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("ascii")


def _raw_body_config(**signature: object) -> dict[str, object]:
    return {
        "signature": {"source": "header", **signature},
        "timestamp": {"source": "none"},
        "signed_payload": {"parts": [{"source": "raw_body"}]},
    }


def _stripe_style_config() -> dict[str, object]:
    return {
        "signature": {
            "source": "header",
            "header": "Stripe-Signature",
            "parser": "comma_kv",
            "value_key": "v1",
            "encoding": "hex",
            "allow_multiple": True,
        },
        "timestamp": {
            "source": "signature_param",
            "key": "t",
            "unit": "seconds",
            "tolerance_seconds": 300,
        },
        "signed_payload": {
            "parts": [
                {"source": "timestamp"},
                {"literal": "."},
                {"source": "raw_body"},
            ]
        },
    }


def _slack_style_config(*, unit: str = "seconds") -> dict[str, object]:
    return {
        "signature": {
            "source": "header",
            "header": "X-Slack-Signature",
            "parser": "prefixed",
            "prefix": "v0=",
            "encoding": "hex",
        },
        "timestamp": {
            "source": "header",
            "header": "X-Slack-Request-Timestamp",
            "unit": unit,
            "tolerance_seconds": 300,
        },
        "signed_payload": {
            "parts": [
                {"literal": "v0:"},
                {"source": "timestamp"},
                {"literal": ":"},
                {"source": "raw_body"},
            ]
        },
    }


def _verify(config, headers, **kwargs):
    return verify_webhook_signature(
        headers=headers,
        secret=SECRET,
        config=config,
        raw_body=kwargs.pop("raw_body", BODY),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def test_raw_hex_body_signature_is_accepted() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = _verify(config, {"X-Signature": _hex_digest(BODY)})

    assert result.ok is True
    assert result.reason == ""


def test_header_lookup_is_case_insensitive_and_trims_whitespace() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = _verify(config, {"x-SIGNATURE": f"  {_hex_digest(BODY)}  "})

    assert result.ok is True


def test_uppercase_hex_digest_is_accepted() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = _verify(config, {"X-Signature": _hex_digest(BODY).upper()})

    assert result.ok is True


def test_prefixed_hex_body_signature_is_accepted() -> None:
    config = _raw_body_config(
        header="X-Hub-Signature-256", parser="prefixed", prefix="sha256=", encoding="hex"
    )

    result = _verify(config, {"X-Hub-Signature-256": f"sha256={_hex_digest(BODY)}"})

    assert result.ok is True


def test_prefixed_parser_rejects_a_header_without_the_prefix() -> None:
    config = _raw_body_config(
        header="X-Hub-Signature-256", parser="prefixed", prefix="sha256=", encoding="hex"
    )

    result = _verify(config, {"X-Hub-Signature-256": _hex_digest(BODY)})

    assert result.ok is False
    assert result.reason == "auth_failed"


def test_raw_base64_body_signature_is_accepted() -> None:
    config = _raw_body_config(header="X-Acme-Hmac-Sha256", parser="raw", encoding="base64")

    result = _verify(config, {"x-acme-hmac-sha256": _base64_digest(BODY)})

    assert result.ok is True


def test_base64_config_rejects_the_hex_rendering_of_the_same_digest() -> None:
    config = _raw_body_config(header="X-Acme-Hmac-Sha256", parser="raw", encoding="base64")

    result = _verify(config, {"X-Acme-Hmac-Sha256": _hex_digest(BODY)})

    assert result.ok is False
    assert result.error == "Invalid webhook signature"


def test_comma_kv_signature_and_timestamp_are_extracted() -> None:
    signed = f"{int(NOW)}.".encode() + BODY
    header = f"t={int(NOW)},v1={_hex_digest(signed)}"

    result = _verify(_stripe_style_config(), {"Stripe-Signature": header})

    assert result.ok is True


def test_comma_kv_accepts_any_matching_duplicate_key_when_allow_multiple() -> None:
    signed = f"{int(NOW)}.".encode() + BODY
    stale = "a" * 64
    header = f"t={int(NOW)},v1={stale},v1={_hex_digest(signed)}"

    result = _verify(_stripe_style_config(), {"Stripe-Signature": header})

    assert result.ok is True


def test_comma_kv_rejects_duplicate_keys_when_allow_multiple_is_disabled() -> None:
    config = _stripe_style_config()
    config["signature"]["allow_multiple"] = False
    signed = f"{int(NOW)}.".encode() + BODY
    header = f"t={int(NOW)},v1={'a' * 64},v1={_hex_digest(signed)}"

    result = _verify(config, {"Stripe-Signature": header})

    assert result.ok is False
    assert result.error == "Webhook signature header is malformed"


@pytest.mark.parametrize(
    "header",
    [
        't=1760000000,v1="deadbeef"',
        r"t=1760000000,v1=dead\beef",
        "t=1760000000,v1={inner=1}",
        "t=1760000000,v1",
        "t=1760000000,,v1=deadbeef",
        "t=1760000000,=deadbeef",
    ],
)
def test_comma_kv_rejects_quoted_escaped_nested_and_empty_fields(header: str) -> None:
    result = _verify(_stripe_style_config(), {"Stripe-Signature": header})

    assert result.ok is False
    assert result.reason == "auth_failed"


def test_timestamp_from_a_separate_header_is_accepted() -> None:
    signed = f"v0:{int(NOW)}:".encode() + BODY

    result = _verify(
        _slack_style_config(),
        {
            "X-Slack-Signature": f"v0={_hex_digest(signed)}",
            "X-Slack-Request-Timestamp": str(int(NOW)),
        },
    )

    assert result.ok is True


def test_millisecond_timestamps_are_normalized_for_tolerance_but_signed_verbatim() -> None:
    milliseconds = int(NOW) * 1000
    signed = f"v0:{milliseconds}:".encode() + BODY

    result = _verify(
        _slack_style_config(unit="milliseconds"),
        {
            "X-Slack-Signature": f"v0={_hex_digest(signed)}",
            "X-Slack-Request-Timestamp": str(milliseconds),
        },
    )

    assert result.ok is True


def test_expired_timestamp_is_rejected_before_digest_comparison() -> None:
    stale = int(NOW) - 900
    signed = f"v0:{stale}:".encode() + BODY

    result = _verify(
        _slack_style_config(),
        {
            "X-Slack-Signature": f"v0={_hex_digest(signed)}",
            "X-Slack-Request-Timestamp": str(stale),
        },
    )

    assert result.ok is False
    assert result.error == "Webhook timestamp is too old"


def test_future_timestamp_is_rejected() -> None:
    ahead = int(NOW) + 900
    signed = f"v0:{ahead}:".encode() + BODY

    result = _verify(
        _slack_style_config(),
        {
            "X-Slack-Signature": f"v0={_hex_digest(signed)}",
            "X-Slack-Request-Timestamp": str(ahead),
        },
    )

    assert result.ok is False
    assert result.error == "Webhook timestamp is too far in the future"


def test_malformed_timestamp_is_rejected() -> None:
    result = _verify(
        _slack_style_config(),
        {
            "X-Slack-Signature": f"v0={'a' * 64}",
            "X-Slack-Request-Timestamp": "not-a-timestamp",
        },
    )

    assert result.ok is False
    assert result.error == "Webhook timestamp is malformed"


def test_missing_timestamp_header_is_rejected() -> None:
    result = _verify(
        _slack_style_config(),
        {"X-Slack-Signature": f"v0={'a' * 64}"},
    )

    assert result.ok is False
    assert result.error == "Webhook timestamp header is missing"


def test_missing_signature_header_is_rejected() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = _verify(config, {"Content-Type": "application/json"})

    assert result.ok is False
    assert result.error == "Webhook signature header is missing"


def test_empty_signature_header_is_rejected() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = _verify(config, {"X-Signature": "   "})

    assert result.ok is False
    assert result.error == "Webhook signature header is empty"


def test_oversized_signature_header_is_rejected() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = _verify(config, {"X-Signature": "a" * 9000})

    assert result.ok is False
    assert result.error == "Webhook signature header is too large"


def test_body_tampering_is_rejected() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = _verify(
        config,
        {"X-Signature": _hex_digest(BODY)},
        raw_body=BODY + b" ",
    )

    assert result.ok is False
    assert result.error == "Invalid webhook signature"


def test_missing_secret_is_reported_as_a_configuration_failure() -> None:
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")

    result = verify_webhook_signature(
        headers={"X-Signature": _hex_digest(BODY)},
        secret="   ",
        config=config,
        raw_body=BODY,
        now=NOW,
    )

    assert result.ok is False
    assert result.reason == "config_invalid"


@pytest.mark.parametrize(
    "config",
    [
        _raw_body_config(header="X-Signature", parser="rot13", encoding="hex"),
        _raw_body_config(header="X-Signature", parser="raw", encoding="base32"),
        _raw_body_config(header="", parser="raw", encoding="hex"),
        _raw_body_config(header="X Signature", parser="raw", encoding="hex"),
        _raw_body_config(header="X-Signature", parser="prefixed", encoding="hex"),
        _raw_body_config(header="X-Signature", parser="comma_kv", encoding="hex"),
        {
            "signature": {"source": "query", "header": "X-Signature", "parser": "raw"},
            "timestamp": {"source": "none"},
            "signed_payload": {"parts": [{"source": "raw_body"}]},
        },
    ],
)
def test_unsupported_signature_config_fails_closed(config: dict[str, object]) -> None:
    result = _verify(config, {"X-Signature": _hex_digest(BODY)})

    assert result.ok is False
    assert result.reason == "config_invalid"


@pytest.mark.parametrize(
    "timestamp",
    [
        {"source": "clock", "unit": "seconds"},
        {"source": "header", "header": "X-Ts", "unit": "nanoseconds"},
        {"source": "header", "header": "X-Ts", "unit": "seconds", "tolerance_seconds": 0},
        {"source": "header", "header": "X-Ts", "unit": "seconds", "tolerance_seconds": -5},
        {"source": "header", "header": "X-Ts", "unit": "seconds", "tolerance_seconds": 999999},
        {"source": "header", "unit": "seconds"},
        {"source": "signature_param", "unit": "seconds"},
    ],
)
def test_unsupported_timestamp_config_fails_closed(timestamp: dict[str, object]) -> None:
    config = {
        "signature": {
            "source": "header",
            "header": "X-Signature",
            "parser": "prefixed",
            "prefix": "v0=",
            "encoding": "hex",
        },
        "timestamp": timestamp,
        "signed_payload": {
            "parts": [
                {"literal": "v0:"},
                {"source": "timestamp"},
                {"literal": ":"},
                {"source": "raw_body"},
            ]
        },
    }

    result = _verify(config, {"X-Signature": f"v0={'a' * 64}", "X-Ts": str(int(NOW))})

    assert result.ok is False
    assert result.reason == "config_invalid"


def test_signature_param_timestamp_requires_the_comma_kv_parser() -> None:
    config = _stripe_style_config()
    config["signature"] = {
        "source": "header",
        "header": "Stripe-Signature",
        "parser": "raw",
        "encoding": "hex",
    }

    with pytest.raises(WebhookSignatureConfigError, match="comma_kv"):
        normalize_webhook_signature_config(config)


@pytest.mark.parametrize(
    "parts",
    [
        [],
        [{"source": "timestamp"}],
        [{"source": "raw_body"}, {"source": "raw_body"}],
        [{"source": "raw_body"}, {"literal": "."}, {"source": "timestamp"}],
        [{"source": "method"}, {"source": "raw_body"}],
        [{"literal": "x:"}, {"source": "timestamp"}, {"literal": ":"}, {"source": "raw_body"}],
        [{"source": "raw_body", "literal": "."}],
        ["raw_body"],
    ],
)
def test_unsupported_signed_payload_recipes_are_rejected(parts: list[object]) -> None:
    config = {
        "signature": {
            "source": "header",
            "header": "X-Signature",
            "parser": "raw",
            "encoding": "hex",
        },
        "timestamp": {"source": "none"},
        "signed_payload": {"parts": parts},
    }

    with pytest.raises(WebhookSignatureConfigError):
        normalize_webhook_signature_config(config)


def test_signed_payload_parts_must_be_a_list() -> None:
    config = {
        "signature": {
            "source": "header",
            "header": "X-Signature",
            "parser": "raw",
            "encoding": "hex",
        },
        "timestamp": {"source": "none"},
        "signed_payload": {"parts": "raw_body"},
    }

    with pytest.raises(WebhookSignatureConfigError, match="must be a list"):
        normalize_webhook_signature_config(config)


def test_timestamp_recipe_without_timestamp_config_is_rejected() -> None:
    config = _stripe_style_config()
    config["timestamp"] = {"source": "none"}

    with pytest.raises(WebhookSignatureConfigError, match="timestamp verification"):
        normalize_webhook_signature_config(config)


def test_timestamp_config_without_a_timestamp_recipe_is_rejected() -> None:
    config = _slack_style_config()
    config["signed_payload"] = {"parts": [{"source": "raw_body"}]}

    with pytest.raises(WebhookSignatureConfigError, match="signed_payload that includes"):
        normalize_webhook_signature_config(config)


def test_normalize_applies_defaults_and_carries_the_secret_reference() -> None:
    normalized = normalize_webhook_signature_config(
        {
            "signature": {"header": "X-Signature"},
            "signed_payload": {"parts": [{"source": "raw_body"}]},
        },
        secret_ref=SECRET_REF,
    )

    assert normalized == {
        "signature": {
            "source": "header",
            "header": "X-Signature",
            "parser": "raw",
            "encoding": "hex",
        },
        "timestamp": {"source": "none"},
        "signed_payload": {"parts": [{"source": "raw_body"}]},
        "secret_ref": SECRET_REF,
    }


@pytest.mark.parametrize("tolerance", [1.5, "1.5", "300.0", " 12 s", [300], {"seconds": 300}])
def test_fractional_or_non_numeric_tolerance_is_rejected_instead_of_truncated(
    tolerance: object,
) -> None:
    config = _stripe_style_config()
    config["timestamp"]["tolerance_seconds"] = tolerance

    with pytest.raises(WebhookSignatureConfigError, match="whole number"):
        normalize_webhook_signature_config(config)


@pytest.mark.parametrize(("tolerance", "expected"), [(120, 120), (120.0, 120), ("120", 120)])
def test_whole_second_tolerances_are_accepted_in_every_json_rendering(
    tolerance: object, expected: int
) -> None:
    config = _stripe_style_config()
    config["timestamp"]["tolerance_seconds"] = tolerance

    normalized = normalize_webhook_signature_config(config)

    assert normalized["timestamp"]["tolerance_seconds"] == expected


def test_normalize_defaults_comma_kv_tolerance_to_300_seconds() -> None:
    config = _stripe_style_config()
    del config["timestamp"]["tolerance_seconds"]

    normalized = normalize_webhook_signature_config(config)

    assert normalized["timestamp"]["tolerance_seconds"] == 300


def test_streamed_raw_body_file_is_verified_without_buffering() -> None:
    payload = b"x" * (3 * 1024 * 1024 + 17)
    staged = io.BytesIO(payload)
    config = _raw_body_config(header="X-Signature", parser="raw", encoding="hex")
    digest = hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    result = verify_webhook_signature(
        headers={"X-Signature": digest},
        secret=SECRET,
        config=config,
        raw_body=b"",
        raw_body_file=staged,
        now=NOW,
    )

    assert result.ok is True
    assert staged.tell() == 0


def test_streamed_raw_body_file_participates_in_a_timestamped_recipe() -> None:
    payload = b'{"streamed":true}'
    staged = io.BytesIO(payload)
    signed = f"{int(NOW)}.".encode() + payload
    header = f"t={int(NOW)},v1={_hex_digest(signed)}"

    result = verify_webhook_signature(
        headers={"Stripe-Signature": header},
        secret=SECRET,
        config=_stripe_style_config(),
        raw_body=b"",
        raw_body_file=staged,
        now=NOW,
    )

    assert result.ok is True


def test_reserved_headers_expose_signature_and_timestamp_header_names() -> None:
    assert webhook_signature_reserved_headers(_slack_style_config()) == {
        "x-slack-signature",
        "x-slack-request-timestamp",
    }
    assert webhook_signature_reserved_headers(_stripe_style_config()) == {"stripe-signature"}
    assert webhook_signature_reserved_headers(None) == set()
