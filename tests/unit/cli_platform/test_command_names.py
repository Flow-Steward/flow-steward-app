"""Every command shown to an operator runs when pasted where it was shown."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.entrypoints.cli.platform import command_names
from core.entrypoints.cli.platform.command_names import (
    command_line,
    invocation_prefix,
    set_invocation_prefix,
    with_invocation_prefix,
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    set_invocation_prefix(None)
    yield
    set_invocation_prefix(None)


def test_without_a_launcher_the_canonical_name_is_kept() -> None:
    assert invocation_prefix() == "flow-steward"
    assert with_invocation_prefix("Run ./flow-steward status") == "Run flow-steward status"


@pytest.mark.parametrize("prefix", ["./flow-steward", "fs/flow-steward", "/opt/fs/flow-steward"])
def test_host_hints_use_the_name_the_operator_ran(prefix: str) -> None:
    set_invocation_prefix(prefix)

    text = "Run flow-steward maintenance disable --confirm, then ./flow-steward status."

    assert with_invocation_prefix(text) == (
        f"Run {prefix} maintenance disable --confirm, then {prefix} status."
    )
    assert command_line("users", "list") == f"{prefix} users list"


@pytest.mark.parametrize(
    "text",
    [
        "image ghcr.io/flow-steward/flow-steward-app:0.9.13",
        "network flow-steward_default",
        "the flow-steward runtime restarted",
        "compose project flow-steward was stopped",
    ],
)
def test_names_that_are_not_commands_are_left_alone(text: str) -> None:
    set_invocation_prefix("./flow-steward")

    assert with_invocation_prefix(text) == text


def test_root_flags_count_as_commands() -> None:
    set_invocation_prefix("./flow-steward")

    assert with_invocation_prefix("See flow-steward --help") == "See ./flow-steward --help"


@pytest.mark.parametrize("unsafe", ["rm -rf /;", "$(id)", "a b", "", "x" * 300])
def test_an_unexpected_launcher_value_keeps_the_canonical_name(unsafe: str) -> None:
    set_invocation_prefix(unsafe)

    assert command_names.invocation_prefix() == "flow-steward"
