"""AC17-R160: an operator hint names only commands and flags that exist.

The installer's stopped-update hint said `./flow-steward backup create --confirm`; the
command takes no --confirm, so an operator following it got "Usage" at step two of a
migration, with maintenance already on. Derived from the command specs the CLI is built
from, over every hint the installer prints and every step the runbook gives.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.application.management.command_specs.contracts import InteractivePolicy
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

ROOT = Path(__file__).resolve().parents[2]
_SOURCES = (
    *(
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "core/infrastructure/installer").glob("gateway*.py"))
    ),
    "core/application/installer/service.py",
    "docs/operations/compact-scalable-deployment.md",
)
_GLOBAL = {"--json", "--plain", "--no-color", "--verbose", "--non-interactive", "--help"}
_INVOCATION = re.compile(
    r"\./flow-steward((?: [a-z][a-z-]*)+)((?: --[a-z][a-z-]*(?: [^\s`\"'-][^\s`\"']*)?)*)"
)


def _specs() -> dict[tuple[str, ...], set[str]]:
    flags: dict[tuple[str, ...], set[str]] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        allowed = {field.option_name or f"--{field.key.replace('_', '-')}" for field in spec.fields}
        if spec.interactive_policy is InteractivePolicy.ALWAYS_REVIEW:
            allowed.add("--confirm")
        flags[tuple(spec.path)] = allowed | _GLOBAL
    return flags


def _invocations() -> list[tuple[str, tuple[str, ...], list[str]]]:
    found = []
    for source in _SOURCES:
        text = (ROOT / source).read_text(encoding="utf-8")
        # a hint split over adjacent Python string literals reads as one sentence
        joined = re.sub(r'"\s*\n\s*"', "", text)
        for match in _INVOCATION.finditer(joined):
            words = tuple(match.group(1).split())
            used = re.findall(r"--[a-z][a-z-]*", match.group(2))
            found.append((source, words, used))
    return found


def test_every_hinted_command_and_flag_exists() -> None:
    specs = _specs()
    invocations = _invocations()
    assert any(words[:2] == ("backup", "create") for _s, words, _f in invocations)  # anchor

    wrong = []
    for source, words, used in invocations:
        path = next((words[:n] for n in range(len(words), 0, -1) if words[:n] in specs), None)
        if path is None:
            continue  # lifecycle commands (config, stack, maintenance...) are specified elsewhere
        unknown = sorted(set(used) - specs[path])
        if unknown:
            wrong.append(f"{source}: ./flow-steward {' '.join(path)} {' '.join(unknown)}")

    assert wrong == []


_RECOVERY_SOURCES = sorted((ROOT / "core/application/lifecycle").glob("*.py"))
_RECOVERY_LITERAL = re.compile(
    r"(?:recovery_command=\(?\s*|_RECOVERY_COMMAND = |_MESSAGE_FLOW_STEWARD_[A-Z_]+ = )"
    r"f?\"((?:\./)?flow-steward [^\"{}]+)"
)


def test_every_recovery_command_status_prints_names_real_flags() -> None:
    """AC17-R162: `status` said "Continue it with ./flow-steward backup create --confirm"
    -- the readiness finding appended --confirm to every recovery command, and backup
    create has no such option. Derived from every recovery command the lifecycle
    services record, as status completes and prints it."""
    from core.application.management.command_specs.registry import complete_operator_command

    specs = _specs()
    commands = {
        match.group(1).strip()
        for source in _RECOVERY_SOURCES
        for match in _RECOVERY_LITERAL.finditer(source.read_text(encoding="utf-8"))
    }
    assert {"flow-steward backup create", "flow-steward config apply"} <= commands  # anchor

    wrong = []
    for command in sorted(commands):
        completed = complete_operator_command(command)
        words = completed.split()[1:]
        positional = tuple(word for word in words if not word.startswith("-"))
        path = next(
            (positional[:n] for n in range(len(positional), 0, -1) if positional[:n] in specs),
            None,
        )
        assert path is not None, command
        unknown = sorted({word for word in words if word.startswith("--")} - specs[path])
        if unknown:
            wrong.append(f"{command} -> {completed}: {unknown}")
    assert wrong == []


def test_a_recovery_command_gets_confirm_exactly_when_it_reviews() -> None:
    from core.application.management.command_specs.registry import complete_operator_command

    assert complete_operator_command("flow-steward backup create") == "./flow-steward backup create"
    assert (
        complete_operator_command("flow-steward config apply")
        == "./flow-steward config apply --confirm"
    )
    assert (
        complete_operator_command("./flow-steward stack stop --confirm")
        == "./flow-steward stack stop --confirm"
    )
    assert complete_operator_command("operator: repair it") == "operator: repair it"
