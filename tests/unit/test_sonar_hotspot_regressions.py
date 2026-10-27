from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_parse_effort_minutes_handles_mixed_units() -> None:
    from core.application.tooling.quality.sonar_generate_backlog import parse_effort_minutes

    assert parse_effort_minutes("2h 30min") == 150
    assert parse_effort_minutes("1d 2h") == 600
    assert parse_effort_minutes("3w") == 3 * 5 * 8 * 60


def test_markdown_component_uses_scheme_allowlist() -> None:
    component_source = (
        ROOT / "assets" / "platform-kit" / "components" / "platform-markdown.js"
    ).read_text()

    assert "startsWith('javascript:')" not in component_source
    assert 'startsWith("javascript:")' not in component_source


def test_markdown_component_rejects_unsafe_link_protocols() -> None:
    component_source = (
        ROOT / "assets" / "platform-kit" / "components" / "platform-markdown.js"
    ).read_text()

    assert "SAFE_LINK_SCHEMES" in component_source
    assert "'mailto'" in component_source
    assert "'tel'" in component_source
