from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.entrypoints.admin.guardrails import GuardrailSave


def _payload(**overrides):
    payload = {
        "display_name": "Policy",
        "enabled": True,
        "mode": "deterministic",
        "rule_type": "forbidden_patterns",
        "severity": "error",
        "config": {"patterns": ["secret"]},
        "agents": ["reviewer"],
    }
    payload.update(overrides)
    return payload


def test_enabled_deterministic_guardrail_rejects_unknown_rule_type() -> None:
    payload = _payload(rule_type="secret_scan")
    with pytest.raises(ValidationError, match="Unsupported deterministic rule type"):
        GuardrailSave.model_validate(payload)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"patterns": []}, "at least one pattern"),
        ({"patterns": [42]}, "strings"),
        ({"patterns": ["("]}, "invalid regular expression"),
        ({"patterns": ["   "]}, "non-blank"),
    ],
)
def test_enabled_forbidden_patterns_requires_working_regexes(config, message) -> None:
    payload = _payload(config=config)
    with pytest.raises(ValidationError, match=message):
        GuardrailSave.model_validate(payload)


def test_disabled_deterministic_draft_may_keep_an_empty_list() -> None:
    result = GuardrailSave.model_validate(_payload(enabled=False, config={"patterns": []}))

    assert result.config == {"patterns": []}


def test_store_toggle_cannot_enable_empty_deterministic_draft() -> None:
    from unittest.mock import MagicMock

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    store._column_exists = MagicMock(return_value=True)
    store.get_guardrail = MagicMock(return_value=_payload(enabled=False, config={"patterns": []}))
    store._transaction = MagicMock()
    store._transaction.return_value.__enter__.return_value.fetchone.return_value = _payload(
        enabled=False, config={"patterns": []}
    )
    store._log_change_cur = MagicMock()
    with pytest.raises(ValueError, match="at least one pattern"):
        store.toggle_guardrail(
            "00000000-0000-0000-0000-000000000733",
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin",
            actor_type="user",
        )


def test_store_toggle_validates_current_configuration_not_stale_snapshot() -> None:
    from unittest.mock import MagicMock

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    store._column_exists = MagicMock(return_value=True)
    store.get_guardrail = MagicMock(return_value=_payload(enabled=False))
    store._transaction = MagicMock()
    cursor = store._transaction.return_value.__enter__.return_value
    cursor.fetchone.return_value = _payload(enabled=False, config={"patterns": []})
    store._log_change_cur = MagicMock()
    with pytest.raises(ValueError, match="at least one pattern"):
        store.toggle_guardrail(
            "00000000-0000-0000-0000-000000000733",
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin",
            actor_type="user",
        )


@pytest.mark.parametrize(
    ("enabled", "config", "expected"),
    [(False, {"patterns": ["secret"]}, True), (True, {"patterns": ["("]}, False)],
)
def test_store_toggle_accepts_valid_activation_and_can_disable_invalid_rule(
    enabled, config, expected
):
    from unittest.mock import MagicMock

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    store._column_exists = MagicMock(return_value=True)
    snapshot = _payload(enabled=enabled, config=config)
    store.get_guardrail = MagicMock(return_value=snapshot)
    store._transaction = MagicMock()
    cursor = store._transaction.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [snapshot, {"enabled": expected}]
    store._log_change_cur = MagicMock()
    assert (
        store.toggle_guardrail(
            "00000000-0000-0000-0000-000000000733",
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin",
            actor_type="user",
        )
        is expected
    )


def test_required_sections_requires_nonblank_string_items() -> None:
    payload = _payload(rule_type="required_sections", config={"sections": [""]})
    with pytest.raises(ValidationError, match="non-blank"):
        GuardrailSave.model_validate(payload)


def test_semantic_custom_rule_type_and_config_remain_compatible() -> None:
    result = GuardrailSave.model_validate(
        _payload(
            mode="semantic",
            rule_type="customer_specific_policy",
            prompt_text="Confirm the response addresses the stated risk.",
            config={"threshold": 0.8, "custom_key": "preserved"},
        )
    )

    assert result.rule_type == "customer_specific_policy"
    assert result.config["custom_key"] == "preserved"
