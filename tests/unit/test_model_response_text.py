from __future__ import annotations

from types import SimpleNamespace

from core.application.orchestration.model_response_text import model_response_text


def test_model_response_text_preserves_plain_string_content() -> None:
    assert model_response_text(SimpleNamespace(content="plain")) == "plain"


def test_model_response_text_flattens_typed_blocks_in_order() -> None:
    response = SimpleNamespace(
        content=[
            {"type": "reasoning", "summary": []},
            {"type": "text", "text": "first"},
            {"type": "output_text", "content": " second"},
        ]
    )

    assert model_response_text(response) == "first second"


def test_model_response_text_supports_object_blocks_and_bare_strings() -> None:
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="one"), " two"])

    assert model_response_text(response) == "one two"


def test_model_response_text_skips_empty_object_attribute_before_text_content() -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(text=None, content="provider fallback")]
    )

    assert model_response_text(response) == "provider fallback"
