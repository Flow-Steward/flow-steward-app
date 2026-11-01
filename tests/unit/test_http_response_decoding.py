"""HTTP inline bodies retain source content independently of diagnostic excerpts."""

import pytest

from core.application.orchestration.http_response_decoding import _inline_payload_from_bytes


@pytest.mark.parametrize(
    "content_type,encoding,prefix",
    [
        ("text/plain; charset=utf-8", "utf-8", "Public source evidence. "),
        ("text/html; charset=utf-8", "utf-8", "<p>Public source evidence.</p>"),
        ("application/json", "utf-8", "Invalid JSON is still source text. "),
        ("text/plain; charset=iso-8859-1", "iso-8859-1", "Café source evidence. "),
    ],
)
def test_inline_text_preserves_evidence_beyond_diagnostic_excerpt(content_type, encoding, prefix):
    source = prefix * 200 + "END OF SOURCE: final acceptance evidence."

    body, excerpt = _inline_payload_from_bytes(source.encode(encoding), content_type)

    assert body == source
    assert excerpt == source[:2000]
    assert "final acceptance evidence" in body
    assert "final acceptance evidence" not in excerpt


def test_inline_json_preserves_structured_body_and_bounds_only_excerpt():
    source = '{"evidence": "' + "x" * 3000 + '", "tail": "final evidence"}'

    body, excerpt = _inline_payload_from_bytes(source.encode(), "application/json")

    assert body == {"evidence": "x" * 3000, "tail": "final evidence"}
    assert excerpt == source[:2000]
