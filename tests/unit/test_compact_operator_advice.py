"""No remediation text may name a container the running profile does not have.

AC17-R106: twelve advice strings were written when every runtime role had a
container of its own, and they kept naming those containers after Compact collapsed
five of them into one. They are read at the worst possible moment -- an install that
has already failed, or a readiness payload the Compact doctor points at -- so an
operator is handed `docker compose logs fs-worker` for a service that does not exist
exactly when they are least able to tell good advice from bad.

Counting these by hand went wrong four times between two reviewers, in four
different ways: by the verbs the sentences used, by which call argument held them,
by whether the value was a literal, a variable or a dict entry, and finally by
assuming any Scalable-looking name was wrong when rabbitmq-fs, pg-fs and traefik are
containers on both profiles. So nothing here is listed by hand. The collapsed set is
derived from the compose files, and the advice values are found by walking the
syntax tree.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

from core.infrastructure.runtime_containers import COLLAPSED_ON_COMPACT
from tests.support.source_index import parse_file

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/docker"
ADVICE_FIELD = re.compile(r"remediation|recovery")
# Calls that exist to turn a Scalable service name into the right one: a name handed
# to them is an input to the mapping, not text an operator will read.
NAME_MAPPERS = (
    "operator_container",
    "_operator_container",
    "_runtime_image_consumers",
    "_worker_container",
)


def _services(path: Path) -> set[str]:
    found: set[str] = set()
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^services:\s*$", line):
            inside = True
            continue
        if inside and re.match(r"^[A-Za-z]", line):
            inside = False
        if inside:
            match = re.match(r"^  ([A-Za-z][A-Za-z0-9_.-]*):\s*$", line)
            if match:
                found.add(match.group(1))
    return found


def _profile_services(base: str, overlay_dir: str) -> set[str]:
    services = _services(DOCKER / base)
    for overlay in sorted((DOCKER / overlay_dir).glob("*.yaml")):
        services |= _services(overlay)
    return services


def _collapsed_by_the_compose_files() -> set[str]:
    scalable = _profile_services("docker-compose.yaml", "scalable")
    compact = _profile_services("docker-compose.compact.yaml", "compact")
    assert scalable, "no scalable services parsed; this test would be vacuous"
    assert compact, "no compact services parsed; this test would be vacuous"
    return scalable - compact


def _advice_literals(node: ast.AST):
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in NAME_MAPPERS:
            return
    for child in ast.iter_child_nodes(node):
        yield from _advice_literals(child)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node


def _advice_values(nodes: Iterable[ast.AST]):
    for node in nodes:
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg and ADVICE_FIELD.search(keyword.arg):
                    yield keyword.value
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and ADVICE_FIELD.search(target.id)
                for target in node.targets
            ):
                yield node.value
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and ADVICE_FIELD.search(key.value)
                ):
                    yield value


def test_the_collapsed_set_matches_what_the_compose_files_declare() -> None:
    """The helper must not claim a service collapses when Compact still runs it."""
    collapsed = _collapsed_by_the_compose_files()

    overreach = sorted(COLLAPSED_ON_COMPACT - collapsed)

    assert overreach == [], (
        f"these are declared by the Compact overlay and must not be rewritten: {overreach}"
    )


def test_no_advice_names_a_container_compact_does_not_run() -> None:
    collapsed = _collapsed_by_the_compose_files() & COLLAPSED_ON_COMPACT
    assert collapsed, "nothing to look for; this test would be vacuous"
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, collapsed))) + r")\b")

    sources = sorted((ROOT / "core").rglob("*.py"))
    assert sources, "no sources found; this test would be vacuous"
    offences: list[str] = []
    for source in sources:
        for value in _advice_values(parse_file(source).nodes):
            for literal in _advice_literals(value):
                if pattern.search(literal.value):
                    relative = source.relative_to(ROOT)
                    offences.append(f"{relative}:{literal.lineno} {literal.value[:70]}")

    assert offences == [], (
        "remediation text must name a container the running profile has; "
        "pass the Scalable name through operator_container() instead:\n  "
        + "\n  ".join(sorted(offences))
    )
