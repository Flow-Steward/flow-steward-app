"""Semantic color contracts for generated Click help."""

from __future__ import annotations

import re
from types import SimpleNamespace

from click.testing import CliRunner

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _Registry:
    async def invoke(
        self,
        _key: str,
        _value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> dict[str, object]:
        del progress_sink, technical_sink
        raise AssertionError("help must not invoke services")


def _app() -> object:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(_Registry(), SimpleNamespace())


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _sgr_codes(text: str) -> set[str]:
    return set(ANSI_RE.findall(text))


def _terminal(monkeypatch, value: bool = True) -> None:
    from core.entrypoints.cli.platform import command_help

    monkeypatch.setattr(command_help, "_stdout_is_terminal", lambda: value)


def test_root_group_and_leaf_help_are_styled_in_a_terminal(monkeypatch) -> None:
    _terminal(monkeypatch)
    app = _app()
    runner = CliRunner()
    env = {"NO_COLOR": None, "TERM": "xterm-256color"}

    root = runner.invoke(app, ["--help"], color=True, env=env)
    group = runner.invoke(app, ["users", "--help"], color=True, env=env)
    leaf = runner.invoke(app, ["users", "create", "--help"], color=True, env=env)

    for result in (root, group, leaf):
        assert result.exit_code == 0, result.output
        assert ANSI_RE.search(result.output)
        assert "Usage:" in _strip_ansi(result.output)
    assert len(_sgr_codes(leaf.output)) >= 4
    assert "Required inputs" in _strip_ansi(leaf.output)
    assert "Optional inputs" in _strip_ansi(leaf.output)
    assert "Advanced inputs" in _strip_ansi(leaf.output)
    assert "Examples" in _strip_ansi(leaf.output)
    assert "Secret input policy" in _strip_ansi(leaf.output)


def test_redirected_help_is_plain(monkeypatch) -> None:
    _terminal(monkeypatch, False)
    app = _app()
    for args in (["--help"], ["users", "--help"], ["users", "create", "--help"]):
        result = CliRunner().invoke(app, args, color=True, env={"TERM": "xterm-256color"})
        assert result.exit_code == 0, result.output
        assert not ANSI_RE.search(result.output)
        assert "--plain" in result.output
        assert "--tui" not in result.output


def test_required_optional_and_secret_policy_remain_explicit_without_color() -> None:
    app = _app()

    result = CliRunner().invoke(app, ["users", "create", "--help"], color=True)

    plain = _strip_ansi(result.output)
    assert result.exit_code == 0, result.output
    assert "Required inputs" in plain
    assert "Optional inputs" in plain
    assert "Advanced inputs" in plain
    assert "Required" in plain
    assert "Optional" in plain
    assert "Secret input policy" in plain
    assert "--password-file" in plain
    assert "--password-stdin" in plain


def test_help_color_is_suppressed_for_no_color_modes(
    monkeypatch,
) -> None:
    _terminal(monkeypatch)
    app = _app()
    runner = CliRunner()

    cases = (
        (["--no-color", "--help"], True, {}),
        (["users", "--no-color", "--help"], True, {}),
        (["users", "create", "--no-color", "--help"], True, {}),
        (["--plain", "--help"], True, {}),
        (["--json", "--help"], True, {}),
        (["--help"], True, {"NO_COLOR": "1"}),
        (["--help"], True, {"TERM": "dumb"}),
    )
    for args, color, env in cases:
        result = runner.invoke(app, args, color=color, env=env)
        assert result.exit_code == 0, result.output
        assert not ANSI_RE.search(result.output), (args, result.output)


def test_help_wrapping_is_stable_at_narrow_and_normal_widths() -> None:
    app = _app()
    runner = CliRunner()

    narrow = runner.invoke(app, ["users", "create", "--help"], color=True, terminal_width=60)
    normal = runner.invoke(app, ["users", "create", "--help"], color=True, terminal_width=100)

    assert narrow.exit_code == 0, narrow.output
    assert normal.exit_code == 0, normal.output
    assert "Usage:" in _strip_ansi(narrow.output)
    assert "Usage:" in _strip_ansi(normal.output)
    assert "Required inputs" in _strip_ansi(narrow.output)
    assert "Required inputs" in _strip_ansi(normal.output)
