"""Impacted-test selection for Tier 3 suites (ADR 0068)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.tooling.quality.check_registry import CheckDefinition, TestSelection
from core.application.tooling.quality.evidence import EvidenceRecord
from core.application.tooling.quality.impact_map import ImpactMap
from core.application.tooling.quality.impact_selection import (
    SelectionInputs,
    SelectionPlan,
    plan_selection,
)

RUNTIME = "r" * 64

LIBRARY = '''"""Library."""

LIMIT = 3


def double(value):
    return value * 2


def triple(value):
    return value * LIMIT
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=Quality test", "-c", "user.email=test@example.invalid", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, ".gitignore", "logs/\n")
    _write(root, "core/__init__.py", "")
    _write(root, "core/library.py", LIBRARY)
    _write(
        root,
        "core/service.py",
        "from core.library import double\n\n\ndef run():\n    return double(2)\n",
    )
    _write(root, "core/schema.py", "FIELDS = ('a',)\n")
    _write(
        root,
        "core/api.py",
        "from core.schema import FIELDS\n\n\ndef fields():\n    return FIELDS\n",
    )
    _write(root, "tests/__init__.py", "")
    _write(root, "tests/unit/__init__.py", "")
    _write(root, "tests/unit/test_library.py", "from core.library import double\n")
    _write(root, "tests/unit/test_service.py", "from core.service import run\n")
    _write(root, "tests/unit/test_api.py", "from core.api import fields\n")
    _write(root, "tests/unit/test_docs.py", "DOC = 'docs/guide.md'\n")
    _write(root, "docs/guide.md", "guide\n")
    _write(root, "requirements.txt", "pytest\n")
    return root


def _check(
    kind: str = "pytest-impact", full_run_paths: tuple[str, ...] = ("requirements*.txt",)
) -> CheckDefinition:
    return CheckDefinition(
        id="unit-suite",
        owner_tier=3,
        command=("true",),
        inputs=("**",),
        reuse_scope="inputs",
        selection=TestSelection(kind=kind, full_run_paths=full_run_paths),
    )


def _record(
    sha: str, *, status: str = "passed", failed: tuple[str, ...] = (), runtime: str = RUNTIME
) -> EvidenceRecord:
    now = datetime.now(UTC)
    return EvidenceRecord(
        check_id="unit-suite",
        owner_tier=3,
        fingerprint="f" * 64,
        status=status,  # type: ignore[arg-type]
        trust="local",
        commit_sha=sha,
        command=("true",),
        started_at=now,
        completed_at=now,
        duration_seconds=1,
        failed_modules=failed,
        runtime_digest=runtime,
    )


def _map() -> ImpactMap:
    return ImpactMap(
        modules={
            "tests/unit/test_library.py": frozenset({"core/library.py"}),
            "tests/unit/test_service.py": frozenset({"core/service.py", "core/library.py"}),
            "tests/unit/test_api.py": frozenset({"core/api.py"}),
            "tests/unit/test_docs.py": frozenset({"docs/guide.md"}),
        },
        shared=frozenset({"core/bootstrap.yaml"}),
    )


def _plan(
    repo: Path, head: str, records: list[EvidenceRecord], **overrides: object
) -> SelectionPlan:
    impact = _map()
    impact.commit = records[0].commit_sha if records else ""
    values: dict[str, object] = {
        "check": _check(),
        "root": repo,
        "head_sha": head,
        "runtime_digest": RUNTIME,
        "records": records,
        "impact_map": impact,
        "trusted_ci_required": False,
    }
    values.update(overrides)
    return plan_selection(SelectionInputs(**values))  # type: ignore[arg-type]


def test_a_function_body_change_runs_only_the_tests_that_executed_it(repo: Path) -> None:
    base = _commit(repo, "base")
    _write(repo, "core/library.py", LIBRARY.replace("return value * 2", "return value + value"))
    head = _commit(repo, "body")

    plan = _plan(repo, head, [_record(base)])

    assert not plan.full
    assert plan.base_sha == base
    assert plan.modules == ("tests/unit/test_library.py", "tests/unit/test_service.py")


def test_a_module_level_change_also_reaches_tests_of_importers(repo: Path) -> None:
    base = _commit(repo, "base")
    _write(repo, "core/schema.py", "FIELDS = ('a', 'b')\n")
    head = _commit(repo, "constant")

    plan = _plan(repo, head, [_record(base)])

    # No test executed schema.py; its importer api.py was executed by test_api.
    assert plan.modules == ("tests/unit/test_api.py",)


def test_a_non_python_change_selects_users_and_tests_naming_it(repo: Path) -> None:
    base = _commit(repo, "base")
    _write(repo, "docs/guide.md", "changed\n")
    head = _commit(repo, "docs")

    assert _plan(repo, head, [_record(base)]).modules == ("tests/unit/test_docs.py",)


def test_a_comment_only_module_level_change_stays_narrow(repo: Path) -> None:
    base = _commit(repo, "base")
    _write(repo, "core/library.py", "# A note.\n" + LIBRARY)
    head = _commit(repo, "comment")

    plan = _plan(repo, head, [_record(base)])

    assert plan.modules == ("tests/unit/test_library.py", "tests/unit/test_service.py")


def test_a_failed_base_reruns_its_failing_modules(repo: Path) -> None:
    base = _commit(repo, "base")
    _write(repo, "docs/guide.md", "changed\n")
    head = _commit(repo, "docs")

    plan = _plan(repo, head, [_record(base, status="failed", failed=("tests/unit/test_api.py",))])

    assert plan.modules == ("tests/unit/test_api.py", "tests/unit/test_docs.py")


def test_the_closest_vouching_ancestor_is_the_base(repo: Path) -> None:
    older = _commit(repo, "older")
    _write(repo, "docs/guide.md", "changed\n")
    newer = _commit(repo, "newer")
    _write(repo, "core/library.py", LIBRARY.replace("value * 2", "2 * value"))
    head = _commit(repo, "head")

    plan = _plan(repo, head, [_record(older), _record(newer)])

    assert plan.base_sha == newer
    assert "tests/unit/test_docs.py" not in plan.modules


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda repo: _write(repo, "requirements.txt", "pytest\nhttpx\n"),
            "changes how the suite runs",
        ),
        (lambda repo: (repo / "core/schema.py").unlink(), "was removed"),
        (lambda repo: _write(repo, "core/bootstrap.yaml", "x: 1\n"), "used before any test starts"),
        (lambda repo: _write(repo, "tests/unit/conftest.py", "\n"), "configures every test"),
        (lambda repo: _write(repo, "scripts/tool.py", "VALUE = 1\n"), "outside the import graph"),
    ],
)
def test_changes_the_selection_cannot_follow_run_the_whole_suite(
    repo: Path, change, reason: str
) -> None:
    base = _commit(repo, "base")
    change(repo)
    head = _commit(repo, "change")

    plan = _plan(repo, head, [_record(base)])

    assert plan.full
    assert reason in plan.reason


@pytest.mark.parametrize(
    ("records", "overrides", "reason"),
    [
        (lambda base: [], {}, "no passed ancestor"),
        (lambda base: [_record(base, runtime="x" * 64)], {}, "no passed ancestor"),
        (lambda base: [_record(base, status="failed")], {}, "no passed ancestor"),
        (lambda base: [_record(base)], {"impact_map": None}, "no recorded test impact map"),
        (lambda base: [_record(base)], {"runtime_digest": ""}, "runtime identity unavailable"),
        (lambda base: [_record(base)], {"forced_full": True}, "full suite requested"),
    ],
)
def test_without_a_vouching_base_the_whole_suite_runs(
    repo: Path, records, overrides, reason: str
) -> None:
    base = _commit(repo, "base")
    _write(repo, "docs/guide.md", "changed\n")
    head = _commit(repo, "docs")

    plan = _plan(repo, head, records(base), **overrides)

    assert plan.full
    assert reason in plan.reason


def test_a_record_from_another_branch_is_not_a_base(repo: Path) -> None:
    root = _commit(repo, "root")
    _git(repo, "checkout", "-qb", "other")
    _write(repo, "docs/guide.md", "other\n")
    other = _commit(repo, "other")
    _git(repo, "checkout", "-q", root)
    _write(repo, "core/library.py", LIBRARY.replace("value * 2", "2 * value"))
    head = _commit(repo, "head")

    assert _plan(repo, head, [_record(other)]).full


def test_vitest_related_hands_vitest_the_changed_sources(repo: Path) -> None:
    _write(repo, "assets/frontend/src/page.tsx", "export const page = 1;\n")
    _write(repo, "assets/frontend/src/page.test.tsx", "import { page } from './page';\n")
    base = _commit(repo, "base")
    _write(repo, "assets/frontend/src/page.tsx", "export const page = 2;\n")
    _write(repo, "core/library.py", LIBRARY.replace("value * 2", "2 * value"))
    head = _commit(repo, "page")
    check = _check("vitest-related", ("assets/frontend/package.json",)).model_copy(
        update={"inputs": ("assets/**",)}
    )

    plan = _plan(
        repo,
        head,
        [_record(base, status="failed", failed=("src/page.test.tsx",))],
        check=check,
        impact_map=None,
    )

    assert not plan.full
    assert plan.modules == ("src/page.test.tsx", "src/page.tsx")


def test_vitest_related_runs_whole_for_changes_outside_the_sources(repo: Path) -> None:
    _write(repo, "assets/frontend/src/page.tsx", "export const page = 1;\n")
    _write(repo, "assets/frontend/index.html", "<html></html>\n")
    base = _commit(repo, "base")
    _write(repo, "assets/frontend/index.html", "<html lang='en'></html>\n")
    head = _commit(repo, "html")
    check = _check("vitest-related", ()).model_copy(update={"inputs": ("assets/**",)})

    plan = _plan(repo, head, [_record(base)], check=check, impact_map=None)

    assert plan.full
    assert "outside the frontend sources" in plan.reason


def test_a_frontend_change_reaches_backend_tests_that_used_the_built_shell(repo: Path) -> None:
    _write(repo, "assets/frontend/src/page.tsx", "export const page = 1;\n")
    base = _commit(repo, "base")
    _write(repo, "assets/frontend/src/page.tsx", "export const page = 2;\n")
    head = _commit(repo, "page")
    impact = _map()
    impact.commit = base
    impact.modules["tests/unit/test_api.py"] = frozenset({"assets/frontend/dist/index.html"})

    plan = _plan(repo, head, [_record(base)], impact_map=impact)

    assert not plan.full
    assert plan.modules == ("tests/unit/test_api.py",)


def test_tests_added_or_changed_since_the_map_always_run(repo: Path) -> None:
    mapped = _commit(repo, "mapped")
    _write(repo, "tests/unit/test_new.py", "from core.library import triple\n")
    base = _commit(repo, "new test, passed selected run")
    _write(repo, "docs/guide.md", "changed\n")
    head = _commit(repo, "docs")
    impact = _map()
    impact.commit = mapped

    plan = _plan(repo, head, [_record(base)], impact_map=impact)

    assert plan.modules == ("tests/unit/test_docs.py", "tests/unit/test_new.py")


def test_vitest_related_runs_whole_when_the_gate_tooling_changes(repo: Path) -> None:
    """lint.sh lies outside the suite's inputs but decides how vitest runs."""
    _write(repo, "assets/frontend/src/page.tsx", "export const page = 1;\n")
    _write(repo, "core/entrypoints/cli/scripts/lint.sh", "echo one\n")
    base = _commit(repo, "base")
    _write(repo, "core/entrypoints/cli/scripts/lint.sh", "echo two\n")
    head = _commit(repo, "gate")
    check = _check("vitest-related", ()).model_copy(update={"inputs": ("assets/**",)})

    plan = _plan(repo, head, [_record(base)], check=check, impact_map=None)

    assert plan.full
    assert "changes how the suite runs" in plan.reason
