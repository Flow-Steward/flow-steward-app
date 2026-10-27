from __future__ import annotations

import re
from pathlib import Path

SHELL_CSS = Path(__file__).resolve().parents[2] / "assets" / "shell.css"


def _read_css() -> str:
    return SHELL_CSS.read_text()


def test_auth_btn_uses_accent_token():
    """Login button must use var(--accent) so it respects the design-system token."""
    css = _read_css()
    match = re.search(r"\.auth-btn\{([^}]+)\}", css)
    assert match, ".auth-btn rule not found in shell.css"
    rule_body = match.group(1)
    assert "background:var(--accent)" in rule_body, (
        f"Expected .auth-btn to use 'background:var(--accent)' but got: {rule_body}"
    )


def test_auth_form_controls_share_border_box_width():
    """Full-width auth inputs and buttons must align despite horizontal padding."""
    css = _read_css()
    input_match = re.search(r"\.auth-input\{([^}]+)\}", css)
    button_match = re.search(r"\.auth-btn\{([^}]+)\}", css)
    assert input_match, ".auth-input rule not found in shell.css"
    assert button_match, ".auth-btn rule not found in shell.css"
    assert "box-sizing:border-box" in input_match.group(1)
    assert "box-sizing:border-box" in button_match.group(1)


def test_auth_btn_no_hardcoded_hex_background():
    """Login button must not use a hardcoded hex background that bypasses theming."""
    css = _read_css()
    match = re.search(r"\.auth-btn\{([^}]+)\}", css)
    assert match, ".auth-btn rule not found in shell.css"
    rule_body = match.group(1)
    hardcoded = re.search(r"background:#[0-9a-fA-F]{3,6}", rule_body)
    assert not hardcoded, (
        f"Found hardcoded hex background in .auth-btn: {hardcoded.group()} — use var(--accent) instead"
    )


def test_auth_btn_hover_uses_accent_dark_token():
    """Login button hover must use var(--accent-dark) so it respects the design-system token."""
    css = _read_css()
    match = re.search(r"\.auth-btn:hover\{([^}]+)\}", css)
    assert match, ".auth-btn:hover rule not found in shell.css"
    rule_body = match.group(1)
    assert "background:var(--accent-dark)" in rule_body, (
        f"Expected .auth-btn:hover to use 'background:var(--accent-dark)' but got: {rule_body}"
    )


def test_auth_btn_hover_no_hardcoded_hex_background():
    """Login button hover must not use a hardcoded hex background that bypasses theming."""
    css = _read_css()
    match = re.search(r"\.auth-btn:hover\{([^}]+)\}", css)
    assert match, ".auth-btn:hover rule not found in shell.css"
    rule_body = match.group(1)
    hardcoded = re.search(r"background:#[0-9a-fA-F]{3,6}", rule_body)
    assert not hardcoded, (
        f"Found hardcoded hex background in .auth-btn:hover: {hardcoded.group()} — use var(--accent-dark) instead"
    )


def test_accent_token_defined_in_root():
    """--accent must be defined in the :root block so the login button color resolves."""
    css = _read_css()
    root_match = re.search(r":root\{([^}]+)\}", css)
    assert root_match, ":root block not found in shell.css"
    root_body = root_match.group(1)
    assert "--accent:" in root_body, (
        "--accent is not defined in :root — login button will have no background color"
    )


def test_accent_dark_token_defined_in_root():
    """--accent-dark must be defined in the :root block so the login button hover color resolves."""
    css = _read_css()
    root_match = re.search(r":root\{([^}]+)\}", css)
    assert root_match, ":root block not found in shell.css"
    root_body = root_match.group(1)
    assert "--accent-dark:" in root_body, (
        "--accent-dark is not defined in :root — login button hover will have no background color"
    )
