"""Choose the Tier 3 tests a change reaches since a passed ancestor (ADR 0068).

A Tier 3 suite with a ``selection`` runs only the tests its change reaches when
an earlier result of the same check vouches for the rest:

- the base is the closest ancestor commit with a passed result, or a failed one
  whose failing modules are known (they run again), produced in the same
  runtime and, for release evidence, by trusted CI;
- the change is ``git diff base..HEAD``; a change the selection cannot follow
  (gate tooling, declared ``full_run_paths``, removed modules, files the suite
  used before any of its tests started) runs the whole suite;
- a test module is selected when it changed, when it used a changed file
  (``impact_map``), or when it imports a module whose module level changed --
  following importers that no test used until a used one is reached;
- a non-Python file also selects the tests whose source names it.

A ``vitest-related`` suite hands vitest the changed frontend sources instead:
``vitest related`` follows the module graph from them to every test reaching
them; any other change in the suite's inputs runs it whole.

Anything else runs the whole suite, which also rebuilds the map. The weekly
full run remains the backstop for what a recorded map cannot see.
"""

from __future__ import annotations

import ast
import json
import subprocess
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from core.application.tooling.quality.check_registry import CheckDefinition
from core.application.tooling.quality.evidence import EvidenceRecord
from core.application.tooling.quality.fingerprint import GATE_INPUTS, matches_any_pattern
from core.application.tooling.quality.impact_map import ImpactMap
from core.application.tooling.quality.impacted_tests import (
    GENERIC_NAMES,
    ImportGraph,
    module_name,
)

UNIT_TESTS = PurePosixPath("tests/unit")
FRONTEND_ROOT = PurePosixPath("assets/frontend")
FRONTEND_SOURCES = FRONTEND_ROOT / "src"
STRESS_TESTS = UNIT_TESTS / "stress"
# Selecting most of the suite saves little; the full run refreshes the map too.
FULL_RUN_SHARE = 0.6


@dataclass(frozen=True)
class SelectionPlan:
    """What one Tier 3 suite runs, and the result that vouches for the rest."""

    check_id: str
    full: bool
    reason: str
    base_sha: str = ""
    modules: tuple[str, ...] = ()
    changed_files: int = 0

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class SelectionInputs:
    check: CheckDefinition
    root: Path
    head_sha: str
    runtime_digest: str
    records: Sequence[EvidenceRecord]
    impact_map: ImpactMap | None
    trusted_ci_required: bool
    forced_full: bool = False


class _FullRun(Exception):
    """The change reaches something the selection cannot follow."""


@dataclass
class _Change:
    status: str
    path: str


@dataclass
class _Suite:
    modules: frozenset[str]
    sources: dict[str, str] = field(default_factory=dict)


def plan_selection(inputs: SelectionInputs) -> SelectionPlan:
    check_id = inputs.check.id
    try:
        base = _usable_base(inputs)
        changes = _changes(inputs.root, base.commit_sha, inputs.head_sha)
        if inputs.check.selection is not None and inputs.check.selection.kind == "vitest-related":
            return _related_plan(inputs, base, changes)
        suite = _unit_suite(inputs.root)
        selected = _select(inputs, base, changes, suite)
    except _FullRun as reason:
        return SelectionPlan(check_id=check_id, full=True, reason=str(reason))
    if suite.modules and len(selected) > FULL_RUN_SHARE * len(suite.modules):
        return SelectionPlan(
            check_id=check_id,
            full=True,
            reason=f"change reaches {len(selected)} of {len(suite.modules)} test modules",
        )
    return SelectionPlan(
        check_id=check_id,
        full=False,
        reason=f"tests reached by {len(changes)} changed files since {base.commit_sha[:12]}",
        base_sha=base.commit_sha,
        modules=tuple(sorted(selected)),
        changed_files=len(changes),
    )


# -- base ----------------------------------------------------------------------


def _usable_base(inputs: SelectionInputs) -> EvidenceRecord:
    if inputs.forced_full:
        raise _FullRun("full suite requested")
    if inputs.check.selection is None:
        raise _FullRun("check has no test selection")
    if not inputs.runtime_digest:
        raise _FullRun("runtime identity unavailable")
    if inputs.impact_map is None and inputs.check.selection.kind == "pytest-impact":
        raise _FullRun("no recorded test impact map yet")
    best: tuple[int, int, EvidenceRecord] | None = None
    for record in inputs.records:
        if not _vouches(record, inputs):
            continue
        distance = _distance(inputs.root, record.commit_sha, inputs.head_sha)
        if distance is None:
            continue
        rank = (distance, 0 if record.status == "passed" else 1, record)
        if best is None or rank[:2] < best[:2]:
            best = rank
    if best is None:
        raise _FullRun("no passed ancestor result in this runtime")
    return best[2]


def _vouches(record: EvidenceRecord, inputs: SelectionInputs) -> bool:
    if record.runtime_digest != inputs.runtime_digest or not record.commit_sha:
        return False
    if record.command != inputs.check.command:
        return False
    if inputs.trusted_ci_required and record.trust != "trusted_ci":
        return False
    return record.status == "passed" or (record.status == "failed" and bool(record.failed_modules))


def _distance(root: Path, base: str, head: str) -> int | None:
    if base == head:
        return 0
    ancestor = _git(root, "merge-base", "--is-ancestor", base, head, check=False)
    if ancestor is None:
        return None
    count = _git(root, "rev-list", "--count", f"{base}..{head}", check=False)
    return int(count) if count and count.strip().isdigit() else None


# -- change --------------------------------------------------------------------


def _changes(root: Path, base: str, head: str) -> list[_Change]:
    output = _git(root, "diff", "--no-renames", "--name-status", "-z", base, head)
    fields = (output or "").split("\0")
    changes = []
    for position in range(0, len(fields) - 1, 2):
        status, path = fields[position], fields[position + 1]
        if status and path:
            changes.append(_Change(status=status[0], path=path))
    return changes


def _full_run_reason(inputs: SelectionInputs, change: _Change, impact_map: ImpactMap) -> str:
    path = change.path
    selection = inputs.check.selection
    full_run_paths = selection.full_run_paths if selection is not None else ()
    if matches_any_pattern(path, GATE_INPUTS) or matches_any_pattern(path, full_run_paths):
        return f"{path} changes how the suite runs"
    if change.status == "D" and path.endswith(".py"):
        return f"{path} was removed; its importers are not traceable"
    if impact_map.is_shared(path):
        return f"{path} is used before any test starts"
    pure = PurePosixPath(path)
    if pure.name == "conftest.py" and pure.parent in {PurePosixPath("tests"), UNIT_TESTS}:
        return f"{path} configures every test"
    return ""


# -- vitest ----------------------------------------------------------------------


def _related_plan(
    inputs: SelectionInputs, base: EvidenceRecord, changes: list[_Change]
) -> SelectionPlan:
    """Changed frontend sources and earlier failing test files, for ``vitest related``."""
    selection = inputs.check.selection
    full_run_paths = selection.full_run_paths if selection is not None else ()
    related = {
        failed for failed in base.failed_modules if (inputs.root / FRONTEND_ROOT / failed).is_file()
    }
    for change in changes:
        path = PurePosixPath(change.path)
        if not matches_any_pattern(change.path, inputs.check.inputs) or matches_any_pattern(
            change.path, inputs.check.input_excludes
        ):
            continue  # Outside the suite's inputs: it cannot change a result.
        if matches_any_pattern(change.path, GATE_INPUTS + full_run_paths):
            raise _FullRun(f"{change.path} changes how the suite runs")
        if FRONTEND_SOURCES not in path.parents:
            raise _FullRun(f"{change.path} is outside the frontend sources")
        if change.status == "D":
            raise _FullRun(f"{change.path} was removed; its importers are not traceable")
        related.add(path.relative_to(FRONTEND_ROOT).as_posix())
    return SelectionPlan(
        check_id=inputs.check.id,
        full=False,
        reason=f"vitest related to {len(related)} sources changed since {base.commit_sha[:12]}",
        base_sha=base.commit_sha,
        modules=tuple(sorted(related)),
        changed_files=len(changes),
    )


# -- selection -----------------------------------------------------------------


def _select(
    inputs: SelectionInputs, base: EvidenceRecord, changes: list[_Change], suite: _Suite
) -> set[str]:
    impact_map = inputs.impact_map
    assert impact_map is not None  # _usable_base guarantees it
    graph: ImportGraph | None = None
    selected = {module for module in base.failed_modules if module in suite.modules}
    selected |= _unmapped_tests(inputs, impact_map, suite)
    for change in changes:
        reason = _full_run_reason(inputs, change, impact_map)
        if reason:
            raise _FullRun(reason)
        path = change.path
        pure = PurePosixPath(path)
        if path in suite.modules:
            selected.add(path)
        if pure.name == "conftest.py":
            selected.update(
                module for module in suite.modules if pure.parent in PurePosixPath(module).parents
            )
        selected.update(impact_map.tests_using(path) & suite.modules)
        if pure.parts[0] == "assets":
            # Backend tests reach frontend sources through the built shell in
            # assets/frontend/dist, which the change rebuilds: every test that
            # used a file under assets/ runs again.
            selected.update(impact_map.tests_using_under("assets/") & suite.modules)
        if not path.endswith(".py"):
            selected.update(_naming_tests(inputs.root, path, suite))
            continue
        if (
            change.status == "A"
            or not impact_map.used(path)
            or _module_level_changed(inputs.root, base.commit_sha, inputs.head_sha, path)
        ):
            graph = graph or ImportGraph(inputs.root)
            selected.update(_through_importers(graph, path, impact_map, suite))
    return selected


def _unmapped_tests(inputs: SelectionInputs, impact_map: ImpactMap, suite: _Suite) -> set[str]:
    """Test modules added or changed since the map was recorded: their use is unknown.

    Only whole-suite runs record the map, so these run every time until the next
    whole-suite run records them.
    """
    if not impact_map.commit:
        raise _FullRun("the impact map names no commit")
    return {
        change.path
        for change in _changes(inputs.root, impact_map.commit, inputs.head_sha)
        if change.status in {"A", "M"} and change.path in suite.modules
    }


def _through_importers(
    graph: ImportGraph, path: str, impact_map: ImpactMap, suite: _Suite
) -> set[str]:
    """Tests reached from ``path`` through importers no test used, up to used ones."""
    start = module_name(Path(path))
    if start is None or start not in graph.paths:
        raise _FullRun(f"{path} is outside the import graph")
    selected: set[str] = set()
    seen = {start}
    queue = deque([start])
    while queue:
        for importer in graph.direct_importers([queue.popleft()]):
            if importer in seen:
                continue
            seen.add(importer)
            importer_path = graph.paths[importer].as_posix()
            if importer_path in suite.modules:
                selected.add(importer_path)
                continue
            selected.update(impact_map.tests_using(importer_path) & suite.modules)
            if not impact_map.used(importer_path):
                queue.append(importer)
    return selected


def _naming_tests(root: Path, path: str, suite: _Suite) -> set[str]:
    name = PurePosixPath(path).name
    needles = {path} | ({name} if name not in GENERIC_NAMES else set())
    selected = set()
    for module in suite.modules:
        source = suite.sources.get(module)
        if source is None:
            try:
                source = (root / module).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                source = ""
            suite.sources[module] = source
        if any(needle in source for needle in needles):
            selected.add(module)
    return selected


def _unit_suite(root: Path) -> _Suite:
    modules = frozenset(
        path.relative_to(root).as_posix()
        for path in (root / UNIT_TESTS).rglob("test_*.py")
        if not path.relative_to(root).is_relative_to(Path(STRESS_TESTS))
    )
    return _Suite(modules=modules)


# -- module-level change -------------------------------------------------------


def _module_level_changed(root: Path, base: str, head: str, path: str) -> bool:
    """Whether a change touches code outside function bodies (imports, constants, classes)."""
    old_source = _git(root, "show", f"{base}:{path}", check=False)
    new_source = _git(root, "show", f"{head}:{path}", check=False)
    diff = _git(root, "diff", "-U0", "--no-renames", base, head, "--", path)
    if old_source is None or new_source is None or diff is None:
        return True
    try:
        old_spans = _function_spans(old_source)
        new_spans = _function_spans(new_source)
    except SyntaxError:
        return True
    old_lines = old_source.splitlines()
    new_lines = new_source.splitlines()
    for old_range, new_range in _hunks(diff):
        if _outside(old_range, old_spans, old_lines) or _outside(new_range, new_spans, new_lines):
            return True
    return False


def _function_spans(source: str) -> list[tuple[int, int]]:
    tree = ast.parse(source)
    return [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _hunks(diff: str) -> Iterable[tuple[range, range]]:
    for line in diff.splitlines():
        if not line.startswith("@@ "):
            continue
        old, new = line.split(" ")[1:3]
        yield _range(old.lstrip("-")), _range(new.lstrip("+"))


def _range(spec: str) -> range:
    start, _, count = spec.partition(",")
    length = int(count) if count else 1
    return range(int(start), int(start) + length)


def _outside(lines: range, spans: list[tuple[int, int]], source: list[str]) -> bool:
    for number in lines:
        text = source[number - 1].strip() if 0 < number <= len(source) else ""
        if not text or text.startswith("#"):
            continue
        if not any(start <= number <= end for start, end in spans):
            return True
    return False


def _git(root: Path, *args: str, check: bool = True) -> str | None:
    result = subprocess.run(
        ("git", "-c", f"safe.directory={root}", *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    )
    if result.returncode != 0:
        if check:
            raise _FullRun(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
        return None
    return result.stdout
